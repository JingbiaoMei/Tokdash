"""View models for the revision-5 TUI: payload in, display rows out.

No Textual, no Rich, and no import of the backend: every function here takes the
JSON a route returned and produces already-formatted strings plus the styling
NAME a surface should paint them with. That keeps the correctness rules -- which
field is authoritative, what a missing figure means, which denominator a share
uses -- in one place the tests can drive with contract-shaped fixtures, instead
of scattered across widget code.

Three rules run through all of it:

* Missing is not zero. An absent figure renders as an em-dash; a measured zero
  renders as zero. They mean different things and the TUI may not merge them.
* One figure, one source. A share divides by the denominator the payload names,
  never by whatever else is on screen; when two scans disagree, both numbers
  stay visible instead of being reconciled by invention.
* Never average an average. Rates and hits come from the server's own summed
  figures; this module only formats them.
"""
from __future__ import annotations

from .formatting import (
    EM_DASH,
    fmt_cost_row,
    fmt_hit,
    fmt_int,
    fmt_tokens,
    tool_label,
)

MISSING = EM_DASH
UNAVAILABLE = "unavailable"

# Model table sorts. "cost" is a SORT of the same aggregate table, never a second
# leaderboard: one table, one ranking per glance.
MODEL_SORTS: tuple[tuple[str, str], ...] = (
    ("tokens", "Tokens"),
    ("cost", "Cost"),
    ("messages", "Messages"),
    ("name", "Name"),
)

SESSION_SORTS: tuple[tuple[str, str], ...] = (
    ("last_seen", "Last recorded"),
    ("tokens", "Tokens"),
    ("cost", "Cost"),
    ("model", "Main model"),
)


def _num(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _opt_int(value: object) -> int | None:
    return None if value is None else int(_num(value))


def _share(part: float, whole: float) -> str:
    if not whole:
        return MISSING  # zero denominator: no share exists, "0.0%" would lie
    return f"{100.0 * part / whole:.1f}%"


def _matched(row: dict, needle: str, fields: tuple[str, ...]) -> bool:
    if not needle:
        return True
    lowered = needle.lower()
    return any(lowered in str(row.get(field, "")).lower() for field in fields)


# ---------------------------------------------------------------------------
# Overview: Harnesses
# ---------------------------------------------------------------------------


def _has_key(by: dict, key: object) -> bool:
    """Exact key, then the case-folded fallback (real payloads key by tool id,
    some fixtures key by a cased variant). A display LABEL never bridges to an
    id, so an unmatched source stays a miss and prints a dash."""
    lowered = {str(k).lower() for k in by}
    return str(key) in by or str(key).lower() in lowered


def _active_entry(by: dict, key: object) -> dict:
    entry = by.get(str(key))
    if entry is None:
        entry = {str(k).lower(): v for k, v in by.items()}.get(str(key).lower())
    return entry or {}


def harness_rows(
    usage: dict,
    active_by_tool: dict | None = None,
    *,
    labels: dict | None = None,
    wide: bool = True,
) -> list[dict]:
    """One row per source in ``usage.by_tool``, tokens descending.

    Token splits come from the CURRENT by_tool fields, which carry output for
    every source including OpenClaw; messages do not exist there, so they join
    from ``apps[tool]`` and fall back to summing that source's models.

    Estimated time joins active-time by TOOL ID. Absent is MISSING (that source
    was not read, or has no measurement); a measured zero prints "0s". The old
    Overview hid a measured zero behind a dash, which made an idle day look like
    a broken one.
    """
    active_by_tool = active_by_tool or {}
    apps = usage.get("apps") or {}
    openclaw_models = usage.get("openclaw_models") or []
    rows: list[dict] = []
    for tool, agg in (usage.get("by_tool") or {}).items():
        app = apps.get(tool) or {}
        messages = app.get("messages")
        if messages is None and tool == "openclaw" and openclaw_models:
            counted = [m.get("messages") for m in openclaw_models if m.get("messages") is not None]
            messages = sum(counted) if counted else None
        time_entry = _active_entry(active_by_tool, tool)
        agent_ms = time_entry.get("active_ms_sum")
        rows.append(
            {
                "tool": tool,
                "harness": tool_label(tool, labels),
                "tokens": fmt_tokens(agg.get("tokens")),
                "tokens_sort": _num(agg.get("tokens")),
                "input": fmt_tokens(app.get("tokens_in", agg.get("tokens_in"))),
                "output": fmt_tokens(app.get("tokens_out", agg.get("tokens_out"))),
                "cache": fmt_tokens(app.get("tokens_cache", agg.get("tokens_cache"))),
                "hit": fmt_hit(agg.get("cache_hit_rate")),
                "messages": fmt_int(messages),
                "cost": fmt_cost_row(agg.get("cost")),
                "agent_time": MISSING if agent_ms is None else _duration(agent_ms),
                "clock_time": MISSING
                if time_entry.get("active_ms") is None
                else _duration(time_entry.get("active_ms")),
                "sessions": fmt_int(time_entry.get("session_count")),
                "has_time": bool(active_by_tool) and _has_key(active_by_tool, tool),
            }
        )
    rows.sort(key=lambda r: -r["tokens_sort"])
    if not wide:
        for row in rows:
            for key in ("input", "output", "cache", "hit", "messages"):
                row.pop(key, None)
    return rows


def harness_models(usage: dict, tool: str, *, labels: dict | None = None) -> list[dict]:
    """The models one source recorded, from that source's own payload."""
    apps = usage.get("apps") or {}
    source = (apps.get(tool) or {}).get("models")
    if source is None and tool == "openclaw":
        source = usage.get("openclaw_models") or []
    rows = []
    for row in source or []:
        rows.append(
            {
                "name": str(row.get("name") or MISSING),
                "tokens": fmt_tokens(row.get("tokens")),
                "tokens_sort": _num(row.get("tokens")),
                "cost": fmt_cost_row(row.get("cost")),
                "messages": fmt_int(row.get("messages")),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Overview: Models (aggregate across EVERY harness)
# ---------------------------------------------------------------------------


def model_rows(
    usage: dict,
    *,
    sort: str = "tokens",
    search: str = "",
    wide: bool = True,
) -> dict:
    """The aggregate Models table, straight from ``usage.combined_models``.

    ``combined_models`` is already merged on the NORMALIZED model name across
    every coding source and OpenClaw, so this table has one row per model and no
    Harness column: a model used in three harnesses appears once, with the sums.
    Grouping by source here would double-count, and a Harness column would imply
    this table can answer a per-source question it cannot. Source questions
    belong to the Harnesses view.

    Share always divides by the GLOBAL ``total_tokens``, including while a search
    filters the rows -- otherwise the visible rows would appear to be everything.
    Reasoning is absent from these merged rows, so it is absent here too; it must
    never be reconstructed by subtraction.
    """
    if sort not in {key for key, _ in MODEL_SORTS}:
        sort = "tokens"
    rows_in = [r for r in (usage.get("combined_models") or []) if isinstance(r, dict)]
    total_tokens = _num(usage.get("total_tokens"))
    needle = str(search or "").strip()
    visible = [r for r in rows_in if _matched(r, needle, ("name",))]

    if sort == "name":
        visible.sort(key=lambda r: (str(r.get("name") or "").lower(), -_num(r.get("tokens"))))
    else:
        primary = {"tokens": "tokens", "cost": "cost", "messages": "messages"}[sort]
        secondary = {"tokens": "cost", "cost": "tokens", "messages": "tokens"}[sort]
        visible.sort(
            key=lambda r: (
                -_num(r.get(primary)),
                -_num(r.get(secondary)),
                str(r.get("name") or "").lower(),
            )
        )

    rows = []
    for row in visible:
        rows.append(
            {
                "name": str(row.get("name") or MISSING),
                "tokens": fmt_tokens(row.get("tokens")),
                "share": _share(_num(row.get("tokens")), total_tokens),
                "cost": fmt_cost_row(row.get("cost")),
                "input": fmt_tokens(row.get("tokens_in")),
                "output": fmt_tokens(row.get("tokens_out")),
                "cache": fmt_tokens(row.get("tokens_cache")),
                "hit": fmt_hit(row.get("cache_hit_rate")),
                "messages": fmt_int(row.get("messages")),
            }
        )
    if not wide:
        for row in rows:
            for key in ("input", "output", "cache", "hit", "messages"):
                row.pop(key, None)

    listed = sum(_num(r.get("tokens")) for r in visible)
    return {
        "rows": rows,
        "sort": sort,
        "searched": bool(needle),
        "count": len(rows),
        "total_models": len(rows_in),
        # Coverage of the headline rather than an invented "Other" row: what the
        # listed models add up to against the total this response reports.
        "coverage": _share(listed, total_tokens) if rows else MISSING,
        "listed_tokens": listed,
        "total_tokens": total_tokens,
    }


def kpi_cards(
    usage: dict,
    active: dict | None,
    *,
    window: str = "",
) -> list[dict]:
    """The six headline cards, each with the one authoritative field behind it.

    Values are read whole (``total_tokens``, not a sum of the displayed parts)
    and the deltas come from the payload's own ``comparison`` block, which names
    the prior window it measured against. A null delta means there was nothing
    comparable, which prints as no delta at all rather than "0%".
    """
    usage = usage or {}
    active = active or {}
    comparison = usage.get("comparison") or {}
    active_cmp = (active.get("comparison") or {}) if active else {}
    top = (usage.get("top_models") or [{}])[0]
    cost_value = "${0}".replace("{0}", _money(usage.get("total_cost")))
    return [
        {
            "key": "tokens",
            "label": "Total tokens",
            "short_label": "Tokens",
            "value": fmt_tokens(usage.get("total_tokens")),
            "delta": comparison.get("tokens_pct"),
            "detail": f"{fmt_int(usage.get('total_tokens'))} tokens",
            "short_detail": fmt_int(usage.get("total_tokens")),
            "style": "kpi_tokens",
        },
        {
            "key": "cost",
            "label": "Total cost",
            "short_label": "Cost",
            "value": cost_value,
            "delta": comparison.get("cost_pct"),
            "detail": "usage cost estimate in US dollars, not an invoice",
            "short_detail": "USD, estimate only",
            "style": "kpi_cost",
        },
        {
            "key": "messages",
            "label": "Total messages",
            "short_label": "Messages",
            "value": fmt_int(usage.get("total_messages")),
            "delta": comparison.get("messages_pct"),
            "detail": "assistant + user messages, not sessions",
            "short_detail": "user + assistant",
            "style": "kpi_messages",
        },
        {
            "key": "agent",
            "label": "Est. agent time",
            "short_label": "Agent",
            "value": _duration(active.get("active_ms_sum")) if active else MISSING,
            "short_value": (
                _duration_short(active.get("active_ms_sum")) if active else MISSING
            ),
            "delta": active_cmp.get("active_ms_sum_pct"),
            "detail": "concurrent agents add; estimated clock time is separate",
            "short_detail": "not wall clock",
            "style": "kpi_agent",
        },
        {
            "key": "cache",
            "label": "Cache hit rate",
            "short_label": "Cache hit",
            "value": fmt_hit(usage.get("cache_hit_rate")),
            "delta": None,
            "detail": "prompt-token weighted, not a mean of source rates",
            "short_detail": "by prompt tokens",
            "style": "kpi_cache",
        },
        {
            "key": "model",
            "label": "Most used model",
            "short_label": "Top model",
            "value": str(top.get("name") or MISSING),
            "delta": None,
            # Ranked by TOKENS (top_models[0]), whatever the table below is sorted
            # by -- a cost-sorted table must not rename this card.
            "detail": f"{fmt_tokens(top.get('tokens'))} tokens (token-ranked)",
            "short_detail": f"{fmt_tokens(top.get('tokens'))} tokens",
            "style": "kpi_model",
        },
    ] if (usage or active) else []


def status_note(
    usage: dict | None,
    active: dict | None = None,
    *,
    usage_status: str = "hit",
) -> str | None:
    """ONE relevant status line, or None when nothing needs saying.

    Healthy data needs no banner; a partial read does, because the numbers on
    screen are then smaller than the truth in a specific way.
    """
    bits: list[str] = []
    unavailable = (active or {}).get("unavailable_tools")
    if unavailable:
        if isinstance(unavailable, dict):
            names = ", ".join(str(k) for k in unavailable)
        else:
            names = ", ".join(str(x) for x in unavailable)
        bits.append(f"agent time unmeasured: {names}")
    n, names = source_errors(usage)
    if n:
        # A usage scan that dropped a source answers with SMALLER totals, not
        # wrong ones -- but the reader cannot tell that from a quiet window. The
        # producer names the failures; the Overview has to repeat them. Counting
        # first is the guard: the helper answers (0, "") when nothing failed, and
        # a two-slot tuple is truthy even when it says nothing went wrong.
        bits.append(
            f"usage partial: {n} source{'s' if n != 1 else ''} unreadable ({names})"
            f" · totals below are short by {'that source' if n == 1 else 'those sources'}"
        )
    if usage_status == "stale":
        bits.append("showing the last computed snapshot")
    if (usage or {}).get("range", {}).get("recognized") is False:
        bits.append("range not recognised by the server")
    return " · ".join(bits) if bits else None


def source_errors(usage: dict | None) -> tuple[int, str]:
    """(count, names) of the usage sources the scan could not read.

    ``source_errors`` is the producer's own list. It arrives in several shapes
    over the versions this TUI talks to -- a list of names, a list of {source}
    rows, or a {source: reason} mapping -- and all of them name the same thing:
    the totals on screen are missing that source.
    """
    raw = (usage or {}).get("source_errors")
    if not raw:
        return (0, "")
    names: list[str] = []
    if isinstance(raw, dict):
        names = [str(key) for key in raw]
    elif isinstance(raw, str):
        names = [raw]
    elif isinstance(raw, (list, tuple)):
        for entry in raw:
            if isinstance(entry, dict):
                named = entry.get("source") or entry.get("tool") or entry.get("name")
                if named:
                    names.append(str(named))
            elif entry:
                names.append(str(entry))
    names = [n for n in dict.fromkeys(names) if n]
    return (len(names), ", ".join(names))


# ---------------------------------------------------------------------------
# Overview: the one global activity strip
# ---------------------------------------------------------------------------

_BUCKET_UNIT = {"hour": "hour", "day": "day", "month": "month"}


def activity_strip(
    sparkline: dict | None,
    *,
    width: int = 24,
    glyphs: str = "blocks",
    max_rows: int = 6,
) -> dict:
    """The usage buckets this response actually carries, as a labelled strip.

    ``usage.sparkline`` is GLOBAL -- totals over the window, one series -- so
    this is the only activity chart the payload supports. There is no per-harness
    bucket series, and inventing one by splitting tokens by source share would put
    a fabricated number on screen. Past windows beyond the resolver's bound carry
    no buckets at all; then the honest answer is "unavailable for this range",
    not a made-up line.
    """
    if not isinstance(sparkline, dict) or not sparkline.get("buckets"):
        return {"available": False, "note": "Activity chart unavailable for this range."}
    granularity = str(sparkline.get("granularity") or "day")
    unit = _BUCKET_UNIT.get(granularity, "day")
    buckets = list(sparkline["buckets"])
    peak = max((_num(b.get("tokens")) for b in buckets), default=0.0)
    rows = []
    for bucket in buckets:
        tokens = _num(bucket.get("tokens"))
        rows.append(
            {
                "key": str(bucket.get("key") or MISSING),
                "bar": _bar(tokens, peak, width=width, glyphs=glyphs),
                "tokens": fmt_tokens(tokens),
                "cost": fmt_cost_row(bucket.get("cost")),
                "messages": fmt_int(bucket.get("messages")),
                "zero": tokens == 0,
                # The chart draws EVERY bucket the route returned. Truncating the
                # picture to the rows a pane can show would hide measurements,
                # so the strip below keeps its row budget and the chart does not.
                "value": tokens,
                "chart_key": chart_key(bucket.get("key")),
            }
        )
    shown = rows[-max_rows:] if max_rows else rows
    span = f"{buckets[0].get('key')} .. {buckets[-1].get('key')}" if buckets else MISSING
    return {
        "available": True,
        "granularity": granularity,
        "unit": unit,
        "rows": shown,
        "all_rows": rows,
        "bucket_count": len(buckets),
        "span": span,
        "note": f"{len(buckets)} {unit} buckets · {span} · server local time",
        "peak": fmt_tokens(peak),
    }


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def session_rows(
    payloads: dict[str, dict],
    *,
    search: str = "",
    sort: str = "last_seen",
    labels: dict | None = None,
    narrow: bool = False,
    failed: list[str] | None = None,
) -> dict:
    """Merge one response per source into a single, searchable, sortable list.

    ``/api/sessions`` answers for ONE tool at a time with no server-side search,
    sort, offset or limit, so the merge and the paging are ours. Selection
    identity is the (tool, session_id) PAIR: two sources can carry the same
   -looking id and they are not the same session.

    ``failed`` names the sources that did not answer. Their absence is not a
    count of zero sessions, so the result says "partial" and keeps the sources
    that did answer on screen.
    """
    if sort not in {key for key, _ in SESSION_SORTS}:
        sort = "last_seen"
    rows: list[dict] = []
    for tool, payload in (payloads or {}).items():
        for entry in (payload or {}).get("sessions") or []:
            if not isinstance(entry, dict):
                continue
            session_id = str(entry.get("session_id") or MISSING)
            display = str(entry.get("project") or "").strip() or session_id
            rows.append(
                {
                    "tool": tool,
                    "session_id": session_id,
                    "harness": tool_label(tool, labels),
                    "display": display,
                    "last_seen": str(entry.get("last_seen_at") or ""),
                    "last_seen_shown": _timestamp(entry.get("last_seen_at")),
                    # The same instant at two resolutions. The list picks the
                    # one its column can hold WHOLE: a stamp cropped to
                    # "2026-10-18 11:…" is a timestamp that stops being one, and
                    # the detail line always carries the full figure.
                    "last_seen_minute": _minute_stamp(entry.get("last_seen_at")),
                    "last_seen_clock": _clock_stamp(entry.get("last_seen_at")),
                    "model": str(entry.get("model") or MISSING),
                    "tokens": fmt_tokens(entry.get("tokens")),
                    "tokens_sort": _num(entry.get("tokens")),
                    "cost": fmt_cost_row(entry.get("cost")),
                    "cost_sort": _num(entry.get("cost")),
                    "messages": fmt_int(entry.get("token_events")),
                }
            )
    needle = str(search or "").strip().lower()
    if needle:
        rows = [
            r
            for r in rows
            if needle in r["display"].lower()
            or needle in r["session_id"].lower()
            or needle in r["model"].lower()
            or needle in r["harness"].lower()
        ]
    if sort == "name" or sort == "last_seen":
        rows.sort(key=lambda r: (r["last_seen"], r["tool"], r["session_id"]), reverse=True)
    elif sort == "model":
        rows.sort(key=lambda r: (r["model"].lower(), r["tool"], r["session_id"]))
    else:
        rows.sort(key=lambda r: (-r[sort + "_sort"], r["tool"], r["session_id"]))
    failed = failed or []
    return {
        "rows": rows,
        "sort": sort,
        "count": len(rows),
        "sources": sorted(payloads or {}),
        "failed": list(failed),
        "partial": bool(failed),
        "complete": not failed,
    }


def session_detail_rows(detail: dict, *, limit: int = 0) -> list[dict]:
    """Turns of the ENTIRE session, in the order the reader returned them.

    ``/api/session`` takes no dates, so this is never "the turns inside the
    selected range". Turns without a timestamp keep their recorded order and
    print a dash rather than a time invented from the session's start.
    """
    turns = (detail or {}).get("turns") or []
    if limit:
        turns = turns[-limit:]
    rows = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        rows.append(
            {
                "index": str(turn.get("turn_index") if turn.get("turn_index") is not None else MISSING),
                "timestamp": _timestamp(turn.get("timestamp")),
                "model": str(turn.get("model") or MISSING),
                "tokens": fmt_tokens(turn.get("tokens")),
                "output": fmt_tokens(turn.get("tokens_out")),
                "cost": fmt_cost_row(turn.get("cost")),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# formatting helpers shared by the view models
# ---------------------------------------------------------------------------


def _money(value: object) -> str:
    return f"{_num(value):,.2f}"


def _duration(ms: object) -> str:
    """Compact duration for a card or cell; delegates the wording to formatting."""
    from .formatting import fmt_duration

    return fmt_duration(_num(ms))


_UNIT_LETTERS = {
    "second": "s", "seconds": "s", "minute": "m", "minutes": "m",
    "hour": "h", "hours": "h", "day": "d", "days": "d",
    "week": "w", "weeks": "w", "month": "mo", "months": "mo",
    "year": "y", "years": "y",
}


def _duration_short(ms: object) -> str:
    """The same duration in unit LETTERS: "2 days 3 hours" is "2d 3h".

    A card column at a hundred columns is fourteen cells wide, and the spelled
    duration the contract locks -- which the CLI and the web both print -- does
    not fit it. Abbreviating the UNIT is the only shortening available: a cropped
    duration ("2 days 3 ho...") reads as a different length of time.
    """
    words = _duration(ms).split(" ")
    out: list[str] = []
    for index in range(0, len(words), 2):
        number = words[index]
        unit = words[index + 1] if index + 1 < len(words) else ""
        out.append(f"{number}{_UNIT_LETTERS.get(unit, unit)}")
    return " ".join(out)


def _timestamp(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return MISSING
    head = text.replace("T", " ")[:19]
    return head if head.strip() else MISSING


def _minute_stamp(raw: object) -> str:
    """``2026-10-18 11:40`` -- the instant without its seconds (16 cells)."""
    full = _timestamp(raw)
    return full[:-3] if len(full) == 19 and full[16] == ":" else full


def _clock_stamp(raw: object) -> str:
    """``10-18 11:40`` -- month, day, hour and minute (11 cells).

    The year and the seconds both go, and neither is a secret: the range strip
    above the list names the window these rows came from, and the note under it
    says the column is to the minute. The whole stamp, seconds included, is one
    Enter away in the session detail.
    """
    return _minute_stamp(raw)[5:]




def _bar(value: float, peak: float, *, width: int, glyphs: str = "blocks") -> str:
    from .formatting import text_bar

    return text_bar(value, peak, width=width, glyphs=glyphs)


def _num_str(value: float) -> str:  # pragma: no cover - tiny alias kept for clarity
    return fmt_tokens(value)


# ---------------------------------------------------------------------------
# Stats: one annual window, calendar first
# ---------------------------------------------------------------------------

_INTENSITY_GLYPHS = {
    "blocks": ("·", "▁", "▄", "█", "█"),
    "ascii": (".", "-", "=", "#", "#"),
}


def stats_summary(stats: dict, *, today: str | None = None, window_days: int | None = None) -> dict:
    """The annual headline, with the denominators the payload cannot state.

    ``total_days`` is the FIRST-to-LAST active span, not the window length, so
    the coverage line uses the window it was asked for. ``current_streak`` is
    anchored to today or yesterday by the producer, so a past calendar year has
    no current streak to show -- only the longest run inside that window. And
    ``messages`` is the field to read: ``sessions`` is a deprecated alias of it.
    """
    stats = stats or {}
    active_days = stats.get("active_days")
    span = stats.get("total_days")
    trailing = window_days or span
    current = stats.get("current_streak")
    show_current = False
    if today is not None:
        # The producer only counts a streak that ends today or yesterday, so a
        # current streak of >0 IS the evidence; a zero is a lapsed streak.
        show_current = bool(current)
    return {
        "active_days": fmt_int(active_days),
        "longest_streak": fmt_int(stats.get("longest_streak")),
        "current_streak": fmt_int(current) if show_current else MISSING,
        "current_note": "" if show_current else "no streak ending today or yesterday",
        "tokens": fmt_tokens(stats.get("total_tokens")),
        "messages": fmt_int(stats.get("messages")),
        "coverage": _share(_num(active_days), _num(trailing)),
        "span_note": f"active span {fmt_int(span)} days" if span is not None else MISSING,
        "most_used": str(stats.get("most_used_model") or MISSING),
        "costliest": str(stats.get("highest_cost_model") or MISSING),
    }


def stats_cards(summary: dict) -> list[dict]:
    """The annual headline as cards, in the shape the card band draws.

    Same fields as the paragraph this replaces, and the same honesty about what
    each one covers: a streak that lapsed shows as a dash with its reason beside
    it, and "messages" keeps the note that they are turns rather than sessions.
    """
    summary = summary or {}
    return [
        {"key": "days", "label": "Active days", "short_label": "Days",
         "value": summary["active_days"],
         "detail": summary["span_note"], "short_detail": "in this window"},
        {"key": "streak", "label": "Current streak", "short_label": "Streak",
         "value": summary["current_streak"],
         "detail": summary["current_note"] or "ending today or yesterday",
         "short_detail": "ends today/yesterday"},
        {"key": "longest", "label": "Longest streak", "short_label": "Best",
         "value": summary["longest_streak"], "detail": "within this window",
         "short_detail": "within this window"},
        {"key": "tokens", "label": "Total tokens", "short_label": "Tokens",
         "value": summary["tokens"],
         "detail": "usage total in this window", "short_detail": "this window"},
        {"key": "messages", "label": "Total messages", "short_label": "Messages",
         "value": summary["messages"],
         "detail": "assistant + user turns, not sessions",
         "short_detail": "user + assistant"},
        {"key": "model", "label": "Most used model", "short_label": "Top model",
         "value": summary["most_used"],
         "detail": f"costliest {summary['costliest']}",
         "short_detail": f"costliest {summary['costliest']}"},
    ]


def stats_calendar(
    stats: dict,
    *,
    window_from: str,
    window_to: str,
    selected: str | None = None,
    today: str | None = None,
    glyphs: str = "blocks",
) -> dict:
    """(see the month grid below: month_view is what the TUI paints)"""
    """A week-column calendar of the window, plus the selected day's figures.

    Cells come in three kinds and they must not be conflated: a day inside the
    window with no contribution is a MEASURED ZERO, a day outside the window is
    not part of the question, and a future day is unknown. The legend explains
    the intensity ranks, which are ranks against the other active days rather
    than fixed token thresholds.
    """
    from datetime import date, timedelta

    contributions = {
        str(entry.get("date")): entry
        for entry in (stats or {}).get("contributions") or []
        if isinstance(entry, dict) and entry.get("date")
    }
    start = date.fromisoformat(str(window_from))
    end = date.fromisoformat(str(window_to))
    # Pad the leading edge to a Monday so every column is a whole week.
    grid_start = start - timedelta(days=start.weekday())
    glyphs_map = _INTENSITY_GLYPHS.get(glyphs, _INTENSITY_GLYPHS["blocks"])
    columns = []
    day = grid_start
    while day <= end:
        week = []
        for offset in range(7):
            cell = day + timedelta(days=offset)
            iso = cell.isoformat()
            entry = contributions.get(iso)
            if cell < start or cell > end:
                kind = "outside"
            elif today and iso > today:
                kind = "future"
            elif entry is None:
                kind = "zero"
            else:
                kind = "active"
            totals = (entry or {}).get("totals") or {}
            week.append(
                {
                    "date": iso,
                    "day": str(cell.day),
                    "month": cell.strftime("%b"),
                    "kind": kind,
                    "intensity": int((entry or {}).get("intensity") or 0),
                    "glyph": " " if kind in ("outside", "future") else glyphs_map[
                        0 if kind in ("zero",) else int((entry or {}).get("intensity") or 0)
                    ],
                    "tokens": fmt_tokens(totals.get("tokens")) if entry else MISSING,
                    "cost": fmt_cost_row(totals.get("cost")) if entry else MISSING,
                    "messages": fmt_int(totals.get("messages")) if entry else MISSING,
                }
            )
        columns.append({"week_start": week[0]["date"], "cells": week})
        day += timedelta(days=7)

    chosen = None
    for column in columns:
        for cell in column["cells"]:
            if cell["date"] == selected:
                chosen = cell
    return {
        "columns": columns,
        "selected": chosen,
        "legend": " ".join(f"{glyphs_map[i]}={n}" for i, n in ((0, "none"), (1, "low"), (2, "mid"), (3, "high"))),
        "note": "each column is one week (Mon-Sun); intensity ranks days against each other",
        "month_view": [
            {
                "month": iso[:7],
                "label": _month_label(iso),
                "weeks": _month_weeks(
                    [c for col in columns for c in col["cells"] if c["date"][:7] == iso]
                ),
            }
            # Whole weeks pad the grid edges, so the cells can name a month that
            # holds NOT ONE day of the window (a calendar year's December gets a
            # January tacked on behind it). Paging to that month would show an
            # all-blank grid and read as "you did nothing that month", so the
            # month list is built from the window, not from the padding.
            for iso in sorted(
                {c["date"][:7]
                 for col in columns
                 for c in col["cells"]
                 if c["kind"] != "outside"}
            )
        ],
    }


_MONTH_CELL_WIDTH = 3  # "M  T  W  T  F  S  S" and one 3-cell block per day


def _month_label(iso_month: str) -> str:
    from datetime import date

    return date.fromisoformat(f"{iso_month}-01").strftime("%B %Y")


def _blank_cell(date_text: str = "") -> dict:
    return {"date": date_text, "day": "", "kind": "outside", "glyph": " ",
            "tokens": MISSING, "cost": MISSING, "messages": MISSING}


def _month_weeks(cells: list[dict]) -> list[list[dict]]:
    """A month's own days as whole weeks, Monday first, seven cells a row.

    The days arrive out of a shared week grid, so the month's first row usually
    starts mid-week: the leading cells are padded from the FIRST day's weekday
    rather than sliced by sevens, or every date in the month would land under the
    wrong weekday.
    """
    from datetime import date

    if not cells:
        return []
    lead = date.fromisoformat(str(cells[0]["date"])).weekday()
    padded = [_blank_cell() for _ in range(lead)] + list(cells)
    while len(padded) % 7:
        padded.append(_blank_cell())
    return [padded[start:start + 7] for start in range(0, len(padded), 7)]


# ---------------------------------------------------------------------------
# Report: When and Projects
# ---------------------------------------------------------------------------

WEEKDAY_ORDER = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def report_when(insights: dict, *, width: int = 20, glyphs: str = "blocks") -> dict:
    """All 24 hours in clock order and all seven weekdays in calendar order.

    Two things are kept apart on purpose: a bucket that measured nothing is a
    zero, while a facet that was never requested (or never returned) is
    unavailable and prints as such. Shares divide by ``insights.totals``, the
    same denominator every other facet uses, so the rows add up against the
    number printed above them -- not against ``/api/usage``, which is a different
    scan and may legitimately disagree.
    """
    insights = insights or {}
    totals = _num((insights.get("totals") or {}).get("tokens"))
    hourly = insights.get("hourly") or {}
    weekday = insights.get("weekday") or {}
    bars_hourly = hourly.get("buckets")
    bars_weekday = weekday.get("buckets")
    peak = max(
        [
            _num(b.get("tokens"))
            for b in (bars_hourly or []) + (bars_weekday or [])
            if isinstance(b, dict)
        ],
        default=0.0,
    )
    hours = []
    if bars_hourly:
        for index, bucket in enumerate(bars_hourly[:24]):
            tokens = _num(bucket.get("tokens"))
            hours.append(
                {
                    "hour": f"{index:02d}:00",
                    "bar": _bar(tokens, peak, width=width, glyphs=glyphs),
                    "tokens": fmt_tokens(tokens),
                    "share": _share(tokens, totals),
                    "messages": fmt_int(bucket.get("messages")),
                    "cost": fmt_cost_row(bucket.get("cost")),
                    "zero": tokens == 0,
                }
            )
    days = []
    if bars_weekday:
        for index, bucket in enumerate(bars_weekday[:7]):
            tokens = _num(bucket.get("tokens"))
            days.append(
                {
                    "day": _weekday_name(bucket, index),
                    "weekday": bucket.get("weekday", index),
                    "bar": _bar(tokens, peak, width=width, glyphs=glyphs),
                    "tokens": fmt_tokens(tokens),
                    "share": _share(tokens, totals),
                    "zero": tokens == 0,
                }
            )
    night = [int(h) for h in (hourly.get("night_hours") or []) if str(h).isdigit()]
    peak_hour = _hour_label(hourly.get("peak_hour"))
    peak_weekday = _peak_weekday(weekday.get("peak_weekday"), days)
    return {
        "available": bool(hours or days),
        "hours": hours,
        "weekdays": days,
        "peak_hour": peak_hour,
        "peak_weekday": peak_weekday,
        "night_share": hourly.get("night_share"),
        "night_label": _night_label(night),
        "timezone": insights.get("timezone") or "server local time",
        "denominator": fmt_tokens(totals) if insights.get("totals") else MISSING,
        "missing_hours": not bool(hours),
        "missing_weekdays": not bool(days),
    }


def _weekday_name(bucket: dict, index: int) -> str:
    """The producer's own weekday name ("Monday"), else an abbreviation."""
    name = str(bucket.get("name") or "").strip()
    if name:
        return name
    return WEEKDAY_ORDER[index] if index < len(WEEKDAY_ORDER) else str(index)


def _hour_label(value: object) -> str | None:
    """0-23 or "09:00" in, "09:00" out; None stays None (nothing was measured)."""
    if value is None or value == "":
        return None
    text = str(value)
    if ":" in text:
        return text
    return f"{int(text):02d}:00" if text.isdigit() else text


def _peak_weekday(value: object, days: list[dict]) -> str | None:
    """Resolve the facet's weekday INDEX to the same name the rows print."""
    if value is None or value == "":
        return None
    text = str(value)
    if not text.isdigit():
        return text
    index = int(text)
    for day in days:
        key = day.get("weekday")
        if isinstance(key, int) and key == index:
            return day["day"]
    if 0 <= index < len(WEEKDAY_ORDER):
        return WEEKDAY_ORDER[index]
    return text


def _night_label(hours: list[int]) -> str:
    """Label the server's night hours as the run they actually form.

    ``night_hours`` is a SET of clock hours, and it usually wraps midnight
    ([0, 1, 22, 23]). min/max of that set is 0..23, which reads as a whole-day
    window, so the run is found instead: the largest gap around the clock face
    is where the night is NOT, so the run starts just after it.
    """
    if not hours:
        return "night hours as the server defines them"
    ordered = sorted({int(h) % 24 for h in hours})
    if len(ordered) == 1:
        start = end = ordered[0]
    else:
        widest = -1
        gap_before = 0
        for index in range(len(ordered)):
            nxt = ordered[(index + 1) % len(ordered)]
            gap = (nxt - ordered[index]) % 24 or 24
            if gap > widest:
                widest, gap_before = gap, index
        start = ordered[(gap_before + 1) % len(ordered)]
        end = ordered[gap_before]
    return f"{start:02d}:00-{(end + 1) % 24:02d}:00"


def report_projects(
    insights: dict,
    *,
    sort: str = "tokens",
    anonymous: bool = False,
) -> dict:
    """Project attribution with its own denominator and an honest residual.

    The rows are ranked against ``insights.totals``; attributed plus
    unattributed may not equal that total, because attribution follows the
    transcript path recorded on each row and some sources record none. When they
    differ the difference is printed OUTSIDE the ranked rows -- never as a
    negative "project", and never by rescaling the shares until they add to 100.
    """
    facet = (insights or {}).get("projects") or {}
    rows_in = [r for r in (facet.get("projects") or []) if isinstance(r, dict)]
    totals = _num((insights or {}).get("totals", {}).get("tokens"))
    # The facet's own bucket: a dict of totals now, a bare token count in older
    # payloads. An ABSENT bucket still means "nothing was recorded here", which
    # is a dash, not a zero.
    raw_un = facet.get("unattributed")
    if isinstance(raw_un, dict):
        un_tokens, un_cost = _num(raw_un.get("tokens")), raw_un.get("cost")
        un_seen = bool(raw_un)
    else:
        un_tokens, un_cost = _num(raw_un), None
        un_seen = raw_un is not None

    def value(row: dict) -> float:
        return _num(row.get("cost")) if sort == "cost" else _num(row.get("tokens"))

    rows = []
    for row in sorted(rows_in, key=lambda r: (-value(r), str(r.get("project") or "").lower())):
        tokens = _num(row.get("tokens"))
        name = str(row.get("project") or MISSING)
        rows.append(
            {
                "project": name,
                "tokens": fmt_tokens(tokens),
                "share": _share(tokens, totals),
                "cost": fmt_cost_row(row.get("cost")),
                "messages": fmt_int(row.get("messages")),
                "sort_value": tokens,
            }
        )
    attributed = sum(_num(r.get("tokens")) for r in rows_in)
    residual = totals - attributed - un_tokens
    return {
        "rows": rows,
        "unattributed": {
            "project": "Unattributed",
            "tokens": fmt_tokens(un_tokens) if un_seen else MISSING,
            "share": _share(un_tokens, totals),
            "cost": fmt_cost_row(un_cost) if un_seen else MISSING,
        },
        "totals": fmt_tokens(totals),
        "coverage_note": (
            f"attributed + unattributed differs from the scan total by {residual:+,.0f} tokens"
            if abs(residual) >= 1
            else ""
        ),
        "reason": facet.get("unavailable_reason"),
        "anonymous": anonymous,
        "sort": sort if sort in ("tokens", "cost") else "tokens",
    }


# ---------------------------------------------------------------------------
# Model output speed
# ---------------------------------------------------------------------------

TIMING_KIND_LABELS = {
    "server_decode": "provider decode",
    "post_first_token": "after first token",
    "response_window": "response window",
    "request_window": "whole request",
}
TOKEN_BASIS_LABELS = {
    "output_only": "output tokens",
    "output_including_reasoning": "output + reasoning",
    "": "unspecified",
}
# The same two facts in the room a TABLE column has. Which clock and which tokens
# is a row's identity, so the column is never dropped and never cut in half --
# it is spelled smaller. "response / out+reason" says the same measurement as
# "response window / output + reasoning"; the row's detail (enter) prints the
# long form, and so does every wide-enough table.
TIMING_KIND_SHORT = {
    "server_decode": "decode",
    "post_first_token": "first token",
    "response_window": "response",
    "request_window": "whole req",
}
TOKEN_BASIS_SHORT = {
    "output_only": "out",
    "output_including_reasoning": "out+reason",
    "": "unspecified",
}


def chart_key(key: object) -> str:
    """A bucket key shortened to fit ONE chart column, never to a wrong date.

    The table beside a chart always prints the key in full; the axis underneath
    the bars only has to say WHICH column is which. A response's keys are ISO:
    "2026-10-12T09" (hour), "2026-10-12" (day), "2026-10" (month). The hour form
    keeps the date because an hourly trend runs across days, and "09" alone would
    put nine different Tuesdays under the same tick.
    """
    raw = str(key or "")
    if "T" in raw:
        day, _, hour = raw.partition("T")
        return f"{day[5:]} {hour[:2]}" if len(day) == 10 else raw
    if len(raw) == 10:
        return raw[5:]
    return raw


def chart_entries(rows: list[dict], value_key: str, *, label_key: str = "chart_key",
                  text_key: str | None = None) -> list[dict]:
    """The chart's own entries for rows this module already built.

    ``None`` rides through as ``None``: an unmeasured bucket has to reach the
    renderer as a gap, and a 0.0 there would draw a bar the server never measured.
    """
    return [
        {
            "label": str(row.get(label_key) or row.get("key") or row.get("hour") or ""),
            "value": row.get(value_key),
            "text": str(row.get(text_key) or "") if text_key else "",
        }
        for row in rows
    ]


def _rate_value(value: object) -> float | None:
    """The number behind a rate cell, or None when the server measured nothing."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rate(value: object) -> str:
    return MISSING if value is None else f"{_num(value):.1f}"


def speed_rows(
    payload: dict,
    *,
    lens: str = "stream",
    search: str = "",
    labels: dict | None = None,
) -> dict:
    """Comparison rows for the Across-models view.

    Per stream: one row per (source, model, measurement kind, token basis). Those
    keys STAY on screen because they are different measurement methods -- a
    decode clock and a whole-request clock are not two samples of one quantity,
    and merging them would produce a rate that measures nothing.

    Combined: the payload's ``models`` merge, one row per normalized model across
    the sources that measured it, carrying the server's own combined rate (tokens
    over the UNION of busy time). It has no pooled per-stream rate, and no client
    arithmetic may supply one: averaging per-stream rates is a mean of means, and
    a busy time computed from parallelism is an approximation of a union the
    server already took exactly.
    """
    payload = payload or {}
    needle = str(search or "").strip().lower()
    rows: list[dict] = []
    if lens == "combined":
        for entry in payload.get("models") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("model") or MISSING)
            if needle and needle not in name.lower():
                continue
            rows.append(
                {
                    "model": name,
                    "sources": ",".join(entry.get("sources") or []) or MISSING,
                    "timing": _kinds(entry.get("measurement_kinds")),
                    "timing_short": _kinds(entry.get("measurement_kinds"), short=True),
                    "timing_kind": _kinds(entry.get("measurement_kinds"), short=True),
                    "rate": _rate(entry.get("combined_tok_per_s")),
                    "rate_sort": _num(entry.get("combined_tok_per_s")),
                    "measured": fmt_int(entry.get("speed_calls")),
                    "eligible": fmt_int(entry.get("eligible_calls")),
                    "coverage": _coverage(entry.get("coverage")),
                    "peak_in_flight": fmt_int(entry.get("peak_in_flight")),
                    "parallelism": _parallelism(entry.get("parallelism")),
                }
            )
    else:
        for entry in payload.get("rows") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("model") or MISSING)
            source = str(entry.get("source") or MISSING)
            if needle and needle not in f"{source} {name}".lower():
                # Searched by id AND by display name: a reader types what the
                # table shows them.
                if needle not in tool_label(source, labels).lower():
                    continue
                continue
            rows.append(
                {
                    "model": name,
                    # The name the rest of the dashboard uses for this tool. The
                    # id stays in "source_id" -- it is what the request is built
                    # with -- but a screen that says "codex" here and "Codex" on
                    # the page above it makes the reader compare spellings.
                    "source_id": source,
                    "harness": tool_label(source, labels),
                    "timing": _kinds([entry.get("measurement_kind")])
                    + " / "
                    + _basis(entry.get("token_basis")),
                    "timing_short": _kinds([entry.get("measurement_kind")], short=True)
                    + " / "
                    + _basis(entry.get("token_basis"), short=True),
                    # NARROW: which clock, without the token basis. The basis is
                    # still in the row's detail; a column cannot hold four ideas
                    # in eighty columns and pretend nothing was lost.
                    "timing_kind": _kinds([entry.get("measurement_kind")], short=True),
                    "rate": _rate(entry.get("output_tok_per_s")),
                    "rate_sort": _num(entry.get("output_tok_per_s")),
                    "combined": _rate(entry.get("combined_tok_per_s")),
                    "measured": fmt_int(entry.get("speed_calls")),
                    "eligible": fmt_int(entry.get("eligible_calls")),
                    "coverage": _coverage(entry.get("coverage")),
                    "last": _ms_date(entry.get("last_ms")),
                    "status": entry.get("speed_status") or "",
                }
            )
    hidden = payload.get("hidden_request_windows") or []
    return {
        "rows": rows,
        "lens": lens,
        "count": len(rows),
        "hidden_request_windows": hidden,
        "source_status": payload.get("source_status") or [],
        "available_measurement_range": payload.get("available_measurement_range"),
        "cache": payload.get("cache") or {},
        "note": (payload.get("range") or {}).get("to") and "" or "",
    }


def _kinds(kinds: object, *, short: bool = False) -> str:
    if not kinds:
        return MISSING
    if isinstance(kinds, str):
        kinds = [kinds]
    table = TIMING_KIND_SHORT if short else TIMING_KIND_LABELS
    return ", ".join(table.get(str(k), str(k)) for k in kinds if k)


def _basis(basis: object, *, short: bool = False) -> str:
    text = str(basis or "")
    table = TOKEN_BASIS_SHORT if short else TOKEN_BASIS_LABELS
    return table.get(text, text)


def _coverage(coverage: object) -> str:
    if not isinstance(coverage, dict):
        return MISSING
    ratio = coverage.get("ratio")
    if ratio is None:
        return MISSING
    return f"{100.0 * _num(ratio):.0f}%"


def _parallelism(value: object) -> str:
    return MISSING if value is None else f"{_num(value):.2f}x"


def _ms_date(ms: object) -> str:
    if ms is None:
        return MISSING
    from datetime import datetime

    try:
        return datetime.fromtimestamp(_num(ms) / 1000.0).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return MISSING


def speed_trend_rows(payload: dict, *, key: tuple | None = None, lens: str = "stream") -> dict:
    """One selected series over the server's own buckets, with numeric readouts.

    The bucket resolution comes from ``offered_buckets``; bins are never merged
    client-side to make a chart fit, because the server's bucket counts are what
    make a rate comparable across the window. A null bucket is an empty bucket --
    a gap -- and never a zero.
    """
    payload = payload or {}
    buckets = payload.get("buckets") or []
    series = payload.get("series") or []
    model_series = payload.get("model_series") or []
    if lens == "combined":
        pool = model_series
        pick = pool[0] if pool else None
        if key is not None:
            name = key[1] if isinstance(key, tuple) else key
            pick = next((s for s in pool if s.get("model") == name), pick)
        arrays = pick or {}
        label = str((pick or {}).get("model") or MISSING)
    else:
        pool = series
        pick = pool[0] if pool else None
        if key is not None and isinstance(key, tuple):
            pick = next(
                (
                    s
                    for s in pool
                    if (s.get("source"), s.get("model"), s.get("measurement_kind"), s.get("token_basis")) == key
                ),
                pick,
            )
        arrays = pick or {}
        label = " / ".join(
            str(x)
            for x in (
                (pick or {}).get("source"),
                (pick or {}).get("model"),
                _kinds([(pick or {}).get("measurement_kind")]),
            )
            if x
        )
    per_stream = arrays.get("per_stream") or []
    combined = arrays.get("combined") or []
    calls = arrays.get("calls") or []
    parallelism = arrays.get("parallelism") or []
    spread = (arrays.get("spread") or {}) if lens != "combined" else {}
    rows = []
    for index, bucket in enumerate(buckets):
        stream_rate = per_stream[index] if index < len(per_stream) else None
        comb_rate = combined[index] if index < len(combined) else None
        rows.append(
            {
                "key": str(bucket.get("key") or bucket.get("from") or MISSING),
                "span": f"{bucket.get('from')} .. {bucket.get('to')}",
                "per_stream": _rate(stream_rate),
                "combined": _rate(comb_rate),
                # The FORMATTED cell above is for the table; these are for the
                # bars. A chart that re-reads a rounded string cannot tell an
                # unmeasured bucket from a measured zero, and would draw a stub
                # where the server said nothing at all.
                "per_stream_value": _rate_value(stream_rate),
                "combined_value": _rate_value(comb_rate),
                "chart_key": chart_key(bucket.get("key") or bucket.get("from")),
                "gap": (comb_rate is None and stream_rate is None),
                "calls": fmt_int(calls[index]) if index < len(calls) else MISSING,
                "parallelism": _parallelism(parallelism[index]) if index < len(parallelism) else MISSING,
                "p10": _rate((spread.get("p10") or [None] * (index + 1))[index])
                if spread.get("p10") and index < len(spread["p10"])
                else MISSING,
                "p90": _rate(spread["p90"][index]) if spread.get("p90") and index < len(spread["p90"]) else MISSING,
            }
        )
    measured = [r for r in rows if not r["gap"]]
    latest = measured[-1] if measured else None
    return {
        "rows": rows,
        "selected": label,
        "granularity": payload.get("granularity"),
        "offered": [str(e.get("bucket")) for e in payload.get("offered_buckets") or []],
        "bucket_count": len(buckets),
        "available": bool(buckets),
        "keys": [
            (
                s.get("source"),
                s.get("model"),
                s.get("measurement_kind"),
                s.get("token_basis"),
            )
            for s in series
        ],
        "models": [s.get("model") for s in model_series],
        "spread_note": "spread is duration-weighted and needs 30+ responses in the bucket"
        if spread
        else "no spread returned for this series",
        "latest": f"{latest['key']}: {latest['per_stream'] if lens == 'stream' else latest['combined']} tok/s"
        if latest
        else "no measured bucket",
        "note": f"{payload.get('granularity')} buckets · {len(buckets)} · server local time",
    }


def speed_hour_rows(payload: dict, *, lens: str = "stream") -> dict:
    """Hour-of-day for ONE exact key, or the merged model's own hours.

    ``time-of-day`` needs source + model + kind + basis; the combined lens uses
    the merged model series' ``hours`` arrays instead, because a keyless
    hour-of-day request would answer for a comparison that was never chosen.
    """
    payload = payload or {}
    if lens == "combined":
        arrays = payload if isinstance(payload.get("combined"), list) else (payload.get("hours") or {})
        values = (arrays or {}).get("combined") or []
        calls = (arrays or {}).get("calls") or []
        par = (arrays or {}).get("parallelism") or []
        rows = [
            {
                "hour": f"{h:02d}:00",
                "rate": _rate(values[h] if h < len(values) else None),
                "rate_value": _rate_value(values[h] if h < len(values) else None),
                "gap": not (h < len(values) and values[h] is not None),
                "calls": fmt_int(calls[h]) if h < len(calls) else MISSING,
                # The server measured the overlap for these hours too; it is the
                # only per-hour figure the merged arrays carry besides the rate.
                "parallelism": _parallelism(par[h]) if h < len(par) else MISSING,
            }
            for h in range(24)
        ]
        return {
            "rows": rows,
            "available": any(not r["gap"] for r in rows),
            # Identity rides along with the rates. An hour table that cannot say
            # which merged model it measured, over which sources and which timing
            # kinds, is a chart of nobody -- and the painter reads THESE keys, so
            # dropping them here is what printed "merged model —" over a screen
            # full of perfectly good measurements.
            "selected_key": payload.get("selected_key") or {},
            "sources": payload.get("sources") or [],
            "kinds": payload.get("kinds") or [],
            "empty_reason": payload.get("empty_reason"),
            "note": "merged model hours",
        }
    hourly = payload.get("hourly") or []
    peak = max((_num(b.get("output_tok_per_s")) for b in hourly if isinstance(b, dict)), default=0.0)
    rows = [
        {
            "hour": f"{int(b.get('hour', i)):02d}:00",
            "rate": _rate(b.get("output_tok_per_s")),
            "rate_value": _rate_value(b.get("output_tok_per_s")),
            "gap": b.get("output_tok_per_s") is None,
            "calls": fmt_int(b.get("speed_calls")),
            "eligible": fmt_int(b.get("eligible_calls")),
            "coverage": _coverage(b.get("coverage")),
            "bar": _bar(_num(b.get("output_tok_per_s")), peak, width=20),
        }
        for i, b in enumerate(hourly)
        if isinstance(b, dict)
    ]
    return {
        "rows": rows,
        "available": bool(rows),
        "selected_key": payload.get("selected_key") or {},
        "days_with_measurements": payload.get("days_with_measurements"),
        "note": "hours use the server's local clock; an observed hour is not a ranking",
    }


# ---------------------------------------------------------------------------
# Pricing (read-only)
# ---------------------------------------------------------------------------


def pricing_rows(payload: dict, *, search: str = "") -> dict:
    """Effective rates, USD per 1M tokens, straight from the pricing database.

    Read-only by design: there is no editor here, and a model with no rate prints
    a dash rather than an invented provider. A missing rate and a zero rate stay
    different things.
    """
    data = (payload or {}).get("data") or {}
    models = data.get("models") or {}
    aliases = data.get("aliases") or {}
    needle = str(search or "").strip().lower()
    by_alias: dict[str, list[str]] = {}
    for alias, canonical in aliases.items():
        if isinstance(canonical, str):
            by_alias.setdefault(canonical, []).append(str(alias))
    rows = []
    for name, rates in models.items():
        if not isinstance(rates, dict):
            continue
        names = [str(name)] + by_alias.get(str(name), [])
        if needle and not any(needle in n.lower() for n in names):
            continue
        rows.append(
            {
                "model": str(name),
                "input": _rate_per_m(rates.get("input")),
                "output": _rate_per_m(rates.get("output")),
                "cache_read": _rate_per_m(rates.get("cache_read")),
                "cache_write": _rate_per_m(rates.get("cache_write")),
                "aliases": ", ".join(sorted(by_alias.get(str(name), []))[:3]),
            }
        )
    rows.sort(key=lambda r: r["model"].lower())
    return {
        "rows": rows,
        "count": len(rows),
        "aliases": len(aliases) if isinstance(aliases, dict) else 0,
        "version": str(data.get("version") or MISSING),
        "last_updated": str(data.get("lastUpdated") or MISSING),
        "source": str((payload or {}).get("source") or MISSING),
        "baseline_version": (payload or {}).get("baseline_version") or MISSING,
        "override_note": (
            "an active override fully replaces the bundled baseline and freezes its updates"
            if (payload or {}).get("source") == "override"
            else ""
        ),
    }


def _rate_per_m(value: object) -> str:
    if value is None:
        return MISSING  # no rate recorded -- not the same as a free model
    return f"{_num(value):,.4f}"
