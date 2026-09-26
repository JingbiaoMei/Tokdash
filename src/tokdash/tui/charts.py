"""Pure chart renderers for the TUI (round 2): period visuals, tool+model
detail, quota table. NO textual and NO rich import here — same discipline as
formatting.py/report.py (the import-discipline test proves it), and only
``formatting`` may be imported from tokdash: every function takes the raw
payload dict and returns segments (list[list[Run]]), row-tuple lists, or
plain strings for DataTable cells. app.py owns markup escaping.

Payload shapes read here (verified against the real sources, not guessed):
- stats ``contributions`` rows: {date, totals{tokens,...}, intensity 0-4, ...}
  — ACTIVE days only, token value nested in ``totals.tokens`` (the round-1 F1
  bug class), ``intensity`` is the server rank (compute.assign_contribution_
  intensity) and is NEVER recomputed by a renderer where the spec says so.
- active-time payload ``by_tool``: {tool: {tool_label, session_count,
  active_ms, active_ms_sum}} (sessions.get_active_time_data).
- usage ``apps[tool]["models"]`` (openclaw excluded; its models live top-level
  in ``openclaw_models``) + ``by_tool`` incl. the synthesized openclaw row.
- quota_state() providers{status, buckets[{account, bucket, bucket_label,
  used_percent, resets_at, ...}]} and UsageEntryStore.quota_history()
  series[{provider, account, bucket, points[{captured_at, used_percent}]}].

Every renderer COPIES what it sorts and mutates nothing (non-mutation tests);
null-vs-zero law: None → em-dash/"n/a", measured 0 → "0"/" 0.0%".
"""
from __future__ import annotations

import math
from bisect import bisect_right
from datetime import date, datetime, timedelta
from typing import Any

from .formatting import (
    EM_DASH,
    Run,
    day_spark,
    fmt_cost_row,
    fmt_duration,
    fmt_hit,
    fmt_int,
    fmt_tokens,
    heat_glyph,
    text_bar,
    tool_label,
)

# Fixed English weekday table — datetime.strftime("%a") is locale-dependent
# and the byte stream must not depend on the host locale (same rule as
# fmt_int's fixed grouping).
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

# Bar scale for the quota table's used-percent cell; DataTable cells are
# fixed-width so this is a constant, not a terminal-width parameter.
_QUOTA_BAR_WIDTH = 10


def _parse_day(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def _tokens_of(contrib: dict) -> Any:
    # The F1 bug class: contribution token counts are NESTED in totals.
    return (contrib.get("totals") or {}).get("tokens")


def _index_by_date(contribs: list[dict] | None) -> dict[str, dict]:
    return {
        str(row.get("date")): row
        for row in (contribs or [])
        if row.get("date")
    }


def _intensity_fallback(rows: list[dict]) -> dict[int, int]:
    """Rank levels for contribution rows that carry NO ``intensity`` key.

    Mirrors compute.assign_contribution_intensity: rank = share of the sorted
    nonzero values at-or-below this day's tokens, level = ceil(rank*4) clamped
    1..4 (a 0-token or empty-set day stays 0). Rows that already carry an
    intensity are returned untouched — a renderer never overrides the server.
    """
    unranked = [r for r in rows if r.get("intensity") is None]
    active = sorted(
        int(_tokens_of(r) or 0) for r in unranked if (int(_tokens_of(r) or 0) > 0)
    )
    levels: dict[int, int] = {}
    for row in unranked:
        tokens = int(_tokens_of(row) or 0)
        if not active or tokens <= 0:
            levels[id(row)] = 0
        else:
            rank = bisect_right(active, tokens) / len(active)
            levels[id(row)] = min(4, max(1, math.ceil(rank * 4)))
    return levels


# ---------------------------------------------------------------------------
# week view: per-day token bars over the FULL calendar window
# (the day view has NO chart since round 3: per-tool time is the Time column
# of the Overview tools table, not a top section)
# ---------------------------------------------------------------------------

def day_bars(
    contribs: list[dict] | None, date_from: str, date_to: str,
    *, width: int, glyphs: str = "blocks",
) -> list[list[Run]]:
    """One row per CALENDAR day from date_from to date_to, inclusive.

    ``contribs`` carries ACTIVE days only, so the gaps are enumerated
    client-side — an inactive day renders its label + EM_DASH, never a
    skipped row (the count of rows is the window's span). Token values read
    the NESTED ``totals.tokens``; a present-but-zero day is a MEASURED 0 and
    prints "0", never a dash.
    """
    by_date = _index_by_date(contribs)
    start, end = _parse_day(date_from), _parse_day(date_to)
    present = [
        int(_tokens_of(by_date[(start + timedelta(days=offset)).isoformat()]) or 0)
        for offset in range((end - start).days + 1)
        if (start + timedelta(days=offset)).isoformat() in by_date
    ]
    maxv = max(present) if present else 0
    seg: list[list[Run]] = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        label = f"{_WEEKDAYS[day.weekday()]} {day.day:>2}  "
        row = by_date.get(day.isoformat())
        if row is None:
            seg.append([Run(label), Run(EM_DASH, "muted")])
            continue
        tokens = _tokens_of(row)
        seg.append([
            Run(label),
            Run(
                text_bar(int(tokens or 0), maxv, width=width, glyphs=glyphs)
                if tokens is not None
                else EM_DASH,
            ),
            Run(" "),
            Run(fmt_tokens(tokens) if tokens is not None else EM_DASH),
        ])
    return seg


# ---------------------------------------------------------------------------
# month view: Monday-first calendar heat grid
# ---------------------------------------------------------------------------

def month_grid(
    contribs: list[dict] | None, date_from: str, date_to: str,
    *, glyphs: str = "blocks",
) -> list[list[Run]]:
    """Calendar grid: a Mon..Sun header row, then one line per calendar week.

    Cells are ``day-of-month + heat_glyph(level)``; days outside the window
    render as blank cells (GitHub pads the partial edge weeks). Level comes
    from the server ``intensity`` when present; rows that lack it fall back
    to a local rank of the displayed tokens (``_intensity_fallback``) — an
    intensity of 0 is authoritative and never overridden.
    """
    by_date = _index_by_date(contribs)
    start, end = _parse_day(date_from), _parse_day(date_to)
    grid_start = start - timedelta(days=start.weekday())  # Monday on-or-before
    grid_end = end + timedelta(days=6 - end.weekday())    # Sunday on-or-after
    in_window = [
        by_date[(grid_start + timedelta(days=offset)).isoformat()]
        for offset in range((grid_end - grid_start).days + 1)
        if (grid_start + timedelta(days=offset)).isoformat() in by_date
        and grid_start + timedelta(days=offset) >= start
    ]
    fallback = _intensity_fallback(in_window)
    # Weekdays run HORIZONTALLY (calendar orientation): a header row of 4-char
    # weekday cells, then one row per week. (The year heatmap is the opposite
    # by design — GitHub puts weekdays in the row gutter and weeks in columns;
    # do not "unify" the two.)
    seg: list[list[Run]] = [[Run("".join(f"{w} " for w in _WEEKDAYS))]]
    for week in range((grid_end - grid_start).days // 7 + 1):
        cells = []
        for weekday in range(7):
            day = grid_start + timedelta(days=week * 7 + weekday)
            if day < start or day > end:
                cells.append("    ")
                continue
            row = by_date.get(day.isoformat())
            if row is None:
                level = 0
            elif row.get("intensity") is not None:
                level = int(row["intensity"])
            else:
                level = fallback[id(row)]
            cells.append(f"{day.day:>2}{heat_glyph(level, glyphs=glyphs)} ")
        seg.append([Run("".join(cells))])
    return seg


# ---------------------------------------------------------------------------
# year view: GitHub-style contribution heatmap
# ---------------------------------------------------------------------------

def heatmap_origin(year: int) -> date:
    """Column 0's Monday: the Monday on-or-before Jan 1 of ``year``.

    Exposed because the grid's whole geometry hangs off it — tests (and the
    app's width math) lock e.g. 2026 → Mon 2025-12-29 (Jan 1 2026 is a
    Thursday), which is exactly why a naive Jan-1-first grid is wrong.
    """
    jan1 = date(int(year), 1, 1)
    return jan1 - timedelta(days=jan1.weekday())


def year_heatmap(
    contribs: list[dict] | None, year: int, *, today: str, glyphs: str = "blocks"
) -> list[list[Run]]:
    """Monday-first 7-row × ≤53-column year grid, month header + weekday gutter.

    Cell intensity is the server's ``intensity`` rank — read, clamped inside
    heat_glyph, NEVER recomputed from tokens. Dates after ``today`` (an
    explicit ISO string; the caller owns the clock) and the off-year padding
    days render blank even if a stray contribution names them. An entirely
    empty year still renders the grid skeleton plus the note line
    ``no activity <year>`` — an empty heat map is a MEASURED zero, not a
    missing figure, and the skeleton is what says so.
    """
    by_date = _index_by_date(contribs)
    origin = heatmap_origin(year)
    cols = (date(int(year), 12, 31) - origin).days // 7 + 1
    today_day = _parse_day(today)

    header = [" "] * (cols * 2)
    last_month = None
    for col in range(cols):
        for row in range(7):
            day = origin + timedelta(days=col * 7 + row)
            if day.year == int(year):
                if day.month != last_month:
                    pos = col * 2
                    for offset, char in enumerate(_MONTHS[day.month - 1]):
                        if pos + offset < len(header):
                            header[pos + offset] = char
                    last_month = day.month
                break

    seg: list[list[Run]] = [[Run("    "), Run("".join(header))]]
    for weekday in range(7):
        cells = []
        for col in range(cols):
            day = origin + timedelta(days=col * 7 + weekday)
            row = by_date.get(day.isoformat())
            if (
                day.year != int(year)
                or day > today_day
                or row is None
                or row.get("intensity") is None
            ):
                cells.append("  ")
                continue
            cells.append(f"{heat_glyph(int(row['intensity']), glyphs=glyphs)} ")
        seg.append([Run(f"{_WEEKDAYS[weekday]:<3} "), Run("".join(cells))])
    if not by_date:
        seg.append([Run(f"no activity {int(year)}", "muted")])
    return seg


# ---------------------------------------------------------------------------
# Detail table: Tool + Model, sectioned by tool
# ---------------------------------------------------------------------------

def active_time_ms(active_by_tool: dict | None, key: Any) -> Any:
    """Active-ms for one tool id out of an active payload's ``by_tool``.

    Real payloads key by tool id, some fixtures key by a cased variant
    ("Claude" for "claude") — exact key first, then the case-folded
    fallback (report.py:active_row precedent). The fold is CASE-only: a
    full display label ("Claude Code") never bridges to the id and stays a
    miss — a dash, never a crash. One helper for every Time column (tools
    table, detail section rows).
    """
    by = active_by_tool or {}
    row = by.get(str(key)) or {str(k).lower(): v for k, v in by.items()}.get(
        str(key).lower()
    ) or {}
    return row.get("active_ms_sum")


def tool_model_detail(
    usage: dict,
    *,
    top_n: int | None = None,
    labels: dict | None = None,
    active: dict | None = None,
) -> list[tuple[str, str, str, str, str, str, str, str, str, str]]:
    """``(kind, name, in, out, cache, total, hit, cost, msgs, time)`` row
    tuples for a DataTable — section row per tool, then the tool's indented
    model rows. ``kind`` is "tool" (section header row), "model" or "more"
    (the overflow row, only with an explicit ``top_n``); the caller styles
    by kind, the data layer stays presentation-free.

    Round 4 widened every row to the tools-table field set (the user asked:
    "the model tool/model do not have all the fields i asked, only tool
    have"). Section rows take the SAME by_tool+apps two-source join and the
    active-time join as the tools table — apps first for Input/Output/Cache/
    Msgs, by_tool fallback, Time via ``active_time_ms`` (pass the active
    payload's ``by_tool`` as ``active``; absent → every Time dashes).
    Model rows read the model dict directly (compute.py gives models
    in/out/cache/total/hit/cost/messages) — and a model row's Time is ALWAYS
    EM_DASH: no per-model active time exists anywhere in the payloads
    (active intervals are per-tool only), and the round-3 override already
    prints measured-zero as a dash, so "no source" and "measured zero" land
    on the same cell by law.

    ``top_n=None`` (the default) lists EVERY model — the round-3 rule: lists
    never collapse (the "  ...+k more" overflow row only exists when a caller
    explicitly passes a cap). ``labels`` maps tool ids to display names
    (``sessions.TOOL_LABELS``); it is passed IN as data — charts must not
    import the backend (import-discipline test).

    Section order: tokens DESC, name ASC (locked by test). Tool figures come
    from ``by_tool`` (which includes the synthesized openclaw row); models
    from ``apps[tool]["models"]`` — openclaw is NOT in ``apps``
    (compute_usage strips it), so its section is synthesized from
    ``openclaw_models`` beside the by_tool row (no apps join for it — those
    cells dash). Models sort by COST desc on a
    COPY — the payload's token ranking is shared with other consumers and
    this function mutates nothing (deep-compare test).
    """
    usage = usage or {}
    apps = usage.get("apps") or {}
    by_tool = usage.get("by_tool") or {}
    seg: list[tuple] = []
    tools = sorted(
        by_tool.keys(),
        key=lambda key: (-int((by_tool[key] or {}).get("tokens") or 0), str(key)),
    )
    for key in tools:
        row = by_tool[key] or {}
        app_row = apps.get(key) or {}  # richer fields; ABSENT for openclaw
        ms = active_time_ms(active, key)
        seg.append((
            "tool",
            tool_label(key, labels),
            fmt_tokens(app_row.get("tokens_in", row.get("tokens_in"))),
            fmt_tokens(app_row.get("tokens_out")),
            fmt_tokens(app_row.get("tokens_cache", row.get("tokens_cache"))),
            fmt_tokens(row.get("tokens")),
            fmt_hit(row.get("cache_hit_rate")),
            fmt_cost_row(row.get("cost")),
            fmt_int(app_row.get("messages")),
            fmt_duration(ms) if ms else EM_DASH,
        ))
        models = list(usage.get("openclaw_models") or []) if key == "openclaw" \
            else list((apps.get(key) or {}).get("models") or [])
        ranked = sorted(models, key=lambda m: -float(m.get("cost") or 0.0))
        shown = ranked if top_n is None else ranked[: max(0, top_n)]
        for model in shown:
            seg.append((
                "model",
                f"  {model.get('name', '?')}",
                fmt_tokens(model.get("tokens_in")),
                fmt_tokens(model.get("tokens_out")),
                fmt_tokens(model.get("tokens_cache")),
                fmt_tokens(model.get("tokens")),
                fmt_hit(model.get("cache_hit_rate")),
                fmt_cost_row(model.get("cost")),
                fmt_int(model.get("messages")),
                EM_DASH,
            ))
        if top_n is not None and len(ranked) > max(0, top_n):
            seg.append(
                ("more", f"  ...+{len(ranked) - max(0, top_n)} more") + ("",) * 8
            )
    return seg


# ---------------------------------------------------------------------------
# Quota tab: state + 24h history folded into one table
# ---------------------------------------------------------------------------

# Falsy epochs (None / 0 / "") mean "never", and a garbage epoch is a DASH,
# never a crash: fromtimestamp raises OverflowError on absurd magnitudes on
# EVERY platform and OSError on epoch 0 under Windows — the same exception
# set _expiry_text already guards (house style for untrusted epochs; these
# values come straight from provider payloads).
def _fmt_epoch_hm(epoch: Any) -> str:
    if not epoch:
        return EM_DASH
    try:
        return datetime.fromtimestamp(int(epoch)).strftime("%H:%M")
    except (OverflowError, OSError, ValueError):
        return EM_DASH


def _fmt_epoch_full(epoch: Any) -> str:
    if not epoch:
        return EM_DASH
    try:
        return datetime.fromtimestamp(int(epoch)).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return EM_DASH


def _is_usage_bucket(bucket: dict) -> bool:
    # The web's isQuotaUsageBucket, ported: "api"/"reset_credits" are not
    # windows, and a bucket that measured nothing (no used_percent and not
    # unlimited) is noise — that filter is what keeps antigravity's per-model
    # rows and dead providers from flooding the tab.
    if str(bucket.get("bucket")) in {"api", "reset_credits"}:
        return False
    return bucket.get("unlimited") is True or bucket.get("used_percent") is not None


def _remaining_pct(bucket: dict) -> float | None:
    # Companion semantics: the bar and the percent describe what is LEFT
    # (web: bar fill = remaining, "62.3% left"), not the raw used_percent.
    if bucket.get("unlimited") is True:
        return 100.0
    remaining = bucket.get("remaining_percent")
    if remaining is None:
        used = bucket.get("used_percent")
        if used is None:
            return None
        remaining = 100.0 - float(used)
    return float(remaining)


def _antigravity_window(bucket: dict, now_ts: float) -> str:
    # Antigravity exposes no window-duration field: the web infers it from
    # the reset horizon — a 5-hour window can never reset more than 5h out;
    # the 8h margin absorbs skew. No reset time → "5-hour" (idle pool).
    resets = bucket.get("resets_at")
    if resets is None:
        return "5-hour"
    base = int(bucket.get("captured_at") or now_ts)
    return "Weekly" if int(resets) - base > 8 * 3600 else "5-hour"


def _window_label(provider: str, bucket: dict, now_ts: float) -> str:
    """Normalized window label — the web's quotaWindowLabel ported. Every tool
    shows "5-hour"/"Weekly" instead of its raw bucket_label ("5-hour window",
    "Session", "7-day window", per-model pool names)."""
    key = str(bucket.get("bucket") or "")
    if provider == "antigravity":
        return _antigravity_window(bucket, now_ts)
    if provider == "codex":
        if key == "5h" or key.endswith("_5h"):
            return "5-hour"
        if key == "7d" or key.endswith("_7d"):
            return "Weekly"
    elif provider == "claude":
        # The web's claudeBucketKind, ported: a `~/.claude-<profile>` install
        # prefixes its window ids with the profile name (`academic_session`).
        # After the prefix comes off, BOTH normalized kinds match EXACTLY —
        # substring matching here used to label the scoped
        # ``weekly_scoped_fable`` window as the plain "Weekly" pool; the
        # Fable weekly is its own meter and keeps its own name.
        kind = key
        account = str(bucket.get("account") or "")
        if account and kind.startswith(f"{account}_"):
            kind = kind[len(account) + 1:]
        kind = kind.lower()
        if kind == "session":
            return "5-hour"
        if kind == "weekly_all":
            return "Weekly"
        if "fable" in kind:
            return "Fable"  # real payloads carry bucket_label "Fable"
    elif provider in ("kimi", "grok"):
        if key == "5h":
            return "5-hour"
        if key == "7d" or (provider == "kimi" and key == "plan"):
            return "Weekly"
    elif provider == "minimax":
        account = str(bucket.get("account") or "")
        if account and key == f"{account}_general_5h":
            return "5-hour"
        if account and key == f"{account}_general_7d":
            return "Weekly"
    label = str(bucket.get("bucket_label") or key or EM_DASH)
    # Region suffixes belong to the provider cell, not the window row
    # (same trim the web card applies to MiniMax labels).
    for suffix in (" (Mainland China)", " (Global)"):
        if label.endswith(suffix):
            label = label[: -len(suffix)]
    return label


def _reset_cell(bucket: dict, now_ts: float) -> str:
    """Companion reset line: relative countdown plus the local absolute time
    ("in 3h 12m · 14:32"); em-dash when the window never resets."""
    resets = bucket.get("resets_at")
    if not resets:  # None / 0 / "" — epoch 0 is a "never" sentinel, not 1970
        return EM_DASH
    absolute = _fmt_epoch_hm(resets)
    secs = int(resets) - int(now_ts)
    if secs <= 0:
        return f"reset · {absolute}"
    if secs >= 86_400:
        rel = f"{secs // 86_400}d {secs % 86_400 // 3600}h"
    elif secs >= 3_600:
        rel = f"{secs // 3_600}h {secs % 3_600 // 60}m"
    else:
        rel = f"{max(1, secs // 60)}m"
    return f"in {rel} · {absolute}"


def _usage_rows(provider: str, buckets: list[dict], now_ts: float) -> list[dict]:
    """Filter to real usage windows and pick ONE row per (account, label).

    Antigravity reports one quotaInfo per model against a SHARED pool — the
    ten raw rows are ten readings of at most two windows, and the one that
    binds is the one with the most used. Other providers keep their distinct
    bucket keys (codex "5h" and "spark_5h" share a label but are different
    meters) and are only deduped by (account, key), which is a no-op unless
    duplicate snapshots made it through.
    """
    kept: dict[tuple[str, str], dict] = {}
    for bucket in buckets:
        if not _is_usage_bucket(bucket):
            continue
        account = str(bucket.get("account") or "default")
        if provider == "antigravity":
            group = (account, _window_label(provider, bucket, now_ts))
        else:
            group = (account, str(bucket.get("bucket") or ""))
        best = kept.get(group)
        if best is None or (bucket.get("used_percent") or 0.0) > \
                (best.get("used_percent") or 0.0):
            kept[group] = bucket
    return list(kept.values())


def _expiry_text(value: Any) -> str:
    # The web's formatEpochSeconds, ported: numeric values are epoch seconds,
    # other strings are ISO-8601 (real payloads: "2026-10-22T20:48:12.396262Z").
    # Both render in LOCAL time — the same convention as the reset countdown.
    if value is None:
        return EM_DASH
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        num: float | None = float(value)
    elif str(value).strip().replace(".", "", 1).isdigit():
        num = float(str(value).strip())
    else:
        num = None
    try:
        if num is not None:
            dt = datetime.fromtimestamp(num)
        else:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone()
    except (OverflowError, OSError, ValueError):
        return EM_DASH
    return dt.strftime("%Y-%m-%d %H:%M")


def _credit_sources(
    providers: dict, labels: dict | None
) -> list[tuple[str, dict]]:
    """``(provider-label, reset_credits block)`` pairs across ALL providers.

    Round 4: the section is not Codex's any more — Claude Code limit resets
    (PR #109) land in the SAME ``reset_credits`` key, and a row must say
    which provider (and install) it belongs to ("currently do not say
    codex, i.e., which is which"). Collection rule:

    - A multi-account card answers per install via ``accounts[]``: each
      install's grants ride on its own entry (absent-not-null), so a card
      with ``accounts[]`` takes ONLY those rows — the provider-level block
      is the PRIMARY install's same block, and counting both would print
      it twice. Its entry carries the account name ("Claude Code@academic"
      sits beside "Claude Code@default").
    - Otherwise (single-account card — Claude's accounts[] appears only when
      more than one install exists) the provider-level block, plain label.
    - ``labels`` (TOOL_LABELS as data — charts never imports the backend)
      turns ids into display names; an unknown id keeps its raw name.
    """
    out: list[tuple[str, dict]] = []
    for provider in sorted(providers):
        card = providers[provider] or {}
        label = tool_label(str(provider), labels)
        accounts = card.get("accounts")
        if isinstance(accounts, list) and accounts:
            for acc in accounts:
                if not isinstance(acc, dict):
                    continue
                block = acc.get("reset_credits")
                if isinstance(block, dict) and block:
                    name = str(acc.get("account") or "default")
                    out.append((f"{label}@{name}", block))
            continue
        block = card.get("reset_credits")
        if isinstance(block, dict) and block:
            out.append((label, block))
    return out


def _expiry_sort_key(value: Any) -> tuple[int, float, str]:
    """Expiry order across the payload's duality: Claude Code rows carry
    EPOCH-SECONDS ints, Codex rows ISO strings, and either can be null.
    Parsed instants sort first (ascending, nearest expiry on top); null or
    unparseable sort last, stable by original text."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (0, float(value), "")
    s = str(value).strip() if value is not None else ""
    if s.replace(".", "", 1).isdigit():
        return (0, float(s), "")
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return (0, dt.timestamp(), "")
    except (ValueError, OverflowError, OSError):
        return (1, 0.0, s)


def _credit_section(sources: list[tuple[str, dict]]) -> list[list[Run]]:
    """The reset-credits bottom section, web-card parity plus round-4
    provider labels: a header line ("reset credits · N available", N summed
    across every source) then up to five credit rows TOTAL across sources,
    sorted by expiry ascending (epoch ints and ISO strings order together),
    each prefixed with its provider (and install) label. Never a row in the
    usage table."""
    count = 0
    items: list[tuple[Any, str, str]] = []  # (expiry, label, name)
    for label, credits in sources:
        c = credits.get("available_count")
        if isinstance(c, (int, float)) and not isinstance(c, bool):
            count += int(c)
        for credit in credits.get("credits") or []:
            if isinstance(credit, dict):
                items.append((
                    credit.get("expires_at"),
                    label,
                    str(credit.get("title") or credit.get("id") or "credit"),
                ))
    items.sort(key=lambda it: _expiry_sort_key(it[0]))
    lines: list[list[Run]] = [[
        Run("reset credits ", "bold"),
        Run(f"{count} available", "ok" if count > 0 else "muted"),
    ]]
    for expiry, label, name in items[:5]:  # the web slices 0..5 too
        lines.append([
            Run(f"{label} · {name}"),
            Run(f" · expires {_expiry_text(expiry)}", "muted"),
        ])
    return lines


def quota_table(
    state: dict,
    history: dict | None,
    *,
    now_ts: float,
    glyphs: str = "blocks",
    labels: dict | None = None,
) -> tuple[list[list[Run]], list[list[str]], str, list[list[Run]]]:
    """(banner segments, DataTable string rows, note, credits section) for the
    Quota tab.

    Rows mirror the web companion card: Provider | Window (normalized
    "5-hour"/"Weekly") | remaining bar + "62.3% left" (the bar and the
    percent show what is LEFT, colored by urgency web-side) | resets
    ("in 3h 12m · 14:32") | 24h trend (day_spark over the matching series'
    points, em-dash without data) | status.

    - Only REAL usage windows render (``_usage_buckets``) — antigravity's
      per-model pool readings collapse to its binding window per label.
    - Reset credits, when present on ANY provider (Codex, Claude Code
      limit resets), return as a SEPARATE bottom section (the 4th tuple
      item: header + ≤5 rows with expiry dates, provider-labeled, sorted by
      expiry), never as a row in the usage table. ``labels`` maps provider
      ids to display names for those rows (passed IN as data).
    - Multi-account providers fold the account into the Provider cell
      (``claude@work``) only when the provider has >1 distinct account — no
      dynamic columns (locked decision).
    - ROWS ARE GROUPED BY PROVIDER (round 5): the provider names its group
      on the first row; continuation rows are blank in the Provider cell
      (or ``@account`` where the account differs from the PRECEDING row, so
      an interleaved account never steals the group above it), and an
      all-empty spacer row separates groups. ``rows`` is therefore display
      data — group membership is read by walking r[0] (a group starts where
      r[0] is non-empty and does not start with "@").
    - remaining None (not unlimited, never measured) is filtered out; a
      measured 0% left prints "0.0% left", never a dash.
    - A detected provider whose status is "unavailable" and which has no
      usable windows gets one status-only row (the card the web dims);
      providers never detected and never measured are omitted entirely.
    - Trend series match by provider+bucket (+account when the history row
      carries one), points outside the trailing 24 h window ending at
      ``now_ts`` are dropped.
    """
    providers = (state or {}).get("providers") or {}
    poll = (state or {}).get("poll") or {}
    consent = (state or {}).get("consent") or {}
    series = (history or {}).get("series") or []

    any_points = any(s.get("points") for s in series)

    banner: list[list[Run]] = [[
        Run("quota ", "bold"),
        Run("enabled" if (state or {}).get("enabled") else "disabled",
            "ok" if (state or {}).get("enabled") else "warn"),
        Run(f" · poll every {poll.get('interval_minutes', '?')} min · last run "),
        Run(_fmt_epoch_full(poll.get("last_run"))),
    ]]

    rows: list[list[str]] = []
    # Credits are a property of the CARDS, not of a rendered window row —
    # collected whole (all providers) before the per-provider loop, so the
    # no-windows early-continue can't drop a card's grants. Round 4:
    # provider-labeled rows from every card that carries the block (was a
    # Codex-only gate; Claude Code limit resets share the same key).
    sources = _credit_sources(providers, labels)
    # No card carries the block at all -> NO section (the "0 available"
    # header belongs to a card that granted none, never to a universe
    # without credits — the pane hides an empty list).
    credits_section = _credit_section(sources) if sources else []

    # Round 5 provider grouping (user: "same provider only show one
    # provider, and give some margin between providers"): a provider writes
    # its name on its group's FIRST row only — continuation rows carry ""
    # (or the bare "@account" when the card folds accounts, so the account
    # still travels where it differs from the group's opener) — and an
    # all-empty SPACER row separates provider groups. A blank row is the
    # only "margin" a DataTable can render between rows; the caller paints
    # rows verbatim.
    def _gap() -> None:
        if rows:  # never before the FIRST group — no leading blank row
            rows.append(["", "", "", "", "", ""])

    for provider in sorted(providers.keys()):
        card = providers[provider] or {}
        buckets = _usage_rows(str(provider), list(card.get("buckets") or []), now_ts)
        if not buckets:
            if card.get("status") == "unavailable" and card.get("detected"):
                _gap()
                rows.append([str(provider), EM_DASH, EM_DASH, EM_DASH, EM_DASH,
                             "unavailable"])
            # else: no windows but the card says something (e.g. "local_plan")
            # — nothing tabular to show; render no row.
            continue
        accounts = {str(b.get("account") or "default") for b in buckets}
        fold = len(accounts) > 1
        # Account of the PRECEDING row (not the group opener): the first row
        # names the group; a continuation blanks the Provider cell, and only
        # re-labels "@acct" when the account CHANGES — so interleaved
        # accounts each re-assert their own label instead of reading as the
        # group above them.
        prev_acct: str | None = None
        for bucket in buckets:
            remaining = _remaining_pct(bucket)
            if bucket.get("unlimited") is True:
                used_cell = text_bar(100.0, 100.0, width=_QUOTA_BAR_WIDTH,
                                     glyphs=glyphs) + " unlimited"
            elif remaining is None:
                used_cell = EM_DASH
            else:
                used_cell = (
                    text_bar(remaining, 100.0, width=_QUOTA_BAR_WIDTH,
                             glyphs=glyphs)
                    + f" {remaining:.1f}% left"
                )
            window_cell = _window_label(str(provider), bucket, now_ts)
            if not str(bucket.get("source") or "").endswith("_api"):
                window_cell += " · local"  # web's "local logs" tag: may lag
            trend = EM_DASH
            for s in series:
                if str(s.get("provider")) != str(provider):
                    continue
                if str(s.get("bucket")) != str(bucket.get("bucket")):
                    continue
                if s.get("account") is not None and len(accounts) > 1 and \
                        str(s.get("account")) != str(bucket.get("account") or "default"):
                    continue
                points = [
                    p for p in (s.get("points") or [])
                    if now_ts - 86_400 <= float(p.get("captured_at") or 0) <= now_ts
                ]
                if points:
                    trend = day_spark(
                        [float(p.get("used_percent") or 0.0) for p in points],
                        glyphs=glyphs,
                    )
                    break
            acct = str(bucket.get("account") or "default")
            if prev_acct is None:
                _gap()
                provider_cell = f"{provider}@{acct}" if fold else str(provider)
            elif fold and acct != prev_acct:
                provider_cell = f"@{acct}"
            else:
                provider_cell = ""
            prev_acct = acct
            rows.append([
                provider_cell,
                window_cell,
                used_cell,
                _reset_cell(bucket, now_ts),
                trend,
                str(card.get("status") or "unavailable"),
            ])
    notes: list[str] = []
    if not (state or {}).get("enabled"):
        notes.append("quota tracking is off — enable it with `tokdash quota consent --enabled on`")
    elif consent and not any(consent.values()):
        notes.append("network quota polling is not consented — grant it with `tokdash quota consent --<provider>-api on`")
    if not any_points:
        notes.append("no 24h quota history yet — press u to poll once")
    return banner, rows, " · ".join(notes), credits_section
