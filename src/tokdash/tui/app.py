"""The Textual app + the ``tokdash tui`` entry point (revision 5).

This is the ONLY tokdash module allowed to import textual (and rich, which
textual pulls in) — the import-discipline test proves ``report``/``data``/
``formatting``/``charts``/``views``/``ranges``/``remote`` stay textual-free so a
stripped install can still run ``tokdash report``.

Layout law (revision 5): separation comes from spacing and background tone, not
from box outlines and rules. Surfaces are the navy pair from the approved
preview (#0d1118 page / #121924 panel), the active tab is a lavender fill, the
six Overview figures keep their individual accent colours, and the only rules
left in a table are the quiet header separators. Selection is a background fill;
focus is a visible outline, because this app must be operable with a keyboard
and no mouse.

Verified Textual 8.x facts (do not "fix" these back to old-Textual patterns):
there is NO ``on_<worker>_success`` result delivery, a non-async ``@work`` method
REQUIRES ``thread=True``, and results arrive only via ``await worker.wait()``,
which re-raises a crash as ``WorkerFailed`` (unwrapped with
``getattr(exc, "error", exc)``). Every decorator therefore sets
``exit_on_error=False`` — the default True would kill the app on one failed
fetch. Threads never touch widgets: blocking fetches run in thread jobs, and only
the async supervisors paint, on the event loop, behind a generation guard so a
superseded load can never stale-paint the pane.

Data law: every number on screen names the field it came from. ``views.py``
turns a payload into display rows and owns the missing-versus-zero rules;
``ranges.py`` owns what a date selection means; this module owns widgets, workers
and the order of paints.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import date, timedelta

from rich.cells import cell_len
from rich.markup import escape
from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import Key, Resize
from textual.screen import ModalScreen
from textual.worker import WorkerCancelled
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    ListView,
    ListItem,
    Static,
    TabbedContent,
    TabPane,
)

from ..api import REPORT_FACETS, CacheBackpressureError, _local_today
from ..compute import period_is_recognized
from ..sessions import SESSION_TOOLS, TOOL_LABELS
from ..usage_store import UsageDatabaseSchemaTooNewError
from .data import (
    FetchOutcome,
    db_summary,
    ensure_usage_db_compatible,
    fetch_active_time,
    fetch_insights,
    fetch_output_speed,
    fetch_output_speed_job_status,
    fetch_pricing_db,
    fetch_quota_history,
    fetch_quota_state,
    fetch_session_detail,
    fetch_session_speed,
    fetch_sessions,
    fetch_stats,
    fetch_usage,
    follow_output_speed_job,
    reset_remote,
)
from .formatting import (
    EM_DASH,
    Run,
    fmt_cost_row,
    fmt_duration,
    fmt_hit,
    fmt_int,
    fmt_tokens,
    glyph_style,
    tool_label,
)
from .charts import quota_table
from .ranges import (
    PRESETS,
    PRESET_BY_KEY,
    RANGE_KEYS,
    DateWindow,
    Preset,
    StatsWindow,
    custom_window,
    window_for,
    window_for_period,
)
from .report import build_report_segments, emit_markup
from . import views

# Web parity: USAGE_REPORT_STALE_REREAD_MS = 12000 (index.html:14469). A stale
# answer gets exactly one silent, NON-forced re-read per window-load — a forced
# refresh would start a second full recompute the stale-while-revalidate daemon
# is already doing.
STALE_REREAD_SECONDS = 12.0

# Session fan-out: one /api/sessions request per source, but no more than three
# in flight at a time (each cold read can parse a source's whole history).
SESSION_FANOUT = 3

PANI = ("overview", "sessions", "stats", "report", "speed", "quota", "pricing")
PANE_TITLES = {
    "overview": "Overview",
    "sessions": "Sessions",
    "stats": "Stats",
    "report": "Report",
    "speed": "Speed",
    "quota": "Quota",
    "pricing": "Pricing",
}
# The panes that share ONE historical range (spec 2). Stats has its own annual
# window; Quota and Pricing describe the present and have no range at all.
RANGE_PANES = ("overview", "sessions", "report", "speed")
# Panes that take the Speed source filter (independent of Sessions' filter).
SPEED_PERIOD_DEFAULT = "year"

_SKELETON = "computing… first run scans session logs and can take tens of seconds"


def _safe_glyphs() -> str:
    """glyph_style() with the _PrintCapture fallback (same utf8-or-ascii rule,
    probed on the encoding captured before Textual touched stdout)."""
    try:
        return glyph_style()
    except Exception:  # noqa: BLE001 — no .encoding on the capture shim
        return "blocks" if _STDOUT_ENCODING.lower().replace("-", "").startswith("utf") else "ascii"


# Captured at import: every Textual driver REPLACES sys.stdout with a
# _PrintCapture shim (print() must not corrupt the screen), and that shim has no
# .encoding — so the glyph probe would crash mid-run just as it does headless.
_STDOUT_ENCODING = getattr(sys.stdout, "encoding", "") or ""


def _num_cell(value: str) -> Text:
    """A formatted-digit cell, right-aligned. Textual's DataTable has no
    column-level justify, so alignment rides on each cell as a rich Text; the
    name column stays a plain (escaped) string."""
    return Text(value, justify="right")


def _clamp_report_width(raw: int) -> int:
    """Report-pane content width -> builder width: an un-laid-out pane reports 0
    (headless, or never shown) and falls back to the piped report's 80; anything
    laid out is clamped to the builder's sane 60..200 band."""
    if raw <= 0:
        return 80
    return max(60, min(200, raw))


def run_tui(args: argparse.Namespace) -> int:
    """``tokdash tui``: refuse anything that is not an interactive terminal
    BEFORE importing/starting Textual (the alt screen is not a place for error
    messages), then hand the app the validated period token."""
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
        raise SystemExit(f"Unknown period {args.period!r}.")
    app = TokdashApp(initial_period=args.period)
    app.run()
    return app.return_code or 0


class ViewPicker(ModalScreen):
    """``v`` — the fallback for terminals that eat F-keys.

    A list, not a text prompt: every view is one arrow key and Enter away, and
    nothing here can be mistyped or accidentally executed.
    """

    BINDINGS = [Binding("escape, q", "dismiss", "Close")]

    def __init__(self, panes: tuple[str, ...], active: str) -> None:
        super().__init__()
        self._panes = panes
        self._active = active

    def compose(self) -> ComposeResult:
        with Vertical(id="view-picker"):
            yield Static("Go to view", id="view-picker-title")
            yield ListView(
                *[
                    ListItem(
                        Static(
                            Text(("● " if pane == self._active else "   ") + PANE_TITLES[pane])
                        ),
                        id=f"view-{pane}",
                    )
                    for pane in self._panes
                ],
                id="view-picker-list",
            )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        item_id = getattr(item, "id", None) or ""
        self.dismiss(item_id[len("view-"):] if item_id.startswith("view-") else None)


class PresetPicker(ModalScreen):
    """The range presets that did not fit on the strip, each with its own dates.

    Every row shows the window it selects BEFORE it is chosen, so "Last Month"
    is never a guess about which month that is on this machine today.
    """

    BINDINGS = [Binding("escape, q", "dismiss", "Close")]

    def __init__(self, today: date, active: DateWindow) -> None:
        super().__init__()
        self._today = today
        self._active = active

    def compose(self) -> ComposeResult:
        with Vertical(id="view-picker"):
            yield Static("Date range", id="view-picker-title")
            yield ListView(
                *[
                    ListItem(
                        Static(
                            Text(
                                ("● " if window.preset == self._active.preset else "   ")
                                + f"{window.label:<19}{window.describe()}"
                            )
                        ),
                        id=f"preset-{window.preset}",
                    )
                    for window in (window_for(p.key, self._today) for p in PRESETS)
                ],
                id="view-picker-list",
            )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = getattr(event.item, "id", None) or ""
        self.dismiss(
            item_id[len("preset-"):] if item_id.startswith("preset-") else None
        )


class SpeedKeyPicker(ModalScreen):
    """Pick one speed comparison key (source/model/kind/basis) or merged model.

    Trend and hour-of-day are only meaningful for ONE key at a time, so the
    chooser offers exactly the keys this snapshot returned — never a
    hand-typed combination that measures nothing.
    """

    def __init__(self, options: list[tuple[str, object]]) -> None:
        super().__init__()
        self._options = options

    def compose(self) -> ComposeResult:
        with Vertical(id="view-picker"):
            yield Static("Series to chart", id="view-picker-title")
            yield ListView(
                *[
                    ListItem(Static(Text(label)), id=f"key-{index}")
                    for index, (label, _value) in enumerate(self._options)
                ],
                id="view-picker-list",
            )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        item_id = getattr(item, "id", None) or ""
        index = item_id[len("key-"):] if item_id.startswith("key-") else None
        try:
            value = self._options[int(index)][1]
        except (TypeError, ValueError, IndexError):
            value = None
        self.dismiss(value)


class TokdashApp(App):
    """Seven views over the same compute the web dashboard uses.

    The round-2 law still holds: OPTIONAL read-only HTTP delegation to a
    same-version live ``tokdash serve`` (``data.py``/``remote.py`` — GETs only,
    fail-closed to the identical in-process path on any doubt;
    ``TOKDASH_TUI_NO_REMOTE=1`` disables it). Still no server of ours, no
    auto-refresh timer (polling contends with the server's compute semaphore;
    ``r`` is the refresh), and quota NEVER leaves the process — network quota
    polling happens only on an explicit ``u``.

    Revision 5 adds Sessions, an annual Stats view, measured Speed and a read-only
    Pricing view, and corrects Overview: Models aggregates normalized model names
    across every harness with no Harness column, Cost is a SORT of that one table,
    and the activity strip is the one global series the usage payload carries.
    """

    TITLE = "tokdash"

    CSS = """
    App { background: #0d1118; color: #e3eaf3; }
    Header { background: #121924; color: #b9a7ff; }
    Footer { background: #121924; color: #a0adbf; }
    #status { height: 1; background: #121924; color: #a0adbf; padding: 0 1; }

    /* The shared scope strip: surface fill, no outlines (revision 5). */
    #rangebar { height: auto; background: #121924; padding: 0 1 0 1; }
    #rangebar Button {
        width: auto; min-width: 0;
        background: #1b2534; color: #a0adbf; border: none;
        padding: 0 1; margin: 0 1 0 0; text-style: none;
    }
    #rangebar Button:hover { background: #253044; color: #e3eaf3; }
    #rangebar Button.-active { background: #b9a7ff; color: #12121a; text-style: bold; }
    #rangebar Button:focus { background: #253044; color: #e3eaf3; text-style: bold underline; }
    #rangebar Button.-selected { background: #b9a7ff; color: #12121a; text-style: bold; }
    #range-dates {
        width: 1fr; height: 1; text-align: right; text-wrap: nowrap;
        color: #e3eaf3; background: #121924; padding: 0 0 0 2;
    }

    TabbedContent { height: 1fr; background: #0d1118; }
    Tabs { height: 1; background: #0d1118; padding: 0 1; }
    Underline { display: none; }
    Tab { background: transparent; color: #7f8da3; padding: 0 1; margin: 0 1 0 0; }
    Tab:hover { color: #e3eaf3; }
    Tab.-active { background: #b9a7ff; color: #12121a; text-style: bold; }
    TabPane { background: #0d1118; padding: 0 1; height: 1fr; overflow-y: auto; }

    /* Section separation is spacing + surface tone; the only rule left in a
       table is the header separator. */
    Section { margin-top: 1; }
    Static { background: transparent; }
    .label { color: #8fa0b8; }

    DataTable {
        height: auto; background: #121924; border: none; padding: 0 1;
    }
    DataTable > .datatable--header {
        background: #121924; color: #8fa0b8; text-style: bold;
    }
    DataTable > .datatable--cursor { background: #263141; color: #e3eaf3; text-style: none; }
    DataTable > .datatable--header-cursor { background: #263141; color: #cdd8e6; }
    DataTable:focus { background: #131c28; }

    /* A search box is one of the few places a rule still belongs: it is an
       AFFORDANCE, not decoration. The height is the border pair plus ONE text
       line: with vertical padding in between, the box is three rows tall, draws
       its frame, and the typed characters have nowhere to be painted. */
    Input {
        height: 3; width: 1fr;
        background: #16202e; color: #e3eaf3; border: round #2b3648;
        padding: 0 1;
    }
    Input:focus { border: round #b9a7ff; background: #1a2534; }
    #range-dates-input { width: 34; }
    Input.-disabled { opacity: 100%; }

    /* Textual's Horizontal defaults to height: 1fr, which turns a one-line
       toolbar into a spring: the subview row swallows the pane and shoves the
       table to the bottom. Toolbars are one row tall, always. */
    .toggle { height: auto; }
    #controls { height: auto; background: transparent; padding: 0; }
    #controls Button, .toggle Button {
        width: auto; min-width: 0;
        background: #1b2534; color: #a0adbf; border: none; padding: 0 1;
        margin: 0 1 0 0; text-style: none;
    }
    /* -active is Textual's 200 ms press flash. -selected is ours: the only
       thing that says which preset, subview or lens is in force. */
    #controls Button.-active, .toggle Button.-active,
    #controls Button.-selected, .toggle Button.-selected {
        background: #b9a7ff; color: #12121a; text-style: bold;
    }
    #controls Button:focus, .toggle Button:focus {
        background: #253044; color: #e3eaf3; text-style: bold underline;
    }

    #view-picker { padding: 1 2; background: #121924; border: round #344157; width: 60; height: auto; }
    #view-picker-title { color: #b9a7ff; text-style: bold; }
    ListView { background: #121924; height: auto; }
    ListItem:focus { background: #263141; }
    ListItem > Static { background: transparent; color: #cdd8e6; }
    ListItem:focus > Static { background: #263141; }
    """

    BINDINGS = [
        # F-keys are the stable view keys; 1/2/3 stay as legacy shortcuts for the
        # three views that had them before. Digits never pick a harness.
        Binding("f1", "show_pane('overview')", "Overview", show=False),
        Binding("f2", "show_pane('sessions')", "Sessions", show=False),
        Binding("f3", "show_pane('stats')", "Stats", show=False),
        Binding("f4", "show_pane('report')", "Report", show=False),
        Binding("f5", "show_pane('speed')", "Speed", show=False),
        Binding("f6", "show_pane('quota')", "Quota", show=False),
        Binding("f7", "show_pane('pricing')", "Pricing", show=False),
        Binding("1", "show_pane('overview')", "Overview", show=False),
        Binding("2", "show_pane('report')", "Report", show=False),
        Binding("3", "show_pane('quota')", "Quota", show=False),
        Binding("v", "view_picker", "Go to view", show=False),
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Reload"),
        # Shared historical range. Letters are inert while an input has focus,
        # which is what lets a search box accept "q", "r" and digits.
        Binding("t", "set_range('today')", "Today", show=False),
        Binding("d", "set_range('yesterday')", "Yesterday", show=False),
        Binding("w", "set_range('7d')", "7 days", show=False),
        Binding("m", "set_range('month')", "Month", show=False),
        Binding("y", "set_range('year')", "Year", show=False),
        Binding("a", "set_range('all')", "All", show=False),
        Binding("left_square_bracket", "shift_range(-1)", "Earlier", show=False),
        Binding("right_square_bracket", "shift_range(1)", "Later", show=False),
        Binding("0", "reset_range", "Today", show=False),
        Binding("c", "custom_range", "Custom dates", show=False),
        # Per-pane keys. ``s`` sorts the focused table, ``/`` searches it, ``h``
        # picks sources in Sessions and Speed only, ``p`` moves Stats' year.
        Binding("slash", "begin_search", "Search", show=False),
        Binding("s", "cycle_sort", "Sort", show=False),
        Binding("h", "choose_sources", "Sources", show=False),
        Binding("p", "stats_year", "Stats year", show=False),
        Binding("k", "choose_series", "Series", show=False),
        Binding("b", "cycle_bucket", "Bucket", show=False),
        Binding("enter", "open_row", "Open", show=False),
        Binding("escape", "close_detail", "Close", show=False),
        Binding("u", "poll_quota", "Poll quota", show=False),
        Binding("question_mark", "show_help_panel", "Keys"),
    ]

    def __init__(self, initial_period: str = "today", **kwargs) -> None:
        super().__init__(**kwargs)
        self._today = _local_today()
        self._initial_period = initial_period
        # The one shared historical range (Overview/Sessions/Report/Speed). The
        # default is Today, matching the shipped TUI; the preview's seven-day
        # window was illustrative only. ``--period`` is honoured here rather than
        # dropped on the floor: run_tui validates the token, and a length the
        # routes would accept must open on the same dates they would report.
        self._win: DateWindow = window_for_period(initial_period, self._today)
        self._stats_win = StatsWindow(None)  # trailing 365 days
        self._stats_payload: dict | None = None
        self._stats_selected: str | None = None
        # Only used at 80 columns, where the year is one month at a time.
        self._stats_month: int | None = None
        # Generation guard: bumped by every action that (re)starts a load.
        # Supervisors snapshot it and drop their paint the moment it moves.
        self._gen = 0
        self._state = dict.fromkeys(PANI, "empty")   # empty | ok | error
        self._started = dict.fromkeys(PANI, False)   # lazy first load
        self._inflight: dict[str, int] = {}
        # Loads still running per pane. A pane can briefly hold two (a cancelled
        # twin and its successor), so "is anything still loading here?" is a
        # count, never a single generation number.
        self._open_loads: dict[str, int] = dict.fromkeys(PANI, 0)
        self._stranded: set[str] = set()
        self._load_started: dict[str, float] = {}
        self._cache_str: dict[str, str] = {}
        self._cache_status: dict[str, str] = {}
        self._reread_done: set[tuple[str, int]] = set()
        self._db_line: str | None = None
        self._polling = False
        # Overview
        self._ov_usage: dict | None = None
        self._ov_active: dict | None = None
        self._ov_subview = "harnesses"
        self._ov_sort = "tokens"
        self._ov_search = ""
        self._ov_selected: object | None = None
        self._custom_mode = False
        # Sessions
        self._sn_sources: tuple[str, ...] = tuple(SESSION_TOOLS)
        self._sn_payloads: dict[str, dict] = {}
        self._sn_failed: list[str] = []
        self._sn_search = ""
        self._sn_selected_key: tuple[str, str] | None = None
        self._sn_sort = "last_seen"
        self._sn_detail: dict | None = None
        self._sn_detail_key: tuple[str, str] | None = None
        self._sn_detail_speed: dict | None = None
        self._sn_detail_note = ""
        self._sn_review: bool | None = None  # None = the server's own default
        # Report
        self._rp_usage: dict | None = None
        self._rp_insights: dict | None = None
        self._rp_active: dict | None = None
        # Soft-failure text for the two facet scans, carried into the body.
        self._rp_warnings: list[str] = []
        self._rp_section = "summary"
        self._rp_project_sort = "tokens"
        # Speed
        self._sp_mode = "across"
        self._sp_lens = "stream"
        self._sp_source: str | None = None
        self._sp_bucket = "auto"
        self._sp_include_windows = False
        self._sp_payload: dict | None = None
        self._sp_trend: dict | None = None
        self._sp_hours: dict | None = None
        self._sp_key: tuple | None = None
        self._sp_job: dict | None = None
        self._sp_job_note = ""
        self._sp_following = False
        # Last state the build stream reported, and the build this pane stopped
        # watching (see _stop_watching). Both are per-job, never global.
        self._sp_job_state = ""
        self._sp_gaveup: str | None = None
        self._sp_gaveup_note = ""
        # Quota / Pricing
        self._quota_state_val: dict | None = None
        self._quota_history_val: dict | None = None
        self._quota_detail: str | None = None
        self._pricing: dict | None = None
        self._pr_search = ""

    # ------------------------------------------------------------------ UI --

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="status")
        with Horizontal(id="rangebar"):
            for preset in PRESETS:
                yield Button(preset.short, id=f"range-{preset.key}", variant="default")
            yield Button("More...", id="range-more", variant="default")
            yield Static("", id="range-dates")
            yield Input(placeholder="YYYY-MM-DD .. YYYY-MM-DD", id="range-dates-input")
        with TabbedContent(initial="pane-overview", id="tabs"):
            with TabPane("Overview", id="pane-overview"):
                yield Static("", id="ov-range")
                yield Static("", id="ov-note")
                yield Static("", id="ov-cards")
                with Horizontal(id="ov-subview", classes="toggle"):
                    yield Button("Harnesses", id="ov-tab-harnesses")
                    yield Button("Models", id="ov-tab-models")
                yield Input(placeholder="filter names", id="ov-search")
                yield DataTable(id="ov-table", cursor_type="row", zebra_stripes=False)
                yield Static("", id="ov-detail")
                yield Static("", id="ov-activity")
            with TabPane("Sessions", id="pane-sessions"):
                yield Static("", id="sn-note")
                yield Input(placeholder="search project, id, model", id="sn-search")
                yield DataTable(id="sn-table", cursor_type="row", zebra_stripes=False)
                yield Static("", id="sn-detail")
            with TabPane("Stats", id="pane-stats"):
                yield Static("", id="st-window")
                yield Static("", id="st-summary")
                yield Static("", id="st-calendar")
                yield DataTable(id="st-days", cursor_type="row", zebra_stripes=False)
                yield Static("", id="st-day")
            with TabPane("Report", id="pane-report"):
                with Horizontal(id="rp-sections", classes="toggle"):
                    yield Button("Summary", id="rp-tab-summary")
                    yield Button("When", id="rp-tab-when")
                    yield Button("Projects", id="rp-tab-projects")
                yield Static("", id="rp-hints")
                yield Static("", id="rp-body")
            with TabPane("Speed", id="pane-speed"):
                with Horizontal(id="sp-controls", classes="toggle"):
                    yield Button("Across models", id="sp-mode-across")
                    yield Button("Trend", id="sp-mode-trend")
                    yield Button("Hour of day", id="sp-mode-hour")
                    yield Button("Per stream", id="sp-lens-stream")
                    yield Button("Combined", id="sp-lens-combined")
                yield Static("", id="sp-note")
                yield DataTable(id="sp-table", cursor_type="row", zebra_stripes=False)
                yield Static("", id="sp-detail")
            with TabPane("Quota", id="pane-quota"):
                yield Static("", id="quota-note")
                yield DataTable(id="quota-table", cursor_type="row", zebra_stripes=False)
                yield Static("", id="quota-detail")
                yield Static("", id="quota-credits")
            with TabPane("Pricing", id="pane-pricing"):
                yield Static("", id="pr-note")
                yield Input(placeholder="search model or alias", id="pr-search")
                yield DataTable(id="pr-table", cursor_type="row", zebra_stripes=False)
        yield Footer()

    # --------------------------------------------------------------- headers --

    @staticmethod
    def _add_grid(table: DataTable, columns: tuple[tuple[str, int | None], ...]) -> None:
        """One table on a fixed-width grid.

        Each DataTable sizes itself, so fixed widths are the only cross-table
        alignment lever the widget has; numeric cells carry their own right
        justify (_num_cell), so digit edges line up across sections and a wide
        name crops instead of pushing the numbers around.
        """
        for label, width in columns:
            if width is None:
                table.add_column(label, key=label)
            else:
                table.add_column(label, width=width, key=label)

    def on_mount(self) -> None:
        if not hasattr(sys.stdout, "encoding"):
            try:  # hand the real terminal's encoding back to the driver's shim
                sys.stdout.encoding = _STDOUT_ENCODING
            except Exception:  # noqa: BLE001 — _safe_glyphs is the fallback
                pass
        self._add_grid(
            self.query_one("#ov-table", DataTable),
            (("Harness", 22), ("Tokens", 8), ("Share", 6), ("Cost", 10), ("Input", 8),
             ("Output", 8), ("Cache", 8), ("Hit", 7), ("Msgs", 8), ("Est. time", 14)),
        )
        self._add_grid(
            self.query_one("#sn-table", DataTable),
            (("Harness", 14), ("Session", 30), ("Last recorded", 20), ("Main model", 22),
             ("Tokens", 8), ("Cost", 10)),
        )
        self._add_grid(
            self.query_one("#st-days", DataTable),
            (("Date", 12), ("Tokens", 8), ("Cost", 10), ("Msgs", 8), ("Sources", 24)),
        )
        self._add_grid(
            self.query_one("#sp-table", DataTable),
            (("Model", 26), ("Harness", 12), ("Timing", 24), ("tok/s", 8),
             ("Measured", 10), ("Eligible", 10), ("Coverage", 9)),
        )
        self._add_grid(
            self.query_one("#quota-table", DataTable),
            (("Provider", 20), ("Window", 12), ("Remaining", 18), ("Resets in", 18),
             ("24h", 12), ("Status", 14)),
        )
        self._add_grid(
            self.query_one("#pr-table", DataTable),
            (("Model", 40), ("Input", 10), ("Output", 10), ("Cache read", 10),
             ("Cache write", 11), ("Aliases", 20)),
        )
        for wid in ("#ov-search", "#sn-search", "#pr-search", "#range-dates-input"):
            self.query_one(wid, Input).display = False
        for wid in ("#ov-detail", "#sn-detail", "#sp-detail", "#quota-detail", "#st-day"):
            widget = self.query_one(wid, Static)
            widget.update("")
            widget.display = False
        self.set_interval(1.0, self._tick_status)
        self._refresh_db_line()
        self._paint_rangebar()
        self._sync_toggle_states()
        # Claim the initial pane here so the TabActivated hook that fires for it
        # does not start a SECOND load of the same window: the group is
        # exclusive, so the twin would cancel the first worker and a cold scan
        # would be paid for twice.
        if not self._started["overview"]:
            self._started["overview"] = True
            self._load_pane("overview", refresh=False)
        self._update_status()

    # ------------------------------------------------------------- dispatch --

    def _load_pane(self, pane: str, refresh: bool) -> None:
        {"overview": self._load_overview,
         "sessions": self._load_sessions,
         "stats": self._load_stats,
         "report": self._load_report,
         "speed": self._load_speed,
         "quota": self._load_quota,
         "pricing": self._load_pricing}[pane](refresh)

    def _reload_pane(self, pane: str) -> None:
        """(Re-)start the load for one pane at the CURRENT state."""
        self._load_pane(pane, False)

    # ---------------------------------------------------------------- jobs --
    # Non-async @work methods MUST be thread=True (8.x WorkerDeclarationError
    # otherwise). These run the blocking reads OFF the event loop; they never
    # touch widgets — only the supervisors paint.

    @work(thread=True, exit_on_error=False)
    def _usage_job(self, period: str, date_from, date_to, refresh: bool) -> FetchOutcome:
        return fetch_usage(period, date_from, date_to, refresh=refresh)

    @work(thread=True, exit_on_error=False)
    def _active_job(self, period: str, date_from, date_to, refresh: bool) -> FetchOutcome:
        # include_review_sessions=None is the route default (and the warmer's key
        # on Overview); Report pins True below the same way the web does.
        return fetch_active_time(period, date_from, date_to, None, refresh=refresh)

    @work(thread=True, exit_on_error=False)
    def _sessions_job(self, tool: str, period: str, date_from, date_to) -> FetchOutcome:
        return fetch_sessions(tool, period, date_from, date_to, self._sn_review)

    @work(thread=True, exit_on_error=False)
    def _session_detail_job(self, tool: str, session_id: str) -> FetchOutcome:
        return fetch_session_detail(tool, session_id)

    @work(thread=True, exit_on_error=False)
    def _session_speed_job(self, tool: str, session_id: str) -> FetchOutcome:
        # No dates: the session detail shows the ENTIRE session, so its timing
        # must cover the same scope or the two would describe different things.
        return fetch_session_speed(tool, session_id)

    @work(thread=True, exit_on_error=False)
    def _stats_job(self, refresh: bool) -> FetchOutcome:
        return fetch_stats(self._stats_win.request(), refresh=refresh)

    @work(thread=True, exit_on_error=False)
    def _rp_usage_job(self, period, date_from, date_to, refresh: bool) -> FetchOutcome:
        # ``period`` is the shared window's own token: "today" for every dated
        # window (the warmer's key, dates win anyway) and "all" when the window
        # has no dates at all, which is the only case the token decides.
        return fetch_usage(period, date_from, date_to, refresh=refresh)

    @work(thread=True, exit_on_error=False)
    def _rp_insights_job(self, period, date_from, date_to, refresh: bool) -> FetchOutcome:
        # "year" is the route default the warmer primes and the facet string
        # stays VERBATIM: its order is part of the cache key. An All Time window
        # has no dates to win with, so there the scan covers every recorded day
        # rather than whichever trailing year the token happened to name.
        return fetch_insights(
            "year" if date_from else period,
            date_from, date_to, REPORT_FACETS, True, refresh=refresh,
        )

    @work(thread=True, exit_on_error=False)
    def _rp_active_job(self, period, date_from, date_to, refresh: bool) -> FetchOutcome:
        # Same law as the usage read above: the window's token, not a constant.
        return fetch_active_time(period, date_from, date_to, True, refresh=refresh)

    @work(thread=True, exit_on_error=False)
    def _speed_across_job(self, refresh: bool, cache_only: bool) -> FetchOutcome:
        date_from, date_to = self._speed_dates()
        return fetch_output_speed(
            view="across-models",
            period=SPEED_PERIOD_DEFAULT,
            date_from=date_from,
            date_to=date_to,
            source=self._sp_source,
            bucket=self._sp_bucket,
            refresh=refresh,
            cache_only=cache_only,
            include_request_windows=self._sp_include_windows,
        )

    @work(thread=True, exit_on_error=False)
    def _speed_trend_job(self) -> FetchOutcome:
        date_from, date_to = self._speed_dates()
        return fetch_output_speed(
            view="trend",
            period=SPEED_PERIOD_DEFAULT,
            date_from=date_from,
            date_to=date_to,
            source=self._sp_source,
            bucket=self._sp_bucket,
            refresh=False,  # spelled out: a follow-up never asks for a rebuild
            cache_only=True,  # ... and reads the snapshot the table came from
            include_request_windows=self._sp_include_windows,
        )

    @work(thread=True, exit_on_error=False)
    def _speed_hour_job(self, key: tuple) -> FetchOutcome:
        date_from, date_to = self._speed_dates()
        source, model, kind, basis = key
        if self._sp_lens == "combined":
            # A merged model's hours come from the trend payload: the exact-key
            # time-of-day route needs one key, and a merged model IS not one key.
            return fetch_output_speed(
                view="trend",
                period=SPEED_PERIOD_DEFAULT,
                date_from=date_from,
                date_to=date_to,
                source=self._sp_source,
                bucket=self._sp_bucket,
                refresh=False,
                cache_only=True,
                include_request_windows=self._sp_include_windows,
            )
        return fetch_output_speed(
            view="time-of-day",
            period=SPEED_PERIOD_DEFAULT,
            date_from=date_from,
            date_to=date_to,
            source=source,
            model=model,
            measurement_kind=kind,
            token_basis=basis,
            refresh=False,
            cache_only=True,
            include_request_windows=self._sp_include_windows,
        )

    @work(thread=True, exit_on_error=False)
    def _speed_job_status_job(self, job_id: str) -> FetchOutcome:
        return fetch_output_speed_job_status(job_id)

    @work(thread=True, exit_on_error=False)
    def _speed_job_follow(self, job_id: str) -> dict:
        """Blocking SSE follow (thread job: it must never sit on the loop).

        Delivers each status into ``_on_speed_event`` through call_from_thread,
        so the paint stays on the event loop. Returns a small result dict rather
        than raising: a lost subscription is a NOTE with a recovery key, not an
        app crash.
        """
        try:
            delivered = follow_output_speed_job(
                job_id,
                lambda status: self.call_from_thread(self._on_speed_event, status),
            )
            return {"ok": True, "delivered": delivered}
        except Exception as exc:  # noqa: BLE001 — connection loss is a note, not a crash
            return {"ok": False, "error": str(exc)}

    @work(thread=True, exit_on_error=False)
    def _qp_state_job(self, refresh: bool) -> FetchOutcome:
        return fetch_quota_state(refresh=refresh)

    @work(thread=True, exit_on_error=False)
    def _qp_history_job(self) -> FetchOutcome:
        return fetch_quota_history(hours=24)

    @work(thread=True, exit_on_error=False)
    def _qp_poll_job(self) -> dict:
        from ..cli import _quota_poll_once  # lazy: mirrors cli.py's own entry point

        return _quota_poll_once(include_network=True)

    @work(thread=True, exit_on_error=False)
    def _pricing_job(self, refresh: bool) -> FetchOutcome:
        return fetch_pricing_db(refresh=refresh)

    @work(thread=True, exit_on_error=False)
    def _db_summary_job(self) -> str:
        try:
            return db_summary()
        except Exception as exc:  # noqa: BLE001 — the footer never dies the app
            return f"db status unavailable: {exc}"

    @work(group="dbline", exclusive=True, exit_on_error=False)
    async def _refresh_db_line(self) -> None:
        line = await self._db_summary_job().wait()
        self._db_line = line
        self._update_status()

    # ---------------------------------------------------------- supervisors --
    # Serial, not gathered: parallel computes contend on the GIL beside the
    # server's own semaphore, and the measured web order (usage → paint → next →
    # paint) won first-paint. Every await is followed by the generation check
    # BEFORE any widget is touched.

    def _request(self) -> tuple[str, str | None, str | None]:
        """The shared window as one immutable triple, snapshotted per load. A key
        pressed mid-flight bumps the generation, so no load can pair one pane's
        new dates with another's old ones."""
        return self._win.request()

    def _speed_dates(self) -> tuple[str, str]:
        """Speed always travels as explicit dates; an open-ended period would ask
        the timing cache for a window the shared range never named."""
        _period, date_from, date_to = self._request()
        if not date_from or not date_to:
            # All Time: the timing cache still needs bounds, so ask for the whole
            # span the usage resolver itself would pick, widened to a year window.
            end = self._today
            return ((end - timedelta(days=365)).isoformat(), end.isoformat())
        return (date_from, date_to)

    @work(group="overview", exclusive=True, exit_on_error=False)
    async def _load_overview(self, refresh: bool) -> None:
        gen = self._gen
        self._begin_load("overview", gen)
        period, date_from, date_to = self._request()
        outcomes: list[FetchOutcome] = []
        try:
            try:
                u = await self._usage_job(period, date_from, date_to, refresh).wait()
                if gen != self._gen:
                    return
                self._ov_usage = u.value
                outcomes.append(u)
                self._paint_overview()
                a = await self._active_job(period, date_from, date_to, refresh).wait()
                if gen != self._gen:
                    return
                self._ov_active = a.value
                outcomes.append(a)
                self._paint_overview()
            except Exception as exc:  # noqa: BLE001 — the failure matrix classifies
                if gen == self._gen:
                    self._pane_fail("overview", exc)
                return
            if gen == self._gen:
                self._state["overview"] = "ok"
                self._finish_load("overview", gen, outcomes)
        finally:
            self._end_load("overview", gen)

    @work(group="sessions", exclusive=True, exit_on_error=False)
    async def _load_sessions(self, refresh: bool) -> None:
        """One request per selected source, THREE AT A TIME.

        ``/api/sessions`` has no tool=all and no server-side search, sort, limit
        or offset, so the fan-out and the merge are ours. A source that fails
        keeps its last-good rows out of the answer (never a zero) and the header
        says how many sources did not answer; the sources that did stay on screen.
        """
        gen = self._gen
        self._begin_load("sessions", gen)
        period, date_from, date_to = self._request()
        sources = list(self._sn_sources)
        outcomes: list[FetchOutcome] = []
        self._sn_payloads = {}
        self._sn_failed = []
        semaphore = asyncio.Semaphore(SESSION_FANOUT)

        async def _one(tool: str):
            async with semaphore:
                return await self._sessions_job(tool, period, date_from, date_to).wait()

        try:
            try:
                tasks = {tool: asyncio.ensure_future(_one(tool)) for tool in sources}
                for tool, task in tasks.items():
                    try:
                        outcome = await task
                    except Exception:  # noqa: BLE001 — one source failing is partial
                        if gen != self._gen:
                            return
                        self._sn_failed.append(tool)
                        continue
                    if gen != self._gen:
                        return
                    self._sn_payloads[tool] = outcome.value
                    outcomes.append(outcome)
                    self._paint_sessions()  # progressive: rows land as sources answer
                if gen != self._gen:
                    return
                self._paint_sessions()
            except Exception as exc:  # noqa: BLE001
                if gen == self._gen:
                    self._pane_fail("sessions", exc)
                return
            if gen == self._gen:
                self._state["sessions"] = "ok"
                if outcomes:
                    self._finish_load("sessions", gen, outcomes)
        finally:
            self._end_load("sessions", gen)

    @work(group="stats", exclusive=True, exit_on_error=False)
    async def _load_stats(self, refresh: bool) -> None:
        gen = self._gen
        self._begin_load("stats", gen)
        outcomes: list[FetchOutcome] = []
        try:
            try:
                s = await self._stats_job(refresh).wait()
                if gen != self._gen:
                    return
                self._stats_payload = s.value
                outcomes.append(s)
                self._paint_stats()
            except Exception as exc:  # noqa: BLE001
                if gen == self._gen:
                    self._pane_fail("stats", exc)
                return
            if gen == self._gen:
                self._state["stats"] = "ok"
                self._finish_load("stats", gen, outcomes)
        finally:
            self._end_load("stats", gen)

    @work(group="report", exclusive=True, exit_on_error=False)
    async def _load_report(self, refresh: bool) -> None:
        gen = self._gen
        self._begin_load("report", gen)
        period, date_from, date_to = self._request()
        outcomes: list[FetchOutcome] = []
        self._rp_insights = None
        self._rp_active = None
        self._rp_warnings = []
        try:
            try:
                u = await self._rp_usage_job(period, date_from, date_to, refresh).wait()
                if gen != self._gen:
                    return
                self._rp_usage = u.value
                outcomes.append(u)
                self._paint_report()
                # The two facet scans are SOFT here exactly as they are in the
                # one-shot report: a missing facet is a named warning inside the
                # body, not a dead pane. Usage is the hard source above it.
                try:
                    i = await self._rp_insights_job(
                        period, date_from, date_to, refresh).wait()
                except Exception as exc:  # noqa: BLE001 — soft by contract
                    if gen != self._gen:
                        return
                    self._rp_warnings = [_report_warning("analytics scan", exc)]
                else:
                    if gen != self._gen:
                        return
                    self._rp_insights = i.value
                    outcomes.append(i)
                self._paint_report()
                try:
                    a = await self._rp_active_job(
                        period, date_from, date_to, refresh).wait()
                except Exception as exc:  # noqa: BLE001 — soft by contract
                    if gen != self._gen:
                        return
                    self._rp_warnings.append(_report_warning("active time scan", exc))
                else:
                    if gen != self._gen:
                        return
                    self._rp_active = a.value
                    outcomes.append(a)
                self._paint_report()
            except Exception as exc:  # noqa: BLE001
                if gen == self._gen:
                    self._pane_fail("report", exc)
                return
            if gen == self._gen:
                self._state["report"] = "ok"
                self._finish_load("report", gen, outcomes)
        finally:
            self._end_load("report", gen)

    @work(group="speed", exclusive=True, exit_on_error=False)
    async def _load_speed(self, refresh: bool, cache_only: bool = False) -> None:
        """Read the timing snapshot. A plain read may QUEUE a build (that is what
        ``cache_only=False`` means on this route); ``r`` adds refresh. Never both:
        cache_only wins server-side and a caller that meant to rebuild would get
        the old snapshot back."""
        gen = self._gen
        self._begin_load("speed", gen)
        outcomes: list[FetchOutcome] = []
        try:
            try:
                p = await self._speed_across_job(refresh, cache_only).wait()
                if gen != self._gen:
                    return
                self._sp_payload = p.value
                self._sp_trend = None
                self._sp_hours = None
                outcomes.append(p)
                self._track_speed_job(p.value)
                self._paint_speed()
                if self._sp_mode == "trend":
                    await self._load_trend(gen)
                elif self._sp_mode == "hour":
                    await self._load_hours(gen)
            except Exception as exc:  # noqa: BLE001
                if gen == self._gen:
                    self._pane_fail("speed", exc)
                return
            if gen == self._gen:
                self._state["speed"] = "ok"
                self._finish_load("speed", gen, outcomes)
        finally:
            self._end_load("speed", gen)

    async def _load_trend(self, gen: int) -> None:
        t = await self._speed_trend_job().wait()
        if gen != self._gen:
            return
        self._sp_trend = t.value
        self._paint_speed()

    async def _load_hours(self, gen: int) -> None:
        key = self._sp_key or self._default_speed_key()
        if key is None:
            return
        if self._sp_lens == "combined" and not isinstance(key[0], str):
            return
        h = await self._speed_hour_job(key).wait()
        if gen != self._gen:
            return
        payload = h.value
        if self._sp_lens == "combined":
            model = self._sp_key[1] if self._sp_key else None
            merged = next(
                (s for s in payload.get("model_series") or [] if s.get("model") == model),
                None,
            )
            self._sp_hours = {"hours": merged or {}, "selected_key": {"model": model}}
        else:
            self._sp_hours = payload
        self._paint_speed()

    def _default_speed_key(self) -> tuple | None:
        keys = views.speed_trend_rows(self._sp_trend or {}).get("keys") or []
        if keys:
            return tuple(keys[0])
        rows = (self._sp_payload or {}).get("rows") or []
        for row in rows:
            if row.get("output_tok_per_s") is not None:
                return (
                    row.get("source"),
                    row.get("model"),
                    row.get("measurement_kind"),
                    row.get("token_basis"),
                )
        return None

    def _track_speed_job(self, payload: dict) -> None:
        """Start or stop the build subscription for the snapshot just painted."""
        cache = (payload or {}).get("cache") or {}
        job_id = cache.get("job_id")
        state = str(cache.get("state") or "")
        if state in ("building", "stale", "error") and job_id:
            self._sp_job = {"job_id": job_id, "state": state}
            self._sp_job_note = "build queued · watching this page only"
            if self._sp_gaveup == job_id:
                # Already lost this build once. Say the same thing again rather
                # than re-subscribing: a snapshot that is still "building" is
                # not a reason to ask the same dead stream a second time.
                self._sp_job_note = self._sp_gaveup_note
            elif not self._sp_following and self._active_pane() == "speed":
                self._sp_following = True
                self._follow_speed_job(str(job_id))
        else:
            self._sp_job = None
            self._sp_job_note = ""
            self._sp_gaveup = None  # a served snapshot ends the blackout

    @work(group="speed-follow", exclusive=True, exit_on_error=False)
    async def _follow_speed_job(self, job_id: str) -> None:
        result = await self._speed_job_follow(job_id).wait()
        self._sp_following = False
        if self._active_pane() != "speed":
            return
        if isinstance(result, dict) and not result.get("ok", True):
            # A lost subscription is labelled as one, with the key that recovers
            # it. The last-good rows stay exactly where they are.
            self._sp_job_note = "update connection lost · r Reload"
            self._paint_speed()
            return
        delivered = int((result or {}).get("delivered") or 0)
        if delivered == 0:
            # No subscription existed (no same-version service to ask), so this
            # pane cannot learn when the build lands. Say that instead of
            # implying a refresh that will never arrive.
            self._stop_watching(job_id, "build requested · no live update here")
            return
        if self._sp_job_state == "error":
            # The stream DID name the failure, and the note already carries it.
            # Re-reading would only re-queue the work that just failed.
            self._sp_gaveup = job_id
            return
        if self._sp_job_state not in _SP_JOB_DONE:
            # The stream closed while the build was still running. That is a
            # lost connection, NOT a finished build: re-reading here would ask
            # for the snapshot that is still "building", which would re-arm the
            # subscription, which would drop again -- a rebuild loop dressed up
            # as progress. Name it once and leave the last-good rows alone.
            self._stop_watching(job_id, "update connection ended")
            return
        # The job reached a terminal state: re-read ONCE, cache-only, so the
        # follow-up cannot queue the build all over again.
        self._sp_gaveup = None
        self._load_speed(False, True)

    def _stop_watching(self, job_id: str, what: str) -> None:
        """Note a build this pane can no longer follow, and stop asking."""
        self._sp_gaveup = job_id
        self._sp_gaveup_note = f"{what} · r Reload"
        self._sp_job_note = self._sp_gaveup_note
        self._paint_speed()

    def _on_speed_event(self, status: dict) -> None:
        state = str(status.get("state") or "")
        self._sp_job_state = state or self._sp_job_state
        done = status.get("completed_inputs")
        total = status.get("total_inputs")
        progress = f" {done}/{total}" if done is not None and total else ""
        self._sp_job_note = f"building{progress} · last-good rows shown"
        if state == "error":
            self._sp_job_note = "build failed · r Retry"
        self._paint_speed()

    @work(group="session-detail", exclusive=True, exit_on_error=False)
    async def _load_session_detail(self, tool: str, session_id: str) -> None:
        gen = self._gen
        self._sn_detail_key = (tool, session_id)
        self._sn_detail = None
        self._sn_detail_speed = None
        self._sn_detail_note = "loading whole session…"
        self._paint_sessions()
        try:
            detail = await self._session_detail_job(tool, session_id).wait()
            if gen != self._gen or self._sn_detail_key != (tool, session_id):
                return
            self._sn_detail = detail.value
            self._sn_detail_note = ""
            self._paint_sessions()
            speed = await self._session_speed_job(tool, session_id).wait()
            if gen != self._gen or self._sn_detail_key != (tool, session_id):
                return
            self._sn_detail_speed = speed.value
        except Exception as exc:  # noqa: BLE001 — a missing session is a note
            if gen == self._gen and self._sn_detail_key == (tool, session_id):
                err = getattr(exc, "error", exc)
                self._sn_detail_note = f"session unavailable: {err}"
        self._paint_sessions()

    @work(group="quota", exclusive=True, exit_on_error=False)
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
                self._paint_quota()
                h = await self._qp_history_job().wait()
                if gen != self._gen:
                    return
                self._quota_history_val = h.value
                outcomes.append(h)
                self._paint_quota()
            except Exception as exc:  # noqa: BLE001
                if gen == self._gen:
                    self._pane_fail("quota", exc)
                return
            if gen == self._gen:
                self._state["quota"] = "ok"
                self._finish_load("quota", gen, outcomes)
        finally:
            self._end_load("quota", gen)

    @work(group="qpoll", exclusive=False, exit_on_error=False)
    async def _poll_quota(self) -> None:
        gen = self._gen
        try:
            try:
                result = await self._qp_poll_job().wait()
            except Exception as exc:  # noqa: BLE001 — a failed poll is a note
                if gen == self._gen:
                    err = getattr(exc, "error", exc)
                    self.query_one("#quota-note", Static).update(
                        "[red]poll failed: " + str(err).replace("[", "\\[") + "[/]"
                    )
                return
            if gen != self._gen:
                return
            if isinstance(result, dict) and result.get("disabled"):
                # Never a fake "ok": tracking off means say-so and stop.
                self.query_one("#quota-note", Static).update(
                    emit_markup([[Run("quota tracking disabled — tokdash quota consent", "warn")]])
                )
                return
            self.query_one("#quota-note", Static).update("poll ok")
            self._load_quota(True)
        finally:
            self._polling = False

    @work(group="pricing", exclusive=True, exit_on_error=False)
    async def _load_pricing(self, refresh: bool) -> None:
        gen = self._gen
        self._begin_load("pricing", gen)
        outcomes: list[FetchOutcome] = []
        try:
            try:
                p = await self._pricing_job(refresh).wait()
                if gen != self._gen:
                    return
                self._pricing = p.value
                outcomes.append(p)
                self._paint_pricing()
            except Exception as exc:  # noqa: BLE001
                if gen == self._gen:
                    self._pane_fail("pricing", exc)
                return
            if gen == self._gen:
                self._state["pricing"] = "ok"
                self._finish_load("pricing", gen, outcomes)
        finally:
            self._end_load("pricing", gen)

    # ------------------------------------------------------------- load book --

    def _paint_loading(self, pane: str) -> None:
        """A reload NEVER leaves the PREVIOUS window's figures standing behind the
        new one: the moment a load starts the pane clears to a dim notice. First
        run (and an error-stated pane) keep the longer skeleton copy instead."""
        notice = (
            emit_markup([[Run("computing…", "muted")]])
            if self._state[pane] == "ok"
            else _SKELETON
        )
        if pane == "overview":
            self.query_one("#ov-range", Static).update(notice)
            self.query_one("#ov-cards", Static).update("")
            self.query_one("#ov-activity", Static).update("")
            self.query_one("#ov-table", DataTable).clear()
            self.query_one("#ov-detail", Static).display = False
        elif pane == "sessions":
            self.query_one("#sn-note", Static).update(notice)
            self.query_one("#sn-table", DataTable).clear()
        elif pane == "stats":
            self.query_one("#st-window", Static).update(notice)
            self.query_one("#st-summary", Static).update("")
            self.query_one("#st-calendar", Static).update("")
            self.query_one("#st-days", DataTable).clear()
        elif pane == "report":
            self.query_one("#rp-body", Static).update(notice)
        elif pane == "speed":
            self.query_one("#sp-note", Static).update(notice)
            self.query_one("#sp-table", DataTable).clear()
            self.query_one("#sp-detail", Static).display = False
        elif pane == "pricing":
            self.query_one("#pr-note", Static).update(notice)
            self.query_one("#pr-table", DataTable).clear()
        else:
            # The credits section and the account detail are painted by the
            # same load, so they clear with the table: a reload that left the
            # last grants standing would show a second window's figures under
            # the "computing" notice.
            self.query_one("#quota-note", Static).update(notice)
            self.query_one("#quota-table", DataTable).clear()
            self.query_one("#quota-credits", Static).display = False
            detail = self.query_one("#quota-detail", Static)
            detail.display = False
            detail.update("")

    def _begin_load(self, pane: str, gen: int) -> None:
        self._inflight[pane] = gen
        self._open_loads[pane] = self._open_loads.get(pane, 0) + 1
        self._stranded.discard(pane)
        # Drop the PREVIOUS window's late-arriving payloads for the secondary
        # sources of this pane: usage repaints mid-load and reads these slots, so
        # left filled they would mix last window's figures into the new window.
        if pane == "overview":
            self._ov_active = None
        elif pane == "report":
            self._rp_insights = None
            self._rp_active = None
            self._rp_warnings = []
        elif pane == "quota":
            self._quota_history_val = None
        self._load_started[pane] = time.monotonic()
        self._paint_loading(pane)
        self._update_status()

    def _end_load(self, pane: str, gen: int) -> None:
        self._open_loads[pane] = max(0, self._open_loads.get(pane, 0) - 1)
        if self._open_loads[pane]:
            return  # a newer load of this pane is running; it owns the next paint
        tracked = self._inflight.get(pane) == gen
        if tracked:
            self._inflight.pop(pane, None)
        self._load_started.pop(pane, None)
        if gen != self._gen:
            # Superseded without a successor for this pane: a VISIBLE pane gets
            # its load re-issued now; a hidden one waits for the tab hook. The
            # test is the generation, not the marker -- a cancelled sibling used
            # to clear the marker and the survivor then stranded nothing, which
            # left the pane on its skeleton with no load and no recovery.
            self._stranded.add(pane)
            if pane == self._active_pane():
                self._reload_pane(pane)
        self._update_status()

    def _finish_load(self, pane: str, gen: int, outcomes: list[FetchOutcome]) -> None:
        statuses = [o.status for o in outcomes]
        agg = "recomputed" if "recomputed" in statuses else "stale" if "stale" in statuses else "hit"
        ages = [o.age_seconds or 0.0 for o in outcomes if o.status == agg]
        self._cache_str[pane] = f"cache {agg} age {max(ages, default=0.0):.0f}s"
        # The bare aggregate too: a pane that has to SAY "this is stale"
        # needs the status, not the status line's display string.
        was = self._cache_status.get(pane, "hit")
        self._cache_status[pane] = agg
        if agg != was:
            # The note that carries this fact is painted by the pane, and the
            # pane has already painted for this load: repaint or the reader
            # never learns the snapshot is stale.
            self._repaint_pane(pane)
        if "stale" in statuses and (pane, gen) not in self._reread_done:
            self._reread_done.add((pane, gen))
            self.set_timer(STALE_REREAD_SECONDS, lambda: self._maybe_reread(pane, gen))
        self._update_status()

    def _repaint_pane(self, pane: str) -> None:
        """Repaint one pane from the payloads already in hand (no new reads)."""
        {"overview": self._paint_overview,
         "sessions": self._paint_sessions,
         "stats": self._paint_stats,
         "report": self._paint_report,
         "speed": self._paint_speed,
         "quota": self._paint_quota,
         "pricing": self._paint_pricing}.get(pane, lambda: None)()

    def _maybe_reread(self, pane: str, gen: int) -> None:
        if gen != self._gen:
            return  # a user action superseded this window-load meanwhile
        if pane == "speed":
            self._load_speed(False, True)  # a re-read here must never queue work
        else:
            self._reload_pane(pane)

    def _pane_fail(self, pane: str, exc: Exception) -> None:
        err = getattr(exc, "error", exc)
        if isinstance(err, WorkerCancelled):
            # Superseded, not failed. An exclusive group cancels the load that a
            # newer one replaced, so a red line here would blame the user for a
            # refresh they just asked for; the surviving load owns the pane.
            return
        if isinstance(err, UsageDatabaseSchemaTooNewError):
            # Terminal by design: never pane-local, never swallowed.
            sys.stderr.write(f"{err} — run 'tokdash update'\n")
            self.exit(return_code=1)
            return
        if isinstance(err, CacheBackpressureError):
            msg = "busy computing — press r"
        else:
            msg = f"error: {err}"
        line = "[red]" + msg.replace("[", "\\[") + "[/]"
        self._state[pane] = "error"
        target = {
            "overview": "#ov-range",
            "sessions": "#sn-note",
            "stats": "#st-window",
            "report": "#rp-body",
            "speed": "#sp-note",
            "quota": "#quota-note",
            "pricing": "#pr-note",
        }[pane]
        self.query_one(target, Static).update(line)
        self._update_status()

    # ------------------------------------------------------------- status --

    def _active_pane(self) -> str:
        active = self.query_one("#tabs", TabbedContent).active or "pane-overview"
        return active.replace("pane-", "")

    def _update_status(self) -> None:
        pane = self._active_pane()
        range_keys = " · [ ] range · c custom"
        hints = {
            "overview": "/ search · s sort · enter open · Esc close" + range_keys,
            "sessions": "/ search · s sort · h sources · enter whole session" + range_keys,
            "stats": "p year · enter day opens Overview",
            "report": "s sorts projects" + range_keys,
            "speed": "k series · b bucket · h source · enter details" + range_keys,
            "quota": "u poll · enter account detail",
            "pricing": "/ search · read-only",
        }
        # Overview repeats its window inside the pane, so only Overview pays for
        # it twice on the status line. The others spend the room on the keys and
        # the cache state, which the strip above cannot say.
        scope = (
            self._win.describe() if pane == "overview"
            else self._stats_win.describe(self._today) if pane == "stats"
            else "current snapshots" if pane in ("quota", "pricing")
            else ""
        )
        parts = [PANE_TITLES[pane], scope, hints[pane],
                 self._cache_str.get(pane, "cache —"), self._ensure_db_line()]
        line = " · ".join(escape(part) for part in parts if part)
        started = self._load_started.get(pane)
        if started is not None:
            line += f" · computing… {int(time.monotonic() - started)}s"
        self.query_one("#status", Static).update(line)

    def _tick_status(self) -> None:
        if self._load_started:
            self._update_status()

    # ------------------------------------------------------------ actions --

    def action_show_pane(self, pane: str) -> None:
        self.query_one("#tabs", TabbedContent).active = f"pane-{pane}"

    def action_view_picker(self) -> None:
        self.push_screen(ViewPicker(PANI, self._active_pane()), self._on_picker)

    def _on_picker(self, pane: str | None) -> None:
        if pane:
            self.action_show_pane(pane)

    def action_refresh(self) -> None:
        self._db_line = None
        self._sp_gaveup = None  # an explicit reload gets the stream back
        self._refresh_db_line()
        reset_remote()  # an explicit reload deserves a second look at the service
        pane = self._active_pane()
        self._gen += 1
        self._started[pane] = True
        if pane == "speed":
            # Manual reload asks the route to queue work (refresh) AND to ignore
            # the cached answer; never cache_only on this path.
            self._load_speed(True)
        else:
            self._load_pane(pane, True)

    def _set_window(self, window: DateWindow) -> None:
        """Install a new shared window and reload every pane that reads it.

        One generation bump for the lot, so no pane can end up showing the old
        window's figures under the new labels; the load that owns each pane
        re-issues at the new generation.
        """
        self._win = window
        self._gen += 1
        self._paint_rangebar()
        for pane in RANGE_PANES:
            if self._started[pane] or pane == self._active_pane():
                self._started[pane] = True
                self._reload_pane(pane)
        self._update_status()

    def action_set_range(self, token: str) -> None:
        if self._active_pane() not in RANGE_PANES:
            return  # Stats/Quota/Pricing never borrow the shared range
        token = RANGE_KEYS.get(token, token)
        try:
            window = window_for(token, self._today)
        except ValueError:
            return
        self._set_window(window)

    def _on_preset(self, key: str | None) -> None:
        if not key:
            return
        try:
            self._set_window(window_for(key, self._today))
        except ValueError:
            return

    def action_shift_range(self, delta: int) -> None:
        if self._active_pane() == "stats":
            # Stats owns an annual window, not the shared one, so here the
            # bracket keys walk that year a month at a time.
            self.action_stats_month(delta)
            return
        if self._active_pane() not in RANGE_PANES:
            return
        moved = self._win.step(delta, self._today)
        if moved is None:
            return  # All Time has no bounds; nothing steps past today
        self._set_window(moved)

    def action_reset_range(self) -> None:
        if self._active_pane() not in RANGE_PANES:
            return
        self._set_window(window_for("today", self._today))

    def action_custom_range(self) -> None:
        """c reveals (or puts away) the typed-date pair on the scope strip."""
        if self._active_pane() not in RANGE_PANES:
            return
        self._custom_mode = not self._custom_mode
        box = self.query_one("#range-dates-input", Input)
        box.display = self._custom_mode
        if self._custom_mode:
            self._paint_rangebar()  # the input takes room the dates were using
            box.focus()
        else:
            box.value = ""
            self._paint_rangebar()

    def action_stats_year(self) -> None:
        if self._active_pane() != "stats":
            return
        # p walks the annual ladder: trailing 365 days, then whole calendar years.
        self._stats_win = self._stats_win.step(-1, self._today)
        # The new window has its own months. Carrying the old page number over
        # lands on some unrelated month of the new year (or off the end of it).
        self._stats_month = None
        self._gen += 1
        self._started["stats"] = True
        self._load_stats(False)

    def action_cycle_sort(self) -> None:
        pane = self._active_pane()
        if pane == "overview":
            order = [key for key, _ in views.MODEL_SORTS]
            self._ov_sort = order[(order.index(self._ov_sort) + 1) % len(order)]
            self._paint_overview()
        elif pane == "sessions":
            order = [key for key, _ in views.SESSION_SORTS]
            self._sn_sort = order[(order.index(self._sn_sort) + 1) % len(order)]
            self._paint_sessions()
        elif pane == "report":
            self._rp_project_sort = "cost" if self._rp_project_sort == "tokens" else "tokens"
            self._paint_report()

    def action_choose_sources(self) -> None:
        pane = self._active_pane()
        if pane == "sessions":
            # Cycle: all supported sources -> the three busiest in practice ->
            # back. Sessions and Speed keep SEPARATE source filters on purpose.
            self._sn_sources = _next_source_cycle(self._sn_sources, SESSION_TOOLS)
            self._gen += 1
            self._started["sessions"] = True
            self._load_sessions(False)
        elif pane == "speed":
            sources = sorted(
                {
                    str(row.get("source"))
                    for row in (self._sp_payload or {}).get("rows") or []
                    if row.get("source")
                }
            )
            options = [None] + sources
            current = self._sp_source
            self._sp_source = options[(options.index(current) + 1) % len(options)] if current in options else None
            self._gen += 1
            self._started["speed"] = True
            self._load_speed(False)

    def action_choose_series(self) -> None:
        if self._active_pane() != "speed" or self._sp_mode == "across":
            return
        trend = self._sp_trend or {}
        options = [
            (" / ".join(str(x) for x in key if x), tuple(key))
            for key in (views.speed_trend_rows(trend).get("keys") or [])
        ]
        if self._sp_lens == "combined":
            options = [(str(m), str(m)) for m in (views.speed_trend_rows(trend).get("models") or [])]
        if not options:
            return
        self.push_screen(SpeedKeyPicker(options), self._on_series_picked)

    def _on_series_picked(self, value) -> None:
        if value is None:
            return
        self._sp_key = value
        self._gen += 1
        if self._sp_mode == "hour":
            self._load_hours_start()
        else:
            self._load_speed_trend_only()

    def _load_hours_start(self) -> None:
        self._gen += 1

        async def _run() -> None:
            await self._load_hours(self._gen)

        self.run_worker(_run(), exclusive=True, exit_on_error=False)

    def _load_speed_trend_only(self) -> None:
        gen = self._gen

        async def _run() -> None:
            await self._load_trend(gen)

        self.run_worker(_run(), exclusive=True, exit_on_error=False)

    def action_cycle_bucket(self) -> None:
        if self._active_pane() != "speed":
            return
        offered = (self._sp_trend or {}).get("offered_buckets") or []
        names = ["auto"] + [str(e.get("bucket")) for e in offered]
        if len(names) < 2:
            return
        self._sp_bucket = names[(names.index(self._sp_bucket) + 1) % len(names)] if self._sp_bucket in names else "auto"
        self._gen += 1
        self._started["speed"] = True
        self._load_speed(False, True)  # a resolution change re-reads the snapshot

    def action_begin_search(self) -> None:
        pane = self._active_pane()
        wid = {"overview": "#ov-search", "sessions": "#sn-search", "pricing": "#pr-search"}.get(pane)
        if wid is None:
            return
        box = self.query_one(wid, Input)
        box.display = True
        box.focus()

    def action_close_detail(self) -> None:
        """Esc closes the open detail and hands focus back to its table."""
        pane = self._active_pane()
        focused = self.focused
        if isinstance(focused, Input):
            focused.display = False
            focused.value = ""
            if pane == "overview":
                self._ov_search = ""
                self._paint_overview()
            elif pane == "sessions":
                self._sn_search = ""
                self._paint_sessions()
            elif pane == "pricing":
                self._pr_search = ""
                self._paint_pricing()
            table = {"overview": "#ov-table", "sessions": "#sn-table",
                     "pricing": "#pr-table"}.get(pane)
            if table:
                self.query_one(table, DataTable).focus()
            return
        if pane == "overview":
            self._ov_selected = None
            self._paint_overview()
        elif pane == "sessions":
            self._sn_detail_key = None
            self._sn_detail = None
            self._paint_sessions()
        elif pane == "speed":
            self.query_one("#sp-detail", Static).display = False
        elif pane == "quota":
            self._quota_detail = None
            self._paint_quota()

    def action_open_row(self) -> None:
        focused = self.focused
        if not isinstance(focused, DataTable):
            return
        key = _row_key_at(focused, focused.cursor_row)
        if key is None:
            return
        pane = self._active_pane()
        if pane == "overview":
            self._ov_selected = key
            self._paint_overview()
        elif pane == "sessions":
            self._sn_selected_key = tuple(key)
            self._load_session_detail(*tuple(key))
        elif pane == "stats":
            # Enter on a day moves the SHARED window to that date and shows the
            # Overview for it; Stats itself keeps its own annual window.
            self._win = DateWindow("custom", "Selected day", _as_date(str(key)), _as_date(str(key)), custom=True)
            self.action_show_pane("overview")
            self._set_window(self._win)
        elif pane == "speed":
            self._show_speed_row(focused, str(key))
        elif pane == "quota":
            self._quota_detail = quota_detail_text(self._quota_state_val or {}, focused, str(key))
            self._paint_quota()

    def _show_speed_row(self, table: DataTable, key: str) -> None:
        """One row's measurement context: what the rate was divided by.

        A tok/s figure means nothing without its comparison key, its basis and
        how much of the window it actually covers, so Enter shows those rather
        than repeating the number the table already shows.
        """
        source, model, timing = (key.split("|") + ["", ""])[:3]
        rows = views.speed_rows(
            self._sp_payload or {}, lens=self._sp_lens
        )["rows"]
        row = next((r for r in rows if f"{r.get('harness') or r.get('sources')}|{r['model']}|{r['timing']}" == key), None)
        detail = self.query_one("#sp-detail", Static)
        detail.display = True
        if row is None:
            detail.update(emit_markup([[Run(f"selected {model} · row is no longer in this snapshot", "warn")]]))
            return
        lines = [[Run(f"{row['model']} · {row['timing']}", "header")]]
        if self._sp_lens == "stream":
            lines.append([
                Run("source ", "muted"), Run(str(source)),
                Run("  · per-stream ", "muted"), Run(row["rate"]),
                Run("  · combined for this model ", "muted"), Run(row.get("combined", EM_DASH)),
            ])
        else:
            lines.append([
                Run("sources ", "muted"), Run(str(source)),
                Run("  · combined ", "muted"), Run(row["rate"]),
                Run("  · parallelism ", "muted"), Run(row.get("parallelism", EM_DASH)),
                Run("  · peak in flight ", "muted"), Run(row.get("peak_in_flight", EM_DASH)),
            ])
        lines.append([
            Run(f"measured {row['measured']} of {row['eligible']} eligible calls · coverage {row['coverage']}", "muted"),
        ])
        lines.append([
            Run("rate = summed tokens over summed duration; coverage is measured/eligible calls, "
                "not continuous days observed", "muted"),
        ])
        detail.update(emit_markup(lines))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Enter/Space on a row is the same action as the enter binding, but the
        # widget also emits it on click, so handle it once here.
        self.action_open_row()

    def action_poll_quota(self) -> None:
        """u — the TUI's ONLY network quota entry point, Quota only. No timer may
        ever reach this."""
        if self._active_pane() != "quota" or self._polling:
            return
        self._polling = True
        self.query_one("#quota-note", Static).update("polling…")
        self._poll_quota()

    # ------------------------------------------------------------ widgets --

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "range-more":
            # Checked before the preset prefix: "more" is not a preset key.
            self.push_screen(PresetPicker(self._today, self._win), self._on_preset)
            return
        if button_id.startswith("range-"):
            key = button_id[len("range-"):]
            try:
                self._set_window(window_for(key, self._today))
            except ValueError:
                return
            return
        if button_id in ("ov-tab-harnesses", "ov-tab-models"):
            self._ov_subview = "harnesses" if button_id.endswith("harnesses") else "models"
            self._ov_selected = None
            self._sync_toggle_states()
            self._paint_overview()
            return
        if button_id.startswith("rp-tab-"):
            self._rp_section = button_id[len("rp-tab-"):]
            self._sync_toggle_states()
            self._paint_report()
            return
        if button_id.startswith("sp-mode-"):
            self._sp_mode = button_id[len("sp-mode-"):]
            self._sync_toggle_states()
            self._gen += 1
            self._started["speed"] = True
            self._load_speed(False, True)  # lens/mode changes re-read the snapshot
            return
        if button_id.startswith("sp-lens-"):
            self._sp_lens = "stream" if button_id.endswith("stream") else "combined"
            self._sp_key = None
            self._sync_toggle_states()
            self._paint_speed()

    def _sync_toggle_states(self) -> None:
        for key, active in (
            ("ov-tab-harnesses", self._ov_subview == "harnesses"),
            ("ov-tab-models", self._ov_subview == "models"),
            ("rp-tab-summary", self._rp_section == "summary"),
            ("rp-tab-when", self._rp_section == "when"),
            ("rp-tab-projects", self._rp_section == "projects"),
            ("sp-mode-across", self._sp_mode == "across"),
            ("sp-mode-trend", self._sp_mode == "trend"),
            ("sp-mode-hour", self._sp_mode == "hour"),
            ("sp-lens-stream", self._sp_lens == "stream"),
            ("sp-lens-combined", self._sp_lens == "combined"),
        ):
            try:
                self.query_one(f"#{key}", Button).set_class(active, "-selected")
            except Exception:  # noqa: BLE001 — before compose, nothing to sync
                pass

    def on_input_changed(self, event: Input.Changed) -> None:
        box_id = event.input.id or ""
        value = event.input.value
        if box_id == "ov-search":
            self._ov_search = value
            self._ov_selected = None
            self._paint_overview()
        elif box_id == "sn-search":
            self._sn_search = value
            self._paint_sessions()
        elif box_id == "pr-search":
            self._pr_search = value
            self._paint_pricing()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if (event.input.id or "") == "range-dates-input":
            left, _sep, right = event.input.value.partition(" ")
            window, error = custom_window(left.strip(), (right or left).strip(), self._today)
            if error:
                event.input.tooltip = error
                self.query_one("#range-dates", Static).update(emit_markup([[Run(error, "bad")]]))
                return  # the typed text stays put; only a valid pair is applied
            self._set_window(window)

    def _check_day_rollover(self) -> None:
        """Re-anchor every window when the day changes underneath the app.

        Preset windows are resolved against a "today" and stamped into the
        cache keys, so an app left open past midnight would keep serving the
        day it opened on. There is deliberately no midnight timer (the app owns
        no clock that can wake it): the next keystroke notices the change, which
        is as fresh as a manually-refreshed dashboard can be.
        """
        today = _local_today()
        if today == self._today:
            return
        self._today = today
        self._win = self._win.redate(today)
        self._gen += 1
        self._paint_rangebar()
        for pane in PANI:
            if self._started[pane] or pane == self._active_pane():
                self._started[pane] = True
                self._reload_pane(pane)
        self._update_status()

    def on_resize(self, event: Resize) -> None:
        """Re-fit what is on screen to the new geometry.

        Column sets, the card grid and the preset strip are all measured against
        the pane's real width, so a fit made at another size would otherwise
        stand for the rest of the session -- the squeeze a narrow terminal
        caused would outlive the narrow terminal.
        """
        self._paint_rangebar()
        pane = self._active_pane()
        if self._state[pane] == "ok":
            self._repaint_pane(pane)

    def on_key(self, event: Key) -> None:
        self._check_day_rollover()
        if event.key == "escape" and isinstance(self.focused, Input):
            event.stop()
            self.action_close_detail()

    # Every pane keeps its keyboard entry point in one place: the table. A search
    # box that is hidden with its pane KEEPS focus, and then the next keystroke --
    # "/" included -- is eaten by a widget the user cannot see.
    _PANE_BODY = {
        "overview": "#ov-table",
        "sessions": "#sn-table",
        "stats": "#st-days",
        "report": "#rp-body",
        "speed": "#sp-table",
        "quota": "#quota-table",
        "pricing": "#pr-table",
    }

    def _hand_off_focus(self, pane: str) -> None:
        focused = self.focused
        body = self._PANE_BODY.get(pane)
        if focused is None or body is None:
            return
        try:
            shown = self.query_one(f"#pane-{pane}")
        except Exception:  # noqa: BLE001 - before compose there is no pane to serve
            return
        node = focused
        while node is not None:  # display=False on the box itself is not the only
            if node is shown:  # way a widget can be off-screen: its pane can be
                return        # hidden too
            node = node.parent
        self.query_one(body).focus()

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        pane = event.tabbed_content.get_pane(event.tab)
        name = (pane.id or "").replace("pane-", "")
        self._hand_off_focus(name)
        self._paint_rangebar()
        if not self._started[name]:
            # Lazy first load: a pane the user never opened never pays for a read.
            self._started[name] = True
            self._load_pane(name, False)
        elif name in self._stranded:
            self._reload_pane(name)
        elif self._state[name] == "ok":
            # Re-fit to the width it is being shown at: the pane may have been
            # painted while another pane held the layout, or while the terminal
            # was a different size.
            self._repaint_pane(name)
        if name == "speed":
            cache = (self._sp_payload or {}).get("cache") or {}
            if (cache.get("job_id") and not self._sp_following
                    and self._sp_gaveup != cache.get("job_id")):
                self._sp_following = True
                self._follow_speed_job(str(cache["job_id"]))
        self._update_status()

    # ------------------------------------------------------------ painting --

    def _pane_width(self, pane_id: str) -> int:
        width = self.query_one(f"#pane-{pane_id}").content_region.width
        return width if width > 0 else 80

    def _pane_screen_width(self) -> int:
        """Cells available to a full-width strip (the rangebar), never 0."""
        width = self.screen.size.width if self.screen is not None else 0
        return width if width > 0 else 80

    def _mono(self) -> bool:
        return bool(self.no_color)

    @staticmethod
    def _button_cells(preset: Preset) -> int:
        """Cells a preset button costs: its label plus all of its chrome."""
        return len(preset.short) + _BUTTON_CHROME

    @staticmethod
    def _more_cells() -> int:
        """Cells the "More..." button costs once any preset is hidden behind it."""
        return len("More...") + _BUTTON_CHROME

    def _visible_presets(self, bar_width: int, dates_cells: int) -> set[str]:
        """The presets that fit on one line WITH their dates, in priority order.

        The exact dates are not negotiable -- a shortcut row that hides which
        window is live is how a dashboard lies -- so the buttons yield space to
        them, and everything else goes behind "More...". The active preset always
        keeps a slot, so what is on screen always names itself.
        """
        # What the strip actually has: its own padding, then the dates (with the
        # left padding the Static adds), then the buttons -- and the "More..."
        # button costs a slot too once anything is hidden. Leaving that out is
        # how the dates ended up clipped mid-value at 80 columns.
        inner = max(0, bar_width - _RANGEBAR_PAD)
        room = max(0, inner - dates_cells)
        total = sum(self._button_cells(preset) for preset in PRESETS)
        if total <= room:
            return {preset.key for preset in PRESETS}
        room -= self._more_cells()
        shown: set[str] = set()
        used = 0
        for preset in PRESETS:
            cost = self._button_cells(preset)
            if used + cost > room:
                break
            shown.add(preset.key)
            used += cost
        active = self._win.preset
        if active in PRESET_BY_KEY and active not in shown:
            # The live window always names itself. It takes the LAST slot rather
            # than adding one, so guaranteeing it can never widen the row.
            order = [preset.key for preset in PRESETS]
            if shown:
                shown.discard(max(shown, key=order.index))
            shown.add(active)
        return shown

    def _paint_rangebar(self) -> None:
        """Highlight the preset that names the CURRENT window, and print the exact
        inclusive dates beside it. A shifted window matches no button, which is
        the point: the dates are the truth, the buttons are shortcuts."""
        bar = self.query_one("#rangebar")
        bar.display = self._active_pane() in RANGE_PANES
        if not bar.display:
            return
        shift = "" if self._win.shift == 0 else f"  · {self._win.label}"
        # Key hints belong to the status line. On the strip they would be the
        # first thing a narrow terminal eats, and the DATES are the one thing
        # here that must never be the thing that does not fit.
        screen = self._pane_screen_width()
        inner = max(0, screen - _RANGEBAR_PAD)

        def cells(text: str) -> int:
            return len(text) + _RANGE_DATES_PAD

        def named(text: str) -> str:
            """The dates with the window's name, but only while BOTH fit.

            The strip is right-aligned with no wrap, so an over-long line loses
            its LEFT edge -- the leading digits of the start date. A clipped date
            is a wrong date, so the label is what goes, not the numbers.
            """
            with_label = f"{text}{shift}"
            return with_label if cells(with_label) <= inner else text

        roomy = named(self._win.describe())
        dates_text = roomy
        shown = self._visible_presets(screen, cells(roomy))
        if len(shown) < len(PRESETS):
            # Both forms name the same inclusive window; the tighter one only
            # buys shortcut slots back, so the roomier wording survives for as
            # long as every preset still fits beside it.
            tight = named(self._win.short_window())
            tight_shown = self._visible_presets(screen, cells(tight))
            if len(tight_shown) > len(shown):
                dates_text, shown = tight, tight_shown
        self.query_one("#range-dates", Static).update(escape(dates_text))
        for preset in PRESETS:
            button = self.query_one(f"#range-{preset.key}", Button)
            button.display = preset.key in shown
            button.set_class(
                # Textual 8 takes the flag first: set_class(bool, "class").
                self._win.preset == preset.key
                and self._win.shift == preset.shift
                and not self._win.custom,
                "-selected",
            )
        self.query_one("#range-more", Button).display = len(shown) < len(PRESETS)

    def _paint_overview(self) -> None:
        self._sync_toggle_states()
        usage = self._ov_usage or {}
        note = self.query_one("#ov-note", Static)
        status = views.status_note(
            usage, self._ov_active, usage_status=self._cache_status.get("overview", "hit")
        )
        note.display = bool(status)
        if status:
            note.update(emit_markup([[Run(status, "warn")]]))
        cards = views.kpi_cards(usage, self._ov_active, window=self._win.describe())
        width = self._pane_width("overview")
        per_row = 6 if width >= 116 else 3 if width >= 76 else 2
        self.query_one("#ov-cards", Static).update(_card_block(cards, per_row, width, self._mono()))
        self._paint_range()
        if self._ov_subview == "models":
            self._paint_models(usage, width)
        elif self._ov_selected is not None:
            self._paint_harness_detail(usage)
        else:
            self._paint_harnesses(usage, width)
        self._paint_activity(usage)

    def _paint_range(self) -> None:
        text = f"{self._win.describe()}  ·  {PANE_TITLES['overview']}"
        rng = (self._ov_usage or {}).get("range") or {}
        if rng.get("recognized") is False:
            text += "  (range not recognised by the server)"
        self.query_one("#ov-range", Static).update(
            emit_markup([[Run(text, "header")]])
        )

    @staticmethod
    def _shape(
        table: DataTable,
        spec: tuple[tuple[str, str, int, bool], ...],
        rows: list[tuple] | None = None,
    ) -> None:
        """Rebuild a table's columns for the view it is showing right now.

        One DataTable serves several shapes (Overview's harnesses, models and
        harness detail; Speed's three modes), and a DataTable cannot rename a
        column set, so the shape is rebuilt per paint.

        Widths are then FITTED to the table's own cell budget (``fit_columns``):
        a DataTable that does not fit scrolls sideways, and a number clipped at
        the right edge is a wrong number. Columns marked optional go first, then
        the name column shrinks, then the numeric columns -- always with a floor,
        so a squeeze can never turn "1,234" into "1,2".
        """
        # A header that does not fit is a header that lies, so its own width
        # enters the BUDGET: paying for it after the fit would let one long label
        # push the measured numbers off the right edge instead.
        need = _content_widths(spec, rows) if rows else {}
        sized = [
            (key, label, max(int(width), cell_len(label) + 1, need.get(key, 0) + 1), optional)
            for key, label, width, optional in spec
        ]
        budget = _table_budget(table)
        table.clear(columns=True)
        for key, label, width in fit_columns(sized, budget):
            # A column narrower than its own label prints "Hour (server clock" --
            # a clipped header is a wrong header, so the label always fits and
            # the FIT above is what gives room to the columns that matter more.
            table.add_column(label, key=key, width=max(width, len(label) + 1))

    @staticmethod
    def _add_row(table: DataTable, cells: list[tuple[str, object]], key=None) -> None:
        """Add one row by COLUMN KEY, so a fitted (shorter) spec cannot misalign.

        Every cell is named, and the row is assembled in the order of the columns
        the table currently has. When ``fit_columns`` drops an optional column at
        a narrow terminal the numbers stay under their own headers instead of
        sliding one column left, which is the failure mode of positional rows.

        Words a column cannot hold are cut with a visible ellipsis. DataTable
        clips without a trace otherwise, and "after first token, respo" reads
        exactly like a complete timing kind.
        """
        present = [column.key for column in table.columns.values()]
        widths = {
            column_key: int(column.width or 0)
            for column_key, column in zip(present, table.columns.values())
        }
        by_key = dict(cells)
        row = [
            _clip_text(by_key.get(name, Text(EM_DASH)), widths.get(name, 0))
            for name in present
        ]
        if key is None:
            table.add_row(*row)
        else:
            table.add_row(*row, key=key)

    @staticmethod
    def _restore_cursor(table: DataTable, key) -> None:
        """Put the cursor back on the row the user was on after a repaint.

        The lookup is positional on purpose. Sessions keys rows by the
        (tool, session_id) PAIR, and Textual wraps a non-string key in an
        object that a raw tuple will not match -- asking it for the index of a
        pair raises RowDoesNotExist, which would abort the whole paint.
        """
        if key is None:
            return
        for position, stored in enumerate(table.rows.keys()):
            if getattr(stored, "value", stored) == key:
                table.move_cursor(row=position)
                return

    def _paint_harnesses(self, usage: dict, width: int) -> None:
        table = self.query_one("#ov-table", DataTable)
        wide = width >= 104
        rows = views.harness_rows(
            usage,
            (self._ov_active or {}).get("by_tool"),
            labels=TOOL_LABELS,
            wide=wide,
        )
        spec = [("harness", "Harness", 22, False), ("tokens", "Tokens", 8, False),
                ("share", "Share", 6, False), ("cost", "Cost", 10, False)]
        if wide:
            spec += [("input", "Input", 8, True), ("output", "Output", 8, True),
                     ("cache", "Cache", 8, True), ("hit", "Hit", 7, True),
                     ("msgs", "Msgs", 8, True)]
        spec.append(("time", "Est. time", 14, False))
        self._shape(table, tuple(spec))
        selected = self._ov_selected
        for row in rows:
            self._add_row(table, [
                ("harness", escape(row["harness"])),
                ("tokens", _num_cell(row["tokens"])),
                ("share", _num_cell(usage_share(usage, row["tokens_sort"]))),
                ("cost", _num_cell(row["cost"])),
                ("input", _num_cell(row.get("input", EM_DASH))),
                ("output", _num_cell(row.get("output", EM_DASH))),
                ("cache", _num_cell(row.get("cache", EM_DASH))),
                ("hit", _num_cell(row.get("hit", EM_DASH))),
                ("msgs", _num_cell(row.get("messages", EM_DASH))),
                ("time", _num_cell(row["agent_time"])),
            ], key=row["tool"])
        self._restore_cursor(table, selected)

    def _paint_models(self, usage: dict, width: int) -> None:
        """The aggregate table: one row per NORMALIZED model, no Harness column.

        Cost is a sort of this same list, never a second leaderboard, and the
        "Most used model" card above stays token-ranked whatever sort is showing.
        """
        table = self.query_one("#ov-table", DataTable)
        wide = width >= 104
        result = views.model_rows(usage, sort=self._ov_sort, search=self._ov_search, wide=wide)
        label = "Model" if not result["searched"] else (
            f"Model · {result['count']} of {result['total_models']} · by {result['sort']}"
        )
        spec = [("model", label, 30 if wide else 40, False),
                ("tokens", "Tokens", 8, False), ("share", "Share", 6, False),
                ("cost", "Cost", 10, False)]
        if wide:
            spec += [("input", "Input", 8, True), ("output", "Output", 8, True),
                     ("cache", "Cache", 8, True), ("hit", "Hit", 7, True),
                     ("msgs", "Msgs", 8, True)]
        self._shape(table, tuple(spec))
        selected = self._ov_selected
        for row in result["rows"]:
            self._add_row(table, [
                ("model", escape(row["name"])),
                ("tokens", _num_cell(row["tokens"])),
                ("share", _num_cell(row["share"])),
                ("cost", _num_cell(row["cost"])),
                ("input", _num_cell(row.get("input", EM_DASH))),
                ("output", _num_cell(row.get("output", EM_DASH))),
                ("cache", _num_cell(row.get("cache", EM_DASH))),
                ("hit", _num_cell(row.get("hit", EM_DASH))),
                ("msgs", _num_cell(row.get("messages", EM_DASH))),
            ], key=row["name"])
        self._restore_cursor(table, selected)
        detail = self.query_one("#ov-detail", Static)
        chosen = next((r for r in result["rows"] if r["name"] == selected), None)
        if selected is None or chosen is None:
            detail.display = False
            return
        lines = [
            chosen["name"],
            f"tokens {chosen['tokens']} · share {chosen['share']} · cost {chosen['cost']}",
            f"input {chosen.get('input', EM_DASH)} · output {chosen.get('output', EM_DASH)} · "
            f"cache read {chosen.get('cache', EM_DASH)} · hit {chosen.get('hit', EM_DASH)} · "
            f"messages {chosen.get('messages', EM_DASH)}",
            "merged across every harness · reasoning is not part of this figure · "
            f"rows listed cover {result['coverage']} of the reported total",
        ]
        detail.display = True
        detail.update(emit_markup([[Run(line, "header" if i == 0 else None)] for i, line in enumerate(lines)]))

    def _paint_harness_detail(self, usage: dict) -> None:
        """One source's own models, labelled with the source and the window."""
        table = self.query_one("#ov-table", DataTable)
        tool = str(self._ov_selected)
        label = tool_label(tool, TOOL_LABELS)
        rows = views.harness_models(usage, tool, labels=TOOL_LABELS)
        self._shape(table, (
            ("model", f"{label} · {self._win.describe()}", 40, False),
            ("tokens", "Tokens", 8, False), ("cost", "Cost", 10, False),
            ("msgs", "Msgs", 8, True),
        ))
        for row in rows:
            self._add_row(table, [
                ("model", escape(row["name"])),
                ("tokens", _num_cell(row["tokens"])),
                ("cost", _num_cell(row["cost"])),
                ("msgs", _num_cell(row["messages"])),
            ], key=row["name"])
        detail = self.query_one("#ov-detail", Static)
        scope = (
            "this source's models · the figures above stay global"
            if tool in SESSION_TOOLS
            else "this source's models · no session list for this source"
        )
        detail.display = True
        detail.update(emit_markup([[Run(scope, "muted")]]))

    def _paint_activity(self, usage: dict) -> None:
        widget = self.query_one("#ov-activity", Static)
        strip = views.activity_strip(
            usage.get("sparkline"),
            width=max(8, min(28, self._pane_width("overview") // 5)),
            glyphs=_safe_glyphs(),
            max_rows=6 if self._pane_width("overview") >= 96 else 3,
        )
        if not strip["available"]:
            widget.display = True
            widget.update(emit_markup([[Run(strip["note"], "muted")]]))
            return
        lines = [[Run(f"Activity · {strip['note']}", "bold")]]
        for row in strip["rows"]:
            lines.append([
                Run(f"{row['key']:<17}", "muted"),
                Run(row["bar"], "accent"),
                Run(f" {row['tokens']} tokens · {row['cost']} · {row['messages']} msgs"),
            ])
        widget.display = True
        widget.update(emit_markup(lines))

    def _paint_sessions(self) -> None:
        table = self.query_one("#sn-table", DataTable)
        wide = self._pane_width("sessions") >= 104
        result = views.session_rows(
            self._sn_payloads,
            search=self._sn_search,
            sort=self._sn_sort,
            labels=TOOL_LABELS,
            narrow=not wide,
            failed=self._sn_failed,
        )
        note = self.query_one("#sn-note", Static)
        asked = len(self._sn_sources)
        answered = len(result["sources"])
        scope = (
            f"{answered} of {asked} source{'s' if asked != 1 else ''} · "
            f"{result['count']} session{'' if result['count'] == 1 else 's'}"
            " · sorted by "
            f"{SESSION_SORT_LABELS[result['sort']]}"
        )
        if result.get("loading"):
            scope = (
                f"{answered} of {asked} sources loaded · "
                f"{result['count']} sessions so far"
            )
        if self._sn_failed:
            n = len(self._sn_failed)
            scope += f" · Partial · {n} source{'s' if n != 1 else ''} unavailable · r Retry"
        scope += " · review sessions " + (
            "server default" if self._sn_review is None
            else ("included" if self._sn_review else "excluded")
        )
        note.update(emit_markup([[Run(scope, "warn" if self._sn_failed else "muted")]]))
        spec = [("harness", "Harness", 14, False), ("session", "Project / session", 30, False)]
        if wide:
            spec += [("last", "Last recorded", 20, True), ("model", "Main model", 22, True)]
        spec += [("tokens", "Tokens", 8, False), ("cost", "Cost", 10, False)]
        self._shape(table, tuple(spec))
        for row in result["rows"]:
            self._add_row(table, [
                ("harness", escape(row["harness"])),
                ("session", escape(row["display"])),
                ("last", escape(row["last_seen_shown"])),
                ("model", escape(row["model"])),
                ("tokens", _num_cell(row["tokens"])),
                ("cost", _num_cell(row["cost"])),
            ], key=(row["tool"], row["session_id"]))
        self._restore_cursor(table, self._sn_selected_key)
        detail = self.query_one("#sn-detail", Static)
        if self._sn_detail_key is None:
            detail.display = False
            return
        detail.display = True
        detail.update(emit_markup(session_detail_lines(
            self._sn_detail, self._sn_detail_speed, self._sn_detail_note,
            self._sn_detail_key, self._win, TOOL_LABELS,
        )))

    def _paint_stats(self) -> None:
        payload = getattr(self, "_stats_payload", None) or {}
        start, end = self._stats_win.bounds(self._today)
        self.query_one("#st-window", Static).update(
            emit_markup([[Run(self._stats_win.describe(self._today), "header")]])
        )
        # /api/stats answers in three blocks: summary (web-card shape),
        # contributions (the calendar) and stats (the annual figures). The annual
        # headline lives in ``stats`` -- reading the envelope instead turns every
        # figure into a dash.
        summary = views.stats_summary(
            payload.get("stats") or {},
            today=self._today.isoformat(),
            window_days=(end - start).days + 1,
        )
        lines = [
            [
                Run("Active days ", "muted"), Run(summary["active_days"], "bold"),
                Run("  · Longest streak ", "muted"), Run(summary["longest_streak"], "bold"),
                Run("  · Current streak ", "muted"), Run(summary["current_streak"], "bold"),
                Run("  · Coverage ", "muted"), Run(summary["coverage"]),
            ],
            [Run(f"{summary['span_note']} · {summary['current_note'] or 'streak ends today or yesterday'}", "muted")],
            [Run("Tokens ", "muted"), Run(summary["tokens"]), Run("  · Messages ", "muted"),
             Run(summary["messages"]), Run("  · Most used ", "muted"), Run(summary["most_used"]),
             Run("  · Costliest ", "muted"), Run(summary["costliest"])],
            [Run("annual window only · messages read from stats.messages · no model or project tables here", "muted")],
        ]
        self.query_one("#st-summary", Static).update(emit_markup(lines))
        cal = views.stats_calendar(
            payload,
            window_from=start.isoformat(),
            window_to=end.isoformat(),
            selected=self._stats_selected,
            today=self._today.isoformat(),
            glyphs=_safe_glyphs(),
        )
        self._paint_calendar(cal)
        self._paint_stat_days(payload, cal)

    def _stats_month_index(self, months: list[dict]) -> int:
        """Which month the narrow calendar is showing (never out of range)."""
        if not months:
            return 0
        if self._stats_month is not None:
            return max(0, min(self._stats_month, len(months) - 1))
        if self._stats_selected:
            for position, month in enumerate(months):
                if str(month["month"]) == str(self._stats_selected)[:7]:
                    return position
        return len(months) - 1

    def action_stats_month(self, delta: int) -> None:
        """``[`` / ``]`` inside Stats: walk the year one month at a time.

        The bracket keys move the SHARED window everywhere else, and Stats does
        not have a shared window -- so the same keys move its own months here.
        """
        cal = views.stats_calendar(
            self._stats_payload or {},
            window_from=self._stats_win.bounds(self._today)[0].isoformat(),
            window_to=self._stats_win.bounds(self._today)[1].isoformat(),
            today=self._today.isoformat(),
            glyphs=_safe_glyphs(),
        )
        months = cal.get("month_view") or []
        if not months:
            return
        current = self._stats_month_index(months)
        self._stats_month = max(0, min(current + delta, len(months) - 1))
        self._paint_stats()

    def _calendar_shape(self, months: list[dict], width: int) -> tuple[int, bool]:
        """(months per row, page them one at a time?) for the pane's real size.

        Width alone cannot answer this: thirteen months at four per row is four
        eight-line rows, which no 36-row terminal shows at all. A grid that
        cannot be displayed pages a month at a time so that no month is
        unreachable rather than silently trimmed off the bottom.
        """
        per_row = 4 if width >= 108 else (3 if width >= 76 else 1)
        pane = self.query_one("#pane-stats")
        height = pane.size.height if pane.size.height > 0 else 24
        budget = max(_CAL_MONTH_LINES + 1, height - _CAL_OTHER_LINES)
        rows = -(-len(months) // max(1, per_row))
        return per_row, rows * _CAL_MONTH_LINES + 1 > budget

    def _paint_calendar(self, cal: dict) -> None:
        """The annual calendar as month grids.

        A narrow terminal gets ONE month at a time with the bracket keys between
        them: every month stays reachable and none is silently trimmed, which is
        what a "fit the whole year on one line" rule would otherwise do.
        """
        width = self._pane_width("stats")
        all_months = cal["month_view"]
        per_row, paged = self._calendar_shape(all_months, width)
        note = ""
        if paged:
            index = self._stats_month_index(all_months)
            months = all_months[index:index + 1]
            note = f" · month {index + 1} of {len(all_months)} · [ ] month"
        else:
            months = all_months
        widget = self.query_one("#st-calendar", Static)
        widget.display = True
        widget.update(_calendar_text(
            months,
            mono=self._mono(),
            per_row=per_row,
            selected=self._stats_selected,
            legend=cal["legend"] + note,
        ))

    def _paint_stat_days(self, payload: dict, cal: dict) -> None:
        table = self.query_one("#st-days", DataTable)
        contributions = [c for c in (payload or {}).get("contributions") or [] if isinstance(c, dict)]
        contributions = contributions[-60:]
        self._shape(table, (
            ("date", "Date", 12, False), ("tokens", "Tokens", 8, False),
            ("cost", "Cost", 10, False), ("msgs", "Msgs", 8, True),
            ("sources", "Models recorded", 24, True),
        ))
        for entry in reversed(contributions):
            totals = entry.get("totals") or {}
            sources = ", ".join(
                str(s.get("modelId") or "") for s in (entry.get("sources") or [])[:2]
                if isinstance(s, dict)  # the route's shape; a bare string is data
            )
            self._add_row(table, [
                ("date", escape(str(entry.get("date")))),
                ("tokens", _num_cell(fmt_tokens(totals.get("tokens")))),
                ("cost", _num_cell(fmt_cost_row(totals.get("cost")))),
                ("msgs", _num_cell(fmt_int(totals.get("messages")))),
                ("sources", escape(sources or EM_DASH)),
            ], key=str(entry.get("date")))
        day = cal.get("selected")
        widget = self.query_one("#st-day", Static)
        if day is None:
            widget.display = False
            return
        widget.display = True
        widget.update(
            emit_markup([[
                Run(f"{day['date']} · ", "header"),
                Run(f"tokens {day['tokens']} · cost {day['cost']} · messages {day['messages']}"),
                Run("  · Enter opens Overview on this day", "muted"),
            ]])
        )

    def _paint_report(self) -> None:
        self._sync_toggle_states()
        if self._rp_usage is None:
            return
        width = _clamp_report_width(self._pane_width("report"))
        _period, date_from, date_to = self._request()
        window = {
            "period": "range",
            "from": date_from,
            "to": date_to,
            "days": self._win.days,
            "timezone": (self._rp_insights or {}).get("timezone"),
            # None during a progressive paint means STILL RUNNING, which the
            # builder must label as running rather than as a failed scan.
            "pending": self._rp_insights is None or self._rp_active is None,
            # Named soft failures, worded the way the one-shot report words them.
            "warnings": list(self._rp_warnings),
            "db_line": self._ensure_db_line(),
        }
        if self._rp_section == "summary":
            markup = emit_markup(
                build_report_segments(
                    self._rp_usage,
                    self._rp_insights,
                    self._rp_active,
                    window=window,
                    width=width,
                    glyphs=_safe_glyphs(),
                    sections=("summary",),
                )
            )
        elif self._rp_section == "when":
            markup = emit_markup(
                build_report_segments(
                    self._rp_usage,
                    self._rp_insights,
                    self._rp_active,
                    window=window,
                    width=width,
                    glyphs=_safe_glyphs(),
                    sections=("when",),
                )
            )
        else:
            markup = emit_markup(
                build_report_segments(
                    self._rp_usage,
                    self._rp_insights,
                    self._rp_active,
                    window=window,
                    width=width,
                    glyphs=_safe_glyphs(),
                    sections=("projects",),
                )
            )
        self.query_one("#rp-hints", Static).update(
            emit_markup([[Run(f"{self._win.describe()} · {width} columns · s sorts projects", "muted")]])
        )
        self.query_one("#rp-body", Static).update(markup)

    def _paint_speed(self) -> None:
        self._sync_toggle_states()
        payload = self._sp_payload or {}
        cache = payload.get("cache") or {}
        state = str(cache.get("state") or "unknown")
        wide = self._pane_width("speed") >= 100
        note_bits = [
            f"window {self._win.describe()}",
            f"source {self._sp_source or 'all measured'}",
            "timing " + ("whole request + decode windows" if self._sp_include_windows
                          else "decode windows (excludes the initial wait)"),
            f"cache {state}",
        ]
        if self._sp_job_note:
            note_bits.append(self._sp_job_note)
        hidden = payload.get("hidden_request_windows") or []
        if hidden:
            note_bits.append(f"{len(hidden)} groups hidden by the timing filter")
        status = payload.get("source_status") or []
        if status:
            note_bits.append(
                "no measured speed: "
                + ", ".join(f"{s.get('source')} ({s.get('status')})" for s in status[:4])
            )
        self.query_one("#sp-note", Static).update(
            emit_markup([[Run(" · ".join(note_bits), "warn" if self._sp_job_note else "muted")]])
        )
        if self._sp_mode == "trend" and self._sp_trend is not None:
            self._paint_speed_trend(wide)
            return
        if self._sp_mode == "hour" and self._sp_hours is not None:
            self._paint_speed_hours()
            return
        table = self.query_one("#sp-table", DataTable)
        result = views.speed_rows(payload, lens=self._sp_lens)
        if self._sp_lens == "combined":
            spec = [("model", "Model (merged)", 30, False),
                    ("sources", "Sources", 16, True), ("timing", "Timing kinds", 24, True)]
        else:
            spec = [("model", "Model", 26, False)]
            if wide:
                spec += [("harness", "Harness", 12, True),
                         ("timing", "Timing / basis", 26, True)]
        spec += [("rate", "tok/s", 8, False), ("measured", "Measured", 10, True),
                 ("eligible", "Eligible", 10, True), ("coverage", "Coverage", 9, False)]
        self._shape(table, tuple(spec))
        for row in result["rows"]:
            self._add_row(table, [
                ("model", escape(row["model"])),
                ("harness", escape(row.get("harness", EM_DASH))),
                ("sources", escape(row.get("sources", EM_DASH))),
                ("timing", escape(row["timing"])),
                ("rate", _num_cell(row["rate"])),
                ("measured", _num_cell(row["measured"])),
                ("eligible", _num_cell(row["eligible"])),
                ("coverage", _num_cell(row["coverage"])),
            ], key=f"{row.get('harness') or row.get('sources')}|{row['model']}|{row['timing']}")
        detail = self.query_one("#sp-detail", Static)
        available = payload.get("available_measurement_range")
        if not result["rows"]:
            detail.display = True
            detail.update(emit_markup([[
                Run("no measured rows for this window", "warn"),
                Run(" · try all sources"
                    + (f" · measurements exist {available['from']} .. {available['to']}" if available else "")
                    + ("" if self._sp_include_windows else " · or include whole-request timing"),
                    "muted"),
            ]]))
            return
        if self._sp_lens == "combined":
            detail.display = True
            detail.update(emit_markup([[
                Run("combined = summed tokens over the UNION of busy time · no pooled per-stream rate exists for a merged model", "muted")
            ]]))
        else:
            detail.display = True
            detail.update(emit_markup([[
                Run("one row per source / model / timing kind / token basis — separate methods stay separate rows", "muted")
            ]]))

    def _paint_speed_trend(self, wide: bool) -> None:
        table = self.query_one("#sp-table", DataTable)
        trend = views.speed_trend_rows(self._sp_trend or {}, key=self._sp_key, lens=self._sp_lens)
        spec = [("bucket", "Bucket", 16, False), ("span", "Window", 24, True)]
        spec.append(("rate", "tok/s per stream", 8, False) if self._sp_lens == "stream"
                    else ("rate", "tok/s combined", 8, False))
        spec += [("calls", "Responses", 10, True), ("par", "Parallelism", 12, True)]
        if self._sp_lens == "stream":
            spec += [("p10", "p10", 8, True), ("p90", "p90", 8, True)]
        self._shape(table, tuple(spec))
        for row in trend["rows"]:
            self._add_row(table, [
                ("bucket", escape(row["key"])),
                ("span", escape(row["span"] if wide else ("gap" if row["gap"] else ""))),
                ("rate", _num_cell(row["per_stream"] if self._sp_lens == "stream"
                                    else row["combined"])),
                ("calls", _num_cell(row["calls"])),
                ("par", _num_cell(row["parallelism"])),
                ("p10", _num_cell(row["p10"])),
                ("p90", _num_cell(row["p90"])),
            ], key=row["key"])
        detail = self.query_one("#sp-detail", Static)
        detail.display = True
        detail.update(emit_markup([
            [Run(f"series {trend['selected'] or 'none'} · {trend['note']}", "muted")],
            [Run(f"buckets offered: {', '.join(trend['offered']) or 'none'} · b changes resolution · k chooses the series", "muted")],
            [Run(f"latest measured point: {trend['latest']} · {trend['spread_note']}", "muted")],
        ]))

    def _paint_speed_hours(self) -> None:
        table = self.query_one("#sp-table", DataTable)
        hours = views.speed_hour_rows(self._sp_hours or {}, lens=self._sp_lens)
        self._shape(table, (
            ("hour", "Hour (server clock)", 18, False), ("rate", "tok/s", 8, False),
            ("calls", "Responses", 10, True), ("coverage", "Coverage", 9, True),
        ))
        for row in hours["rows"]:
            self._add_row(table, [
                ("hour", escape(row["hour"])),
                ("rate", _num_cell(row["rate"])),
                ("calls", _num_cell(row.get("calls", EM_DASH))),
                ("coverage", _num_cell(row.get("coverage", EM_DASH))),
            ], key=row["hour"])
        detail = self.query_one("#sp-detail", Static)
        detail.display = True
        key = hours.get("selected_key") or {}
        label = " / ".join(str(v) for v in key.values() if v) or "merged model"
        detail.update(emit_markup([
            [Run(f"key {label} · {hours['note']}", "muted")],
            [Run(f"days with measurements: {hours.get('days_with_measurements', EM_DASH)} · an hour that measured nothing is a gap, not a zero", "muted")],
        ]))

    def _paint_quota(self) -> None:
        banner, rows, note, credit_lines = quota_table(
            self._quota_state_val or {},
            self._quota_history_val,
            now_ts=time.time(),
            glyphs=_safe_glyphs(),
            labels=TOOL_LABELS,
        )
        segs = list(banner)
        if note:
            segs.append([Run(note, "muted")])
        self.query_one("#quota-note", Static).update(emit_markup(segs))
        table = self.query_one("#quota-table", DataTable)
        # Every quota row is a handful of words ("62.3% left", "in 1d 23h") that
        # cannot be abbreviated and should not be cropped: a cut "unlimi…" reads
        # like a value, not like a column that is too narrow. So the columns are
        # measured against the rows they are about to hold, and fit_columns drops
        # a whole optional column rather than cutting a word in half.
        self._shape(table, QUOTA_COLUMNS, list(rows))
        for row in rows:
            cells = list(row)
            status = str(cells[-1]) if cells else ""
            if status:
                cells[-1] = Text(status, style="green" if status == "ok" else "red")
            self._add_row(
                table,
                [(key, escape(str(cell)) if not isinstance(cell, Text) else cell)
                 for key, cell in zip([c[0] for c in QUOTA_COLUMNS], cells)],
            )
        credits = self.query_one("#quota-credits", Static)
        credits.display = bool(credit_lines)
        if credit_lines:
            credits.update(emit_markup(credit_lines))
        detail = self.query_one("#quota-detail", Static)
        if self._quota_detail:
            detail.display = True
            detail.update(emit_markup([[Run(line, "muted")] for line in self._quota_detail.split("\n")]))
        else:
            detail.display = False
            detail.update("")

    def _paint_pricing(self) -> None:
        result = views.pricing_rows(self._pricing or {}, search=self._pr_search)
        note = (
            f"{result['count']} of {len((self._pricing or {}).get('data', {}).get('models') or {})} models · "
            f"{result['aliases']} aliases · version {result['version']} · "
            f"updated {result['last_updated']} · source {result['source']} · "
            f"baseline {result['baseline_version']} · USD per 1M tokens · read-only"
        )
        if result["override_note"]:
            note += " · " + result["override_note"]
        self.query_one("#pr-note", Static).update(emit_markup([[Run(note, "warn" if result["override_note"] else "muted")]]))
        table = self.query_one("#pr-table", DataTable)
        self._shape(table, (
            ("model", "Model", 40, False), ("input", "Input", 10, False),
            ("output", "Output", 10, False), ("cache_read", "Cache read", 10, True),
            ("cache_write", "Cache write", 11, True), ("aliases", "Aliases", 20, True),
        ))
        for row in result["rows"]:
            self._add_row(table, [
                ("model", escape(row["model"])),
                ("input", _num_cell(row["input"])),
                ("output", _num_cell(row["output"])),
                ("cache_read", _num_cell(row["cache_read"])),
                ("cache_write", _num_cell(row["cache_write"])),
                ("aliases", escape(row["aliases"])),
            ], key=row["model"])

    def _ensure_db_line(self) -> str:
        # PURE READ: the SQLite open/count happens in _db_summary_job's thread.
        return self._db_line if self._db_line is not None else "db status pending…"


# ---------------------------------------------------------------------------
# module-level display helpers (pure: no widgets, no payload mutation)
# ---------------------------------------------------------------------------

SESSION_SORT_LABELS = dict(views.SESSION_SORTS)
# charts.quota_table returns positional cells in this order, so the column keys
# and the row builder have exactly one shared definition.
QUOTA_COLUMNS: tuple[tuple[str, str, int, bool], ...] = (
    ("provider", "Provider", 20, False),
    ("window", "Window", 12, False),
    ("remaining", "Remaining", 18, False),
    ("resets", "Resets in", 18, True),
    ("trend", "24h", 12, True),
    ("status", "Status", 14, True),
)
# Card order matches views.kpi_cards; the colours are the approved preview's six
# KPI tones, held constant so a figure keeps its colour across views.
_CARD_COLORS = ("#c4a1ff", "#a8b1ff", "#86bdff", "#79dfbd", "#f2d07e", "#8cdeeb")


# A DataTable charges itself two cells of padding per column on top of the
# width it was given (measured, not guessed: 10 columns of the shipped widths
# need 20 cells more than their sum).
_TABLE_CELL_PAD = 2


def _row_key_at(table: DataTable, index: int | None) -> object:
    """The key of the row under the cursor, resolved by POSITION.

    Textual 8.x has no ``row_keys`` sequence: ``table.rows`` is the ordered
    key-to-Row mapping, so a cursor row becomes a key by counting into it.
    Out of range is "no row", not a crash.
    """
    if index is None:
        return None
    keys = list(table.rows.keys())
    if not 0 <= index < len(keys):
        return None
    # Textual wraps a caller's key in a RowKey object; everything downstream
    # (headers, lookups, the str() a picker hands back) wants the value itself.
    return getattr(keys[index], "value", keys[index])


def _table_budget(table: DataTable) -> int:
    """Cells a table may use for columns, before any horizontal scroll."""
    region = table.content_region.width
    if region > 0:
        return region
    return 76  # 80-column terminal minus the pane and table padding


def fit_columns(
    spec: list[tuple[str, str, int, bool]],
    width: int,
    *,
    name_floor: int = 16,
    number_floor: int = 6,
) -> list[tuple[str, str, int]]:
    """Trim a column spec until it fits ``width`` cells.

    Order of concessions, deliberately: drop columns the view can live without
    (the detail line still carries them), then narrow the name column, then
    narrow the numeric ones. Every floor holds, so no figure is ever squeezed
    into a value that reads wrong.
    """
    cols = [[key, label, int(width_), bool(optional)] for key, label, width_, optional in spec]

    def need(items: list[list]) -> int:
        return sum(item[2] for item in items) + _TABLE_CELL_PAD * len(items)

    # The name column is the only one allowed to be cropped -- the detail line
    # always carries the full text -- so it gives ground first and every measured
    # number keeps its home for as long as one can be found.
    while len(cols) > 2 and need(cols) > width and cols[0][2] > name_floor:
        cols[0][2] -= 1
    while len(cols) > 2 and need(cols) > width:
        drop = next((i for i in range(len(cols) - 1, 0, -1) if cols[i][3]), None)
        if drop is None:
            break
        cols.pop(drop)
    while need(cols) > width:
        moved = False
        for col in cols[1:]:
            if col[2] > number_floor:
                col[2] -= 1
                moved = True
                if need(cols) <= width:
                    break
        if not moved:
            break
    return [(key, label, width_) for key, label, width_, _opt in cols]


def _content_widths(
    spec: tuple[tuple[str, str, int, bool], ...], rows: list[tuple]
) -> dict[str, int]:
    """Widest cell per column, in cells, for a table about to be filled.

    Quota's figures are words ("unlimited", "in 1d 23h"). A fixed width chosen
    away from the data crops them, and a cropped word is a wrong word.
    """
    need: dict[str, int] = {}
    keys = [key for key, _label, _width, _optional in spec]
    for row in rows:
        for key, cell in zip(keys, row):
            text = cell.plain if isinstance(cell, Text) else str(cell)
            need[key] = max(need.get(key, 0), cell_len(escape(text)))
    return need


def _clip_text(cell: object, width: int) -> object:
    """Crop a WORD cell to its column; never touch a figure.

    Only plain strings are cropped. Figures arrive as right-justified Text, and a
    cut number is worse than a cut name: the column fit is what gives a figure its
    digits, and a column too narrow for one loses the figure rather than a label.
    A crop always shows itself as a crop.
    """
    if not isinstance(cell, str) or width <= 1:
        return cell
    return _fit(cell, width)


def _fit(text: object, width: int) -> str:
    """Crop a name to a cell. The full text always remains reachable in the
    detail line, so a crop here never hides a figure."""
    value = str(text if text is not None else EM_DASH)
    if width <= 1 or len(value) <= width:
        return value
    return value[: max(1, width - 1)] + "…"


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - len(text))


def _delta_text(pct: object, *, short: bool = False) -> str:
    """A neutral signed change: above 999% the exact figure is meaningless on a
    card, so it prints as a bound and the exact value stays in the payload.

    ``short`` keeps the number and drops the sentence -- on a narrow card a
    clipped "+12.0% vs prior w..." is worse than a plain "+12.0%", and the
    window it compares to is spelled out in the range strip above it.
    """
    try:
        value = float(pct)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "no comparable prior window" if not short else "no prior window"
    if abs(value) > 999.0:
        return ">999%" if value > 0 else "<-999%"
    return f"{value:+.1f}%" if short else f"{value:+.1f}% vs prior window"


def _as_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return _local_today()


def usage_share(usage: dict, tokens: float) -> str:
    total = float((usage or {}).get("total_tokens") or 0)
    if not total:
        return EM_DASH
    return f"{100.0 * float(tokens or 0) / total:.1f}%"


def session_detail_lines(
    detail: dict | None,
    speed: dict | None,
    note: str,
    key: tuple[str, str],
    window: DateWindow,
    labels: dict,
) -> list[list[Run]]:
    """The whole-session panel: scope first, then turns, then timing.

    The scope line is not decoration. ``/api/session`` has NO date parameter, so
    the turns below always cover the entire session even though the list was
    filtered to a range -- saying so is the difference between a detail pane and
    a misunderstanding.
    """
    tool, session_id = key
    header = f"{tool_label(tool, labels)} · {session_id}"
    if detail is None:
        return [
            [Run(header, "header")],
            [Run(note or "loading…", "muted")],
        ]
    session = detail.get("session") or {}
    lines: list[list[Run]] = [
        [Run(header, "header")],
        [
            Run("Entire session (this endpoint takes no dates) · opened from ", "muted"),
            Run(window.describe()),
        ],
        [
            Run("tokens ", "muted"), Run(fmt_tokens(session.get("tokens"))),
            Run("  · cost ", "muted"), Run(fmt_cost_row(session.get("cost"))),
            Run("  · calls ", "muted"), Run(fmt_int(session.get("token_events"))),
            Run("  · main model ", "muted"), Run(str(session.get("model") or EM_DASH)),
            Run("  · last recorded ", "muted"),
            Run(str(session.get("last_seen_at") or EM_DASH)[:19]),
        ],
    ]
    turns = views.session_detail_rows(detail or {}, limit=12)
    if not turns:
        lines.append([Run("no turns recorded for this session", "muted")])
    for turn in turns[-8:]:
        lines.append([
            Run(f"  #{turn['index']:<4}", "muted"),
            Run(f"{turn['timestamp']:<20}", "muted"),
            Run(f"{turn['model'][:28]:<28}"),
            Run(f"{turn['tokens']:>9} tokens · {turn['cost']:>8}"),
        ])
    total_turns = len(detail.get("turns") or [])
    if total_turns > 8:
        lines.append([Run(f"  … {total_turns - 8} earlier turns (this view shows the last 8)", "muted")])
    if speed is None:
        lines.append([Run("timing: not requested yet", "muted")])
    else:
        summary = speed.get("summary") or {}
        scope = speed.get("scope") or {}
        state = (speed.get("cache") or {}).get("state") or "unknown"
        lines.append([
            Run("timing (Entire session) · ", "muted"),
            Run(f"{fmt_int(speed.get('total_responses'))} responses · "
                f"{fmt_int(summary.get('speed_calls'))} measured · "
                f"{views._rate(summary.get('output_tok_per_s'))} tok/s · "
                f"cache {state} · scope {scope.get('kind') or EM_DASH}"),
        ])
        if speed.get("truncated"):
            lines.append([Run("  series truncated to the point budget; aggregates still cover the session", "muted")])
    if note:
        lines.append([Run(note, "warn")])
    return lines


def quota_detail_text(state: dict, table: DataTable, key: str) -> str:
    """Detail for the selected quota row: plan, absolute reset, observation time,
    source and account errors.

    A high remaining percentage does NOT override stale credentials or an old
    sample, so those stay visible next to the number rather than behind it.
    """
    try:
        row = table.zrows[table.get_row_index(key)] if hasattr(table, "zrows") else None
    except (KeyError, ValueError):
        row = None
    cells = [str(c) for c in (row or [])]
    provider = (cells[0].split("@")[0] if cells else "").strip()
    window_label = cells[1].strip() if len(cells) > 1 else ""
    for name, entry in ((state or {}).get("providers") or {}).items():
        if provider and provider.lower() not in str(name).lower():
            continue
        if not isinstance(entry, dict):
            continue
        bits = [f"provider {name}"]
        if entry.get("plan"):
            bits.append(f"plan {entry['plan']}")
        for account_id, account in (entry.get("accounts") or {}).items():
            if not isinstance(account, dict):
                continue
            bits.append(f"account {account_id}")
            if account.get("error"):
                bits.append(f"account error {account['error']}")
            for wid, window in (account.get("windows") or {}).items():
                if not isinstance(window, dict):
                    continue
                if window_label and window_label.lower() not in str(window.get("label") or wid).lower():
                    continue
                bits.append(
                    f"{wid}: used {window.get('used_percent')}% "
                    f"resets {window.get('resets_at') or EM_DASH} "
                    f"observed {window.get('observed_at') or window.get('captured_at') or EM_DASH}"
                )
            break
        if entry.get("status"):
            bits.append(f"status {entry['status']}")
        return " · ".join(str(b) for b in bits)
    return "no account detail for this row"


_CARD_SURFACE = "#121924"


_CAL_BG = ("#161e2b", "#2f2a4d", "#4a3f80", "#6d59bd", "#b9a7ff")
_CAL_WEEKDAYS = "MTWTFSS"
# States a build stream may report that mean the build is OVER. Anything else
# and a closed stream is a lost connection, not a completion.
_SP_JOB_DONE = frozenset({"ready", "complete", "done"})

# The rangebar's own horizontal padding, and the left padding #range-dates adds
# in front of the dates. Measured from the CSS above, not guessed.
_RANGEBAR_PAD = 2
# A Textual Button lays out WIDER than "label + padding: 0 1": the widget adds a
# label gutter of its own on top of ours, and every button carries a 1-cell right
# margin. Counting only what our own CSS asks for is what clipped the exact dates
# at 100 columns, so the strip budgets the measured cell cost instead.
_RANGE_DATES_PAD = 2
_BUTTON_CHROME = 5
_MONTH_CELLS = 22  # seven 3-cell day blocks plus the label gutter
_CAL_MONTH_LINES = 8  # month label, weekday header, up to six week rows
_CAL_OTHER_LINES = 12  # window, summary, day list and day detail in the pane


def _month_lines(month: dict, *, mono: bool, selected: str | None) -> list[Text]:
    """One month as: title, weekday header, then a row per week."""
    lines: list[Text] = [Text(month.get("label") or month["month"], style="#b9a7ff bold")]
    header = Text()
    for index, letter in enumerate(_CAL_WEEKDAYS):
        header.append(_pad(letter, 3), style="#7f8da3")
    lines.append(header)
    for week in month["weeks"]:
        line = Text()
        for cell in week:
            line.append(_cal_cell(cell, mono=mono, selected=selected))
        lines.append(line)
    return lines


def _cal_cell(cell: dict, *, mono: bool, selected: str | None) -> Text:
    """Three cells: intensity glyph + the day number, or a deliberate blank."""
    kind = cell.get("kind")
    day = str(cell.get("day") or "")
    glyph = str(cell.get("glyph") or " ")
    if kind == "outside" or not day:
        return Text(" " * 3)
    if kind == "future":
        return Text(_pad(glyph + day, 3), style="dim #39424f")
    if mono:
        return Text(_pad(glyph + day, 3), style="bold" if cell.get("intensity") else "#8fa0b8")
    try:
        rank = max(0, min(4, int(cell.get("intensity") or 0)))
    except (TypeError, ValueError):
        rank = 0
    if kind == "zero":
        rank = 0
    fg = "#12121a" if rank >= 4 else ("#e3eaf3" if rank >= 2 else "#8fa0b8")
    style = f"{fg} on {_CAL_BG[rank]}"
    if cell.get("date") == selected:
        style = f"{fg} on #b9a7ff bold"
    return Text(_pad(glyph + day, 3), style=style)


def _calendar_text(
    months: list[dict],
    *,
    mono: bool,
    per_row: int,
    selected: str | None,
    legend: str,
) -> Text:
    """Months as grids: weekday header, then one 3-cell block per day.

    Each block carries the day number AND its intensity glyph, so the calendar
    reads in a monochrome terminal exactly as it reads in colour -- there the
    shade is decoration, never the only signal. A day outside the window is
    blank, a future day is dim, and a day that recorded nothing still shows its
    number, because "we looked and there was nothing" is a fact.
    """
    out = Text()
    out.append(legend, style="#7f8da3")
    out.append("\n")
    for start in range(0, len(months), max(1, per_row)):
        row = months[start:start + max(1, per_row)]
        blocks = [_month_lines(month, mono=mono, selected=selected) for month in row]
        height = max(len(block) for block in blocks)
        for line_index in range(height):
            for column, block in enumerate(blocks):
                cell = block[line_index] if line_index < len(block) else Text()
                out.append(cell)  # styles ride along; only the padding is plain
                out.append(" " * max(0, _MONTH_CELLS - cell.cell_len))
                if column < len(blocks) - 1:
                    out.append("   ")
            out.append("\n")
    return out


def _report_warning(name: str, exc: Exception) -> str:
    """The report body's words for a facet scan that did not answer.

    The one-shot report already says "analytics scan failed: ..." on stderr and
    in its warnings; the interactive pane says the same sentence in the same
    place, so neither surface can turn a missing scan into a quiet zero.
    """
    err = getattr(exc, "error", exc)
    return f"{name} failed: {err}"  # emit_markup escapes any literal bracket


def _card_block(cards: list[dict], per_row: int, pane_width: int, mono: bool) -> Text:
    """Six figures as coloured blocks, reflowed to the pane's real width.

    Card width comes from the pane and the row count, never from the longest
    value, so a long model name wraps inside its own column instead of pushing a
    neighbour's digits off the screen. Separation is the tinted surface and the
    gap between blocks -- the approved design has no rules and no borders.

    In a monochrome terminal the tints cannot carry the hierarchy, so the layout
    and the words carry it instead: short delta wording, no surface fill, and the
    same block geometry.
    """
    width = max(15, (max(40, pane_width) - 2 * (per_row - 1)) // per_row)
    inner = max(6, width - 2)  # one cell of padding each side of the surface
    short = inner < 24  # "+12.0% vs prior window" needs 22 cells to say itself
    surface = "" if mono else f"on {_CARD_SURFACE}"
    body = Text()
    for start in range(0, len(cards), per_row):
        row = cards[start:start + per_row]
        for line_index in range(3):
            for column, card in enumerate(row):
                tint = _CARD_COLORS[(start + column) % len(_CARD_COLORS)]
                if line_index == 0:
                    text, style = _fit(str(card["label"]).upper(), inner), f"#8fa0b8 {surface}"
                elif line_index == 1:
                    text = _fit(str(card["value"]), inner)
                    style = (f"bold {tint} {surface}").strip() if not mono else surface
                else:
                    delta = card.get("delta")
                    if delta is not None:
                        text = _delta_text(delta, short=short)
                    else:
                        detail = str(card.get("detail", ""))
                        if short or len(detail) > inner:
                            detail = str(card.get("short_detail") or detail)
                        text = _fit(detail, inner)
                    style = f"#7f8da3 {surface}"
                body.append(_pad(f" {_pad(text, inner)} ", width), style=style)
                if column < len(row) - 1:
                    body.append("  ")
            body.append("\n")
    return body
