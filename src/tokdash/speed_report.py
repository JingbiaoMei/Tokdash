"""Grouped output-throughput aggregation behind GET /api/output-speed.

Two questions, both answered from one grouped query:

  across models   "how fast is each model, and how much of it do I actually know?"
  time of day     "for one model, does throughput move with the clock?"

Everything here works on the additive columns, so a rate is always
``SUM(tokens)/SUM(duration)`` over the rows that fall in the bucket. Hourly and
six-hourly figures both come from the raw matched calls, never from averaging the
rates below them -- averaging hourly rates would weight a 2-call hour the same as
a 200-call one.

The temporal view groups by *local* hour of day. SQLite has no IANA timezone
support, so the conversion happens once per row in Python using the same clock
convention the report already uses. A historical corpus spans DST transitions, so
the offset is resolved per timestamp; applying one current offset to a month of
history would shift every row by an hour for half the range.

Coverage is deliberately allowed to stay unknown. If a source has no counted-call
population that we can prove corresponds to the timing population, coverage is
null rather than a guess, because 0% and 100% are both readable claims that the
data does not support.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable, Optional

from .dateutil import parse_date_range
from .model_normalization import normalize_model_name
from functools import lru_cache

_speed_model = lru_cache(maxsize=2048)(normalize_model_name)
SUPPORTED_READERS = frozenset({"kimi", "omp", "codex", "dsh", "qwen_code", "opencode", "kilocode", "mimo"})
from .output_speed import (
    MEASUREMENT_KINDS,
    STATUSES,
    TOKEN_BASES,
    STATUS_MEASURED,
    coverage as coverage_for,
    rate_from_totals,
)

#: Response-shape version. Bump when a field is added, renamed or redefined, so a
#: cached payload is never served to a reader expecting a different contract.
SPEED_CACHE_CONTRACT_VERSION = 6

#: Mirrors output_speed.SPEED_CONTRACT_VERSION. Separate so a stored duration's
#: *meaning* can retire comparison caches on its own, without the response shape
#: having changed at all.
SPEED_MEASUREMENT_CONTRACT_VERSION = 3

#: Six-hour bins, half-open, with 24 meaning the next midnight.
SIX_HOUR_BINS: tuple[tuple[str, int, int], ...] = (
    ("00-06", 0, 6),
    ("06-12", 6, 12),
    ("12-18", 12, 18),
    ("18-24", 18, 24),
)

#: Statuses that mean "this call was deliberately not counted", as opposed to
#: simply having no timing available. Shown separately so a reader can tell an
#: exclusion policy apart from missing data.
EXCLUDED_STATUSES = (
    "ambiguous_pair",
    "invalid_timing",
    "incomplete",
    "retry_contaminated",
)


def _day_bounds_ms(day_from: str, day_to: str) -> tuple[int, int]:
    """Inclusive lower and exclusive upper epoch-ms for a closed date range.

    Built by :func:`tokdash.dateutil.parse_date_range`, the same helper every
    other windowed report uses, so a day means local midnight here exactly as it
    does next to it: the report's own day buckets come from local time, and a
    window anchored on UTC midnight would disagree with them in both directions
    (in London on 1 July, dropping the first local hour and borrowing one from
    the 2nd). There is no timezone control in the UI, and none is needed -- the
    report already has one clock, and this now reads from it.

    Membership follows the owning usage row's timestamp, the same convention every
    other report window uses. A call whose duration straddles midnight belongs to
    whichever day its usage row is stamped on; tokens and durations are never
    prorated across the boundary, which would invent a split nobody measured.
    """
    since, until = parse_date_range(day_from, day_to)
    return int(since.timestamp() * 1000), int(until.timestamp() * 1000)


def _group_rows(
    conn: sqlite3.Connection, since_ms: int, until_ms: int
) -> list[sqlite3.Row]:
    """One grouped pass over the timing columns for the window.

    GROUP BY leads on (source, model, kind, basis) so incompatible measurements
    never share a row: a provider-reported decode window and a whole request
    window are different physical quantities, and blending them into one number
    would produce something that measures nothing.
    """
    conn.row_factory = sqlite3.Row
    return list(
        conn.execute(
            f"""
            SELECT source,
                   model,
                   speed_kind,
                   speed_token_basis,
                   speed_status,
                   count(*)                              AS rows_seen,
                   sum(speed_tokens)                     AS speed_tokens,
                   sum(speed_ms)                         AS speed_ms,
                   sum(speed_calls)                      AS speed_calls,
                   min(CASE WHEN speed_calls > 0 THEN timestamp END) AS first_ms,
                   max(CASE WHEN speed_calls > 0 THEN timestamp END) AS last_ms
            FROM {_report_table(conn)}
            WHERE timestamp >= ? AND timestamp < ?
              AND (speed_calls > 0 OR speed_status != 'unknown')
            GROUP BY source, model, speed_kind, speed_token_basis, speed_status
            """,
            (since_ms, until_ms),
        )
    )


def _timing_index_hint(conn):
    # Native request-local projections have only their own source/time index.
    present = conn.execute("""SELECT 1 FROM sqlite_master
        WHERE type='index' AND name='idx_usage_entries_speed_time'""").fetchone()
    return ' INDEXED BY idx_usage_entries_speed_time' if present else ''


def _report_index_hint(conn):
    """Use the measured covering projection, with a linear legacy fallback.

    A source/time index followed by table lookups is extremely expensive for
    broad histories on mounted devices. Before a worker upgrades the disposable
    cache, scanning its canonical rows once avoids that scattered-read plan.
    Ordinary usage-table adapters keep their existing query plans.
    """
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='speed_responses'").fetchone():
        return ''
    present=conn.execute("SELECT 1 FROM sqlite_master WHERE name='idx_speed_report'").fetchone()
    return ' INDEXED BY idx_speed_report' if present else ' NOT INDEXED'


def _report_table(conn, *, timing=False):
    hint=_report_index_hint(conn)
    if hint:
        return 'speed_responses'+hint
    return 'usage_entries'+(_timing_index_hint(conn) if timing else '')


def _eligible_queries(conn, since_ms, until_ms, sources, select, *, group='', model=None):
    """Cover known verdicts; read only unknown row identities from the table.

    Both existing indexes carry rowid. Their population difference supplies
    legacy rows without a second full-size persistent index. This matters when
    a handful of missing historical inputs prevents complete reprocessing.
    select/group are internal SQL fragments, never supplied by an API caller.
    """
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='speed_responses'").fetchone():
        names = sorted(sources)
        clause = ' AND speed_model(model)=?' if model is not None else ''
        args = (since_ms, until_ms, *names) + ((model,) if model is not None else ())
        return [(f"SELECT {select} FROM speed_responses{_report_index_hint(conn)} WHERE timestamp>=? AND timestamp<? AND source IN ({','.join('?' for _ in names)}) AND eligible=1{clause} {group}", args)]
    names = sorted(sources)
    placeholders = ','.join('?' for _ in names)
    args = (since_ms, until_ms, *names)
    base = f'timestamp >= ? AND timestamp < ? AND source IN ({placeholders})'
    known = "(speed_calls > 0 OR speed_status != 'unknown')"
    hint = _timing_index_hint(conn)
    known_count = conn.execute(f"""SELECT count(*) FROM usage_entries{hint}
        WHERE {base} AND {known}""", args).fetchone()[0]
    total = conn.execute(f"""SELECT count(*) FROM usage_entries INDEXED BY idx_usage_entries_source_time
        WHERE {base}""", args).fetchone()[0]
    eligible = "(output > 0 OR (source = 'qwen_code' AND reasoning > 0))"
    model_clause = ' AND speed_model(model) = ?' if model is not None else ''
    model_args = (model,) if model is not None else ()
    queries = [(f"""SELECT {select} FROM usage_entries{hint}
        WHERE {base} AND {known} AND {eligible}{model_clause} {group}""", args + model_args)]
    if known_count != total:
        difference = f"""SELECT rowid FROM usage_entries INDEXED BY idx_usage_entries_source_time WHERE {base}
            EXCEPT SELECT rowid FROM usage_entries{hint} WHERE {base} AND {known}"""
        queries.append((f"""SELECT {select} FROM usage_entries
            WHERE rowid IN ({difference}) AND {eligible}{model_clause} {group}""", args + args + model_args))
    return queries


def _eligible_rows(conn, since_ms, until_ms, sources):
    if not sources:
        return {}
    eligible = {}
    for query, args in _eligible_queries(conn, since_ms, until_ms, sources,
                                        'source, model, count(*) AS n', group='GROUP BY source, model'):
        for row in conn.execute(query, args):
            key = (str(row['source']), _speed_model(row['model']))
            eligible[key] = eligible.get(key, 0) + int(row['n'])
    return eligible


def across_model_rows(
    conn: sqlite3.Connection, day_from: str, day_to: str, *, source: str = ""
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rows for the Across models table, plus the range metadata.

    ``source`` filters here rather than at the caller so the rows and the metadata
    describe the same population. Coverage of measured days is a per-source union
    (see ``source_days_with_measurements``), and a caller that filtered the rows
    afterwards would pair one source's table with a day count drawn from every
    source in the database.
    """
    since_ms, until_ms = _day_bounds_ms(day_from, day_to)
    _hour_of_local, date_of_local = local_clock()
    groups: dict[tuple, dict[str, Any]] = {}
    wanted = (source or "").strip().lower()
    selected_days = (
        datetime.strptime(day_to, "%Y-%m-%d").date()
        - datetime.strptime(day_from, "%Y-%m-%d").date()
    ).days + 1

    # Calls with no usable duration, counted per (source, model). A stored row
    # without a measurement has no measurement kind to group by, so these are
    # gathered by model and reported against the model's measured rows.
    unmeasured_by_model: dict[tuple[str, str], dict[str, Any]] = {}


    for row in _group_rows(conn, since_ms, until_ms):
        if wanted and str(row["source"]).lower() != wanted:
            continue
        key = (row["source"], _speed_model(row["model"]), row["speed_kind"], row["speed_token_basis"])
        entry = groups.setdefault(
            key,
            {
                "source": str(row["source"]),
                "model": _speed_model(row["model"]),
                "measurement_kind": str(row["speed_kind"] or ""),
                "token_basis": str(row["speed_token_basis"] or ""),
                "speed_tokens": 0,
                "speed_ms": 0.0,
                "speed_calls": 0,
                "first_ms": None,
                "last_ms": None,
                "eligible_calls": None,
                # Overwritten from unmeasured_by_model below, where they are scoped
                # to the (source, model) the row belongs to.
                "unmeasured": {
                    "scope": "source_model",
                    "missing_timing_calls": 0,
                    "excluded_calls_by_reason": {},
                },
            },
        )
        status = str(row["speed_status"] or "unknown")
        calls = int(row["speed_calls"] or 0)
        if calls:
            entry["speed_tokens"] += int(row["speed_tokens"] or 0)
            entry["speed_ms"] += float(row["speed_ms"] or 0.0)
            entry["speed_calls"] += calls
            if entry["first_ms"] is None or row["first_ms"] < entry["first_ms"]:
                entry["first_ms"] = int(row["first_ms"])
            if entry["last_ms"] is None or row["last_ms"] > entry["last_ms"]:
                entry["last_ms"] = int(row["last_ms"])
        elif status in EXCLUDED_STATUSES:
            # An unmeasured row carries no kind and no basis -- there was nothing
            # to kind -- so its counts belong to the (source, model), not to one
            # measurement variant. They are collected per model below and hung on
            # the measured rows there, rather than becoming a dash-row here.
            unmeasured = unmeasured_by_model.setdefault(
                (str(row["source"]), _speed_model(row["model"])),
                {"missing_timing_calls": 0, "excluded_calls_by_reason": {}},
            )
            unmeasured["excluded_calls_by_reason"][status] = (
                unmeasured["excluded_calls_by_reason"].get(status, 0) + int(row["rows_seen"] or 0)
            )
        elif status == "missing_timing":
            unmeasured = unmeasured_by_model.setdefault(
                (str(row["source"]), _speed_model(row["model"])),
                {"missing_timing_calls": 0, "excluded_calls_by_reason": {}},
            )
            unmeasured["missing_timing_calls"] += int(row["rows_seen"] or 0)

    eligible = _eligible_rows(conn, since_ms, until_ms,
                             {key[0] for key, entry in groups.items() if entry['speed_calls']})
    measured_days = _measured_day_map(conn, since_ms, until_ms, date_of_local)
    # Distinct local days each source measured SOMETHING on, unioned across the
    # models, kinds and bases under it. Neither the maximum nor the sum of the
    # per-row counts can recover this: a source with model A measured on day 1 and
    # model B on day 2 has two measured days and a per-row maximum of one, while
    # two models measured on the same day would sum to two days that were really
    # one. The note about coverage of the selected range is a claim about the
    # source, so it counts the source's own days.
    source_days: dict[str, set] = {}
    population_days: set = set()
    for (d_source, _d_model, _d_kind, _d_basis), d_set in measured_days.items():
        if wanted and d_source.lower() != wanted:
            continue
        if not d_set:
            continue
        source_days.setdefault(d_source, set()).update(d_set)
        population_days.update(d_set)
    rows: list[dict[str, Any]] = []
    for key, entry in groups.items():
        source, model = key[0], key[1]
        if not entry["speed_calls"]:
            # Not a comparison row: no rate, no measured calls, and its absence is
            # already counted against the model. A model measured nowhere is
            # reported by source_statuses, where the reason can be read.
            continue
        days = measured_days.get(key, set())
        entry["unmeasured"] = dict(
            {"scope": "source_model"},
            **(unmeasured_by_model.get((source, model)) or {
                "missing_timing_calls": 0,
                "excluded_calls_by_reason": {},
            }),
        )
        base_eligible = eligible.get((source, model))
        entry["eligible_calls"] = base_eligible
        # A group that measured nothing has an unknown rate share, not 0%. 0.0
        # would claim "we looked at every eligible call and none of them had
        # timing", which is a statement about the source's instrumentation that
        # this row is not entitled to make -- it holds one status bucket, while
        # the eligible population spans the whole (source, model). Only a group
        # with at least one measured call can honestly report a fraction.
        entry["coverage"] = (
            coverage_for(entry["speed_calls"], base_eligible) if entry["speed_calls"] else None
        )
        entry["output_tok_per_s"] = rate_from_totals(entry["speed_tokens"], entry["speed_ms"])
        entry["first_measured_at"] = entry["first_ms"]
        entry["last_measured_at"] = entry["last_ms"]
        entry["days_with_measurements"] = len(days)
        # This SOURCE's measured-day coverage, unioned across its models: see the
        # note where it is built. A per-row count cannot answer a per-source
        # question, and this is the field the note reads.
        entry["source_days_with_measurements"] = len(source_days.get(source, ()))
        entry["selected_days"] = selected_days
        entry["observed_range_status"] = _observed_status(
            entry["first_ms"], entry["last_ms"], len(days), selected_days
        )
        rows.append(entry)

    # Deterministic: measured-call count first (the sample size is what a reader
    # needs to judge a rate by), then the rate, then the key for stability.
    rows.sort(
        key=lambda r: (
            -r["speed_calls"],
            -(r["output_tok_per_s"] or 0.0),
            r["source"],
            r["model"],
        )
    )
    meta = {
        "selected_range": {"from": day_from, "to": day_to},
        "selected_days": selected_days,
        # Union across everything this response returned, for a reader looking at
        # rows from more than one source at once.
        "days_with_measurements": len(population_days),
        "_days": population_days,
    }
    return rows, meta



def _measured_day_map(
    conn: sqlite3.Connection,
    since_ms: int,
    until_ms: int,
    date_of_local,
) -> dict[tuple[str, str], set]:
    """Local days on which each (source, model) produced at least one measured call.

    Counted from the rows themselves rather than from a min/max span, because a
    status bucket spans a whole range: a model measured on eleven separate days
    whose first and last measurement happen to fall on the same day would
    otherwise report one.
    Keyed by the full comparison key, not just (source, model): a row whose own
    measurements are all absent must report zero days, because the days its
    *sibling* variant was measured on are not evidence that this row measured
    anything. Sharing the key's day set across variants would put a date on a
    row that has no rate to date.
    """
    days: dict[tuple, set] = {}
    for row in conn.execute(
        f"""
        SELECT source, model, speed_kind, speed_token_basis, timestamp
        FROM {_report_table(conn)}
        WHERE timestamp >= ? AND timestamp < ? AND speed_calls > 0
          AND (speed_calls > 0 OR speed_status != 'unknown')
        """,
        (since_ms, until_ms),
    ):
        key = (str(row[0]), _speed_model(row[1]), str(row[2] or ""), str(row[3] or ""))
        days.setdefault(key, set()).add(date_of_local(int(row[4])))
    return days


def _observed_status(first_ms, last_ms, days_with: int, selected_days: int) -> str:
    """Describe the *measured* span without claiming the logs are complete.

    ``observed_all_days`` says only that at least one call was measured on each
    selected day. It is not evidence that every log survived: a source whose
    history began mid-range and a source with genuinely idle days both look
    identical from here, and the UI says so rather than picking one.
    """
    if first_ms is None:
        return "no_measurements"
    if days_with >= selected_days:
        return "observed_all_days"
    return "partial"


def temporal_rows(
    conn: sqlite3.Connection,
    day_from: str,
    day_to: str,
    *,
    source: str,
    model: str,
    measurement_kind: str,
    token_basis: str,
    hour_of_local: Callable[[int], int],
    date_of_local: Callable[[int], str],
) -> dict[str, Any]:
    """Hourly and six-hourly rows for exactly one comparison key.

    The two derived tables are computed from one snapshot of the matched calls,
    so the six-hour rows cannot drift from the hourly ones they summarise.
    """
    since_ms, until_ms = _day_bounds_ms(day_from, day_to)
    conn.row_factory = sqlite3.Row
    conn.create_function("speed_model", 1, _speed_model, deterministic=True)
    table = _report_table(conn, timing=True)
    # Two reads on purpose. The measured population is the selected variant: a
    # request window and a provider decode window are different quantities and
    # must not share an hour. The unmeasured population is not -- a row with no
    # duration has no measurement kind either, so filtering it by the selected
    # kind would drop every gap on the floor and leave the hour strip claiming a
    # completeness it does not have.
    matched = list(
        conn.execute(
            f"""
            SELECT timestamp, speed_tokens, speed_ms, speed_calls, speed_status
            FROM {table}
            WHERE timestamp >= ? AND timestamp < ?
              AND source = ? AND speed_model(model) = ?
              AND speed_calls > 0
              AND (speed_calls > 0 OR speed_status != 'unknown')
              AND speed_kind = ? AND speed_token_basis = ?
            ORDER BY timestamp
            """,
            (since_ms, until_ms, source, model, measurement_kind, token_basis),
        )
    )
    unmeasured_rows = list(
        conn.execute(
            f"""
            SELECT timestamp, speed_status
            FROM {table}
            WHERE timestamp >= ? AND timestamp < ?
              AND source = ? AND speed_model(model) = ?
              AND speed_calls = 0
              AND speed_status != 'unknown'
              AND (speed_calls > 0 OR speed_status != 'unknown')
            ORDER BY timestamp
            """,
            (since_ms, until_ms, source, model),
        )
    )

    hourly: dict[int, dict[str, Any]] = {
        h: {
            "hour": h,
            "speed_tokens": 0,
            "speed_ms": 0.0,
            "speed_calls": 0,
            "_days": set(),
            "excluded_calls_by_reason": {},
            "missing_timing_calls": 0,
        }
        for h in range(24)
    }
    excluded: dict[str, int] = {}
    missing_timing = 0
    first_ms = last_ms = None
    all_measured_days: set[str] = set()
    # Days per six-hour bin, filled by the same pass that sums the rates so a
    # bin's day count can never disagree with its own throughput.
    bin_days: dict[tuple[int, int], set] = {
        (lo, hi): set() for _label, lo, hi in SIX_HOUR_BINS
    }

    for row in unmeasured_rows:
        # Counted into the hour it happened in, so an hour that was all retries
        # does not read as an empty hour: "nothing measured" and "measured
        # nothing usable" are different statements.
        ms = int(row["timestamp"])
        hour = hour_of_local(ms)
        status = str(row["speed_status"] or "unknown")
        if status in EXCLUDED_STATUSES:
            excluded[status] = excluded.get(status, 0) + 1
            hourly[hour]["excluded_calls_by_reason"][status] = (
                hourly[hour]["excluded_calls_by_reason"].get(status, 0) + 1
            )
        elif status == "missing_timing":
            missing_timing += 1
            hourly[hour]["missing_timing_calls"] += 1

    for row in matched:
        ms = int(row["timestamp"])
        hour = hour_of_local(ms)
        calls = int(row["speed_calls"] or 0)
        bucket = hourly[hour]
        bucket["speed_tokens"] += int(row["speed_tokens"] or 0)
        bucket["speed_ms"] += float(row["speed_ms"] or 0.0)
        bucket["speed_calls"] += calls
        day = date_of_local(ms)
        bucket["_days"].add(day)
        all_measured_days.add(day)
        for _label, lo, hi in SIX_HOUR_BINS:
            if lo <= hour < hi:
                bin_days[(lo, hi)].add(day)
        if first_ms is None or ms < first_ms:
            first_ms = ms
        if last_ms is None or ms > last_ms:
            last_ms = ms

    eligible_total = 0
    eligible_by_hour: dict[int, int] = {}
    for query, args in _eligible_queries(conn, since_ms, until_ms, {source}, 'timestamp', model=model):
        for row in conn.execute(query, args):
            eligible_total += 1
            h = hour_of_local(int(row[0]))
            eligible_by_hour[h] = eligible_by_hour.get(h, 0) + 1

    hourly_rows = []
    for h in range(24):
        bucket = hourly[h]
        days_in_hour = bucket.pop("_days", set())
        hourly_rows.append(
            {
                "hour": h,
                "label": f"{h:02d}:00-{(h + 1) % 24:02d}:00",
                # Null, never zero: an unmeasured hour is absence of evidence,
                # and a zero here would plot as "the model ran at 0 tok/s".
                "output_tok_per_s": rate_from_totals(bucket["speed_tokens"], bucket["speed_ms"]),
                "speed_tokens": bucket["speed_tokens"],
                "speed_ms": bucket["speed_ms"],
                "speed_calls": bucket["speed_calls"],
                "eligible_calls": eligible_by_hour.get(h),
                "coverage": coverage_for(bucket["speed_calls"], eligible_by_hour.get(h, 0)),
                "days_with_measurements": len(days_in_hour),
                # Carried through from the pass above: an hour that measured
                # nothing usable is a different fact from an hour that saw
                # nothing, and the six-hour rows sum these to say so too.
                "excluded_calls_by_reason": bucket["excluded_calls_by_reason"],
                "missing_timing_calls": bucket["missing_timing_calls"],
            }
        )

    six_hour_rows = []
    for label, lo, hi in SIX_HOUR_BINS:
        span = range(lo, hi)
        tokens = sum(hourly_rows[h]["speed_tokens"] for h in span)
        ms = sum(hourly_rows[h]["speed_ms"] for h in span)
        calls = sum(hourly_rows[h]["speed_calls"] for h in span)
        eligible = sum(hourly_rows[h]["eligible_calls"] or 0 for h in span)
        reasons: dict[str, int] = {}
        for h in span:
            for reason, count in hourly_rows[h]["excluded_calls_by_reason"].items():
                reasons[reason] = reasons.get(reason, 0) + count
        six_hour_rows.append(
            {
                "label": label,
                # Sum of sums, not a mean of the hourly rates above it.
                "output_tok_per_s": rate_from_totals(tokens, ms),
                "speed_tokens": tokens,
                "speed_ms": ms,
                "speed_calls": calls,
                "eligible_calls": eligible or None,
                "coverage": coverage_for(calls, eligible),
                "days_with_measurements": len(bin_days[(lo, hi)]),
                "excluded_calls_by_reason": reasons,
                "missing_timing_calls": sum(
                    hourly_rows[h]["missing_timing_calls"] for h in span
                ),
            }
        )

    selected_days = (
        datetime.strptime(day_to, "%Y-%m-%d").date()
        - datetime.strptime(day_from, "%Y-%m-%d").date()
    ).days + 1
    return {
        "selected_key": {
            "source": source,
            "model": model,
            "measurement_kind": measurement_kind,
            "token_basis": token_basis,
        },
        "timestamp_basis": "usage_timestamp",
        "bucket": "local_hour_of_day",
        "hourly": hourly_rows,
        "six_hour": six_hour_rows,
        "eligible_calls": int(eligible_total or 0),
        "coverage": coverage_for(sum(r["speed_calls"] for r in hourly_rows), eligible_total),
        "excluded_calls_by_reason": excluded,
        "missing_timing_calls": missing_timing,
        "first_measured_at": first_ms,
        "last_measured_at": last_ms,
        "days_with_measurements": len(all_measured_days),
        "selected_days": selected_days,
        "observed_range_status": _observed_status(
            first_ms, last_ms, len(all_measured_days), selected_days
        ),
    }


def available_measurement_range(conn: sqlite3.Connection) -> Optional[dict[str, str]]:
    """Recorded date bounds, independent of the selected window.

    Two endpoint seeks in the compact timing index avoid walking unsupported
    usage history. These bounds describe observed measurements, not continuous
    coverage or a suggested replacement for the user's chosen dates.
    """
    first = conn.execute("""
        SELECT timestamp FROM usage_entries
        WHERE speed_calls > 0 AND (speed_calls > 0 OR speed_status != 'unknown')
        ORDER BY timestamp ASC LIMIT 1
    """).fetchone()
    if first is None:
        return None
    last = conn.execute("""
        SELECT timestamp FROM usage_entries
        WHERE speed_calls > 0 AND (speed_calls > 0 OR speed_status != 'unknown')
        ORDER BY timestamp DESC LIMIT 1
    """).fetchone()
    local_day = lambda ms: datetime.fromtimestamp(int(ms) / 1000).astimezone().date().isoformat()
    return {"from": local_day(first[0]), "to": local_day(last[0])}


def source_statuses(
    conn: sqlite3.Connection, day_from: str, day_to: str
) -> list[dict[str, Any]]:
    """Per-source explanation for anything the table does not show.

    A source with no measured rows is reported distinctly from a source whose
    timing has not been reprocessed yet, which is distinct from a source that
    failed to read. Collapsing those three into "no data" would hide the one
    case the user can act on.
    """
    since_ms, until_ms = _day_bounds_ms(day_from, day_to)
    # The first pass fits entirely in the partial timing index. The lifetime
    # usage counts below fit in the existing (source, timestamp) index, avoiding
    # table lookups for hundreds of thousands of unknown timing rows.
    timing = {str(row['source']): row for row in conn.execute(
        f"""
        SELECT source, count(*) AS known_rows,
               sum(CASE WHEN speed_calls > 0 THEN 1 ELSE 0 END) AS measured
        FROM {_report_table(conn)}
        WHERE timestamp >= ? AND timestamp < ?
          AND (speed_calls > 0 OR speed_status != 'unknown')
        GROUP BY source
        """, (since_ms, until_ms),
    )}
    derived = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name='speed_responses'").fetchone())
    out = []
    for row in conn.execute(
        f"""
        SELECT source, count(*) AS rows_seen
        FROM {_report_table(conn)}
        WHERE timestamp >= ? AND timestamp < ?
        GROUP BY source
        ORDER BY source
        """,
        (since_ms, until_ms),
    ):
        source = str(row["source"])
        tracked = timing.get(source)
        measured = int(tracked['measured'] or 0) if tracked else 0
        pending = not derived and source in SUPPORTED_READERS and int(tracked['known_rows'] or 0) < int(row['rows_seen']) if tracked else (not derived and source in SUPPORTED_READERS)
        if measured and not pending:
            continue
        if pending:
            status = 'pending_reprocessing'
        elif derived and source in SUPPORTED_READERS:
            status = "timing_unavailable"
        elif tracked is None:
            # Either the source has no per-call timing, or its stored rows predate
            # the timing parser and will fill in on reprocess. From here the two
            # are indistinguishable, so the status says which of them it is not.
            status = "unsupported_reader"
        else:
            status = "timing_unavailable"
        out.append({"source": source, "status": status, "usage_rows": int(row["rows_seen"] or 0)})
    return out


def validate_key(
    source: str, model: str, measurement_kind: str, token_basis: str
) -> Optional[str]:
    """Reject an incomplete or invented comparison key before it reaches SQL."""
    if not source or not model:
        return "source and model are required for view=time-of-day"
    if measurement_kind not in MEASUREMENT_KINDS:
        return "unknown measurement_kind"
    if token_basis not in TOKEN_BASES:
        return "unknown token_basis"
    return None


def local_clock(hour_offset: int = 0) -> tuple[Callable[[int], int], Callable[[int], str]]:
    """Hour-and-day extractors on the report's existing clock convention.

    Tokdash already lays its reports out on the machine's local clock -- see
    ``api._local_today`` -- so the hourly view bins by local hour of day and uses
    the same ``astimezone()`` resolution that decides what "today" means in the
    date picker. Reusing that one clock is the point: a route that resolved the
    day from the local zone but the hour from some other zone would split a
    caller's own report in two.

    The conversion resolves per timestamp rather than once for the query. A
    month-long corpus can straddle a DST transition, and one fixed offset read at
    request time would misplace every row on the far side of it. ``astimezone()``
    gives each instant its own correct offset, which covers real transitions and
    half-hour zone offsets alike. When clocks fall back, the repeated local hour
    folds into that hour-of-day population -- the honest reading of "what happened
    around 02:00" -- and when they jump forward the skipped hour simply receives
    no calls and renders as a gap.

    ``hour_offset`` exists for tests. Nothing in the UI exposes a timezone
    control, so there is no user-supplied zone to honour.
    """

    def _instant(ms: int) -> datetime:
        return datetime.fromtimestamp(ms / 1000.0, timezone.utc).astimezone()

    def hour_of_local(ms: int) -> int:
        # Modulo keeps a deliberate test offset inside the 0..23 bin space.
        return (_instant(ms).hour + hour_offset) % 24

    def date_of_local(ms: int) -> str:
        return _instant(ms).date().isoformat()

    return hour_of_local, date_of_local
