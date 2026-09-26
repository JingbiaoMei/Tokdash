"""Shared report renderer + the one-shot ``tokdash report`` command (spec §5).

The anti-drift seam: the pipeable ``tokdash report`` bytes and the TUI Report tab
are both built by ``build_report_segments`` from the same three payloads, so the
two surfaces structurally cannot disagree — the segment list IS the view model.

Hard boundary: NO textual and NO rich import here (the import-discipline test
proves the whole report path imports on a stripped install that lacks both), so
``emit_markup``'s bracket escape is hand-rolled. ``run_report`` imports
``cli._emit_json`` lazily inside the ``--json`` branch: cli.py imports api.py at
module top and api.py imports this lane nowhere, but the lazy import keeps the
text path paying nothing and mirrors the lazy-dispatch precedent
(``from .onboard.engine import run_lifecycle``).

Failure policy (spec §7): usage is the hard-fail source (SystemExit, exit 1);
insights and active-time are SOFT — a failed scan dashes its sections and adds a
``[warn]`` line inside the body, and the exit code stays 0 (dashes + warnings,
not a dead report). ``UsageDatabaseSchemaTooNewError`` is terminal by design and
re-raised from every soft path.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..api import REPORT_FACETS, CacheBackpressureError
from ..usage_store import UsageDatabaseSchemaTooNewError
from .data import (
    db_summary,
    ensure_usage_db_compatible,
    fetch_active_time,
    fetch_insights,
    fetch_usage,
    resolve_report_period,
)
from .formatting import (
    EM_DASH,
    Run,
    _accent,
    _bad,
    _bold,
    _color_enabled,
    _ok,
    _warn,
    day_spark,
    fmt_cost_headline,
    fmt_cost_row,
    fmt_delta,
    fmt_duration,
    fmt_hit,
    fmt_int,
    fmt_tokens,
    glyph_style,
    text_bar,
    tool_label,
    window_line,
)
# The canonical tool-id → display-name map (claude → "Claude Code"). Law-clean
# here: the import-discipline test blocks textual/rich only, and the report
# path already pulls sessions in via ..api.
from ..sessions import TOOL_LABELS

# Spec §5: name truncation caps at 22 chars on a full-width report; a narrow
# terminal gets a proportionally tighter cap so a project path cannot push the
# bars off the line.
_NAME_CAP = 22

# The Daily section never runs longer than a month window, whatever the
# window["days"] claim says (defense-in-depth against a malformed range block).
_DAILY_MAX_ROWS = 31

_ANSI_STYLES = {"ok": _ok, "bad": _bad, "warn": _warn, "bold": _bold,
                "accent": _accent, "header": _bold}
# "muted"/None render plain in ANSI (the engine has no dim code); markup has one.
# "header" is the section-header fill: reverse-video on the TUI panes (the
# engine has no reverse helper, so the piped report just keeps it bold).
_MARKUP_STYLES = {"ok": "green", "bad": "red", "warn": "yellow", "bold": "bold",
                  "accent": "cyan", "muted": "dim", "header": "reverse bold"}


def _name_cap(width: int) -> int:
    return _NAME_CAP if width >= 70 else max(8, width - 58)


def _trunc(name: Any, width: int) -> str:
    text = str(name)
    cap = _name_cap(width)
    return text if len(text) <= cap else text[: cap - 1] + "…"


def _rule_line(width: int, glyphs: str) -> Run:
    """Faint section rule: "─" in blocks mode, "-" in ascii (cp1252-safe),
    always "muted" — it draws structure, never data."""
    return Run(("─" if glyphs == "blocks" else "-") * width, "muted")


def _daily_section(
    daily: list[dict], window: dict, *, width: int, glyphs: str
) -> list[list[Run]]:
    """Per-day bar rows for a ≤31-day window (spec §5 Daily section).

    The "daily" facet carries ACTIVE days only, so the calendar is enumerated
    client-side from window["from"]/["to"]: a day with no facet row is a real
    measured nothing and renders dash columns, never a silently dropped row.
    Rows are hard-capped at _DAILY_MAX_ROWS. A window whose dates cannot be
    parsed renders nothing — a broken "range" block must not take the report
    down. Bars scale to the facet max (missing days never dilute the scale).
    """
    try:
        start = datetime.strptime(str(window.get("from")), "%Y-%m-%d").date()
        end = datetime.strptime(str(window.get("to")), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return []
    by_date = {str(row.get("date")): row for row in daily}
    maxv = max((int(row.get("tokens") or 0) for row in daily), default=0)
    bar_w = 20 if width >= 70 else 12  # spec §5 bar-width rule
    seg: list[list[Run]] = [[Run("Daily", "bold")], [_rule_line(width, glyphs)]]
    day, rows = start, 0
    while day <= end and rows < _DAILY_MAX_ROWS:
        row = by_date.get(day.strftime("%Y-%m-%d"))
        if row is None:
            bar, tokens, cost = " " * bar_w, EM_DASH, EM_DASH
        else:
            tokens_i = int(row.get("tokens") or 0)
            bar = text_bar(tokens_i, maxv, width=bar_w, glyphs=glyphs).ljust(bar_w)
            tokens, cost = fmt_tokens(tokens_i), fmt_cost_row(row.get("cost"))
        seg.append(
            [Run(f"{day:%m-%d}  {bar} "), Run(f"{tokens:>7}"), Run(" "), Run(cost)]
        )
        day += timedelta(days=1)
        rows += 1
    return seg


def build_report_segments(
    usage: dict,
    insights: dict | None,
    active_time: dict | None,
    *,
    window: dict,
    width: int = 80,
    glyphs: str = "blocks",
    surface: str = "report",
) -> list[list[Run]]:
    """One entry per output line; each line is a list[Run].

    ``insights`` or ``active_time`` may be ``None`` (soft-failed fetch) — those
    sections render EM_DASH placeholders and a warning line, never an exception.
    ``window`` is {"period": label, "from", "to", "days", "timezone"} built from
    the usage payload's "range" block; two optional keys extend that shape for
    this renderer only: "recognized" (default True, feeds window_line's warning
    branch) and "warnings" (soft-fail reasons with their exception text — the
    builder is the one function both surfaces render through, so the warn lines
    live in the body, not beside it).

    Null-vs-zero law (§4): every figure below goes through a fmt_* helper, which
    owns the None→EM_DASH / measured-0→"0" split. Rows are read, never mutated —
    compute payloads share nested objects (top_models IS a combined_models slice,
    compute.py:989) and the non-mutation test locks that.
    """
    seg: list[list[Run]] = []

    def line(*runs: Run) -> None:
        seg.append(list(runs))

    def plain(text: Any) -> Run:
        return Run(str(text))

    rng = {
        "from": window.get("from") or EM_DASH,
        "to": window.get("to") or EM_DASH,
        "days": window.get("days") if window.get("days") is not None else EM_DASH,
        "recognized": bool(window.get("recognized", True)),
    }

    # --- header ---------------------------------------------------------------
    header = f"Tokdash report · {window.get('period') or EM_DASH} · {window_line(rng)}"
    timezone = window.get("timezone")
    if timezone:
        header += f" · {timezone}"
    line(Run(header, "header"))

    # --- hero (usage scan + active-time clock) --------------------------------
    comparison = usage.get("comparison") or {}
    at = active_time or {}
    at_cmp = at.get("comparison") or {}

    def hero(label: str, value: str, pct: float | None) -> None:
        line(Run(f"{label:<7}"), plain(value), Run("  "), fmt_delta(pct, surface=surface))

    hero("Tokens", fmt_tokens(usage.get("total_tokens")), comparison.get("tokens_pct"))
    hero("Cost", fmt_cost_headline(usage.get("total_cost")), comparison.get("cost_pct"))
    hero("Msgs", fmt_int(usage.get("total_messages")), comparison.get("messages_pct"))
    # active_ms is the cross-tool clock UNION, active_ms_sum the additive agent
    # time — two different facts, so the line labels both (spec §5 field note).
    line(
        Run("Agent  "),
        plain(fmt_duration(at.get("active_ms"))),
        Run(" clock · "),
        plain(fmt_duration(at.get("active_ms_sum"))),
        Run(" sum  "),
        fmt_delta(at_cmp.get("active_ms_sum_pct"), surface=surface),
    )

    streaks = (insights or {}).get("streaks") or {}
    firsts = (insights or {}).get("firsts") or {}
    line(
        Run("Streak "),
        plain(fmt_int(streaks.get("current_streak"))),
        Run(" days · longest "),
        plain(fmt_int(streaks.get("longest_streak"))),
        Run(" · active "),
        plain(fmt_int(streaks.get("active_days"))),
        Run(" of "),
        plain(fmt_int(streaks.get("total_days"))),
        Run(" · busiest "),
        plain(firsts.get("busiest_day") or EM_DASH),
        Run(" ("),
        plain(fmt_tokens(firsts.get("busiest_day_tokens"))),
        Run(")"),
    )

    daily = (insights or {}).get("daily") or []
    spark = day_spark([d.get("tokens", 0) for d in daily], glyphs=glyphs)
    if daily:
        dates = f"{daily[0].get('date') or EM_DASH} → {daily[-1].get('date') or EM_DASH}"
    else:
        dates = f"{EM_DASH} → {EM_DASH}"
    line(Run("Daily  "), plain(spark), Run("  "), plain(dates))

    # --- when (hourly + weekday facets) ----------------------------------------
    hourly = (insights or {}).get("hourly") or {}
    weekday = (insights or {}).get("weekday") or {}
    peak_hour = hourly.get("peak_hour")
    line(
        Run("When   peak hour "),
        plain(f"{peak_hour:02d}:00" if peak_hour is not None else EM_DASH),
        Run(" · night share "),
        # fmt_hit(None) is "n/a" per the law — no prompt input is not a 0% night.
        plain(fmt_hit(hourly.get("night_share"))),
    )
    bar_w = 20 if width >= 70 else 12  # spec §5 bar-width rule
    buckets = sorted(
        (b for b in (hourly.get("buckets") or []) if (b.get("tokens") or 0) > 0),
        key=lambda b: -int(b["tokens"]),
    )[:3]
    if buckets:
        # Sorted desc, so buckets[0] is also the global max the bars scale to.
        maxv = int(buckets[0]["tokens"])
        for bucket in buckets:
            line(
                Run(f"       {int(bucket['hour']):02d}:00 "),
                plain(text_bar(int(bucket["tokens"]), maxv, width=bar_w, glyphs=glyphs)),
                Run(" "),
                plain(fmt_tokens(bucket["tokens"])),
            )
    else:
        line(Run("       "), plain(EM_DASH))
    peak_weekday = weekday.get("peak_weekday")
    peak_bucket = next(
        (b for b in (weekday.get("buckets") or []) if b.get("weekday") == peak_weekday),
        None,
    )
    line(
        Run("       busiest weekday "),
        plain(peak_bucket.get("name") if peak_bucket else EM_DASH),
        Run(" ("),
        plain(fmt_tokens(peak_bucket.get("tokens")) if peak_bucket else EM_DASH),
        Run(")"),
    )

    # --- daily bars (week / month windows only) ---------------------------------
    # Year and >31-day windows skip: the year's calendar lives on the Overview
    # tab, and hundreds of rows would bury a report meant to be read in one
    # screen. An empty facet omits the section; insights=None never gets here.
    days = window.get("days")
    if insights is not None and daily and isinstance(days, int) and 0 < days <= 31:
        seg.extend(_daily_section(daily, window, width=width, glyphs=glyphs))

    # --- joins onto the active-time by_tool table ------------------------------
    by_tool = at.get("by_tool") or {}

    def active_row(tool: Any) -> dict | None:
        # Exact key first (the real payloads key by_tool by tool id); the
        # case-folded fallback covers payloads keyed by display label — a join
        # miss then stays a join miss (EM_DASH per the law), not a crash.
        row = by_tool.get(str(tool))
        if row is None:
            row = {str(k).lower(): v for k, v in by_tool.items()}.get(str(tool).lower())
        return row

    tools_ranked = ((insights or {}).get("tools") or {}).get("ranked") or []
    models_ranked = ((insights or {}).get("models") or {}).get("ranked") or []
    projects = (insights or {}).get("projects") or {}

    # --- podium -----------------------------------------------------------------
    if tools_ranked:
        top = tools_ranked[0]
        act = active_row(top.get("tool")) or {}
        line(
            Run("Podium Top agent    "),
            plain(_trunc(tool_label(top.get("tool"), TOOL_LABELS), width)),
            Run(" · "),
            plain(fmt_tokens(top.get("tokens"))),
            Run(" · "),
            plain(fmt_cost_row(top.get("cost"))),
            Run(" · "),
            plain(fmt_int(act.get("session_count"))),  # join miss → —
            Run(" sessions · "),
            plain(fmt_duration(act.get("active_ms_sum"))),
        )
    else:
        line(Run("Podium Top agent    "), plain(EM_DASH))
    if models_ranked:
        top = models_ranked[0]
        line(
            Run("       Top model    "),
            plain(_trunc(top.get("model"), width)),
            Run(" · "),
            plain(fmt_tokens(top.get("tokens"))),
            Run(" · "),
            plain(fmt_cost_row(top.get("cost"))),
        )
    else:
        line(Run("       Top model    "), plain(EM_DASH))
    reason = projects.get("unavailable_reason")
    project_rows = projects.get("projects") or []
    if reason:
        # e.g. the projects facet without the persistent DB — say why instead of
        # silently rendering the podium as if there were no projects.
        line(Run("       "), Run(f"projects: {reason}", "warn"))
    elif project_rows:
        top = project_rows[0]
        line(
            Run("       Top project  "),
            plain(_trunc(top.get("project"), width)),
            Run(" · "),
            plain(fmt_tokens(top.get("tokens"))),
            Run(" · "),
            plain(fmt_cost_row(top.get("cost"))),
        )
    else:
        line(Run("       Top project  "), plain(EM_DASH))

    # --- agents table -------------------------------------------------------------
    # usageReportBuildModel rule (index.html:14659-14684): tools with tokens>0,
    # joined onto active time; when active time ANSWERED, rows with no session
    # count move aside rather than headlining a table of em dashes — when it
    # didn't, the ranking stands on the token columns it still has.
    agent_rows = []
    for row in tools_ranked:
        if (row.get("tokens") or 0) <= 0:
            continue
        act = active_row(row.get("tool"))
        agent_rows.append(
            {
                # display name for the table (the join above used the raw id)
                "tool": tool_label(row.get("tool"), TOOL_LABELS),
                "tokens": row.get("tokens"),
                "cost": row.get("cost"),
                "sessions": act.get("session_count") if act else None,
                "active_ms_sum": act.get("active_ms_sum") if act else None,
            }
        )
    if active_time is not None:
        agent_rows = [r for r in agent_rows if r["sessions"] is not None]
    agent_rows = agent_rows[:10]
    line(_rule_line(width, glyphs))
    # Numeric columns are right-aligned to fixed widths so digits line up the
    # way the web table lines them up; the name column pads to the same cap
    # _trunc enforces, so a 22-char name can no longer shove the numbers
    # off-grid (the old 13-char pad broke on any long tool label).
    name_w = _name_cap(width)
    line(
        Run(
            "       "
            + "Agent".ljust(name_w)
            + "Sessions".rjust(10)
            + "Tokens".rjust(9)
            + "Cost".rjust(10)
            + " Active",
            "muted",
        )
    )
    if agent_rows:
        for r in agent_rows:
            line(
                plain(
                    f"       {_trunc(r['tool'], width):<{name_w}}"
                    f"{fmt_int(r['sessions']):>10}"
                    f"{fmt_tokens(r['tokens']):>9}"
                    f"{fmt_cost_row(r['cost']):>10}"
                    f" {fmt_duration(r['active_ms_sum'])}"
                )
            )
    else:
        line(Run("       "), plain(EM_DASH))

    # --- note (per-figure source labels, spec §5) ------------------------------
    line(Run("Note   Headline totals are the usage scan; facet/agent figures are the analytics"))
    line(Run("       scan (tools scope only) and may differ."))
    line(_rule_line(width, glyphs))

    # --- warnings -----------------------------------------------------------------
    warns = [str(w) for w in (window.get("warnings") or [])]
    # window["pending"] marks a TUI load still in flight: there a None source
    # means "still running", NOT "scan failed" — the spec reserves the failure
    # line for real soft-failures (the one-shot path, which builds once, after
    # the scans actually failed, and never sets pending). Running sources get a
    # dim note instead, so the body never hides a gap either.
    pending = bool(window.get("pending"))
    pairs = [(w, "warn") for w in warns]
    if insights is None and not any("analytics scan failed" in w for w in warns):
        pairs.append(("analytics scan running…" if pending
                      else "analytics scan failed",
                      "muted" if pending else "warn"))
    if active_time is None and not any("active time scan failed" in w for w in warns):
        pairs.append(("active time scan running…" if pending
                      else "active time scan failed",
                      "muted" if pending else "warn"))
    for text, style in pairs:
        line(Run(f"[warn] {text}", style))

    # Source errors are ALWAYS surfaced: a window that half-failed to read must
    # not read as a quiet zero. Entries are strings or dicts with "source"
    # (compute.py:825 — same defensive read as index.html:14687).
    names = []
    for entry in usage.get("source_errors") or []:
        source = entry.get("source") if isinstance(entry, dict) else entry
        if source:
            names.append(str(source))
    if names:
        line(Run(f"[warn] {len(names)} source(s) failed this window: {', '.join(names)}", "warn"))

    # --- db footer ----------------------------------------------------------------
    # The footer is decoration; a store fault (or TOKDASH_USAGE_DB=0 surprises)
    # must not kill an otherwise good report body. The TUI passes its cached
    # line via window["db_line"] — the Report pane repaints up to ~4× per load
    # and the footer must not re-open sqlite on the UI event loop each time
    # (the one-shot path owns its own db_summary call and passes nothing).
    db_line = window.get("db_line")
    if db_line is None:
        try:
            db_line = db_summary()
        except Exception as exc:  # noqa: BLE001 — footer must never take the report down
            db_line = f"db status unavailable: {exc}"
    line(Run("db     "), plain(db_line))

    return seg


def emit_plain(segments: list[list[Run]]) -> str:
    """Styles stripped, no ANSI — the contract for piped stdout, --output, tests."""
    return "\n".join("".join(run.text for run in seg) for seg in segments)


def emit_ansi(segments: list[list[Run]]) -> str:
    """Applies the engine color family; "muted"/None render plain. The engine
    helpers already no-op when _color_enabled() is false, so an accidental call
    on a dumb terminal degrades to plain text (double gate with run_report)."""
    out = []
    for seg in segments:
        parts = []
        for run in seg:
            style = _ANSI_STYLES.get(run.style or "")
            parts.append(style(run.text) if style else run.text)
        out.append("".join(parts))
    return "\n".join(out)


def emit_markup(segments: list[list[Run]]) -> str:
    """Textual/rich markup for the TUI Report tab. report.py must not import
    rich/textual, so the literal-bracket escape is hand-rolled: "[warn]" is data,
    never a markup tag."""
    out = []
    for seg in segments:
        parts = []
        for run in seg:
            text = run.text.replace("[", "\\[")
            color = _MARKUP_STYLES.get(run.style or "")
            parts.append(f"[{color}]{text}[/]" if color else text)
        out.append("".join(parts))
    return "\n".join(out)


def run_report(args: argparse.Namespace) -> int:
    """``tokdash report``: one window, one serial pass, ccusage-style text.

    Serial on purpose — a one-shot process never contends with its own semaphore,
    and the retry helper in data.py covers contention with a running server. The
    fetch order mirrors the web's loadUsageReport(): usage paints the hero first.
    """
    try:
        period_used, date_from, date_to = resolve_report_period(args.period)
    except ValueError as exc:
        # Normally the cli.py parse-time guard already exited 2 (§2.2).
        raise SystemExit(str(exc))
    calendar = date_from is not None  # calendar window vs rolling token
    try:
        ensure_usage_db_compatible()
    except UsageDatabaseSchemaTooNewError as exc:
        raise SystemExit(f"{exc} — run 'tokdash update'")

    try:
        usage = fetch_usage(period_used, date_from, date_to).value
    except UsageDatabaseSchemaTooNewError as exc:
        raise SystemExit(f"{exc} — run 'tokdash update'")
    except CacheBackpressureError:
        # data.py already retried 3 attempts; a contended cold miss is "later".
        raise SystemExit("Tokdash is busy computing — try again shortly")
    except Exception as exc:  # noqa: BLE001 — the one hard-fail source of the report
        raise SystemExit(str(exc))

    # SOFT sources: dash their sections, warn inside the body, exit stays 0.
    warnings: list[str] = []
    insights: dict | None
    try:
        if calendar:
            # Report windows prime the warmer's year-period insights key.
            insights = fetch_insights("year", date_from, date_to, REPORT_FACETS, True).value
        else:
            insights = fetch_insights(period_used, None, None, REPORT_FACETS, True).value
    except UsageDatabaseSchemaTooNewError as exc:
        raise SystemExit(f"{exc} — run 'tokdash update'")
    except Exception as exc:  # noqa: BLE001 — soft by contract
        insights = None
        warnings.append(f"analytics scan failed: {exc}")
    active_time: dict | None
    try:
        # Report pins include_review_sessions=True — the web and the warmer key
        # (api.py:504-508); Overview's None-default is a different key, never mix.
        active_time = fetch_active_time(period_used, date_from, date_to, True).value
    except UsageDatabaseSchemaTooNewError as exc:
        raise SystemExit(f"{exc} — run 'tokdash update'")
    except Exception as exc:  # noqa: BLE001 — soft by contract
        active_time = None
        warnings.append(f"active time scan failed: {exc}")

    rng = usage.get("range") or {}

    if args.json:
        from ..cli import _emit_json  # lazy: the text path never pays for the CLI module

        payload = {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "window": {
                "period": args.period,
                "mode": "calendar" if calendar else "rolling",
                "from": rng.get("from"),
                "to": rng.get("to"),
                "days": rng.get("days"),
                # Echo the payload's own range block, never the request token
                # (the v1 guard makes unrecognized unreachable; defense-in-depth).
                "recognized": bool(rng.get("recognized", True)),
                "timezone": insights.get("timezone") if insights else None,
            },
            "usage": usage,
            "insights": insights,
            "active_time": active_time,
        }
        _emit_json(payload, args.pretty, args.output)
        return 0

    window = {
        "period": args.period,
        "from": rng.get("from"),
        "to": rng.get("to"),
        "days": rng.get("days"),
        "recognized": bool(rng.get("recognized", True)),
        "timezone": insights.get("timezone") if insights else None,
        "warnings": warnings,
    }
    # 80 when piped or writing a file: pipeable bytes are CI-stable bytes, never
    # a function of whoever happened to pipe.
    if args.output or not sys.stdout.isatty():
        width = 80
    else:
        width = max(60, min(200, shutil.get_terminal_size((80, 24)).columns))
    # A file is written utf-8, so blocks are always safe there; the terminal
    # probe only applies to terminal glyphs.
    glyphs = "blocks" if args.output else glyph_style()
    segments = build_report_segments(
        usage, insights, active_time, window=window, width=width, glyphs=glyphs
    )
    # Piped bytes and file bytes are ALWAYS plain; color is a tty privilege.
    if not args.output and _color_enabled():
        text = emit_ansi(segments)
    else:
        text = emit_plain(segments)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0
