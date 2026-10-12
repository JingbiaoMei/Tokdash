"""Combined throughput and time-bucketed series for the Model output speed page.

Two rates describe the same measured responses:

  per stream   SUM(tokens) / SUM(each response's own duration) -- what
               speed_report has always returned as ``output_tok_per_s``. Every
               response runs on its own clock, so responses that overlap never
               add up. This is the single-stream speed.
  combined     SUM(tokens) / union of the time at least one response was in
               flight. Overlapping responses do add up, so this is the output a
               workload actually got per wall-clock second.

They are tied by ``combined = per stream * parallelism`` with ``parallelism =
SUM(duration) / union``, which is why both are returned from one pass and the
page can show combined as a rate plus a "×N" instead of a third number.

Each stored response has one timestamp and one duration. Where that timestamp
sits differs by reader, traced through the parsers and checked against real logs:
the OpenCode family (opencode, kilocode, mimo) stores ``time_created``, the start
of its created-to-completed window, exactly; the file readers (kimi, omp, codex,
dsh, qwen_code) store the record that settles the response, a few milliseconds
after the window ends. Claude Code bills a request's first content-block write,
which is neither end of a multi-block window, so its reader records where the
window opened (``speed_start``) and that interval is exact; an end anchor stays
its fallback. Codex does the same: its counted snapshot follows a response that
ended in a tool call only once the tool has run. ``busy_interval`` encodes that, and
tests/test_speed_trend.py pins it per reader so a parser that moves its
timestamp fails loudly instead of skewing overlap. Codex's duration has any hosted
web-search time removed, so such an interval is a slightly compressed stand-in
for the response; that is fine for counting overlap, and no reader stores more.

Membership follows the stored timestamp, as everywhere else in the report: a
response belongs to the day (hour, week ...) its usage row is stamped on, and
the busy time of a bucket is the union of that bucket's own responses. A window
total is computed directly, never by adding bucket busy times, because a
response that crosses a bucket boundary would otherwise be counted twice.

Combined across harnesses: the same model reached through several harnesses is
one workload when they run at once (one local server answering Codex and
OpenCode, say), so ``models`` / ``model_series`` take the union over every
harness's responses for a model, matched by the same normalisation Combined
Models uses. Their per-stream rate is deliberately absent -- harnesses time
responses different ways, and pooling a decode window with a whole-request
window measures nothing -- but their parallelism and combined rate are defined.
"""
from __future__ import annotations

import heapq
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Callable, Iterable, Optional

from .output_speed import coverage as coverage_for, rate_from_totals
from .speed_report import _day_bounds_ms, _report_table, _speed_model

#: Readers whose stored timestamp is the START of the timed window. Every other
#: supported reader stores a timestamp at (just after) the window's end.
START_ANCHORED_SOURCES = frozenset({"opencode", "kilocode", "mimo"})

#: The grouping ladder: (bucket, cap on buckets, window must cover at least
#: this many days). A size is offered when the window holds at least two of
#: its buckets, no more than its cap, and at least its minimum span; Auto picks
#: the finest size offered. One table, read by the page from the payload, so the
#: browser never carries a second copy that could disagree.
LADDER: tuple[tuple[str, int, int], ...] = (
    ("hour", 36, 1),
    ("day", 45, 2),
    ("week", 40, 8),
    ("month", 36, 60),
    ("quarter", 24, 184),
)
BUCKETS = tuple(name for name, _cap, _min in LADDER)


def busy_interval(source: str, timestamp: float, duration_ms: float,
                  start: Optional[float] = None) -> tuple[float, float]:
    """The wall-clock span a stored response was generating, in epoch ms.

    A recorded window start (speed_start) is exact; otherwise the source's
    anchor decides.
    """
    if start is not None:
        return (start, start + duration_ms)
    if source in START_ANCHORED_SOURCES:
        return (timestamp, timestamp + duration_ms)
    return (timestamp - duration_ms, timestamp)


def sweep(intervals: Iterable[tuple[float, float]]) -> tuple[float, int]:
    """Union length and peak overlap of intervals, in one pass after a sort.

    Touching intervals (one ends exactly where the next starts) neither overlap
    nor leave a gap. A zero-length interval adds no busy time.
    """
    busy = 0.0
    peak = 0
    ends: list[float] = []
    cur_start = cur_end = None
    for start, end in sorted(intervals):
        while ends and ends[0] <= start:
            heapq.heappop(ends)
        heapq.heappush(ends, end)
        if len(ends) > peak:
            peak = len(ends)
        if cur_end is None or start > cur_end:
            if cur_end is not None:
                busy += cur_end - cur_start
            cur_start, cur_end = start, end
        elif end > cur_end:
            cur_end = end
    if cur_end is not None:
        busy += cur_end - cur_start
    return busy, peak


def _combined(tokens: float, ms: float, busy: float) -> dict[str, Any]:
    return {
        "busy_ms": busy,
        "combined_tok_per_s": rate_from_totals(tokens, busy) if busy > 0 else None,
        "parallelism": (ms / busy) if busy > 0 and ms > 0 else None,
    }


# --------------------------------------------------------------------------
# Reading the measured responses once
# --------------------------------------------------------------------------

def _responses(conn: sqlite3.Connection, since_ms: int, until_ms: int, wanted: str):
    """Measured responses in the window, one tuple each.

    The same predicate and covering index as the grouped table read, so this
    pass never touches table pages the table did not.
    """
    # A cache read before the worker's upgrade has no recorded starts yet.
    start = "speed_start" if any(r[1] == "speed_start" for r in conn.execute(
        "PRAGMA table_info(speed_responses)")) else "NULL"
    for row in conn.execute(
        f"""
        SELECT source, model, speed_kind, speed_token_basis, timestamp,
               speed_tokens, speed_ms, speed_calls, {start}
        FROM {_report_table(conn)}
        WHERE timestamp >= ? AND timestamp < ? AND speed_calls > 0
          AND (speed_calls > 0 OR speed_status != 'unknown')
        """,
        (since_ms, until_ms),
    ):
        source = str(row[0])
        if wanted and source.lower() != wanted:
            continue
        ts = float(row[4])
        ms = float(row[6] or 0.0)
        begin, end = busy_interval(source, ts, ms, row[8])
        yield (
            (source, _speed_model(row[1]), str(row[2] or ""), str(row[3] or "")),
            int(row[4]), begin, end, int(row[5] or 0), ms, int(row[7] or 0),
        )


def combined_for_window(
    conn: sqlite3.Connection, day_from: str, day_to: str, *, source: str = ""
) -> tuple[dict[tuple, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Window totals: busy time per comparison key and per model.

    Returns ``(by_key, by_model)``. ``by_key`` is keyed like speed_report's rows
    (source, model, kind, basis); ``by_model`` by normalised model, with busy
    time taken over every harness that reported the model.
    """
    since_ms, until_ms = _day_bounds_ms(day_from, day_to)
    wanted = (source or "").strip().lower()
    per_key: dict[tuple, list] = defaultdict(list)
    for key, _ts, start, end, *_rest in _responses(conn, since_ms, until_ms, wanted):
        per_key[key].append((start, end))
    by_key: dict[tuple, dict[str, Any]] = {}
    model_intervals: dict[str, list] = defaultdict(list)
    for key, intervals in per_key.items():
        busy, peak = sweep(intervals)
        by_key[key] = {"busy_ms": busy, "peak_in_flight": peak}
        model_intervals[key[1]].extend(intervals)
    by_model = {}
    for model, intervals in model_intervals.items():
        busy, peak = sweep(intervals)
        by_model[model] = {"busy_ms": busy, "peak_in_flight": peak}
    return by_key, by_model


def attach_combined(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    day_from: str,
    day_to: str,
    measured_days: dict[tuple, set],
    *,
    source: str = "",
) -> list[dict[str, Any]]:
    """Add combined fields to speed_report's rows; return the per-model rows.

    Mutates ``rows`` in place (each gains busy_ms, combined_tok_per_s,
    parallelism and peak_in_flight) and returns one entry per normalised model
    that merges every harness the model was measured in.
    """
    by_key, by_model = combined_for_window(conn, day_from, day_to, source=source)
    models: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = (row["source"], row["model"], row["measurement_kind"], row["token_basis"])
        busy = by_key.get(key, {"busy_ms": 0.0, "peak_in_flight": 0})
        row.update(_combined(row["speed_tokens"], row["speed_ms"], busy["busy_ms"]))
        row["peak_in_flight"] = busy["peak_in_flight"]

        entry = models.setdefault(row["model"], {
            "model": row["model"], "sources": [], "measurement_kinds": [], "token_bases": [],
            "speed_tokens": 0, "speed_ms": 0.0, "speed_calls": 0,
            "eligible_calls": None, "_eligible": {}, "_days": set(),
            "first_measured_at": None, "last_measured_at": None,
            "missing_timing_calls": 0, "_unmeasured": {},
            "selected_days": row.get("selected_days"),
        })
        for field, value in (("sources", row["source"]), ("measurement_kinds", row["measurement_kind"]),
                             ("token_bases", row["token_basis"])):
            if value not in entry[field]:
                entry[field].append(value)
        entry["speed_tokens"] += row["speed_tokens"]
        entry["speed_ms"] += row["speed_ms"]
        entry["speed_calls"] += row["speed_calls"]
        # Eligibility and unmeasured counts belong to (source, model), shared by
        # that pair's kind/basis variants: count each pair once.
        entry["_eligible"][row["source"]] = row.get("eligible_calls")
        entry["_unmeasured"][row["source"]] = (row.get("unmeasured") or {}).get("missing_timing_calls", 0)
        entry["_days"].update(measured_days.get(key, ()))
        for bound, pick in (("first_measured_at", min), ("last_measured_at", max)):
            value = row.get(bound)
            if value is not None:
                entry[bound] = value if entry[bound] is None else pick(entry[bound], value)

    out = []
    for model, entry in models.items():
        eligible = [v for v in entry.pop("_eligible").values() if v is not None]
        entry["eligible_calls"] = sum(eligible) if eligible else None
        entry["coverage"] = coverage_for(entry["speed_calls"], entry["eligible_calls"]) if eligible else None
        entry["missing_timing_calls"] = sum(entry.pop("_unmeasured").values())
        entry["days_with_measurements"] = len(entry.pop("_days"))
        busy = by_model.get(model, {"busy_ms": 0.0, "peak_in_flight": 0})
        entry.update(_combined(entry["speed_tokens"], entry["speed_ms"], busy["busy_ms"]))
        entry["peak_in_flight"] = busy["peak_in_flight"]
        for field in ("sources", "measurement_kinds", "token_bases"):
            entry[field].sort()
        out.append(entry)
    out.sort(key=lambda m: (-m["speed_calls"], -(m["combined_tok_per_s"] or 0.0), m["model"]))
    return out


# --------------------------------------------------------------------------
# The grouping ladder
# --------------------------------------------------------------------------

def _parse(day: str) -> date:
    return date.fromisoformat(day)


def _bucket_start(day: date, bucket: str) -> date:
    if bucket == "week":
        return day - timedelta(days=day.weekday())
    if bucket == "month":
        return day.replace(day=1)
    if bucket == "quarter":
        return date(day.year, (day.month - 1) // 3 * 3 + 1, 1)
    return day


def _next_start(start: date, bucket: str) -> date:
    if bucket == "week":
        return start + timedelta(days=7)
    if bucket in ("month", "quarter"):
        step = 1 if bucket == "month" else 3
        months = start.year * 12 + start.month - 1 + step
        return date(months // 12, months % 12 + 1, 1)
    return start + timedelta(days=1)


def bucket_key(day: str, bucket: str, hour: Optional[int] = None) -> str:
    """The bucket a local date (and hour, for hourly) falls in."""
    if bucket == "hour":
        return f"{day}T{int(hour or 0):02d}"
    start = _bucket_start(_parse(day), bucket)
    if bucket == "month":
        return start.isoformat()[:7]
    if bucket == "quarter":
        return f"{start.year}-Q{(start.month - 1) // 3 + 1}"
    return start.isoformat()


def bucket_edges(bucket: str, day_from: str, day_to: str) -> list[dict[str, Any]]:
    """Buckets touching the window, each clipped to it, oldest first."""
    first, last = _parse(day_from), _parse(day_to)
    if bucket == "hour":
        edges = []
        day = first
        while day <= last:
            for hour in range(24):
                edges.append({"key": f"{day.isoformat()}T{hour:02d}", "from": day.isoformat(),
                              "to": day.isoformat(), "hour": hour})
            day += timedelta(days=1)
        return edges
    edges = []
    start = _bucket_start(first, bucket)
    while start <= last:
        nxt = _next_start(start, bucket)
        lo = max(start, first)
        hi = min(nxt - timedelta(days=1), last)
        edges.append({"key": bucket_key(start.isoformat(), bucket), "from": lo.isoformat(), "to": hi.isoformat()})
        start = nxt
    return edges


def offered_buckets(day_from: str, day_to: str) -> list[dict[str, Any]]:
    days = (_parse(day_to) - _parse(day_from)).days + 1
    out = []
    for name, cap, min_days in LADDER:
        count = len(bucket_edges(name, day_from, day_to)) if name != "hour" else days * 24
        out.append({"bucket": name, "count": count, "cap": cap,
                    "offered": 2 <= count <= cap and days >= min_days})
    return out


def auto_bucket(offer: list[dict[str, Any]]) -> str:
    for entry in offer:
        if entry["offered"]:
            return entry["bucket"]
    for entry in offer:
        if entry["count"] <= entry["cap"]:
            return entry["bucket"]
    return offer[-1]["bucket"]


# --------------------------------------------------------------------------
# Series
# --------------------------------------------------------------------------

def _round(value: Optional[float], digits: int) -> Optional[float]:
    return None if value is None else round(value, digits)


class _Series:
    """Per-bucket and per-hour-of-day sums for one key, plus raw intervals."""

    __slots__ = ("tokens", "ms", "calls", "intervals", "h_tokens", "h_ms", "h_calls", "h_intervals")

    def __init__(self, n: int):
        self.tokens = [0] * n
        self.ms = [0.0] * n
        self.calls = [0] * n
        self.intervals: list[list] = [[] for _ in range(n)]
        self.h_tokens = [0] * 24
        self.h_ms = [0.0] * 24
        self.h_calls = [0] * 24
        self.h_intervals: list[list] = [[] for _ in range(24)]

    def add(self, b: int, hour: int, start: float, end: float, tokens: int, ms: float, calls: int):
        self.tokens[b] += tokens
        self.ms[b] += ms
        self.calls[b] += calls
        self.intervals[b].append((start, end))
        self.h_tokens[hour] += tokens
        self.h_ms[hour] += ms
        self.h_calls[hour] += calls
        self.h_intervals[hour].append((start, end))

    def merge(self, other: "_Series"):
        for b in range(len(self.tokens)):
            self.tokens[b] += other.tokens[b]
            self.ms[b] += other.ms[b]
            self.calls[b] += other.calls[b]
            self.intervals[b].extend(other.intervals[b])
        for h in range(24):
            self.h_tokens[h] += other.h_tokens[h]
            self.h_ms[h] += other.h_ms[h]
            self.h_calls[h] += other.h_calls[h]
            self.h_intervals[h].extend(other.h_intervals[h])

    @staticmethod
    def _arrays(tokens, ms, calls, intervals, per_stream: bool) -> dict[str, list]:
        out = {"combined": [], "parallelism": [], "calls": list(calls)}
        if per_stream:
            out["per_stream"] = []
        for i, group in enumerate(intervals):
            busy = sweep(group)[0] if group else 0.0
            combined = _combined(tokens[i], ms[i], busy)
            out["combined"].append(_round(combined["combined_tok_per_s"], 2))
            out["parallelism"].append(_round(combined["parallelism"], 3))
            if per_stream:
                out["per_stream"].append(_round(rate_from_totals(tokens[i], ms[i]) if calls[i] else None, 2))
        return out

    def render(self, per_stream: bool) -> dict[str, Any]:
        out = self._arrays(self.tokens, self.ms, self.calls, self.intervals, per_stream)
        out["hours"] = self._arrays(self.h_tokens, self.h_ms, self.h_calls, self.h_intervals, per_stream)
        return out


def trend_rows(
    conn: sqlite3.Connection,
    day_from: str,
    day_to: str,
    *,
    bucket: str = "auto",
    source: str = "",
    hour_of_local: Callable[[int], int],
    date_of_local: Callable[[int], str],
) -> dict[str, Any]:
    """Every comparison key and every model, laid out over the window's buckets.

    One read of the measured responses fills both the time buckets and the 24
    local hours of the day, so the two charts the page draws from this payload
    can never describe different populations.
    """
    offer = offered_buckets(day_from, day_to)
    auto = auto_bucket(offer)
    offered = {entry["bucket"] for entry in offer if entry["offered"]}
    granularity = bucket if bucket in offered else auto
    edges = bucket_edges(granularity, day_from, day_to)
    index = {edge["key"]: i for i, edge in enumerate(edges)}
    since_ms, until_ms = _day_bounds_ms(day_from, day_to)
    wanted = (source or "").strip().lower()

    series: dict[tuple, _Series] = {}
    for key, ts, start, end, tokens, ms, calls in _responses(conn, since_ms, until_ms, wanted):
        hour = hour_of_local(ts)
        slot = index.get(bucket_key(date_of_local(ts), granularity, hour))
        if slot is None:  # pragma: no cover - the window bounds keep every row inside an edge
            continue
        entry = series.get(key)
        if entry is None:
            entry = series[key] = _Series(len(edges))
        entry.add(slot, hour, start, end, tokens, ms, calls)

    rows = []
    models: dict[str, dict[str, Any]] = {}
    for key in sorted(series, key=lambda k: (-sum(series[k].calls), k)):
        data = series[key]
        rows.append({"source": key[0], "model": key[1], "measurement_kind": key[2], "token_basis": key[3],
                     **data.render(per_stream=True)})
        merged = models.get(key[1])
        if merged is None:
            merged = models[key[1]] = {"model": key[1], "sources": set(), "measurement_kinds": set(),
                                       "_series": _Series(len(edges))}
        merged["sources"].add(key[0])
        merged["measurement_kinds"].add(key[2])
        merged["_series"].merge(data)

    model_rows = []
    for model, entry in models.items():
        data = entry.pop("_series")
        model_rows.append({"model": model, "sources": sorted(entry["sources"]),
                           "measurement_kinds": sorted(entry["measurement_kinds"]),
                           **data.render(per_stream=False)})
    model_rows.sort(key=lambda m: (-sum(m["calls"]), m["model"]))
    return {
        "granularity": granularity,
        "requested_bucket": bucket,
        "auto_bucket": auto,
        "offered_buckets": [{"bucket": e["bucket"], "count": e["count"]} for e in offer if e["offered"]],
        "buckets": edges,
        "timestamp_basis": "usage_timestamp",
        "series": rows,
        "model_series": model_rows,
    }
