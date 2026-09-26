"""The Textual app + the ``tokdash tui`` entry point (spec §6).

This is the ONLY tokdash module allowed to import textual (and rich, which
textual pulls in) — the import-discipline test proves ``report``/``data``/
``formatting``/``charts``/``remote`` stay textual-free so a stripped install
can still run ``tokdash report``.

Verified Textual 8.x facts (spec §6, live-tested against 8.2.8 — do not
"fix" these back to old-Textual patterns): there is NO ``on_<worker>_success``
result delivery, a non-async ``@work`` method REQUIRES ``thread=True``, and
results arrive only via ``await worker.wait()``, which re-raises a crash as
``WorkerFailed`` (unwrapped with ``getattr(exc, "error", exc)``). Every
decorator therefore sets ``exit_on_error=False`` — the default True would
kill the app on one failed fetch. Threads never touch widgets: blocking
fetches run in thread jobs, and only the async supervisors paint, on the
event loop, behind a generation guard so a superseded load can never
stale-paint the pane.

The Report pane is ONE Static fed by report.py's shared segment builder —
the same function that produces the piped ``tokdash report`` bytes — so the
two surfaces structurally cannot drift.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date

from rich.markup import escape
from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.events import Key
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Static,
    TabbedContent,
    TabPane,
)

from ..api import REPORT_FACETS, CacheBackpressureError, _local_today
from ..compute import period_is_recognized
from ..sessions import TOOL_LABELS
from ..usage_store import UsageDatabaseSchemaTooNewError
from .data import (
    OVERVIEW_PERIODS,
    REPORT_PERIODS,
    FetchOutcome,
    db_summary,
    ensure_usage_db_compatible,
    fetch_active_time,
    fetch_insights,
    fetch_quota_history,
    fetch_quota_state,
    fetch_stats,
    fetch_usage,
    reset_remote,
    resolve_overview_period,
    resolve_report_period,
)
from .formatting import (
    EM_DASH,
    Run,
    fmt_cost_headline,
    fmt_cost_row,
    fmt_delta,
    fmt_duration,
    fmt_hit,
    fmt_int,
    fmt_tokens,
    glyph_style,
    tool_label,
    window_line,
)
from .charts import (
    active_time_ms,
    day_bars,
    month_grid,
    quota_table,
    tool_model_detail,
    year_heatmap,
)
from .report import build_report_segments, emit_markup

# Web parity: USAGE_REPORT_STALE_REREAD_MS = 12000 (index.html:14469). A
# stale answer gets exactly one silent, NON-forced re-read per window-load —
# a forced refresh would start a second full recompute the
# stale-while-revalidate daemon is already doing.
STALE_REREAD_SECONDS = 12.0

# Bar-field width for the adaptive chart slot (active-time bars / week bars).
# Fixed: DataTable cells and the 80-col reference terminal are width-stable,
# and the chart must not re-wrap on a resize.
_CHART_BAR_WIDTH = 16

# Round 5: ONE shared column grid for the three Overview tables ("align the
# input output ... rows" — each table is its own auto-sizing DataTable, so
# identical FIXED widths per column are the only cross-table alignment lever
# DataTables have; there is no cross-table sizing). Numeric cells are
# right-aligned within their fixed width by _num, so digit edges line up
# across sections; names wider than _COL_NAME crop (rare, display-only).
# Widths cover the fmt_* ranges: tokens ≤ "999.9T", hit ≤ "100.0%",
# cost ≤ "$12,345.67", msgs ≤ "1,234,567", time ≤ "364 days 23 hours".
_COL_NAME = 22
_COL_TOK = 7
_COL_PCT = 6
_COL_COST = 10
_COL_MSGS = 9
_COL_TIME = 17

# Written only while a pane has NOTHING painted yet (first run, or it is
# error-stated). A reload over painted data takes the round-4 clear-and-
# notice path in _begin_load instead — never this long copy, never stale data.
_SKELETON = "computing… first run scans session logs and can take tens of seconds"

# Round 4 key guidance, user-confirmed wording: a spelled-out legend pinned
# at the TOP of each period pane (painted once at mount, never repainted),
# echoed compactly in the status bar. The brackets are literal — the pane
# text goes through emit_markup, which hand-escapes "[" for us.
_HINT_PERIOD_KEYS = " t today · w week · m month · y year · a all"
_HINT_REPORT_KEYS = " w week · m month · y year"
_HINT_SHIFT_KEYS = " [ earlier · ] later · 0 back to today"
_HINT_QUOTA_KEYS = " u poll now"


def _num(value: str) -> Text:
    """A formatted-digit cell, right-aligned inside its fixed-width column.

    Textual 8.2.8 DataTable has NO column-level justify — ``add_column``
    takes only label/width/key/default (verified against the installed
    source) — so alignment rides on each cell as a rich ``Text``. Round 5
    pairs this with the fixed ``width=`` columns of ``_add_ov_grid``: the
    width pins the column edge, the justify pins the digit edge, and the
    three Overview tables line up. Only
    FORMATTED DIGITS from fmt_* may come through here; user data must enter
    as ``escape(str(...))`` (str cells are markup-parsed) or as a bare Text
    (literal, never parsed) — the two paths are deliberate, see
    ``_paint_ov_detail``."""
    return Text(value, justify="right")


def _clamp_report_width(raw: int) -> int:
    """Report-pane content width -> builder width: an un-laid-out pane reports
    0 (headless, or the pane was never shown) and falls back to the piped
    report's 80; anything laid out is clamped to the builder's sane 60..200
    band (the same clamp ``tokdash report`` applies to the terminal width)."""
    if raw <= 0:
        return 80
    return max(60, min(200, raw))

# Captured at import: every Textual driver REPLACES sys.stdout with a
# _PrintCapture shim (print() must not corrupt the screen), and that shim has
# no .encoding — so the §4 glyph probe would crash mid-run on a live terminal
# just as it does headless. on_mount hands the real terminal's encoding back to
# the shim, and _safe_glyphs is the belt if that ever fails.
_STDOUT_ENCODING = getattr(sys.stdout, "encoding", "") or ""


def _safe_glyphs() -> str:
    """glyph_style() with the _PrintCapture fallback (same utf8-or-ascii rule,
    probed on the encoding captured before Textual touched stdout)."""
    try:
        return glyph_style()
    except Exception:  # noqa: BLE001 — no .encoding on the capture shim
        return "blocks" if _STDOUT_ENCODING.lower().replace("-", "").startswith("utf") else "ascii"


def run_tui(args: argparse.Namespace) -> int:
    """``tokdash tui``: refuse anything that is not an interactive terminal
    BEFORE importing/starting Textual (the alt screen is not a place for
    error messages), then hand the app the validated period token."""
    if not sys.stdout.isatty() or os.environ.get("TERM", "") == "dumb":
        raise SystemExit(
            "`tokdash tui` needs an interactive terminal. "
            "One-shot view: `tokdash report`; JSON: `tokdash export --pretty`."
        )
    try:
        ensure_usage_db_compatible()
    except UsageDatabaseSchemaTooNewError as e:
        raise SystemExit(f"{e} — run 'tokdash update'")
    if not period_is_recognized(args.period):
        # Belt-and-suspenders: the cli.py parse-time guard normally fires first.
        raise SystemExit(f"Unknown period {args.period!r}.")
    app = TokdashApp(initial_period=args.period)
    app.run()
    # App.run() returns None; the fatal-exit code lives on App.return_code
    # (textual's own docstring prescribes sys.exit(app.return_code)). Without
    # this, the mid-run schema-too-new path (§7: exit 1) would reach the shell
    # as 0 while the IDENTICAL startup error exits 1 — an automation trap.
    return app.return_code or 0


class TokdashApp(App):
    """Overview + Report + Quota tabs over the same compute the web uses.

    The round-2 law: OPTIONAL read-only HTTP delegation to a same-version
    live ``tokdash serve`` (``data.py``/``remote.py`` — GETs only, fail-closed
    to the identical in-process path on any doubt; ``TOKDASH_TUI_NO_REMOTE=1``
    disables it and every fetch answers exactly as in round 1). Still no
    server of ours, no auto-refresh timer (polling contends with the
    server's compute semaphore; ``r`` is the refresh), and quota NEVER leaves
    the process — network quota polling happens only on an explicit ``u``.
    """

    TITLE = "tokdash"

    CSS = """
    #status { height: 1; }
    TabbedContent { height: 1fr; }
    /* Round 3: the PANE is the scroll container (TabPane ships NO overflow in
       8.2.8, so content past the pane's height was clipped and the wheel was
       dead on Overview, and scrolled the whole SCREEN — dragging the tab
       strip off — on Report). height:1fr + overflow-y:auto per pane: the
       wheel scrolls the pane, the screen and tab strip stay put. height:auto
       panes would size to content (virtual==region) and scroll nothing. */
    #pane-overview, #pane-report, #pane-quota {
        padding: 0 1;
        height: 1fr;
        overflow-y: auto;
    }
    /* max-height was dropped in round 3: the wheel bubbles past a DataTable to
       the pane (verified), so a capped table's rows below the cap were
       wheel-unreachable — defeating the "never collapse" rule. Tables now show
       every row and the pane scrolls them. Round 5 killed the row cursor
       (cursor_type="none" per table, see compose) — the wheel is the only
       scroll axis, so paging keys are gone with it. */
    DataTable { height: auto; }
    /* Round 4: sections read as sections — one blank row of breathing room
       above each Overview block, the hint lines, and the quota blocks
       (margin on a DataTable renders a real 1-row gap; the tables stay
       height:auto and the panes stay the scroll containers). */
    #ov-hints, #ov-chart, #ov-tools, #ov-models, #ov-detail,
    #rp-hints, #quota-hints, #quota-table, #quota-credits {
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("1", "show_pane('pane-overview')", "Overview"),
        Binding("2", "show_pane('pane-report')", "Report"),
        Binding("3", "show_pane('pane-quota')", "Quota"),
        # p cycles the active pane's window forward only — the round-2 "P"
        # (prev) is gone: it read as a near-duplicate of p and confused the
        # two axes. Direct period keys below are the fast path; ``[``/``]``
        # are the date-shift axis.
        Binding("p", "cycle_window(1)", "Next window", show=False),
        # Period keys (round 3): select the period DIRECTLY on BOTH the
        # Overview and the Report pane (same letters, same meaning everywhere);
        # t/a are inert on Report (its windows are the three calendar ones).
        # show=False keeps the Footer compact; HelpPanel still lists them.
        Binding("t", "set_period('today')", "Today", show=False),
        Binding("w", "set_period('week')", "Week", show=False),
        Binding("m", "set_period('month')", "Month", show=False),
        Binding("y", "set_period('year')", "Year", show=False),
        Binding("a", "set_period('all')", "All time", show=False),
        # Date-shift axis: [ steps WHOLE PERIODS back (month view: a whole
        # month; "today" view: a day), ] forward, 0 jumps back to the current
        # period (never future: clamped at 0). Distinct from the period-length
        # keys above — a different axis, per the review.
        Binding("left_square_bracket", "shift_date(-1)", "Prev period", show=False),
        Binding("right_square_bracket", "shift_date(1)", "Next period", show=False),
        Binding("0", "reset_date", "Today", show=False),
        # Quota pane only: the ONE network entry point of the whole TUI.
        Binding("u", "poll_quota", "Poll quota", show=False),
        # "?" — the key name is question_mark; help is the BUILT-IN HelpPanel
        # (App.action_show_help_panel), not a push_screen modal. No tab/
        # shift+tab pane bindings: tab is focus-nav in 8.x (verified).
        Binding("question_mark", "show_help_panel", "Keys"),
    ]

    def __init__(self, initial_period: str = "today", **kwargs) -> None:
        super().__init__(**kwargs)
        self._initial_period = initial_period
        try:
            self._ov_idx: int = OVERVIEW_PERIODS.index(initial_period)
        except ValueError:
            # A recognized-but-not-cyclable token (7d, 30, …) is shown until
            # the first 'p'/'t/w/m/y/a', which then starts the cycle.
            self._ov_idx = -1
        self._rp_idx = 0
        # Generation guard: bumped by every action that (re)starts a load
        # (window cycle, refresh, day rollover). Supervisors snapshot it and
        # drop their paint the moment it moves — superseded results die
        # silently, and abandoned thread jobs finish harmlessly into the cache.
        self._gen = 0
        self._today = _local_today()
        # Period-shift axis (``[``/``]``/``0``): WHOLE PERIODS the window steps
        # back from the current one (clamped >= 0), counted in each pane's own
        # unit — month view steps whole months (one back = all of last month),
        # week view whole weeks, year view whole years, "today" single days.
        # 0 keeps every warm key the round-2 parity tests pin; shift > 0 is a
        # cold/delegated compute but the SAME calendar math — never wrong data.
        # Resolved once per load (below), so a mid-flight keypress can never
        # mis-pair the usage-vs-insights-vs-active windows of one load.
        self._period_shift = 0
        # Window END the CURRENT Overview load was resolved to — the year
        # heatmap's calendar year and today-marker read this (and the painted
        # range line pairs with it), so a shift that lands mid-load cannot
        # repaint an already-drawn surface out of sync with its data. Shift 0
        # on today-pinned windows = today, exactly as before.
        self._ov_anchor = self._today
        self._rp_started = False  # Report lazy-load: first tab activation only
        self._qp_started = False  # Quota lazy-load: same rule
        self._polling = False     # one in-flight quota poll at a time
        # "empty" | "ok" | "error" — drives the skeleton rule in _begin_load.
        self._ov_state = "empty"
        self._rp_state = "empty"
        self._qp_state = "empty"
        # Last painted payloads per surface (re-emit reads these, never a
        # second fetch; rows are only ever read, never mutated).
        self._ov_usage: dict | None = None
        self._ov_active: dict | None = None
        self._ov_stats: dict | None = None
        self._ov_year_stats: dict | None = None  # only the "year" chart needs it
        self._ov_pair: tuple[str | None, str | None] = (None, None)  # resolved window
        self._quota_state_val: dict | None = None
        self._quota_history_val: dict | None = None
        self._rp_usage: dict | None = None
        self._rp_insights: dict | None = None
        self._rp_active: dict | None = None
        # One silent stale re-read per (pane, window-load).
        self._reread_done: set[tuple[str, int]] = set()
        self._inflight: dict[str, int] = {}  # pane -> gen that started it
        # Panes whose load was superseded by a gen bump issued from the OTHER
        # pane (no successor load for this pane exists). Without a recovery
        # these would sit on the skeleton forever — see _end_load and the
        # TabActivated hook, the only re-fetch trigger a stranded pane gets.
        self._stranded: set[str] = set()
        self._load_started: dict[str, float] = {}  # pane -> monotonic start
        self._cache_str: dict[str, str] = {}  # pane -> "cache hit age 3s"
        self._db_line: str | None = None  # cached; status ticks must not re-open sqlite

    # ------------------------------------------------------------------ UI --

    def compose(self) -> ComposeResult:
        # ZERO custom widget classes: built-in Static/DataTable + the segment
        # seam suffice (spec §6 rejected the KpiCard tree).
        yield Header()
        yield Static("", id="status")
        with TabbedContent(initial="pane-overview", id="tabs"):
            with TabPane("Overview", id="pane-overview"):
                # Date line, painted as a background-filled section header.
                yield Static(_SKELETON, id="ov-range")
                # Round 4: key legend at the TOP of the pane (the bottom
                # status hint stays as the compact echo).
                yield Static("", id="ov-hints")
                yield Static("", id="ov-kpis")
                # Adaptive period visual (round 2, kept round 3): bars/grid/
                # heatmap per the active token. Day and all-time show NO chart
                # (the day's active-time bars moved into the tools table's
                # Time column); the slot is hidden for those two.
                yield Static("", id="ov-chart")
                # Round 5: cursor_type="none" on EVERY table — the default
                # cursor ("cell" in 8.2.8) paints its highlighted cell with
                # a background band, and on a fresh table that cell is row 0
                # col 0 — the "first item got background highlighted" the
                # user asked removed. The wheel is the scroll mechanism;
                # nothing needs to read as picked.
                yield DataTable(id="ov-tools", cursor_type="none")
                yield DataTable(id="ov-models", cursor_type="none")
                yield DataTable(id="ov-detail", cursor_type="none")
            with TabPane("Report", id="pane-report"):
                yield Static("", id="rp-hints")
                yield Static(_SKELETON, id="rp-body")
            with TabPane("Quota", id="pane-quota"):
                yield Static("", id="quota-hints")
                yield Static(_SKELETON, id="quota-note")
                yield DataTable(id="quota-table", cursor_type="none")
                # Reset credits (Codex AND Claude Code limit resets, round 4
                # — every row says which provider/install it belongs to)
                # live in their OWN bottom section (with expiry dates),
                # never as a row of the usage table.
                yield Static("", id="quota-credits")
        yield Footer()

    def on_mount(self) -> None:
        # Hand the real terminal's encoding back to the driver's stdout shim
        # (it has none) so glyph_style()/fmt_delta's §4 probe keeps working
        # for the whole run — see _STDOUT_ENCODING.
        if not hasattr(sys.stdout, "encoding"):
            try:
                sys.stdout.encoding = _STDOUT_ENCODING
            except Exception:  # noqa: BLE001 — _safe_glyphs is the fallback
                pass
        # Round 3: the per-tool table carries the full token breakdown plus the
        # per-tool active time (the old standalone day-view bar section folded
        # into this "Time" column). Input/Output/Cache/Msgs come from usage
        # "apps"; Total/Hit/Cost fall back to by_tool; Time from the active
        # payload — a tool absent from "apps" (openclaw) dashes those cells.
        # Round 4: the models and detail tables carry the SAME field set as
        # the tools table; round 5: they share ONE fixed-width column grid
        # (_add_ov_grid) so the Input/Output/… digits line up across sections.
        self._add_ov_grid(self.query_one("#ov-tools", DataTable), "Tool", time_col=True)
        self._add_ov_grid(self.query_one("#ov-models", DataTable), "Model", time_col=False)
        self._add_ov_grid(self.query_one("#ov-detail", DataTable), "Tool / Model", time_col=True)
        # Round 4: the top-of-pane key legends (the user-confirmed spelled-out
        # wording). Painted ONCE here — they are static copy, not load state.
        self.query_one("#ov-hints", Static).update(emit_markup([
            [Run(_HINT_PERIOD_KEYS, "muted")],
            [Run(_HINT_SHIFT_KEYS, "muted")],
        ]))
        self.query_one("#rp-hints", Static).update(emit_markup([
            [Run(_HINT_REPORT_KEYS, "muted")],
            [Run(_HINT_SHIFT_KEYS, "muted")],
        ]))
        self.query_one("#quota-hints", Static).update(
            emit_markup([[Run(_HINT_QUOTA_KEYS, "muted")]])
        )
        # Fixed 6 columns matching charts.quota_table's rows — the fold of the
        # account into the Provider cell is what keeps this column set static.
        self.query_one("#quota-table", DataTable).add_columns(
            "Provider", "Window", "Used", "Resets", "24h", "Status"
        )
        self.set_interval(1.0, self._tick_status)  # "computing… Ns" suffix
        self._refresh_db_line()  # the footer's SQLite read runs off-loop (§17)
        self._load_overview(self._ov_period(), False)
        self._update_status()

    @staticmethod
    def _add_ov_grid(table: DataTable, header: str, *, time_col: bool) -> None:
        """Build one Overview table on the shared fixed-width column grid.

        Each table is its own auto-sizing DataTable, so without fixed
        widths the same column ("Input") lands at a different x in Tools
        than in Models or Tool/Model. One helper for all three is the
        single source of truth for that grid (round 5: "align the input
        output ... rows"); only the first header and the Time column
        differ per table."""
        table.add_column(header, width=_COL_NAME)
        for label in ("Input", "Output", "Cache", "Total"):
            table.add_column(label, width=_COL_TOK)
        table.add_column("Hit", width=_COL_PCT)
        table.add_column("Cost", width=_COL_COST)
        table.add_column("Msgs", width=_COL_MSGS)
        if time_col:
            table.add_column("Time", width=_COL_TIME)

    # ------------------------------------------------------------- actions --

    def action_show_pane(self, pane_id: str) -> None:
        self._check_day_rollover()
        self.query_one("#tabs", TabbedContent).active = pane_id

    def action_refresh(self) -> None:
        self._check_day_rollover()
        self._db_line = None  # a refresh may as well re-read the db footer
        self._refresh_db_line()  # off-loop again (the accessor stays pure)
        # r re-probes the service: a one-shot dead-service latch ("gone")
        # deserves a second look when the user explicitly asks for fresh data.
        reset_remote()
        self._gen += 1
        pane = self._active_pane()
        if pane == "report":
            self._rp_started = True
            self._load_report(self._rp_idx, True)
        elif pane == "quota":
            self._qp_started = True
            self._load_quota(True)
        else:
            self._load_overview(self._ov_period(), True)

    def action_cycle_window(self, delta: int) -> None:
        self._check_day_rollover()
        self._gen += 1
        if self._active_pane() == "overview":
            if self._ov_idx < 0:
                self._ov_idx = 0
            else:
                self._ov_idx = (self._ov_idx + delta) % len(OVERVIEW_PERIODS)
            self._load_overview(OVERVIEW_PERIODS[self._ov_idx], False)
        else:
            # p/P on Report/Quota cycles the REPORT windows (_rp_idx) — a
            # different axis from the Overview period keys; never merge them.
            self._rp_idx = (self._rp_idx + delta) % len(REPORT_PERIODS)
            self._rp_started = True
            self._load_report(self._rp_idx, False)

    def action_set_period(self, token: str) -> None:
        """t/w/m/y/a — direct period keys (round 3). Work on BOTH period panes
        with the SAME meaning (the round-2 pane-only rule made the two panes
        feel arbitrary): on Overview they pick the period token, on Report they
        pick the calendar window by the same letter. t/a are INERT on Report
        (its windows are week/month/year only) and everywhere on Quota.
        resolve_overview_period still normalizes to the warmer's exact call
        shape at the fetch site — never fetch by raw token."""
        self._check_day_rollover()
        pane = self._active_pane()
        if pane == "report":
            if token not in REPORT_PERIODS:
                return  # t/a: no such report window — stay inert
            self._rp_idx = REPORT_PERIODS.index(token)
            self._gen += 1
            self._rp_started = True
            self._load_report(self._rp_idx, False)
            return
        if pane != "overview":
            return  # quota: no period axis at all
        try:
            self._ov_idx = OVERVIEW_PERIODS.index(token)
        except ValueError:
            pass
        self._gen += 1
        self._load_overview(token, False)

    def action_poll_quota(self) -> None:
        """u — the TUI's ONLY network quota entry point, quota pane only.
        No timer may ever reach this: the note is painted "polling…" BEFORE
        the job starts so a slow network is visibly the app's own doing."""
        if self._active_pane() != "quota" or self._polling:
            return
        self._polling = True
        self.query_one("#quota-note", Static).update("polling…")
        self._poll_quota()

    # -------------------------------------------------------- date shift ----

    def _ov_token_has_window(self) -> bool:
        """True when the Overview token resolves to a real date pair. "all"
        and rolling Nd tokens have NO window to step (the shift counter must
        stay honest: stepping a windowless Overview is a silent no-op)."""
        return resolve_overview_period(
            self._ov_period(), today=self._today
        )[1] is not None

    def _shift_unit(self, token: str) -> str:
        """The marker suffix for a token's own period unit (d/w/m/y); "" for
        windowless tokens — a shift can never apply to those, so neither can
        the marker ever lie about them."""
        return {"today": "d", "week": "w", "month": "m", "year": "y"}.get(token, "")

    def _shifted_panels(self) -> tuple[bool, bool]:
        """(reload overview?, reload report?) for a shift — date-pinned panes
        only. Callers MUST treat ``not any(...)`` as fully inert: bumping the
        gen without issuing a successor would strand the in-flight loads of
        panes the shift cannot move."""
        return self._ov_token_has_window(), self._rp_started

    def _apply_shift(self, new_shift: int) -> None:
        ov, rp = self._shifted_panels()
        if not (ov or rp):
            # Nothing on screen is date-pinned ("all"/rolling Overview and
            # Report never started): stay FULLY inert — do not even store the
            # counter, or a later Report lazy-load would silently start out
            # shifted by presses the user never saw do anything. No gen bump
            # either: it would strand the in-flight load with no successor.
            return
        self._period_shift = new_shift
        self._gen += 1
        if ov:
            self._load_overview(self._ov_period(), False)
        if rp:
            self._load_report(self._rp_idx, False)

    def action_shift_date(self, delta: int) -> None:
        """``[`` one period back / ``]`` one period forward — the DATE axis,
        entirely separate from the period-LENGTH keys (t/w/m/y/a). Each press
        steps a WHOLE calendar period in the pane's own unit: month view one
        ``[`` = the full previous month (Aug 1→31, not Sep 1 pulled one day
        shorter), week view the full previous Mon→Sun week, year view the full
        previous year, "today" view single days (never future: clamped at 0).
        Both date-pinned panes reload so neither keeps a shift-stale body
        (mirrors the midnight fan-out). Quota (never date-shifted) is
        untouched; a windowless Overview is inert."""
        self._check_day_rollover()
        new_shift = max(0, self._period_shift - delta)  # ] (delta +1) shrinks back
        if new_shift == self._period_shift:
            return  # already at the current period; ] at 0 is inert (never future)
        self._apply_shift(new_shift)

    def action_reset_date(self) -> None:
        """``0`` — jump the shifted view back to the current period. Unlike a
        STEP this is never inert while shifted: with nothing date-pinned there
        is nothing to reload, but the counter must still land at 0 — "0" always
        means "now", so no hidden shift survives into the next window."""
        self._check_day_rollover()
        if self._period_shift == 0:
            return
        if not any(self._shifted_panels()):
            self._period_shift = 0
            return
        self._apply_shift(0)

    # ---------------------------------------------------- day rollover ------

    def on_key(self, event: Key) -> None:
        # Keypress-triggered midnight check — no midnight timer, no polling.
        # Keys that hit a BINDING run the same check inside the action (a
        # handled key stops before it bubbles here); this covers the rest.
        self._check_day_rollover()

    def _check_day_rollover(self) -> None:
        """Open windows are day-pinned into their cache keys (api.py
        _window_cache_key stamps the capture day) — an app left open past
        midnight must not keep serving yesterday's pinned snapshot. The new
        day's keys miss the cache on their own, so nothing is forced."""
        today = _local_today()
        if today == self._today:
            return
        self._today = today
        self._gen += 1
        pane = self._active_pane()
        if pane == "report":
            self._rp_started = True
            self._load_report(self._rp_idx, False)
        elif pane == "quota":
            self._load_quota(False)  # activating it always set _qp_started
        else:
            self._load_overview(self._ov_period(), False)
        # Day-pinned windows: a started-but-hidden Quota pane re-reads too —
        # quota_state's snapshots are day-scoped the same way, and the
        # exclusive "qp" group keeps this to one flight even if the user then
        # activates the pane.
        if pane != "quota" and self._qp_started:
            self._load_quota(False)

    # ------------------------------------------------------------- events --

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        # event.tab.id is the TAB id ("--content-tab-pane-report"), not the
        # pane's — get_pane() strips the prefix. Compare against the pane.
        pane = event.tabbed_content.get_pane(event.tab)
        if pane.id == "pane-report" and not self._rp_started:
            # Lazy-load: the Report tab's first activation starts the fetch;
            # default idx 0 = week-to-date (the warmer's first report window).
            self._rp_started = True
            self._load_report(self._rp_idx, False)
        elif pane.id == "pane-report" and "report" in self._stranded:
            # The in-flight load was superseded by an Overview-side gen bump
            # and never got a successor: re-issue it now, or this pane would
            # keep showing the skeleton with no fetch pending (_stranded is
            # cleared by the new load's _begin_load).
            self._load_report(self._rp_idx, False)
        elif pane.id == "pane-overview" and "overview" in self._stranded:
            self._load_overview(self._ov_period(), False)
        elif pane.id == "pane-quota" and not self._qp_started:
            # Lazy-load (round 2): the quota readers are in-process-only and
            # fast, but the collector import + the DB read still don't belong
            # on the Overview startup path — same first-activation rule.
            self._qp_started = True
            self._load_quota(False)
        elif pane.id == "pane-quota" and "quota" in self._stranded:
            self._load_quota(False)
        self._update_status()

    # ------------------------------------------------------------- jobs -----
    # Non-async @work methods MUST be thread=True (8.x WorkerDeclarationError
    # otherwise). These run the blocking fetches OFF the event loop; they never
    # touch widgets — only the supervisors paint.

    @work(thread=True, exit_on_error=False)
    def _ov_usage_job(
        self, period: str, date_from: str | None, date_to: str | None,
        refresh: bool,
    ) -> FetchOutcome:
        # period/date_from/date_to come from resolve_overview_period — the
        # warmer's exact call shape, never the raw user token.
        return fetch_usage(period, date_from, date_to, refresh=refresh)

    @work(thread=True, exit_on_error=False)
    def _ov_active_job(
        self, period: str, date_from: str | None, date_to: str | None,
        refresh: bool,
    ) -> FetchOutcome:
        # Overview passes include_review_sessions=None = route default key
        # (api.py:439); Report pins True. Never mixed (spec §11.10).
        return fetch_active_time(
            period, date_from, date_to, None, refresh=refresh
        )

    @work(thread=True, exit_on_error=False)
    def _ov_stats_job(self, refresh: bool) -> FetchOutcome:
        # None = TRAILING 365 days — round 3 feeds ONLY the week/month charts
        # (the band + daily sparkline left the Overview with round 3); the
        # year heatmap reads the calendar-year fetch below, never this window.
        return fetch_stats(None, refresh=refresh)

    @work(thread=True, exit_on_error=False)
    def _ov_year_stats_job(self, year: int, refresh: bool) -> FetchOutcome:
        # The round's ONLY additional fetch, and it fires for exactly one
        # token: the year heatmap needs the CALENDAR year's contributions
        # (the trailing-365 window is not the year's grid, whatever it covers).
        return fetch_stats(year, refresh=refresh)

    @work(thread=True, exit_on_error=False)
    def _qp_state_job(self, refresh: bool) -> FetchOutcome:
        # Always in-process (locked): quota_state() is lazy-imported inside
        # data.py so only this pane ever pays the collector stack.
        return fetch_quota_state(refresh=refresh)

    @work(thread=True, exit_on_error=False)
    def _qp_history_job(self) -> FetchOutcome:
        return fetch_quota_history(hours=24)

    @work(thread=True, exit_on_error=False)
    def _qp_poll_job(self) -> dict:
        # Lazy import mirrors cli.py: the quota poll command calls exactly
        # this function with the same network opt-in. The TUI never polls on
        # a timer — reaching here requires a pressed "u".
        from ..cli import _quota_poll_once

        return _quota_poll_once(include_network=True)

    @work(thread=True, exit_on_error=False)
    def _db_summary_job(self) -> str:
        # The db footer opens SQLite (and on a big store the row count is NOT
        # free) — it runs HERE, never on the event loop (round 6: the status
        # tick used to call db_summary inline the first time it painted).
        # The job maps its own failure to the footer string; it never raises.
        try:
            return db_summary()
        except Exception as exc:  # noqa: BLE001 — status/footer never die
            return f"db status unavailable: {exc}"

    @work(group="dbline", exclusive=True, exit_on_error=False)
    async def _refresh_db_line(self) -> None:
        # Supervisor: fill the slot off-loop, then repaint the two surfaces
        # that read it. Exclusive group = a refresh while a read is in flight
        # coalesces to one watcher; late answers are harmless (the strings
        # are equally valid, last writer wins).
        line = await self._db_summary_job().wait()
        self._db_line = line
        self._update_status()
        # Repaint the report footer ONLY while the pane is idle: _rp_state is
        # sticky across reloads, and repainting mid-reload would re-emit the
        # PREVIOUS window's body right on top of the loading clear (the
        # round-4 law). A load in flight repaints the footer from this slot
        # itself when it next paints — nothing is lost by skipping here.
        if (self._rp_usage is not None and self._rp_state == "ok"
                and "report" not in self._inflight):
            self._paint_report()  # the report footer carries the same line

    # The three report jobs take the RESOLVED (from, to) pair, not the index:
    # the window is resolved ONCE in _load_report and every job of one load
    # reads the identical pair (the date-shift rule — no mid-load shift can
    # split a load's usage-vs-insights-vs-active windows).
    @work(thread=True, exit_on_error=False)
    def _rp_usage_job(self, date_from: str, date_to: str, refresh: bool) -> FetchOutcome:
        return fetch_usage("today", date_from, date_to, refresh=refresh)

    @work(thread=True, exit_on_error=False)
    def _rp_insights_job(self, date_from: str, date_to: str, refresh: bool) -> FetchOutcome:
        # The exact warm triple: insights("year", f, t, REPORT_FACETS, True) —
        # period "year" is the route default the warmer primes, never the
        # user's token; facets verbatim (order IS part of the key).
        return fetch_insights(
            "year", date_from, date_to, REPORT_FACETS, True, refresh=refresh
        )

    @work(thread=True, exit_on_error=False)
    def _rp_active_job(self, date_from: str, date_to: str, refresh: bool) -> FetchOutcome:
        # Report pins include_review_sessions=True (web + warmer key
        # api.py:504-508).
        return fetch_active_time("today", date_from, date_to, True, refresh=refresh)

    # --------------------------------------------------------- supervisors --
    # Serial, not gathered — the measured web order (usage → paint → next →
    # paint; index.html:14399-14408: parallel computes contend on the GIL and
    # serial won 1.1s vs 16.4s first-paint). Every await is followed by the
    # generation check before any widget is touched.

    @work(group="ov", exclusive=True, exit_on_error=False)
    async def _load_overview(self, token: str, refresh: bool) -> None:
        gen = self._gen
        self._begin_load("overview", gen)
        # Resolve the window ONCE per load (never re-read inside thread jobs):
        # a shift that lands mid-flight cannot mis-pair this load's
        # usage-vs-insights-vs-active windows. today/shift are read exactly
        # here; every job below uses the returned pair.
        # Normalize at the call site (round 2): week/month/year become the
        # calendar pairs the warmer uses, "today" the explicit (D, D) pair —
        # everything else passes through. Stored for the chart dispatch too
        # (week/month render the resolved window, not the raw token). Shift 0
        # is the exact unshifted (round-2 warm-key parity) pair; shift > 0
        # steps WHOLE calendar periods back (see resolve_overview_period).
        period, date_from, date_to = resolve_overview_period(
            token, today=self._today, shift=self._period_shift
        )
        self._ov_pair = (date_from, date_to)
        # Anchor = the resolved window's END — the year heatmap's calendar-year
        # cut and today-marker read this, so shifting to a past period moves
        # the heatmap with the data instead of staying on this year. Shift 0
        # on today-pinned windows ends at today: unchanged.
        anchor = date.fromisoformat(date_to) if date_to else self._today
        self._ov_anchor = anchor
        outcomes: list[FetchOutcome] = []
        try:
            try:
                u = await self._ov_usage_job(
                    period, date_from, date_to, refresh
                ).wait()
                if gen != self._gen:
                    return
                self._ov_usage = u.value
                outcomes.append(u)
                self._paint_ov_range()
                self._paint_ov_tables()
                self._paint_ov_detail()
                self._paint_ov_kpis()
                a = await self._ov_active_job(
                    period, date_from, date_to, refresh
                ).wait()
                if gen != self._gen:
                    return
                self._ov_active = a.value
                outcomes.append(a)
                self._paint_ov_kpis()  # agent-KPI joins the active-time payload
                self._paint_ov_tables()  # Time column joins the active payload
                # Round 4: the detail table's section rows joined the same
                # active payload for their Time column — repaint with it.
                self._paint_ov_detail()
                s = await self._ov_stats_job(refresh).wait()
                if gen != self._gen:
                    return
                self._ov_stats = s.value
                outcomes.append(s)
                # (the trailing-365 "band" + daily sparkline left the Overview
                # in round 3; the stats fetch STAYS — week/month charts read
                # its contributions, filtered to the local window.)
                if token == "year":
                    # EXTRA job, year token ONLY (mutation-locked): the
                    # heatmap is the calendar year's, not trailing-365's —
                    # and "the year" is the ANCHOR's year, or a date-shift
                    # across New Year would render the wrong CY.
                    ys = await self._ov_year_stats_job(
                        anchor.year, refresh
                    ).wait()
                    if gen != self._gen:
                        return
                    self._ov_year_stats = ys.value
                    outcomes.append(ys)
                self._paint_ov_chart()
            except Exception as exc:  # noqa: BLE001 — §7 matrix classifies below
                if gen == self._gen:
                    self._pane_fail("overview", exc)
                return
            if gen == self._gen:
                # else superseded mid-flight: the newer supervisor owns pane
                # and status; this result is dropped silently.
                self._ov_state = "ok"
                self._finish_load("overview", gen, outcomes)
        finally:
            self._end_load("overview", gen)

    @work(group="rp", exclusive=True, exit_on_error=False)
    async def _load_report(self, idx: int, refresh: bool) -> None:
        gen = self._gen
        self._begin_load("report", gen)
        # Resolve the window ONCE per load (every job of this load uses the
        # same pair — the shift rule). Shift 0 = today's window = the warm
        # keys; shift > 0 steps WHOLE calendar periods back.
        date_from, date_to = resolve_report_period(
            REPORT_PERIODS[idx], today=self._today, shift=self._period_shift
        )[1:]
        outcomes: list[FetchOutcome] = []
        try:
            try:
                u = await self._rp_usage_job(date_from, date_to, refresh).wait()
                if gen != self._gen:
                    return
                self._rp_usage = u.value
                outcomes.append(u)
                self._paint_report()  # progressive: re-emit as more sources fill
                i = await self._rp_insights_job(date_from, date_to, refresh).wait()
                if gen != self._gen:
                    return
                self._rp_insights = i.value
                outcomes.append(i)
                self._paint_report()
                a = await self._rp_active_job(date_from, date_to, refresh).wait()
                if gen != self._gen:
                    return
                self._rp_active = a.value
                outcomes.append(a)
                self._paint_report()
            except Exception as exc:  # noqa: BLE001 — §7 matrix classifies below
                if gen == self._gen:
                    self._pane_fail("report", exc)
                return
            if gen == self._gen:
                self._rp_state = "ok"
                self._finish_load("report", gen, outcomes)
        finally:
            self._end_load("report", gen)

    @work(group="qp", exclusive=True, exit_on_error=False)
    async def _load_quota(self, refresh: bool) -> None:
        gen = self._gen
        self._begin_load("quota", gen)
        outcomes: list[FetchOutcome] = []
        try:
            try:
                st = await self._qp_state_job(refresh).wait()
                if gen != self._gen:
                    return
                self._quota_state_val = st.value
                outcomes.append(st)
                # Progressive paint while the trend read runs: with history
                # still None every 24h cell is an em-dash, then the repaint
                # below fills the sparks.
                self._paint_quota()
                h = await self._qp_history_job().wait()
                if gen != self._gen:
                    return
                self._quota_history_val = h.value
                outcomes.append(h)
                self._paint_quota()
            except Exception as exc:  # noqa: BLE001 — §7 matrix classifies below
                if gen == self._gen:
                    self._pane_fail("quota", exc)
                return
            if gen == self._gen:
                self._qp_state = "ok"
                self._finish_load("quota", gen, outcomes)
        finally:
            self._end_load("quota", gen)

    @work(group="qpoll", exclusive=False, exit_on_error=False)
    async def _poll_quota(self) -> None:
        # action_poll_quota already set self._polling and painted "polling…".
        # _polling is cleared in the FINALLY on every path — a leaked flag
        # would dead-key the pane.
        gen = self._gen
        try:
            try:
                result = await self._qp_poll_job().wait()
            except Exception as exc:  # noqa: BLE001 — a failed poll is a note, never a dead app
                if gen == self._gen:
                    err = getattr(exc, "error", exc)
                    self.query_one("#quota-note", Static).update(
                        "[red]poll failed: "
                        + str(err).replace("[", "\\[")
                        + "[/]"
                    )
                return
            if gen != self._gen:
                return  # a refresh/rollover owns the pane now; no reload storm
            if result.get("disabled"):
                # Never a fake "ok": tracking off means say-so and stop.
                # The note goes through emit_markup like every other pane
                # text: "warn" is a SEMANTIC run name, not a Textual style —
                # written raw it painted as a literal "[warn]" tag.
                self.query_one("#quota-note", Static).update(
                    emit_markup([[
                        Run("quota tracking disabled — tokdash quota consent",
                            "warn")
                    ]])
                )
                return
            self.query_one("#quota-note", Static).update("poll ok")
            self._load_quota(True)  # the poll's snapshots are the fresh data
        finally:
            self._polling = False

    # ------------------------------------------------------------ painting --

    def _paint_ov_range(self) -> None:
        rng = (self._ov_usage or {}).get("range") or {}
        text = window_line(
            {
                "from": rng.get("from") or EM_DASH,
                "to": rng.get("to") or EM_DASH,
                "days": rng.get("days") if rng.get("days") is not None else EM_DASH,
                "recognized": bool(rng.get("recognized", True)),
            }
        )
        # Round 3: the date line IS the section header — reverse-video fill
        # (the same "header" style the Report body's first line carries, so the
        # two panes agree). It goes through emit_markup: that is where the
        # SEMANTIC "header" style becomes real markup ([header] is NOT a Textual
        # style name — writing it raw is a MarkupError), and where the text is
        # escaped, so pass it RAW. A date shift is shown inline so the filled
        # header always names the exact window on screen. The suffix shows ONLY
        # when the Overview window itself is shifted (a windowless "all"/rolling
        # token is never stepped, even while the Report pane is) and counts in
        # that token's own unit — "viewing 1m back" on month view, not "1d".
        unit = self._shift_unit(self._ov_period())
        if self._period_shift and unit:
            text += f"  (viewing {self._period_shift}{unit} back · 0 = today)"
        self.query_one("#ov-range", Static).update(
            emit_markup([[Run(text, "header")]])
        )

    def _ov_delta(self, pct: float | None) -> str:
        # surface="overview": up = RED, down = GREEN (web renderDelta,
        # index.html:8323) — the OPPOSITE of the Report tab, on purpose (§4).
        return emit_markup([[fmt_delta(pct, surface="overview")]])

    def _paint_ov_kpis(self) -> None:
        usage = self._ov_usage or {}
        comparison = usage.get("comparison") or {}
        active = self._ov_active or {}
        active_cmp = active.get("comparison") or {}
        top = (usage.get("top_models") or [{}])[0]
        table = Table(
            box=None, show_header=True, header_style="bold", pad_edge=False,
            padding=(0, 2),
        )
        for heading in ("Tokens", "Cost", "Msgs", "Agent sum", "Cache hit", "Top model"):
            table.add_column(heading)
        # Agent sum = active_ms_sum (summed per-tool agent time, NOT the
        # clock-union active_ms) — the column header carries the label.
        table.add_row(
            f"{fmt_tokens(usage.get('total_tokens'))}\n{self._ov_delta(comparison.get('tokens_pct'))}",
            f"{fmt_cost_headline(usage.get('total_cost'))}\n{self._ov_delta(comparison.get('cost_pct'))}",
            f"{fmt_int(usage.get('total_messages'))}\n{self._ov_delta(comparison.get('messages_pct'))}",
            f"{fmt_duration(active.get('active_ms_sum'))}\n{self._ov_delta(active_cmp.get('active_ms_sum_pct'))}",
            fmt_hit(usage.get("cache_hit_rate")),
            f"{escape(str(top.get('name') or EM_DASH))}\n{fmt_cost_row(top.get('cost'))}",
        )
        self.query_one("#ov-kpis", Static).update(table)

    def _paint_ov_chart(self) -> None:
        """Adaptive period visual, dispatched by TOKEN — always LOCAL to the
        resolved window:
        today → NO chart (slot hidden; per-tool time is the tools-table Time
                  column now), week → day bars over the resolved calendar week,
        month → heat grid, year → heatmap from the EXTRA calendar-year fetch,
        all/other → NO chart (the trailing sparkline left with round 3)."""
        token = self._ov_period()
        chart = self.query_one("#ov-chart", Static)
        glyphs = _safe_glyphs()
        contribs = (self._ov_stats or {}).get("contributions") or []
        date_from, date_to = self._ov_pair
        if token == "week":
            seg = day_bars(
                contribs, str(date_from), str(date_to),
                width=_CHART_BAR_WIDTH, glyphs=glyphs,
            )
        elif token == "month":
            seg = month_grid(
                contribs, str(date_from), str(date_to), glyphs=glyphs
            )
        elif token == "year":
            # An empty contribs list still paints the grid skeleton + the
            # "no activity" line — a measured zero, not a missing figure. The
            # year/cut read the load's ANCHOR (a shift across New Year would
            # otherwise draw the wrong CY).
            seg = year_heatmap(
                (self._ov_year_stats or {}).get("contributions") or [],
                self._ov_anchor.year,
                today=self._ov_anchor.isoformat(),
                glyphs=glyphs,
            )
        else:
            # today / all / anything else: no chart this round.
            chart.update("")
            chart.display = False
            return
        chart.update(emit_markup(seg))
        chart.display = True

    def _paint_ov_detail(self) -> None:
        # Tool + Model, sectioned by tool (round 2) — charts.tool_model_detail
        # is the pure renderer; PRESENTATION happens HERE, by row kind.
        #
        # The two escaping paths are deliberately different and must not be
        # "unified": a str cell is markup-parsed by the DataTable, so user
        # data (model names, section ids) enters as escape(str); a Text cell
        # is LITERAL (no markup parse), so the pill/justify Text cells carry
        # their raw strings.
        table = self.query_one("#ov-detail", DataTable)
        table.clear()
        # Round 3: EVERY model listed (no cap); round 4: the full field set,
        # the active-time join for section rows, and a background pill on the
        # section names (the user asked to "separate tools for readibility").
        # The label map is passed IN (charts never imports the backend).
        rows = tool_model_detail(
            self._ov_usage or {},
            labels=TOOL_LABELS,
            active=(self._ov_active or {}).get("by_tool"),
        )
        # Pill width (round 5): pad to _COL_NAME — the name column's fixed
        # width — so the fills form a full-width band per tool, not ragged
        # snags, and the band width matches the other two tables' name
        # column. (Round 4 padded to the widest section name; the shared
        # grid made that moot and a name longer than _COL_NAME crops.)
        for kind, name, tok_in, tok_out, cache, total, hit, cost, msgs, time in rows:
            if kind == "tool":
                name_cell = Text(
                    name.ljust(_COL_NAME), style="bold on #334155"
                )
            elif kind == "model":
                name_cell = escape(str(name))
            else:  # "more" overflow row (only with an explicit top_n)
                name_cell = escape(str(name))
            table.add_row(
                name_cell,
                *(_num(c) for c in (tok_in, tok_out, cache, total, hit,
                                    cost, msgs, time)),
            )

    def _paint_ov_tables(self) -> None:
        usage = self._ov_usage or {}
        apps = usage.get("apps") or {}
        active_by = (self._ov_active or {}).get("by_tool") or {}

        tools = self.query_one("#ov-tools", DataTable)
        tools.clear()
        for tool, row in sorted(
            (usage.get("by_tool") or {}).items(),
            key=lambda kv: -(kv[1].get("tokens") or 0),
        ):
            app = apps.get(tool) or {}  # richer fields; ABSENT for openclaw
            # exact-key-then-fold join lives in charts.active_time_ms (one
            # helper for every Time cell: tools table AND detail sections).
            ms = active_time_ms(active_by, tool)
            # ROUND-3 OVERRIDE of the null-vs-zero law for the Time column
            # ONLY: the user asked that a "0s" agent never headline the table,
            # so a MEASURED 0 prints EM_DASH here — a deliberate divergence from
            # the report agents table (report.py), which keeps "0s". Locked by
            # two tests; do not "unify" them.
            tools.add_row(
                escape(tool_label(str(tool), TOOL_LABELS)),
                *(_num(c) for c in (
                    fmt_tokens(app.get("tokens_in", row.get("tokens_in"))),
                    fmt_tokens(app.get("tokens_out")),
                    fmt_tokens(app.get("tokens_cache", row.get("tokens_cache"))),
                    fmt_tokens(row.get("tokens")),
                    fmt_hit(row.get("cache_hit_rate")),
                    fmt_cost_row(row.get("cost")),
                    fmt_int(app.get("messages")),
                    fmt_duration(ms) if ms else EM_DASH,
                )),
            )
        models = self.query_one("#ov-models", DataTable)
        models.clear()
        # Cost desc over the full ranking, then capped at 15 — never the 300
        # rows the store can return (compute slices combined_models[:15] too).
        for row in sorted(
            usage.get("combined_models") or [], key=lambda r: -(r.get("cost") or 0)
        )[:15]:
            models.add_row(
                escape(str(row.get("name") or EM_DASH)),
                *(_num(c) for c in (
                    fmt_tokens(row.get("tokens_in")),
                    fmt_tokens(row.get("tokens_out")),
                    fmt_tokens(row.get("tokens_cache")),
                    fmt_tokens(row.get("tokens")),
                    fmt_hit(row.get("cache_hit_rate")),
                    fmt_cost_row(row.get("cost")),
                    fmt_int(row.get("messages")),
                )),
            )

    def _paint_quota(self) -> None:
        # charts.quota_table owns the card semantics (remaining-not-used,
        # window normalization, account folding); the app owns the escaping
        # and the note tail. _quota_history_val None = the trend read has
        # not landed yet → every 24h cell is an em-dash by design.
        banner, rows, note, credit_lines = quota_table(
            self._quota_state_val or {},
            self._quota_history_val,
            now_ts=time.time(),
            glyphs=_safe_glyphs(),
            # Round 4: credit rows name their provider ("Codex", "Claude
            # Code@academic") — the label map arrives as data, charts stays
            # backend-free.
            labels=TOOL_LABELS,
        )
        segs = list(banner)
        if note:
            segs.append([Run(note, "muted")])
        self.query_one("#quota-note", Static).update(emit_markup(segs))
        table = self.query_one("#quota-table", DataTable)
        table.clear()
        for row in rows:
            cells = [escape(str(cell)) for cell in row]
            # Companion color on the status cell: ok GREEN, everything the
            # companion turns RED ("stale_token", "unavailable", "error").
            # Round 5: an EMPTY status means a provider-gap spacer row —
            # those stay plain, never a red nothing.
            status = str(row[-1])
            if status:
                cells[-1] = Text(status, style="green" if status == "ok" else "red")
            table.add_row(*cells)
        # Credits are their own section below the table, hidden when empty.
        credits = self.query_one("#quota-credits", Static)
        credits.display = bool(credit_lines)
        if credit_lines:
            credits.update(emit_markup(credit_lines))

    def _ensure_db_line(self) -> str:
        # PURE READ (round 6): the SQLite open/count happens in
        # _db_summary_job's thread; the watcher fills the slot and repaints.
        # Callers run on the event loop (every status tick, the report
        # footer) and must never block on the store. Until the first read
        # lands, the footer says so — a beat at startup, never a freeze.
        return self._db_line if self._db_line is not None else "db status pending…"

    def _paint_report(self) -> None:
        if self._rp_usage is None:
            return
        rng = self._rp_usage.get("range") or {}
        window = {
            "period": REPORT_PERIODS[self._rp_idx],
            "from": rng.get("from"),
            "to": rng.get("to"),
            "days": rng.get("days"),
            "timezone": (self._rp_insights or {}).get("timezone"),
            # A None source during progressive paints means STILL RUNNING (a
            # soft-fail in this pane would have replaced the whole body via
            # _pane_fail) — the builder must say "running…", not "scan failed".
            "pending": self._rp_insights is None or self._rp_active is None,
            "db_line": self._ensure_db_line(),
        }
        # ONE Static fed by the SAME builder the piped report uses — the two
        # surfaces structurally cannot drift. Mid-load paints with
        # insights/active still None carry the builder's own warn lines; the
        # final re-emit replaces them. Width is the PANE's content width
        # (recomputed every repaint, clamped 60..200, 80 un-laid-out) — the
        # fixed 72 of round 1 wasted real terminals and clipped wide ones.
        markup = emit_markup(
            build_report_segments(
                self._rp_usage,
                self._rp_insights,
                self._rp_active,
                window=window,
                width=_clamp_report_width(
                    self.query_one("#pane-report").content_region.width
                ),
                glyphs=_safe_glyphs(),
            )
        )
        self.query_one("#rp-body", Static).update(markup)

    # ------------------------------------------------------------- status ---

    def _ov_period(self) -> str:
        return (
            OVERVIEW_PERIODS[self._ov_idx]
            if self._ov_idx >= 0
            else self._initial_period
        )

    def _active_pane(self) -> str:
        tabs = self.query_one("#tabs", TabbedContent)
        if tabs.active == "pane-report":
            return "report"
        if tabs.active == "pane-quota":
            return "quota"
        return "overview"

    def _update_status(self) -> None:
        pane = self._active_pane()
        # Per-pane key hints — the round-4 COMPACT ECHO of the spelled-out
        # legend pinned at the top of each pane ("[ ]" never rendered as a
        # bare bracket pair: here the keys get words, "shift period"). The
        # period keys live on BOTH period panes, the poll key only on Quota.
        if pane == "overview":
            label, hint = self._ov_period(), " · t/w/m/y/a period · [ ] shift period · 0 today"
        elif pane == "report":
            label, hint = REPORT_PERIODS[self._rp_idx], " · w/m/y period · [ ] shift period · 0 today"
        else:
            label, hint = "quota", " · u poll"
        # Shift marker (ASCII — the cp1252-safety law): only while THIS pane's
        # window is shifted, and counted in the pane's own period unit —
        # month view "-1m" (one month back), week "-1w", today "-1d". Quota
        # has no date axis, so it never shows one. The common status line
        # stays byte-identical to round 2 at shift 0.
        shift_token = (
            self._ov_period() if pane == "overview"
            else REPORT_PERIODS[self._rp_idx] if pane == "report"
            else ""
        )
        unit = self._shift_unit(shift_token)
        shift = f" · -{self._period_shift}{unit}" if (self._period_shift and unit) else ""
        # escape(hint): the report/overview hint carries a literal "[/]" (the
        # date-shift key), which Textual would otherwise read as an auto-closing
        # markup tag. The line is plain text — no markup is intended here.
        line = (
            f"{pane.capitalize()} · {label}{escape(hint)}{shift} · "
            f"{self._cache_str.get(pane, 'cache —')} · {escape(self._ensure_db_line())}"
        )
        started = self._load_started.get(pane)
        if started is not None:
            line += f" · computing… {int(time.monotonic() - started)}s"
        self.query_one("#status", Static).update(line)

    def _tick_status(self) -> None:
        # set_interval keeps running; it is a no-op while nothing is in flight.
        if self._load_started:
            self._update_status()

    def _paint_loading(self, pane: str) -> None:
        # Round-4 loading law (user request): a reload NEVER leaves the
        # PREVIOUS window's figures standing behind the new one — the moment
        # a load starts, the pane clears and shows a dim "computing…".
        # First run (and an error-stated pane) keep the longer _SKELETON
        # copy instead: nothing was painted yet, and the wait deserves the
        # explanation. Clearing belongs to the NEWEST load; a superseded
        # result still dies at the gen guards, so a cleared pane is refilled
        # exactly once, by the load that owns it. (The status bar already
        # carries "computing… Ns" via _update_status/_tick_status.)
        # The WIDGETS clear on every load — an error-stated pane can still
        # have stale tables behind its red line (only the note/range row is
        # replaced on failure), and old figures must not survive into a new
        # load either way. Only the notice WORDING knows the pane's state:
        # nothing-ever-painted (first run) earns the full explanation.
        if pane == "overview":
            if self._ov_state == "ok":
                self.query_one("#ov-range", Static).update(
                    emit_markup([[Run("computing…", "muted")]])
                )
            else:
                self.query_one("#ov-range", Static).update(_SKELETON)
            self.query_one("#ov-kpis", Static).update("")
            chart = self.query_one("#ov-chart", Static)
            chart.update("")
            chart.display = False
            for tid in ("#ov-tools", "#ov-models", "#ov-detail"):
                self.query_one(tid, DataTable).clear()
        elif pane == "quota":
            if self._qp_state == "ok":
                self.query_one("#quota-note", Static).update(
                    emit_markup([[Run("computing…", "muted")]])
                )
            else:
                self.query_one("#quota-note", Static).update(_SKELETON)
            self.query_one("#quota-table", DataTable).clear()
            credits = self.query_one("#quota-credits", Static)
            credits.update("")
            credits.display = False
        else:
            # rp-body is ONE Static — its text IS the content, so the notice
            # and the clear are the same update.
            if self._rp_state == "ok":
                self.query_one("#rp-body", Static).update(
                    emit_markup([[Run("computing…", "muted")]])
                )
            else:
                self.query_one("#rp-body", Static).update(_SKELETON)

    def _begin_load(self, pane: str, gen: int) -> None:
        self._inflight[pane] = gen
        self._stranded.discard(pane)  # this load owns the pane from now on
        # Drop the PREVIOUS window's late-arriving payloads (round-4 loading
        # law: a reload shows computing… and NOTHING stale). Usage repaints
        # mid-load and reads these slots — left filled, the KPIs/Time/insights
        # would mix last window's figures into the window being computed.
        # Paint sites all treat None as "dash / pending / running…".
        if pane == "overview":
            self._ov_active = None
        elif pane == "report":
            self._rp_insights = None
            self._rp_active = None
        elif pane == "quota":
            self._quota_history_val = None
        self._load_started[pane] = time.monotonic()
        self._paint_loading(pane)
        self._update_status()

    def _reload_pane(self, pane: str) -> None:
        """(Re-)start the load for one pane at the CURRENT state."""
        if pane == "overview":
            self._load_overview(self._ov_period(), False)
        elif pane == "report":
            self._load_report(self._rp_idx, False)
        elif pane == "quota":
            self._load_quota(False)

    def _end_load(self, pane: str, gen: int) -> None:
        if self._inflight.get(pane) == gen:  # a newer load owns the spinner now
            self._inflight.pop(pane, None)
            self._load_started.pop(pane, None)
            if gen != self._gen:
                # `_inflight.get(pane) == gen` (above) proves no successor load
                # took ownership, and gen moved while this load ran: the guard
                # dropped it without a replacement for this pane. A STRANDED
                # pane the user is looking at gets its load re-issued right now
                # (supersede bumped the gen without a successor for THIS pane —
                # e.g. ``p`` while a quota fetch is in flight); the re-issue
                # runs at the current gen, so it lands and cannot loop. Only a
                # HIDDEN stranded pane waits for the TabActivated hook, the one
                # re-fetch trigger a pane out of sight gets.
                self._stranded.add(pane)
                if pane == self._active_pane():
                    self._reload_pane(pane)
            self._update_status()

    def _finish_load(
        self, pane: str, gen: int, outcomes: list[FetchOutcome]
    ) -> None:
        statuses = [o.status for o in outcomes]
        # Aggregate freshness for the status bar: worst answer wins.
        if "recomputed" in statuses:
            agg = "recomputed"
        elif "stale" in statuses:
            agg = "stale"
        else:
            agg = "hit"
        ages = [o.age_seconds or 0.0 for o in outcomes if o.status == agg]
        self._cache_str[pane] = f"cache {agg} age {max(ages, default=0.0):.0f}s"
        if "stale" in statuses and (pane, gen) not in self._reread_done:
            # Mark at SCHEDULE time: if the re-read itself returns stale the
            # gen is unchanged, so the guard holds and this window-load never
            # re-schedules (spec §6, exactly one silent re-read).
            self._reread_done.add((pane, gen))
            self.set_timer(STALE_REREAD_SECONDS, lambda: self._maybe_reread(pane, gen))
        self._update_status()

    def _maybe_reread(self, pane: str, gen: int) -> None:
        if gen != self._gen:
            return  # a user action superseded this window-load meanwhile
        if pane == "overview":
            self._load_overview(self._ov_period(), False)
        elif pane == "quota":
            self._load_quota(False)
        else:
            self._load_report(self._rp_idx, False)

    # ------------------------------------------------------------- failure --

    def _pane_fail(self, pane: str, exc: Exception) -> None:
        # Worker.wait() re-raises a crashed thread job as WorkerFailed
        # wrapping the original (§6) — unwrap, then classify per §7.
        err = getattr(exc, "error", exc)
        if isinstance(err, UsageDatabaseSchemaTooNewError):
            # Terminal by design (usage_store.py:24): never pane-local, never
            # swallowed — remediation on stderr, exit 1.
            sys.stderr.write(f"{err} — run 'tokdash update'\n")
            self.exit(return_code=1)
            return
        if isinstance(err, CacheBackpressureError):
            # data.py already spent its 3 jittered retries on this key.
            msg = "busy computing — press r"
        else:
            msg = f"error: {err}"
        line = "[red]" + msg.replace("[", "\\[") + "[/]"
        if pane == "overview":
            self._ov_state = "error"
            self.query_one("#ov-range", Static).update(line)
        elif pane == "quota":
            self._qp_state = "error"
            self.query_one("#quota-note", Static).update(line)
        else:
            self._rp_state = "error"
            self.query_one("#rp-body", Static).update(line)
        self._update_status()
