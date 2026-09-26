"""Pure string rules for the TUI and the one-shot ``tokdash report`` (spec §4).

NO textual and NO rich import in this module — the import-discipline test
(tests/test_tui_import_discipline.py) proves the whole report path imports on a
stripped install that lacks both. The ANSI gate is re-exported from the onboard
engine (engine.py:1611-1646, the ONLY existing terminal-styling helpers in the
repo — NO_COLOR / TERM=dumb / isatty all honored there) so report output and
the setup/doctor output share one color gate; report.py's ``emit_ansi`` applies
them and ``emit_plain``/``emit_markup`` never touch them.

Null-vs-zero law (single source of truth; every renderer follows it):
``None`` from the backend is unknown/not-applicable → EM_DASH ("n/a" for hit
rates); a MEASURED 0 renders as 0 / $0.00 / 0s / 0.0%. An unpriced row cost of
0.0 is NOT a measured zero: ``fmt_cost_row`` dashes it, because
PricingDatabase.get_cost returns 0.0 for unknown models (pricing.py:320) and an
unpriced row is not a free row.
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass

from ..onboard.engine import (  # noqa: F401 — re-exported for report.py's emit_ansi
    _accent,
    _bad,
    _bold,
    _color_enabled,
    _ok,
    _warn,
)

EM_DASH = "—"  # cp1252-safe; every missing figure per the web convention
NA = "n/a"

_BLOCK_RAMP = "▁▂▃▅▇"
_ASCII_RAMP = ".:-=#"


@dataclass(frozen=True)
class Run:
    """One styled text segment; the shared currency between formatting,
    report.py's build_report_segments and app.py's panes."""

    text: str
    style: str | None = None  # None | "ok" | "bad" | "warn" | "bold" | "accent" | "muted"


def glyph_style() -> str:
    """'blocks' on a UTF-8 stdout, else 'ascii' (legacy cp1252 conhost degrades
    the block ramp to '?'). Callers override for files: a report written with
    encoding="utf-8" is always 'blocks'."""
    enc = (sys.stdout.encoding or "").lower().replace("-", "")
    return "blocks" if enc.startswith("utf") else "ascii"


def _round1(x: float) -> float:
    """Round to one decimal, HALF UP — matches the web's
    ``Math.round(x * 10) / 10`` (index.html:7999), NOT Python's banker round().
    Keeps the terminal and the dashboard rolling the 999.95 boundary the same
    way."""
    return math.floor(x * 10 + 0.5) / 10


def fmt_int(n: int | float | None) -> str:
    if n is None:
        return EM_DASH
    # Fixed ',' grouping, no locale module — the terminal byte stream must not
    # depend on the host's locale settings.
    return f"{int(n):,}"


def fmt_tokens(n: int | float | None) -> str:
    """Compact token figure mirroring the web's formatCompactTokenCount
    (index.html:7985-8007): < 1000 grouped integer, else one-decimal K/M/B/T
    rolling UP when rounding pushes the displayed value to 1000+."""
    if n is None:
        return EM_DASH
    tokens = int(n)
    if tokens < 1_000:
        return fmt_int(tokens)
    scales = [(1_000_000_000_000, "T"), (1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")]
    idx = len(scales) - 1
    for i, (divisor, _suffix) in enumerate(scales):
        if tokens >= divisor:
            idx = i
            break
    rounded = _round1(tokens / scales[idx][0])
    if rounded >= 1_000 and idx > 0:  # e.g. 999_960 → "1000.0K" is never shown
        idx -= 1
        rounded = _round1(tokens / scales[idx][0])
    return f"{rounded:.1f}{scales[idx][1]}"


def fmt_cost_headline(x: float | None) -> str:
    """Headline cost. $0.00 is a MEASURED zero and prints as "$0.00"."""
    if x is None:
        return EM_DASH
    return f"${x:,.2f}"


def fmt_cost_row(x: float | None) -> str:
    """Table/row cost. None OR <= 0 → EM_DASH: an unpriced row (get_cost
    returns 0.0 for unknown models, pricing.py:320) is not free."""
    if x is None or x <= 0:
        return EM_DASH
    return f"${x:,.2f}"


def fmt_hit(x: float | None) -> str:
    """Cache hit rate. None → "n/a" (no prompt input). Use the backend value;
    NEVER recompute from token counts — the server owns the definition."""
    if x is None:
        return NA
    return f"{x * 100:.1f}%"


def _unit_pair(head_value: int, head_unit: str, tail_value: int, tail_unit: str) -> str:
    """Web formatDurationUnits' `pair` (index.html:10336-10340): at most two
    units, a zero tail is dropped ("2 days", not "2 days 0 hours"), and each
    side pluralizes on its own value ("1 day 1 hour")."""

    def unit(value: int, name: str) -> str:
        return f"{value} {name}" if value == 1 else f"{value} {name}s"

    head = unit(head_value, head_unit)
    return head if not tail_value else f"{head} {unit(tail_value, tail_unit)}"


def fmt_duration(ms: int | float | None) -> str:
    """Duration in words — the locked spec §4 table, a faithful port of the
    web's formatDuration + formatDurationUnits (index.html:10295-10340),
    including their round-half-up seconds and hour-rollover guard."""
    if ms is None:
        return EM_DASH
    seconds = max(0, math.floor(float(ms) / 1000 + 0.5))  # web Math.round(ms/1000)
    if seconds < 60:
        # Sub-minute tier; a MEASURED zero renders "0s" here — the null-vs-zero
        # law forbids the web's `if (!seconds) return "—"` shortcut for 0.
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = seconds // 3600
    if hours < 24:
        return f"{hours}h {minutes % 60:02d}m"  # zero-padded minutes
    days = seconds // 86400
    if days < 7:
        # The web scopes this guard to the day/hour tier ONLY
        # (index.html:10335-10338): 23h59m+ of remainder rounds to a full 24
        # and rolls into the next day rather than printing "1 day 24 hours".
        # Applying it BEFORE tier selection instead inflates `days` past the
        # 7/30 boundaries and shifts the rendered tier versus the web (6d23h35m
        # is "7 days" there, not "1 week"), so keep it inside this branch.
        rem_hours = math.floor((seconds - days * 86400) / 3600 + 0.5)
        if rem_hours == 24:
            days, rem_hours = days + 1, 0
        return _unit_pair(days, "day", rem_hours, "hour")
    if days < 30:
        return _unit_pair(days // 7, "week", days % 7, "day")
    return _unit_pair(days // 30, "month", days % 30, "day")


def fmt_delta(pct: float | None, *, surface: str = "report") -> Run:
    """Render a pct_change figure (PERCENTAGE POINTS already — compute.py
    :1075-1079 returns round(((cur-prev)/prev)*100, 1) — so NEVER multiply by
    100 here; the §10 cross-check test locks that) as a styled Run.

    The per-surface COLOR SPLIT is intentional, not drift, and mirrors the two
    opposite web conventions: the Report tab colors up GREEN
    (.usage-report-delta-up, index.html:14908 + 1752-1755, #16A34A) while the
    Overview KPI chips color up RED (renderDelta, index.html:8323, #DC2626).
    Each surface follows its own web counterpart; do not "unify" them.

    Unknown ``surface`` values render with the report convention — the safe
    neutral reading of a value the spec never defines."""
    if pct is None:
        return Run(EM_DASH, None)
    ascii_mode = glyph_style() == "ascii"
    if surface == "overview" and pct == 0:
        # "→" is as cp1252-hostile as "↑"/"↓", so the ascii mode spells the
        # no-change arrow as "=" (matching the spaceless "+8.2%" ascii form).
        return Run("=0.0%" if ascii_mode else "→ 0.0%", "muted")
    if pct >= 0:
        style = "bad" if surface == "overview" else "ok"
        marker = "+" if ascii_mode else "↑ "
    else:
        style = "ok" if surface == "overview" else "bad"
        marker = "-" if ascii_mode else "↓ "
    return Run(f"{marker}{abs(pct):.1f}%", style)


def day_spark(values: list[float], *, glyphs: str = "blocks") -> str:
    """One glyph per value on a 5-level ramp (blocks "▁▂▃▅▇", ascii ".:-=#").

    Level thresholds are the 20/40/60/80 percentiles of the NONZERO values
    (nearest-rank), so the tallest glyph means "at or above the 80th percentile
    of active days"; a 0-valued day is always level 0 and an all-zero window
    renders a flat ramp floor — only the EMPTY list is a missing figure per the
    null-vs-zero law (→ EM_DASH)."""
    if not values:
        return EM_DASH
    ramp = _ASCII_RAMP if glyphs == "ascii" else _BLOCK_RAMP
    nonzero = sorted(v for v in values if v)
    if not nonzero:
        return ramp[0] * len(values)
    n = len(nonzero)
    thresholds = [nonzero[math.ceil(p * n) - 1] for p in (0.2, 0.4, 0.6, 0.8)]
    return "".join(
        ramp[0] if not v else ramp[sum(1 for t in thresholds if v >= t)] for v in values
    )


def heat_glyph(level: int | None, *, glyphs: str = "blocks") -> str:
    """Calendar cell heat: 0 (or None) is BLANK; 1..4 draw the top four steps
    of the day_spark ramp ("▂▃▅▇", ascii ":-=#"). Out-of-range levels clamp.
    The level is the server's rank (compute.assign_contribution_intensity,
    compute.py:1127) — renderers read it, NEVER recompute it from tokens."""
    ramp = _ASCII_RAMP if glyphs == "ascii" else _BLOCK_RAMP
    lvl = max(0, min(4, int(level or 0)))
    return " " if lvl == 0 else ramp[lvl]


def tool_label(key: object, labels: dict | None = None) -> str:
    """Display name for a tool id — the web's formatToolName (index.html:9339)
    ported: canonical-map hit, else the word-wise title-case fallback
    ("gemini_cli" → "Gemini Cli"; the JS capitalizes
    each word's first letter and never lower-cases the rest, so neither do we).
    The map itself (sessions.TOOL_LABELS) lives in the BACKEND — pure modules
    receive it as data (charts) or import it at the layer that already speaks
    backend (report.py); formatting stays import-clean. sessions.py:6472's
    ``key.title()`` is the internal divergence, locked against tests here.
    """
    key = str(key or "")
    hit = (labels or {}).get(key)
    if hit:
        return str(hit)
    fallback = " ".join(key.replace("_", " ").replace("-", " ").split())
    if not fallback:
        return "Unknown"
    return " ".join(w[:1].upper() + w[1:] for w in fallback.split(" "))


def text_bar(value: float, maxv: float, *, width: int = 20, glyphs: str = "blocks") -> str:
    """Inline bar row: round(width * value / maxv) filled glyphs ("█", "#" in
    ascii mode). maxv <= 0 → empty string: nothing to scale against renders
    blank, not a full bar."""
    if maxv <= 0:
        return ""
    glyph = "#" if glyphs == "ascii" else "█"
    return glyph * round(width * value / maxv)


def window_line(rng: dict) -> str:
    """Label line from a resolve_period / payload "range" block (keys
    from/to/days/recognized) — always the response's block, never the caller's
    request token."""
    line = f"{rng['from']} → {rng['to']} · {rng['days']} days"
    if not rng.get("recognized"):
        # Defense-in-depth: an unrecognized period silently resolves to all
        # time server-side (over-reports), so say so instead of dressing the
        # all-time range up as the requested one. v1's parse guard makes this
        # unreachable on the new commands.
        line += " (unrecognized period — showing all time)"
    return line
