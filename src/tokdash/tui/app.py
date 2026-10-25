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
from datetime import date

from rich.cells import cell_len
from rich.markup import escape
from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.css.query import NoMatches
from textual.events import Key, Resize
from textual.screen import ModalScreen
from textual.worker import WorkerCancelled
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Input,
    ListView,
    ListItem,
    Static,
    Tab,
    TabbedContent,
    TabPane,
    Tabs,
)

from ..api import REPORT_FACETS, CacheBackpressureError, _local_today
from ..compute import period_is_recognized, resolve_period
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
    tool_chip,
    tool_label,
    tool_tint,
)
from .charts import column_chart, column_chart_needs, quota_detail_rows, quota_table
from .terminal import CEILING_MONO, palette as terminal_palette
from .ranges import (
    ALL_PERIOD,
    MORE_KEY,
    PRESETS,
    STRIP_PRESETS,
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
# The navigation row prints the key that selects each view inside the view's own
# tab, as the approved design does. F1..F7 follow PANI, which is also the tab
# order, so the number a reader sees is the number the binding uses.
VIEW_FKEYS: dict[str, str] = dict(zip(PANI, ("F1", "F2", "F3", "F4", "F5", "F6", "F7")))
# Each pane's ONE status strip: the place it says "this is pending / partial /
# failed / still building". A pane gets exactly one, so a reader who wants to
# know whether the figures are trustworthy never has to scan the page.
PANE_ALERT = {
    "overview": "#ov-note",
    "sessions": "#sn-note",
    "stats": "#st-window",
    "speed": "#sp-note",
    "quota": "#quota-note",
    "pricing": "#pr-note",
}
# What each pane's figures COVER, worded so it never repeats the dates the
# range strip above already prints. Stats keeps its own window text: the strip
# is hidden there, so this is the one and only place those dates appear.
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
# The ONE table per pane that Enter opens a row in. Sessions grows a second
# table -- the turns of the open session -- and a turn is not a session: Enter
# there must do nothing rather than hand a turn number to the session loader.
PANE_TABLE = {
    "overview": "ov-table",
    "sessions": "sn-table",
    "stats": "st-days",
    "speed": "sp-table",
    "quota": "quota-table",
    "pricing": "pr-table",
}

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


def _head_cell(value: str) -> Text:
    """A section header's figure: right-aligned AND bold.

    The Tools&Models table puts a tool's own totals on the row that names it,
    and those figures have to read as the heading of the model rows under them
    rather than as one more model in the list."""
    return Text(value, justify="right", style="bold")


#: The badge's edge: a half block in the source's own colour, set on the chip.
_BADGE_EDGE = "▌"


def _source_cell(tool: object, label: object = None) -> Text:
    """A source name in its own palette colour.

    The approved design marks every harness with a coloured badge; a table cell
    has no badge, so the label carries the colour instead. This is identity, not
    decoration -- the Overview, Sessions and Speed tables all list sources
    against near-identical figures, and the colour is what lets the eye follow
    one harness down a page. A monochrome terminal strips it (Textual installs a
    Monochrome filter for NO_COLOR), leaving the same words.
    """
    text = str(label or tool or EM_DASH)
    tint = tool_tint(tool)
    if _safe_glyphs() == "ascii":
        # Half blocks do not exist here; the coloured word still carries it.
        return Text(text, style=f"bold {tint}")
    # The approved design marks each source with a badge. A table cell has no
    # badge, so it borrows the two things that make one legible: an accent edge
    # and a chip of background. Colour alone left a column of tints that a
    # reader had to read word by word to tell apart.
    # No trailing ground: the cell's text IS the contract every caller (and
    # every test) reads, and a chip that adds an invisible cell to it makes
    # "Codex" a different string from "Codex".
    chip = tool_chip(tool)
    return Text.assemble(
        (_BADGE_EDGE, f"bold {tint} on {chip}"),
        (text, f"bold {tint} on {chip}"),
    )


def _report_pane_segments(segments: list[list[Run]]) -> list[list[Run]]:
    """The shared report body as the PANE shows it: same lines, no CLI furniture.

    The piped report is a document. It rules across the page to separate its
    sections and it closes with the store it read from, because a printed page
    has no other way to say either. The pane is a screen, where separation is
    spacing and surface tone (the one law of this design), and the database line
    is already at the foot of EVERY pane in the status strip -- where it belongs
    to all the numbers on screen, not to the one section that happens to print
    last. Nothing here changes a figure or drops news: the [warn] lines, the
    note about which scan each figure came from, and every value stay.
    """
    out: list[list[Run]] = []
    for runs in segments:
        text = "".join(run.text for run in runs).strip()
        if set(text) and set(text) <= set("─-"):
            out.append([])  # the rule becomes the blank row the layout uses
            continue
        if text.startswith("db ") or text == "db":
            continue
        out.append(runs)
    while out and not out[-1]:
        out.pop()
    return out


def _session_cell(display: object, session_id: object, width: int) -> Text:
    """One session, named and identified: the bold project, the dim id after it.

    The approved design gives the id its own line under the name; a table cell
    has one line, so the id follows the name and is given up whole when the
    column cannot hold both. Cropping an id is the worse error -- "codex-202610…"
    looks like another session rather than a longer label.
    """
    name = str(display or "")
    ident = str(session_id or "")
    if not ident or ident == name:
        return Text(name, style="bold")
    both = f"{name}  {ident}"
    if width and cell_len(both) > width:
        return Text(name, style="bold")
    return Text.assemble((name, "bold"), ("  ",), (ident, "#7f8da3"))


def _chip_cells(label: object) -> int:
    """Cells a tool chip costs: its label plus the button chrome it wears."""
    return cell_len(str(label)) + _BUTTON_CHROME


def _session_cell_tall(display: object, session_id: object, width: int) -> Text:
    """The session's name on one line and its id on the next, as the design draws it.

    This is what a two-row-tall list row is FOR: the project name is what the eye
    scans, the id is what tells apart two sessions of the same project on the same
    day, and the two no longer fight over one line. When the column cannot hold
    the id whole it is given up entirely -- a cropped id reads as a different
    session -- and the detail under the list names it in full either way.
    """
    name = str(display or "")
    ident = str(session_id or "")
    if not ident or ident == name:
        return Text(name, style="bold")
    if width and cell_len(ident) > width:
        return Text(name, style="bold")
    return Text.assemble((name, "bold"), ("\n",), (ident, "#7f8da3"))


# "Tools" plus the space the chip row's own label takes before the first chip.
_CHIP_LABEL_CELLS = 6


def _source_text(tool: object, label: object = None) -> str:
    """What `_source_cell` will occupy, as a plain string.

    The column fit sizes a column from the text it is told to expect. Handed the
    bare label it reserves the label's width, the badge then adds its edge, and
    the cell arrives "claude,code..." -- a value clipped by its own decoration.
    """
    text = str(label or tool or EM_DASH)
    if _safe_glyphs() == "ascii":
        return text
    return f"{_BADGE_EDGE}{text}"


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


class ChoicePicker(ModalScreen):
    """A titled keyboard chooser over (label, value) rows, with the current one marked.

    Three places need exactly this -- the speed series, the source filter in
    Sessions and Speed, and Stats\' year -- and a list is the only shape that is
    safe: nothing here can be typed wrong, and every option shows what it means
    before it is chosen. Chosen rows come back by value; Escape returns None and
    changes nothing.
    """

    BINDINGS = [Binding("escape, q", "dismiss", "Close")]

    def __init__(
        self, title: str, options: list[tuple[str, object]], active: object = None
    ) -> None:
        super().__init__()
        self._title = title
        self._options = list(options)
        self._active = active

    def compose(self) -> ComposeResult:
        with Vertical(id="view-picker"):
            yield Static(self._title, id="view-picker-title")
            yield ListView(
                *[
                    ListItem(
                        Static(Text(("● " if value == self._active else "   ") + label)),
                        id=f"choice-{index}",
                    )
                    for index, (label, value) in enumerate(self._options)
                ],
                id="view-picker-list",
            )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = getattr(event.item, "id", None) or ""
        index = item_id[len("choice-"):] if item_id.startswith("choice-") else None
        try:
            value = self._options[int(index)][1]
        except (TypeError, ValueError, IndexError):
            value = None
        self.dismiss(value)


class SpeedKeyPicker(ChoicePicker):
    """Pick one speed comparison key (source/model/kind/basis) or merged model.

    Trend and hour-of-day are only meaningful for ONE key at a time, so the
    chooser offers exactly the keys this snapshot returned — never a
    hand-typed combination that measures nothing.
    """

    def __init__(self, options: list[tuple[str, object]], active: object = None) -> None:
        super().__init__("Series to chart", options, active)


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
    /* THE BRAND ROW, the design's first row: the name on the left, what the
       palette ended up as on the right. Textual's own Header centres its title
       in the middle of the screen and opens the command palette from its icon,
       and neither of those is this design -- the row that names the page has to
       sit where the eye starts, and the colour readout is the one piece of
       terminal news a reader needs before they trust a tint. */
    #brand { height: 1; background: #121924; padding: 0 1; }
    #brand-name { width: auto; color: #b9a7ff; text-style: bold; }
    #brand-note { width: 1fr; text-align: right; color: #7f8da3; padding: 0 0 0 2; }
    /* The pane's own strip below is the ONE footer row the design asks for: it
       names the keys this pane answers and how fresh its figures are. Textual's
       Footer repeats three of those keys in a second band, so it is off and the
       strip carries q / r / ? itself. */
    Footer { display: none; }

    /* One quiet strip at the foot of the screen: the keys THIS pane answers, then
       what the data cost to get. It sits at the bottom on purpose -- at the top it
       competed with the page it described, and its longest member (the database
       line) is a diagnostic, not a headline. */
    /* Height auto, never a fixed 1: at eighty columns the pane's keys, the cache
       age and the database line do not share a row, and a strip that overflows
       the screen does not wrap -- it is CUT, losing whichever half the terminal
       did not have room for. Two rows costs the page one line; a truncated strip
       costs the reader the freshness of the numbers they are about to act on. */
    #status { height: auto; max-height: 2; background: #121924; color: #7f8da3; padding: 0 1; }

    /* The shared scope strip: chips on the page, no outlines (revision 5). The
       strip carries NO background of its own: a band behind a row of buttons
       turns five shortcuts into one slab, and the approved design separates this
       row from the page by the chips themselves. */
    #rangebar { height: 1; background: transparent; padding: 0 1 0 1; margin-top: 1; }
    /* An unchosen window is TEXT, not a button: five boxes in a row read as five
       things to press and give the chosen one nothing to be seen against. The
       key letter keeps its own small plate (the label's span paints it), so the
       shortcut is still printed where it is used, and the lavender plate is the
       ONE window that is on screen. */
    /* The chip's whole width is its label plus ONE cell of ground on each side
       plus the cell that separates it from the next chip: three cells of chrome,
       which is exactly what _BUTTON_CHROME charges and _fit_button pins.
       Textual's Button brings a line-pad of 1 (a cell of background either side
       of the line, which it measures AGAINST the widget's width) and its own
       padding, and the two stack: a chip pinned to label+2 gets its label
       measured two cells short, "t Today" wraps, and the whole strip turns two
       rows tall. line-pad cannot be set to 0 (the parser refuses it), so the
       padding is what pays for it -- the chip keeps one cell of ground and stops
       asking for a second. */
    #rangebar Button {
        width: auto; min-width: 0; height: 1;
        background: transparent; color: #a6b3c6; border: none;
        padding: 0 0; margin: 0 1 0 0; text-style: none;
    }
    #rangebar Button:hover { background: #253044; color: #e3eaf3; }
    #rangebar Button.-active { background: #b9a7ff; color: #12121a; text-style: bold; }
    #rangebar Button:focus { background: #253044; color: #e3eaf3; text-style: bold underline; }
    #rangebar Button.-selected { background: #b9a7ff; color: #12121a; text-style: bold; }
    #range-dates {
        width: 1fr; height: 1; text-align: right; text-wrap: nowrap;
        color: #e3eaf3; background: transparent; padding: 0 0 0 2;
    }

    TabbedContent { height: 1fr; background: #0d1118; }
    Tabs { height: 1; background: #0d1118; padding: 0 1; }
    Underline { display: none; }
    Tab { background: transparent; color: #7f8da3; padding: 0 1; margin: 0; }
    Tab:hover { color: #e3eaf3; }
    Tab.-active { background: #b9a7ff; color: #12121a; text-style: bold; }
    TabPane { background: #0d1118; padding: 0 1; height: 1fr; overflow-y: auto; }

    /* THE SECTION ORDER, one law for every pane: navigation (the tabs) -> range
       (the strip under them) -> page title and scope -> view controls -> content.
       Separation is spacing and surface tone; what is left with a rule is a table
       header, a search box, and the outline that says where the keyboard is. */
    /* The page head: what this page is, and what its numbers cover. ONE blank row
       above the words (the range strip above it already carries a margin of its
       own), and the words sit at the bottom of the band, because this is the
       boundary between the machinery at the top of the screen (tabs, range) and
       the page itself -- the approved design sets the page title below the strip
       with a row of its own, and a title glued under the chips reads as a fourth
       control. The height is the spend and align-vertical is what puts the air
       ABOVE the words rather than under them. */
    .head { height: 2; padding: 0; align-vertical: bottom; }
    .head-title { width: auto; color: #dce6f2; text-style: bold; }
    .head-scope { width: 1fr; text-align: right; color: #7f8da3; padding: 0 0 0 2; }
    .tools { height: auto; padding: 0; margin-top: 1; }
    .tools .fill { width: 1fr; background: transparent; }
    /* A Static is 1fr wide by default, which turns a row label into a spring
       that pushes the chips off the right edge. */
    .chip-label { width: auto; height: 1; padding: 0 1 0 0; }
    Section { margin-top: 1; }
    Static { background: transparent; }
    .label { color: #8fa0b8; }

    /* A content surface: the navy panel for a summary, a chart or a table. The
       padding keeps a figure off the panel edge; the margin-top is the ONE blank
       row between major sections. */
    .surface { background: #121924; padding: 1 2 1 2; margin-top: 1; }
    /* The headline band carries its OWN blank row inside the block (the air
       between the subview buttons and a card's label), so a margin above it
       buys a second empty row in a row that is already empty. That row goes to
       the activity picture at the bottom of the page, where the difference
       between five rows and six is a chart and a caption about a chart. */
    #ov-cards { margin-top: 0; }
    /* The activity band keeps the page's blank row above it (the table's own
       margin) and its own lip below, so its top padding is the one row of the
       surface that can go to the bars instead. It is a row of a fixed grid, not
       a stylistic preference: at thirty-six rows this panel had exactly enough
       room for a two-row picture or none at all. */
    #ov-activity { padding: 0 2 1 2; }
    /* The ONE status strip a pane owns -- pending, failed, partial, building. The
       strip says there is news; the colour of the words says how bad it is. */
    .alert { background: #171e29; padding: 0 1; margin-top: 1; }

    /* A scrollbar is part of the panel it belongs to, not an announcement. The
       palette reserves its bright tones for the keyboard's position and for a
       figure; Textual's default primary-coloured thumb turned every tall table
       into a strip of cyan two rows wide. */
    ScrollBar { background: #121924; color: #31405a; }
    ScrollBar:hover { color: #44566f; }
    /* The thumb colours live on the SCROLLABLE widget, not on ScrollBar: with
       only the rules above every pane still drew Textual's default bright thumb
       down its right edge, two cells wide, over the last column of the table
       underneath it. */
    TabPane {
        scrollbar-color: #31405a;
        scrollbar-background: #121924;
    }
    TabPane:hover { scrollbar-color: #44566f; }

    /* Two cells of inset, not one: a table whose first column starts against
       the edge of the screen reads as a continuation of whatever is above it,
       and the approved design sets its figures inside the panel with air on both
       sides. The column fit reads the table's own content region, so the inset
       costs the figures nothing -- it is paid for before they are measured. */
    DataTable {
        height: auto; background: #121924; border: none; padding: 0 2;
        margin-top: 1;
        scrollbar-color: #31405a;
        scrollbar-background: #121924;
    }
    DataTable:hover { scrollbar-color: #44566f; }
    /* A caption under a table explains the figures above it; it is not one. */
    #ov-detail, #sn-meta, #sn-detail, #st-day, #sp-detail, #quota-detail, #rp-hints {
        color: #7f8da3;
        margin-top: 1;
    }
    /* The header keeps a tone of its own, one step above the even rows, because
       that is the separator this design allows: no rule, and no second row of
       height to draw one with. Same tone as row one and the labels above the
       figures stop being a heading. */
    DataTable > .datatable--header {
        background: #18212f; color: #93a3ba; text-style: bold;
    }
    /* Rows are told apart by a BACKGROUND TONE, not by a rule between them: the
       approved design draws no lines, and a terminal cannot space its lines
       either. Every row here runs the width of the screen, so without a tone a
       reader tracking one figure across ten columns loses the row they started
       on. One step up from the panel, and no more: it is a row, not a wall. */
    DataTable > .datatable--odd-row { background: #161e2b; }
    DataTable > .datatable--even-row { background: #121924; }
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

    /* The turns of the open session, under its summary: a table and not a
       paragraph, because EVERY turn of the whole session has to stay reachable.
       The max-height is what makes that true rather than merely intended: a
       table sized to all of its rows believes it has nothing left to scroll, so
       the rows past the bottom of the pane sit below the clip with the cursor
       walking onto them invisibly. Bounded, the table scrolls itself and the
       cursor drags the view with it, which is the only way turn 1 of 400 (or
       turn 400 of 400) ever reaches the screen. Hidden until a session is open,
       like the detail line above it. */
    #sn-turns { display: none; margin-top: 1; max-height: 40%; }
    /* The session list gets the same treatment: with two unbounded tables in
       one scrollable pane, whichever one lands lower is the one whose tail sits
       under the clip, and a clipped table does not know it is clipped. */
    #sn-table { max-height: 86%; }
    /* With the turns open below it, the list is the smaller of the two tables. */
    #pane-sessions.-session-open #sn-table { max-height: 46%; }
    /* Speed's hour view is 24 rows of its own and its trend view can be a month
       of them, with a strip above and a caption below, so the table gets the
       room that is LEFT rather than the room its rows would like. Given that, it
       scrolls itself: the cursor drags the view, the rows below the fold stay
       reachable, and the caption underneath never slides off the bottom. A table
       sized by its rows instead walks straight out of the terminal, and a table
       clipped by the terminal does not know it has been clipped.

       The chart rides BESIDE the table when the pane can give both their own
       columns, and ABOVE it when it cannot. The same widgets move -- #sp-table
       keeps its cursor, its fitted columns and its scroll position -- because a
       second table for the wide layout would be a second source of truth. */
    /* Speed is the ONE pane that does not scroll, because it is the one pane with
       a 1fr child: a scrollable pane hands a flexible table all the leftover
       space and then lays the caption BELOW the viewport, so the row that
       explains the figures sits under the status strip and nobody can reach it
       (measured: the caption's region started one row past the pane's last
       visible line). With the scroll off, the fixed rows above take what they
       need and the table takes exactly the rest. */
    #pane-speed { overflow-y: hidden; }
    /* Height is set by _speed_size_series, in rows, from the pane's own ledger:
       a min-height here would be a second authority spending the same rows, and
       the rows it took came off the caption below. */
    /* The panel the picture and the rows sit on. It carries the fill so the two
       of them read as ONE surface with a gutter of tone between them, and a
       short table (seven buckets beside a tall picture) leaves navy behind it
       rather than a hole punched down to the status strip. */
    #sp-series { width: 1fr; height: 1fr; layout: vertical; background: #121924; }
    #sp-series.-split { layout: horizontal; }
    /* The chart is drawn to a width computed from the pane, so its box takes its
       own content width instead of a share of a layout it cannot measure. */
    #sp-chart { width: auto; height: auto; padding: 0 3 0 0; }
    /* Stacked (not beside the table), the picture sits one row above the header.
       The ledger reserves that row in _speed_series_budget, so the margin is the
       spend -- not a row stolen from the caption below the box. */
    #sp-chart.-stacked { margin-bottom: 1; }
    /* The table takes what the picture leaves, and its floor has to be small
       enough to survive being left very little: with a floor of seven rows the
       container's children added up to MORE than the container, and a vertical
       layout honours its children -- the table landed below the pane, below the
       status strip, past the screen. Four rows (header plus three) is a table a
       reader can still arrow through; a table off the screen is not a table. */
    #sp-series #sp-table { width: 1fr; height: 1fr; min-height: 4; margin-top: 0; }
    /* At 24 rows the blank row between sections is a row the TABLE lost, so the
       air gives way and the page order does not. One flag, set by _apply_density,
       moves the whole screen -- a layout that thinned itself widget by widget
       would leave the page reading differently at every size. */
    #tabs.-compact .head { height: 1; padding: 0; align-vertical: top; }
    #tabs.-compact .tools, #tabs.-compact .alert, #tabs.-compact .surface,
    #tabs.-compact DataTable, #tabs.-compact .head,
    #tabs.-compact #ov-cards { margin-top: 0; }
    #tabs.-compact .surface { padding: 0 2; }
    #st-cards { margin-top: 1; }
    #tabs.-compact #st-cards,
    #tabs.-compact #ov-detail, #tabs.-compact #sn-detail, #tabs.-compact #st-day,
    #tabs.-compact #sp-detail, #tabs.-compact #quota-detail,
    #tabs.-compact #rp-hints { margin-top: 0; }
    /* BESIDE the table, the chart's caption is that row's bottom edge, so the
       table starts at the same top line rather than one row lower. */
    #sp-series.-split #sp-table { margin-top: 0; }
    /* The two tables that share their pane with a note and (for quota) a detail
       block obey it too, for the same reason and at a size that keeps those
       neighbours on screen. A pricing database is a few hundred models wide:
       measured at 82 rows, the arrow key walked the cursor to a row the clip had
       already swallowed, and an invisible cursor does not search, sort or read. */
    #pr-table { max-height: 75%; }
    #quota-table { max-height: 55%; }
    /* Tools&Models is the ONE Overview shape with more rows than a screen: a
       header per tool plus every model that tool recorded. Left unbounded it
       walks the page off the terminal and the caption under it -- the line that
       says WHICH TOOL a figure belongs to, which is the whole point of the view
       -- lands below the clip. Bounded, the table scrolls itself and the cursor
       drags the view, which is what #sn-table and #pr-table already do. */
    #pane-overview.-sectioned #ov-table { max-height: 55%; }

    /* Textual's Horizontal defaults to height: 1fr, which turns a one-line
       toolbar into a spring: the subview row swallows the pane and shoves the
       table to the bottom. Toolbars are one row tall, always. */
    .toggle { height: auto; margin-top: 1; }
    #tabs.-compact .toggle { margin-top: 0; }
    #controls { height: auto; background: transparent; padding: 0; }
    /* Same arithmetic as the range strip: one cell of ground, one cell of gap,
       one row tall. A toolbar whose chips wrap onto a second row has eaten two
       rows of a twenty-four row terminal to say the same thing. */
    #controls Button, .toggle Button, .tools Button {
        width: auto; min-width: 0; height: 1;
        background: #1b2534; color: #a0adbf; border: none; padding: 0 0;
        margin: 0 1 0 0; text-style: none;
    }
    /* -active is Textual's 200 ms press flash. -selected is ours: the only
       thing that says which preset, subview or lens is in force. */
    #controls Button.-active, .toggle Button.-active, .tools Button.-active,
    #controls Button.-selected, .toggle Button.-selected, .tools Button.-selected {
        background: #b9a7ff; color: #12121a; text-style: bold;
    }
    #controls Button:focus, .toggle Button:focus, .tools Button:focus {
        background: #253044; color: #e3eaf3; text-style: bold underline;
    }
    /* A picker BUTTON is a labelled VALUE, not a command: it reads "Series:
       gpt-5.1-codex" because a bare "Series" button cannot say what is selected,
       and a control that hides the current series hides the thing the reader is
       most likely checking. */
    .picker { background: #16202e; }
    .picker.-off { color: #6c7a90; }

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
        Binding("w", "set_range('thisweek')", "This week", show=False),
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
        # ``H`` walks the Speed sources instead of opening the chooser: All, then
        # each source the SCAN reported. It walks the inventory rather than the
        # rows because a filtered response answers for one source only, and a
        # cycle built from what is on screen can only ever alternate between
        # that source and All -- which is exactly what the first review caught.
        Binding("H, shift+h", "cycle_speed_source", "Next source", show=False),
        # p is the strip's "More" on the four panes that have a strip, and
        # Stats' year key on the one pane that does not. One letter, one job --
        # which job is decided by the page the reader is looking at.
        Binding("p", "pick_more", "More ranges", show=False),
        # A plain terminal reports shift+p as an uppercase "P"; Textual matches
        # binding keys literally, so both spellings are declared.
        Binding("P, shift+p", "stats_year(1)", "Next year", show=False),
        Binding("k", "choose_series", "Series", show=False),
        Binding("x", "toggle_windows", "Timing set", show=False),
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
        # The (period, date_from, date_to) each pane's RETAINED payload answered
        # ("stats" keys on its own annual rung). A pane may only paint a payload
        # under the scope that produced it; see _scope_ok.
        self._payload_scope: dict[str, tuple | None] = dict.fromkeys(PANI, None)
        self._db_line: str | None = None
        self._fit_pending = False  # a pane re-fit is already queued
        # What the last pane fit was measured AGAINST: the screen, and the row
        # the status strip owned when it ran. A resize hands the handler the new
        # size BEFORE the layout has moved anything, so a fit can measure the frame
        # being replaced and conclude, wrongly, that nothing needs doing. When this
        # stamp moves the frame is still settling, so the fit runs once more.
        self._fit_stamp: tuple | None = None
        # Rows the status strip was WRITTEN with: one, or two when the keys
        # and the data line would not share a row. The strip is the last thing
        # on the screen, so this says which row it owns -- and therefore how
        # tall a pane may be -- without waiting for a layout that has not
        # happened yet. See _fit_panes.
        self._strip_rows: int | None = None
        self._fit_rearms = 0  # bounded, so two sizes cannot chase each other
        # Set when the last measurement of the frame CANNOT be trusted -- the page
        # was covered by a chooser, or the chrome above the panes just changed
        # height. A pass that measured a frame which has since moved looks exactly
        # like a pass that has nothing to do, so this one makes the next pass a
        # MEASUREMENT rather than a verdict. See _fit_panes.
        self._fit_forced = False
        # A refit the app DECLINED, per pane. The page needed re-fitting while a
        # read was in flight or a chooser covered it, so the repaint that would
        # have measured the settled frame could not run. Refusing is only safe
        # while the refusal is REMEMBERED: the frame moves again, the guard that
        # refused never gets asked a second time, and the page keeps figures
        # laid out for a size it no longer has. Paid by _end_load / _page_moved.
        self._refit_owed: set[str] = set()
        # Is something covering the page right now? The refit on the way back is
        # owed only after a chooser that was ACTUALLY OPEN goes away: the app also
        # publishes a screen change for its very first screen, and reading that as
        # a dismissal re-fits a page that never moved -- which can move a pane's
        # scroll position before the reader has touched anything.
        self._chooser_was_up = False
        # Rows #sp-series was holding when the Speed picture was last drawn, and
        # the height of the picture that went in. The first paint of a pane runs
        # before Textual has any geometry, so the picture is sized from an
        # estimate; _fit_panes compares these against the settled frame and
        # repaints once when the estimate was wrong. See _speed_series_rows.
        self._sp_series_painted = (-1,)
        self._sp_chart_rows = 0
        # How many buckets the series box is drawing, so the picture and the
        # table beside it can be asked whether they BOTH fit (see
        # _speed_chart_geometry), and the geometry each pane's body was last
        # fitted to, so a resize can refit it once the layout settles.
        self._sp_chart_buckets = 0
        self._body_fitted: dict[str, tuple] = {}
        # Does the mode now on screen draw a picture beside/above its rows?
        self._sp_chart_wanted = False
        self._sp_chart_budget = 0
        # Same ledger for Overview's activity picture: the basis it was sized on.
        self._activity_painted = (-1,)
        self._polling = False
        # The poll's own sentence, verbatim, for as long as it is the truth. A
        # repaint of the Quota pane re-applies it instead of deriving the banner
        # from a snapshot that cannot know about the poll; the next READ of the
        # quota state clears it, because that read is what makes it stale.
        self._quota_note_hold: str | None = None
        # Which row's detail #sp-detail is holding ("comparison" | "bucket" |
        # "hour", or None for the view's own caption). A pane repaints on a
        # snapshot update, a resize and the row-ledger refit; without this, every
        # one of those repaints replaced the opened row's explanation with the
        # view caption, which reads as the row having been forgotten.
        self._sp_detail_kind: str | None = None
        self._sp_detail_key: object = None
        # Rows the caption under the Speed box was ALLOCATED by the last paint
        # (or asked for by an Enter that has not been paid for yet).
        self._sp_detail_wanted = 0
        # Overview
        self._ov_usage: dict | None = None
        self._ov_active: dict | None = None
        self._ov_subview = "harnesses"
        self._ov_sort = "tokens"
        self._ov_search = ""
        # Whether the sectioned view has already panned the page to its open
        # caption. A repaint (resize, refit, a sort change) must not yank the
        # page down again; only a fresh Enter may.
        self._detail_panned = False
        self._ov_selected: object | None = None
        self._custom_mode = False
        # The label form the range buttons are wearing: (key hint, roomy name).
        # Cached so a repaint does not relabel eleven buttons every time.
        self._range_form: tuple[bool, bool] | None = None
        # What the navigation row is currently wearing: (F-key hints, chosen tab).
        self._tab_hint: tuple | None = None
        # Sessions
        self._sn_sources: tuple[str, ...] = tuple(SESSION_TOOLS)
        self._sn_payloads: dict[str, dict] = {}
        self._sn_failed: list[str] = []
        self._sn_search = ""
        # The tools that ANSWERED with at least one session, in the order the
        # chip row shows them, remembered from the last UNFILTERED read. The
        # chips are a filter over what this window holds, so they must survive
        # being filtered: rebuilt from the filtered rows they would collapse to
        # the one tool already on screen.
        self._sn_inventory: tuple[str, ...] = tuple(SESSION_TOOLS)
        # What the "Last recorded" column is abbreviating, in words (see _paint).
        self._sn_stamp_note = ""
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
        # The analytics scan answered WITH a failure, as opposed to not having
        # answered yet: When and Projects say "still running" only while it is
        # genuinely outstanding.
        self._rp_insights_failed = False
        self._rp_section = "summary"
        self._rp_project_sort = "tokens"
        # Rows are the scarce dimension: 24 of them is a different page from 36,
        # and _apply_density decides which of the two this terminal is on.
        self._compact = False
        # Speed
        self._sp_mode = "across"
        self._sp_lens = "stream"
        self._sp_source: str | None = None
        self._sp_bucket = "auto"
        self._sp_include_windows = False
        self._sp_payload: dict | None = None
        self._sp_trend: dict | None = None
        self._sp_hours: dict | None = None
        self._sp_key: tuple | None = None      # exact key, per-stream lens
        self._sp_model: str | None = None      # merged model, combined lens
        # ``_sp_key`` / ``_sp_model`` are what the reader ASKED FOR. The two flags
        # below say whether the table has caught up: a series read in flight keeps
        # the PREVIOUS payload in its slot, so naming the request over those rows
        # would attribute one model's throughput to another.
        self._sp_series_pending = False
        self._sp_series_error = ""  # a series read that failed, keeping its rows
        # Every source the timing scan knows about, seen so far -- kept apart
        # from the rows the CURRENT filter displays, so the chooser cannot
        # forget a source just because something else is on screen.
        self._sp_inventory: tuple[str, ...] = ()
        self._sp_job: dict | None = None
        self._sp_job_note = ""
        self._sp_following = False
        # Bumped whenever a live subscription must stop (navigation away, new
        # window, new filter). The stream polls this between lines.
        self._sp_follow_token = 0
        self._sp_follow_job: str | None = None
        # The token the LIVE subscription was opened under. Events and the
        # stream's completion answer to this, not to the app-wide load
        # generation: a reload or a series change moves the generation while the
        # build it watches stays the same, and a guard on that generation left
        # the pane claiming to watch a stream nobody held.
        self._sp_follow_epoch = -1
        # Last state the build stream reported, and the build this pane stopped
        # watching (see _stop_watching). Both are per-job, never global.
        self._sp_job_state = ""
        self._sp_gaveup: str | None = None
        self._sp_gaveup_note = ""
        # Quota / Pricing
        self._quota_state_val: dict | None = None
        self._quota_history_val: dict | None = None
        # Rows painted as markup, so an account status keeps the colour that
        # says whether the number above it can be trusted.
        self._quota_detail: list[list[Run]] | None = None
        # Row key -> what that row IS, for the rows the table shows right now.
        self._quota_idents: dict[str, dict | None] = {}
        self._pricing: dict | None = None
        self._pr_search = ""

    # ------------------------------------------------------------------ UI --

    #: The key chip's two states. The approved design prints each shortcut in a
    #: box of its own, so the key reads as a KEY and not as the first letter of
    #: the word next to it. Two palettes because the box sits on two surfaces:
    #: the dark button (pale box, dark ink) and the lavender selection (dark box,
    #: pale ink), which is what keeps the letter legible in both.
    _CHIP_PALE = "#121a26 on #c3cde0"
    _CHIP_DARK = "#e7ecf6 on #1b2433"

    def _key_chip(self, key: str, *, selected: bool) -> Text:
        """One shortcut, boxed. Bold ink in monochrome, where there is no box."""
        if self._mono():
            return Text(f"{key} ", style="bold")
        return Text(f"{key} ", style=(self._CHIP_DARK if selected else self._CHIP_PALE))

    def _strip_label(self, preset: Preset, *, hint: bool, roomy: bool,
                     selected: bool = False) -> Text:
        """A range button's label: the key that presses it, then the window.

        The approved design prints the shortcut INSIDE the button it belongs to
        ("t Today"), because a strip whose letters live only in a footer two
        rows away is a strip the reader has to leave to use. The window's own
        name keeps the button's colour, so hover and focus still read.
        """
        text = preset.label if roomy else preset.short
        if hint and preset.hint:
            return Text.assemble(self._key_chip(preset.hint, selected=selected), text)
        return Text(text)

    @classmethod
    def _strip_cells(cls, preset: Preset, *, hint: bool, roomy: bool) -> int:
        """Cells a range button costs with the label form it is about to wear."""
        text = preset.label if roomy else preset.short
        return cell_len(text) + (2 if hint and preset.hint else 0) + _BUTTON_CHROME

    def _more_label(self, selected: bool = False) -> Text:
        return Text.assemble(self._key_chip(MORE_KEY, selected=selected), "More")

    def _build_rangebar(self) -> Horizontal:
        """The one range strip every dated pane shares, built for its slot.

        Presets are shortcuts, so they come first; the exact inclusive dates are
        the truth, so they are the last thing on the row and the last thing a
        narrow terminal gives up (see _visible_presets).

        Every preset still gets a button, so every one is still a click away in
        the chooser. The five that own the row are the ones the strip SHOWS; the
        rest stay mounted but hidden, because a window the reader is IN always
        names itself -- at the right-hand end of this very row, in dates.
        """
        return Horizontal(
            *[
                Button(
                    self._strip_label(preset, hint=True, roomy=True),
                    id=f"range-{preset.key}",
                    variant="default",
                )
                for preset in PRESETS
            ],
            Button(self._more_label(), id="range-more", variant="default"),
            Static("", id="range-dates"),
            Input(placeholder="YYYY-MM-DD .. YYYY-MM-DD", id="range-dates-input"),
            id="rangebar",
        )


    def compose(self) -> ComposeResult:
        # THE FRAME, top to bottom: brand, navigation, range, one page, keys.
        # The range strip is re-parented under the tab row in on_mount, because
        # Textual's TabbedContent owns both the tab row and the panes -- a strip
        # yielded here lands ABOVE the navigation it belongs under.
        with Horizontal(id="brand"):
            yield Static("Tokdash", id="brand-name")
            yield Static("", id="brand-note")
        # The shared range strip is NOT composed here. TabbedContent turns any
        # child that is not a TabPane into a pane of its own -- which is how a
        # range strip ends up with a blank tab of its own -- so on_mount builds it
        # and mounts it into the tab widget's child list, in the one gap it
        # belongs in: under the tabs, above the page.
        with TabbedContent(initial="pane-overview", id="tabs"):
            with TabPane("Overview", id="pane-overview"):
                # Page title and scope: ONE line, and it never repeats the dates
                # the strip above already shows. The alert strip under it is the
                # only place this pane says anything is wrong.
                with Horizontal(id="ov-head", classes="head"):
                    yield Static("Overview", id="ov-title", classes="head-title")
                    yield Static("", id="ov-scope", classes="head-scope")
                yield Static("", id="ov-note", classes="alert")
                with Horizontal(id="ov-subview", classes="toggle"):
                    yield Button("Tools", id="ov-tab-harnesses")
                    yield Button("Models", id="ov-tab-models")
                    yield Button("Tools&Models", id="ov-tab-breakdown")
                    yield Static("", id="ov-sort", classes="head-scope")
                yield Input(placeholder="filter names", id="ov-search")
                yield Static("", id="ov-cards")
                yield DataTable(id="ov-table", cursor_type="row", zebra_stripes=True)
                yield Static("", id="ov-detail")
                yield Static("", id="ov-activity", classes="surface")
            with TabPane("Sessions", id="pane-sessions"):
                with Horizontal(id="sn-head", classes="head"):
                    yield Static("Sessions", id="sn-title", classes="head-title")
                    yield Static("", id="sn-scope", classes="head-scope")
                yield Static("", id="sn-note", classes="alert")
                # WHICH TOOL the rows came from is the first thing a reader wants
                # to narrow by, and it was reachable only as "h" -- a chooser
                # two keystrokes deep that showed nothing about what is on
                # screen. The chips are the same filter, on screen, in the order
                # the window holds them; "More" opens the chooser for the tools
                # this row has no room for.
                with Horizontal(id="sn-tools", classes="tools"):
                    yield Static("Tools", classes="label chip-label")
                    yield Button("All", id="sn-tool-all")
                    for _tool in SESSION_TOOLS:
                        yield Button(
                            tool_label(_tool, TOOL_LABELS), id=f"sn-tool-{_tool}"
                        )
                    yield Button("More", id="sn-tool-more", classes="picker")
                    yield Static("", id="sn-tools-fill", classes="fill")
                yield Input(placeholder="search project, id, model", id="sn-search")
                yield DataTable(id="sn-table", cursor_type="row", zebra_stripes=True)
                # The rows' own caption: how many there are, what they are sorted
                # by, how fresh. It sits UNDER the table because that is where a
                # caption belongs and because above it the line competed with the
                # page title it repeated.
                yield Static("", id="sn-meta")
                yield Static("", id="sn-detail")
                yield DataTable(id="sn-turns", cursor_type="row", zebra_stripes=True)
            with TabPane("Stats", id="pane-stats"):
                with Horizontal(id="st-head", classes="head"):
                    yield Static("Year in review", id="st-title", classes="head-title")
                    yield Static("", id="st-scope", classes="head-scope")
                yield Static("", id="st-window", classes="alert")
                yield Static("", id="st-cards")
                yield Static("", id="st-summary")
                yield Static("", id="st-calendar")
                yield DataTable(id="st-days", cursor_type="row", zebra_stripes=True)
                yield Static("", id="st-day")
            with TabPane("Report", id="pane-report"):
                with Horizontal(id="rp-head", classes="head"):
                    yield Static("Report", id="rp-title", classes="head-title")
                    yield Static("", id="rp-scope", classes="head-scope")
                with Horizontal(id="rp-sections", classes="toggle"):
                    yield Button("Summary", id="rp-tab-summary")
                    yield Button("When", id="rp-tab-when")
                    yield Button("Projects", id="rp-tab-projects")
                yield Static("", id="rp-hints")
                yield Static("", id="rp-body", classes="surface")
            with TabPane("Speed", id="pane-speed"):
                with Horizontal(id="sp-head", classes="head"):
                    yield Static("Model output speed", id="sp-title", classes="head-title")
                    yield Static("", id="sp-scope", classes="head-scope")
                yield Static("", id="sp-note", classes="alert")
                # MODE left, LENS right. They answer different questions -- which
                # view, and which population -- and six buttons in one undivided
                # row read as one group of six.
                with Horizontal(id="sp-controls", classes="tools"):
                    yield Button("Across models", id="sp-mode-across")
                    yield Button("Trend", id="sp-mode-trend")
                    yield Button("Hour of day", id="sp-mode-hour")
                    yield Static("", id="sp-controls-fill", classes="fill")
                    yield Button("Per stream", id="sp-lens-stream")
                    yield Button("Combined", id="sp-lens-combined")
                # The three selectors a reader could otherwise only reach blind on
                # a keypress, each labelled with the value it holds, and the
                # timing preference drawn as the on/off box it is.
                with Horizontal(id="sp-selectors", classes="tools"):
                    yield Button("Source", id="sp-pick-source", classes="picker")
                    yield Button("Series", id="sp-pick-series", classes="picker")
                    yield Button("Bucket", id="sp-pick-bucket", classes="picker")
                    yield Static("", id="sp-selectors-fill", classes="fill")
                    yield Button("Include whole-request timing", id="sp-windows", classes="picker")
                with Container(id="sp-series"):
                    yield Static("", id="sp-chart")
                    yield DataTable(id="sp-table", cursor_type="row", zebra_stripes=True)
                yield Static("", id="sp-detail")
            with TabPane("Quota", id="pane-quota"):
                with Horizontal(id="quota-head", classes="head"):
                    yield Static("Quota", id="quota-title", classes="head-title")
                    yield Static("", id="quota-scope", classes="head-scope")
                yield Static("", id="quota-note", classes="alert")
                yield DataTable(id="quota-table", cursor_type="row", zebra_stripes=True)
                yield Static("", id="quota-detail")
                yield Static("", id="quota-credits")
            with TabPane("Pricing", id="pane-pricing"):
                with Horizontal(id="pr-head", classes="head"):
                    yield Static("Pricing database", id="pr-title", classes="head-title")
                    yield Static("", id="pr-scope", classes="head-scope")
                yield Static("", id="pr-note", classes="alert")
                yield Input(placeholder="search model or alias", id="pr-search")
                yield DataTable(id="pr-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="status")
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

    async def on_mount(self) -> None:
        # The range strip goes in before anything can read from it. Its slot is
        # inside the tab widget -- under the tab row, above the panes -- and the
        # mount must finish before the strip can be painted or hidden.
        tabs = self.query_one("#tabs", TabbedContent)
        await tabs.mount(self._build_rangebar(), after=tabs.query_one(Tabs))
        self._paint_tabs()
        self._paint_brand()
        if not hasattr(sys.stdout, "encoding"):
            try:  # hand the real terminal's encoding back to the driver's shim
                sys.stdout.encoding = _STDOUT_ENCODING
            except Exception:  # noqa: BLE001 — _safe_glyphs is the fallback
                pass
        self._add_grid(
            self.query_one("#ov-table", DataTable),
            (("Tool", 22), ("Tokens", 8), ("Share", 6), ("Cost", 10), ("Input", 8),
             ("Output", 8), ("Cache", 8), ("Hit", 7), ("Msgs", 8), ("Est. time", 14)),
        )
        self._add_grid(
            self.query_one("#sn-table", DataTable),
            (("Tool", 14), ("Session", 34), ("Last recorded", 20), ("Main model", 22),
             ("Tokens", 8), ("Cost", 10)),
        )
        self._add_grid(
            self.query_one("#st-days", DataTable),
            (("Date", 12), ("Tokens", 8), ("Cost", 10), ("Msgs", 8), ("Sources", 24)),
        )
        self._add_grid(
            self.query_one("#sp-table", DataTable),
            # "Source", not "Harness": this is the grid a reader sees while a
            # first run computes, before the pane paints its own fitted columns,
            # and it names the same thing the picker above it names.
            (("Model", 26), ("Source", 12), ("Timing", 24), ("tok/s", 8),
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
        # A strip with nothing to say is a blank tinted band across the page, so
        # it is hidden rather than merely empty.
        for wid in ("#ov-note", "#sn-note", "#st-window", "#quota-note", "#pr-note",
                    "#sp-note", "#ov-activity", "#sp-chart"):
            widget = self.query_one(wid, Static)
            widget.update("")
            widget.display = False
        self.set_interval(1.0, self._tick_status)
        self._refresh_db_line()
        # Navigation, THEN the range it applies to, THEN the page. Textual's
        # TabbedContent owns the tab row and the panes together, so the shared
        # strip is re-parented into the gap between them rather than drawn above
        # the navigation -- which is also the order a figure gets resolved in:
        # which view, which window, which page.
        self._apply_density()
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
        # A chooser that was RESIZED while it was open leaves the page behind it
        # wearing the frame it has not been given yet. ScreenResume does not
        # bubble here, so the app's screen-change signal is the hook; the page
        # only learns its real geometry on the way back. See _screen_changed.
        self.screen_change_signal.subscribe(self, self._screen_changed)

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
    def _speed_hour_job(self, key) -> FetchOutcome:
        """Hour-of-day for one selection: an exact key, or one merged model.

        The lens decides what ``key`` IS, and it decides first: unpacking a
        merged model's name as a four-part comparison key raised before the lens
        was ever consulted, so the combined hour view could not open at all.
        """
        date_from, date_to = self._speed_dates()
        if self._sp_lens == "combined":
            # A merged model's hours come from the trend payload: the exact-key
            # time-of-day route needs one key, and a merged model is not one key.
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
        source, model, kind, basis = key
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

    @work(group="speed-follow-io", exclusive=True, thread=True, exit_on_error=False)
    def _speed_job_follow(self, job_id: str) -> dict:
        """Blocking SSE follow (thread job: it must never sit on the loop).

        Delivers each status into ``_on_speed_event`` through call_from_thread,
        so the paint stays on the event loop. Returns a small result dict rather
        than raising: a lost subscription is a NOTE with a recovery key, not an
        app crash.

        ``stop`` is polled by the reader between lines. Without it a stream the
        user walked away from stayed open -- and kept painting -- until the
        service decided to close it, so a build for a window nobody is looking at
        could still move the note on screen.
        """
        token = self._sp_follow_token
        try:
            delivered = follow_output_speed_job(
                job_id,
                lambda status: self.call_from_thread(self._on_speed_event, status),
                stop=lambda: self._sp_follow_token != token,
            )
            return {"ok": True, "delivered": delivered}
        except Exception as exc:  # noqa: BLE001 — connection loss is a note, not a crash
            return {"ok": False, "error": str(exc)}

    def _stop_speed_follow(self) -> None:
        """Close any live build subscription and give up the following flag.

        Called on navigation away, a new window, a new source and a new timing
        set. ``_sp_following`` is what keeps the pane from subscribing twice to
        one job, so it comes down WITH the stream: left raised, the pane believed
        it was already watching and never followed the job that replaced it.
        """
        self._sp_follow_token += 1
        self._sp_following = False
        self._sp_follow_job = None

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
        the timing cache for a window the shared range never named.

        All Time resolves through ``resolve_period``, the very helper
        ``GET /api/output-speed`` runs, so "All Time" on the strip means the
        endpoint\'s own full span. It used to be replaced by a 365-day window
        while the label stayed "All Time": measurements older than that vanished
        and the comparison population changed under a constant name.
        """
        _period, date_from, date_to = self._request()
        if date_from and date_to:
            return (date_from, date_to)
        resolved = resolve_period(ALL_PERIOD)
        return (str(resolved["from"]), str(resolved["to"]))

    @work(group="overview", exclusive=True, exit_on_error=False)
    async def _load_overview(self, refresh: bool) -> None:
        gen = self._gen
        self._begin_load("overview", gen)
        scope = self._request()
        period, date_from, date_to = scope
        outcomes: list[FetchOutcome] = []
        try:
            try:
                u = await self._usage_job(period, date_from, date_to, refresh).wait()
                if gen != self._gen:
                    return
                self._ov_usage = u.value
                self._payload_scope["overview"] = scope
                outcomes.append(u)
                self._paint_overview()
                a = await self._active_job(period, date_from, date_to, refresh).wait()
                if gen != self._gen:
                    return
                self._ov_active = a.value
                self._payload_scope["overview"] = scope
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
        scope = self._request()
        period, date_from, date_to = scope
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
                        # This scope HAS been answered -- badly, but answered.
                        # Leaving it unstamped would leave a "computing" notice
                        # where the partial-failure line belongs.
                        self._payload_scope["sessions"] = scope
                        continue
                    if gen != self._gen:
                        return
                    self._sn_payloads[tool] = outcome.value
                    self._payload_scope["sessions"] = scope
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
        scope = ("stats", self._stats_win.request())
        outcomes: list[FetchOutcome] = []
        try:
            try:
                s = await self._stats_job(refresh).wait()
                if gen != self._gen:
                    return
                self._stats_payload = s.value
                self._payload_scope["stats"] = scope
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
        scope = self._request()
        period, date_from, date_to = scope
        outcomes: list[FetchOutcome] = []
        self._rp_insights = None
        self._rp_active = None
        self._rp_warnings = []
        self._rp_insights_failed = False
        try:
            try:
                u = await self._rp_usage_job(period, date_from, date_to, refresh).wait()
                if gen != self._gen:
                    return
                self._rp_usage = u.value
                self._payload_scope["report"] = scope
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
                    self._rp_insights_failed = True
                else:
                    if gen != self._gen:
                        return
                    self._rp_insights = i.value
                    self._payload_scope["report"] = scope
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
        # This read carries the series too, so any earlier series debt is paid.
        self._sp_series_pending = False
        self._sp_series_error = ""
        self._begin_load("speed", gen)
        scope = self._request()
        outcomes: list[FetchOutcome] = []
        try:
            try:
                p = await self._speed_across_job(refresh, cache_only).wait()
                if gen != self._gen:
                    return
                self._sp_payload = p.value
                self._payload_scope["speed"] = scope
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
        """Fill the hour-of-day table for the ONE selection this lens offers.

        Per stream the route answers for an exact (source, model, kind, basis)
        key. Combined has no such key: the merged series carries its own 24 local
        hours under ``hours``, and the entry's top-level arrays are the TIME
        buckets -- charting those as hours is how 24 measured bins once printed
        24 gaps.
        """
        if self._sp_lens == "combined":
            h = await self._speed_hour_job(self._sp_model or "").wait()
            if gen != self._gen:
                return
            pool = ((h.value or {}).get("model_series")) or []
            model = self._sp_model or next(
                (str(entry.get("model")) for entry in pool if entry.get("model")), None
            )
            merged = next(
                (entry for entry in pool if entry.get("model") == model), None
            )
            self._sp_hours = {
                "hours": (merged or {}).get("hours") or {},
                "selected_key": {"model": model} if model else {},
                "sources": (merged or {}).get("sources") or [],
                "kinds": (merged or {}).get("measurement_kinds") or [],
                "empty_reason": None if merged else "no merged model series in this snapshot",
                # The chooser needs the whole merged list, not just the entry
                # this table happens to be charting.
                "model_series": pool,
            }
            self._paint_speed()
            return
        key = self._sp_key or self._default_speed_key()
        if key is None:
            return
        h = await self._speed_hour_job(key).wait()
        if gen != self._gen:
            return
        self._sp_hours = h.value
        self._paint_speed()

    def _speed_trend_selection(self) -> tuple[object, str]:
        """The ONE series the Trend table charts, and what to call it.

        The two lenses select different THINGS: the combined lens names a merged
        model, the per-stream lens an exact comparison key. Both fall back to the
        snapshot's own first series, and this is the single place that decides.
        Passing the per-stream key to the merged renderer -- what used to happen
        -- left the heading naming one model while the rows measured another.
        """
        trend = self._sp_trend or {}
        if self._sp_lens == "combined":
            pool = [str(e.get("model")) for e in trend.get("model_series") or [] if e.get("model")]
            model = self._sp_model if self._sp_model in pool else (pool[0] if pool else None)
            return model, (f"merged model {model}" if model else "")
        keys = [tuple(k) for k in views.speed_trend_rows(trend).get("keys") or []]
        key = self._sp_key if self._sp_key in keys else (keys[0] if keys else None)
        return key, (self._speed_key_label(key) if key else "")

    def _speed_selection_label(self) -> str:
        """The series the ROWS on screen were measured for -- not the last pick.

        Hour-of-day answers for ONE selection, and its payload says which:
        ``selected_key`` is written by the read that produced these rows. Reading
        ``_sp_model`` / ``_sp_key`` instead named the request while the previous
        answer was still on screen, so a slow or failed series read put model
        one's rates under model two's heading -- and any repaint (a resize, a
        build-progress tick) could show it.
        """
        if self._sp_mode == "trend":
            return self._speed_trend_selection()[1]
        if self._sp_mode == "hour":
            key = (self._sp_hours or {}).get("selected_key") or {}
            if not key:
                return ""
            if self._sp_lens == "combined":
                model = key.get("model")
                return f"merged model {model}" if model else ""
            return self._speed_key_label(
                (key.get("source"), key.get("model"),
                 key.get("measurement_kind"), key.get("token_basis"))
            )
        return ""

    def _speed_requested_label(self) -> str:
        """What the reader last chose, landed or not."""
        if self._sp_lens == "combined":
            return f"merged model {self._sp_model}" if self._sp_model else ""
        return self._speed_key_label(self._sp_key) if self._sp_key else ""

    def _speed_series_state(self) -> tuple[str, bool]:
        """A note about the series read the rows have not caught up with.

        Returns ``(text, is_error)``; no text means the heading and the rows
        already agree. The wording stays DELIBERATELY anonymous: the figures under
        this line are still the PREVIOUS selection's, so naming the series that was
        asked for here would print its name above another model's rates, which is
        the very misattribution this line exists to warn about. What the reader
        picked goes in the detail under the table, where those rows are no longer
        beneath the name. Saying nothing at all is what let a failed read leave the
        pane looking finished.
        """
        wanted = self._speed_requested_label()
        if not wanted or wanted == self._speed_selection_label():
            return "", False
        if self._sp_series_error:
            # The technical error is NOT here: one strip line has to be readable
            # at a glance, and the sentence under the table -- where the rows that
            # belong to another series no longer sit -- carries the reason and the
            # name that was asked for.
            return ("series update failed · rows below are the previous series"
                    " · r Retry", True)
        if self._sp_series_pending:
            return ("series still loading · rows below are the previous series",
                    True)
        return "", False

    def _speed_series_warning_lines(self) -> list[list[Run]]:
        """The detail pane's version of that warning, which DOES name the pick.

        The note above the table cannot: it sits over rows that belong to another
        series. The detail sits under them, so it can say what is missing and how
        to ask for it again.
        """
        _text, bad = self._speed_series_state()
        wanted = self._speed_requested_label()
        if not bad or not wanted:
            return []
        if self._sp_series_error:
            return [[Run(f"requested {wanted}: read failed ({self._sp_series_error}) · "
                         "r Retry", "warn")]]
        return [[Run(f"requested {wanted} · its read is still on its way", "warn")]]

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
            elif self._active_pane() == "speed" and (
                    not self._sp_following or self._sp_follow_job != str(job_id)):
                if self._sp_following:
                    # This snapshot names a DIFFERENT build than the one being
                    # followed: hand the note over to the new job rather than
                    # letting a stream for the old one keep painting it.
                    self._stop_speed_follow()
                self._sp_following = True
                self._sp_follow_job = str(job_id)
                self._sp_follow_epoch = self._sp_follow_token
                self._follow_speed_job(str(job_id))
        else:
            self._sp_job = None
            self._sp_job_note = ""
            self._sp_gaveup = None  # a served snapshot ends the blackout

    @work(group="speed-follow", exclusive=True, exit_on_error=False)
    async def _follow_speed_job(self, job_id: str) -> None:
        token = self._sp_follow_token
        try:
            result = await self._speed_job_follow(job_id).wait()
        except Exception as exc:  # noqa: BLE001 — a dropped stream is a note, not a crash
            # The reader is gone, so the completion it was meant to deliver will
            # never arrive. The flag has to come down WITH it -- left raised, the
            # pane believes it is still watching, keeps saying "re-reading
            # snapshot", and never follows the build in force. Only for the
            # subscription still in force: a superseded one belongs to nobody.
            if token == self._sp_follow_token:
                self._stop_watching(job_id, f"update connection lost ({type(exc).__name__})")
            return
        if token != self._sp_follow_token:
            # Superseded -- the user navigated away, re-ranged, or changed the
            # source or timing set, each of which retires this build. The
            # subscription that replaced this one owns the note and the flag now,
            # and clearing either here would take the new stream's state down.
            # A manual reload is deliberately NOT in that list: it re-reads the
            # same snapshot and the same queued build, so the stream stays.
            return
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
        self._stop_speed_follow()
        self._sp_gaveup = job_id
        self._sp_gaveup_note = f"{what} · r Reload"
        self._sp_job_note = self._sp_gaveup_note
        self._paint_speed()

    def _on_speed_event(self, status: dict) -> None:
        """One build-progress event, and only ever for the build in force.

        The stream is a side channel: it answers to no generation guard, so a
        subscription that outlived its load could move the note under a window it
        knows nothing about. Both the job the payload names and the generation
        that subscribed have to still match, or the event is dropped.
        """
        named = status.get("id") or status.get("job_id")
        if named and self._sp_follow_job and str(named) != str(self._sp_follow_job):
            return
        if self._sp_follow_epoch != self._sp_follow_token or self._active_pane() != "speed":
            # Not the subscription in force, or nobody is looking at the pane it
            # belongs to. Either way the event has no screen to paint.
            return
        state = str(status.get("state") or "")
        self._sp_job_state = state or self._sp_job_state
        done = status.get("completed_inputs")
        total = status.get("total_inputs")
        progress = f" {done}/{total}" if done is not None and total else ""
        if state in _SP_JOB_DONE:
            # The build landed; the completion below re-reads cache-only. Say
            # which of the two the reader is watching rather than calling a
            # finished build "building" until that read returns.
            self._sp_job_note = "build finished · re-reading snapshot"
        else:
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
        # The timing (or the note saying it failed) has just grown the summary
        # above the turns, pushing the bottom of the table back under the fold
        # the first reveal scrolled it out of. A slow timing fetch left the last
        # turn hidden under the footer at 100x30.
        if self.focused is self.query_one("#sn-turns", DataTable):
            self._reveal_turns()

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
                    self._note_poll(
                        "[red]poll failed: " + str(err).replace("[", "\\[") + "[/]"
                    )
                return
            if gen != self._gen:
                return
            if isinstance(result, dict) and result.get("disabled"):
                # Never a fake "ok": tracking off means say-so and stop.
                self._note_poll(
                    emit_markup([[Run("quota tracking disabled — tokdash quota consent", "warn")]])
                )
                return
            self._note_poll("poll ok")
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
            [[Run("computing…", "muted")]]
            if self._state[pane] == "ok"
            else _SKELETON
        )
        # _set_alert takes run-lines; a Static takes markup. The Report body IS a
        # Static, and handing it the list raised VisualError INSIDE _begin_load --
        # which killed the reload before it issued a read, left the pane counting
        # a load that would never end, and left the previous document on screen.
        notice_markup = notice if isinstance(notice, str) else emit_markup(notice)
        # The head keeps naming the page and what it covers while the numbers are
        # on their way -- only the figures go. The strip goes through _set_alert
        # because a strip hidden for the healthy case has to be UN-hidden by
        # whoever puts text into it.
        self._paint_scope(pane)
        if pane != "report":
            # Report is a document rather than a table with a caption: its body
            # carries its own state line, so it gets no strip above it.
            self._set_alert(pane, notice)
        if pane == "overview":
            self.query_one("#ov-cards", Static).update("")
            activity = self.query_one("#ov-activity", Static)
            activity.update("")
            activity.display = False
            self._activity_painted = (-1,)
            self.query_one("#ov-table", DataTable).clear()
            self.query_one("#ov-detail", Static).display = False
        elif pane == "sessions":
            self.query_one("#sn-table", DataTable).clear()
        elif pane == "stats":
            self.query_one("#st-summary", Static).update("")
            self.query_one("#st-calendar", Static).update("")
            self.query_one("#st-days", DataTable).clear()
        elif pane == "report":
            self.query_one("#rp-body", Static).update(notice_markup)
        elif pane == "speed":
            self.query_one("#sp-table", DataTable).clear()
            self.query_one("#sp-chart", Static).display = False
            self.query_one("#sp-detail", Static).display = False
        elif pane == "pricing":
            self.query_one("#pr-table", DataTable).clear()
        else:
            # The credits section and the account detail are painted by the
            # same load, so they clear with the table: a reload that left the
            # last grants standing would show a second window's figures under
            # the "computing" notice.
            self.query_one("#quota-table", DataTable).clear()
            self.query_one("#quota-credits", Static).display = False
            detail = self.query_one("#quota-detail", Static)
            detail.display = False
            detail.update("")

    def _begin_load(self, pane: str, gen: int) -> None:
        self._inflight[pane] = gen
        self._open_loads[pane] = self._open_loads.get(pane, 0) + 1
        self._stranded.discard(pane)
        # A load drops every payload this pane is about to replace -- secondary
        # slots AND the primary one. Clearing only the secondaries left the old
        # window's headline figures in hand, and any repaint that happened before
        # the new answer arrived (a sort, a search, a resize) put them under the
        # NEW date label. Empty plus "computing" is the honest middle state.
        if pane == "overview":
            self._ov_usage = None
            self._ov_active = None
        elif pane == "report":
            self._rp_usage = None
            self._rp_insights = None
            self._rp_active = None
            self._rp_warnings = []
            self._rp_insights_failed = False
        elif pane == "speed":
            self._sp_payload = None
            self._sp_trend = None
            self._sp_hours = None
        elif pane == "stats":
            self._stats_payload = None
        elif pane == "quota":
            self._quota_history_val = None
            # A read in flight outranks the last poll's sentence: whatever it held
            # is about to be either confirmed or contradicted by newer snapshots.
            self._quota_note_hold = None
            # The account detail is painted from the same snapshot as the table:
            # left standing, it would describe a poll that no longer answers.
            self._quota_detail = None
        self._payload_scope[pane] = None
        self._load_started[pane] = time.monotonic()
        self._paint_loading(pane)
        self._update_status()

    def _end_load(self, pane: str, gen: int) -> None:
        self._open_loads[pane] = max(0, self._open_loads.get(pane, 0) - 1)
        if self._open_loads[pane]:
            return  # a newer load of this pane is running; it owns the next paint
        if pane in self._refit_owed:
            # The frame moved while this read was running and the repaint that
            # would have followed it was refused. The paint this load is about to
            # do measures whatever geometry has landed, which on a slow runner is
            # not the settled one; take the honest measurement once the layout
            # below it stops moving. Guarded by _refit_body's own stamp, so a
            # page that fitted itself correctly in the meantime repaints nothing.
            self._refit_owed.discard(pane)
            self.call_after_refresh(self._refit_body)
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

    def _scope_ok(self, pane: str) -> bool:
        """May this pane paint the payloads it is holding right now?

        Only under the scope that produced them. A range change while a request
        is in flight (or a visited pane that a range change left behind) leaves
        the previous window's answer in hand, and every repaint path -- sort,
        search, subview, resize, a cache status that just turned stale -- reads
        those slots. Painting them below the new dates is the one arrangement of
        figure and label that is always wrong, so the pane holds its pending
        notice instead, and the generation guard is no longer the only wall.
        """
        expected = (
            ("stats", self._stats_win.request()) if pane == "stats" else self._request()
        )
        return self._payload_scope.get(pane) == expected

    def _paint_pending(self, pane: str) -> None:
        """The held state for a pane whose data is not this window's."""
        self._paint_loading(pane)

    def _repaint_pane(self, pane: str) -> None:
        """Repaint one pane from the payloads already in hand (no new reads).

        The geometry the body was fitted to is recorded on the way out, so the
        pass that runs after the layout settles can tell a repaint that refitted
        the page from a repaint that fitted it against the size being replaced.
        """
        {"overview": self._paint_overview,
         "sessions": self._paint_sessions,
         "stats": self._paint_stats,
         "report": self._paint_report,
         "speed": self._paint_speed,
         "quota": self._paint_quota,
         "pricing": self._paint_pricing}.get(pane, lambda: None)()
        self._body_fitted[pane] = self._body_stamp(pane)

    def _body_stamp(self, pane: str) -> tuple:
        """The geometry a pane's BODY was laid out at: cells, rows, density.

        The row the status strip owns rides along with the pane's own box, and it
        is not redundant: the body's row budget runs from the top of its last
        section DOWN to that row, so a footer that re-wraps -- its data line
        growing a second one, the freshness text appearing or going -- changes
        what the body may spend while leaving the pane's width, height and
        density untouched. CI measured the difference: the pane had been handed
        the rows it was owed, the box measured right, and yet the activity band
        was still drawn for four rows more than the frame held, because the only
        guard between "fitted" and "stale" had never seen the strip move. With
        its row in the stamp, a footer that changes shape is a change the guard
        cannot walk past.
        """
        try:
            box = self.query_one(f"#pane-{pane}")
            strip_row = self.query_one("#status", Static).region.y
        except NoMatches:  # painted before compose (early failure paths)
            return ()
        return (box.content_region.width, box.content_region.height, self._compact,
                strip_row)

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
            # Its own sentence, with its own instruction: back-pressure is not a
            # fault and "error: busy computing" blames the reader for a full
            # queue they did not cause.
            line = Text("busy computing — press r", style="bold red")
        else:
            line = (Text("error: ", style="bold red")
                    + Text(str(err).replace("[", "\\["), style="red"))
        self._state[pane] = "error"
        if pane == "report":
            # Report's body IS the document; a strip over a blank page would leave
            # the reader hunting for the failure.
            self.query_one("#rp-body", Static).update(line)
        else:
            self._set_alert(pane, line)
        self._update_status()

    # ------------------------------------------------------------- status --

    def _active_pane(self) -> str:
        active = self.query_one("#tabs", TabbedContent).active or "pane-overview"
        return active.replace("pane-", "")

    #: The keys THIS pane answers, in the order a reader meets the controls.
    #: They sit at the foot of the screen because they are instructions, not
    #: data: nothing here may compete with a figure or cost a table a row.
    PANE_KEYS = {
        "overview": "/ search · s sort · enter open · esc close · [ ] range · c dates",
        "sessions": "/ search · s sort · h tools · enter whole session · [ ] range",
        "stats": "p year · P next year · ← → month · enter day opens overview",
        "report": "← → section · s sorts projects · [ ] range",
        # Speed's selectors are on screen LABELED with what they hold, but a
        # keyboard-only reader still needs the letter each one answers to, and
        # this is the one row that is always visible while they are looking at it.
        "speed": "h source · k series · b bucket · x timing · enter detail · [ ] range",
        "quota": "u poll now · enter account detail",
        "pricing": "/ search · read-only",
    }

    def _update_status(self) -> None:
        """The foot of the screen: this pane's keys, then what its data cost.

        Series identity, window and warnings belong to the pane above, beside the
        figures they describe. This row answers a different question -- what can I
        press, and how fresh is this. It is the one place on the screen where text
        may not be cut off by the terminal's width, because every member of it is
        true information the reader cannot re-derive: on a narrow terminal it
        becomes two rows instead of losing its tail.
        """
        pane = self._active_pane()
        keys = self.PANE_KEYS[pane]
        started = self._load_started.get(pane)
        computing = (
            f"computing… {int(time.monotonic() - started)}s" if started is not None else ""
        )
        cache = self._cache_str.get(pane, "cache —")
        # The WHOLE database line: where the store is, how big it is, or the
        # name of the variable that turned it off. It is the only place on the
        # screen that answers "which database are these numbers from", and the
        # short form of it could read "ready" before the read had landed.
        db = self._ensure_db_line()
        room = max(0, self._pane_screen_width())
        tail_keys = f"{keys} · r reload · ? keys · q quit"
        # The keys that are the same on every pane ride along with the keys
        # that are not, but only while the row can hold them: at 80 columns a
        # global tail pushed the freshness line off a two-row strip, which is
        # the one piece of footer a reader cannot get anywhere else.
        line = "  ".join(
            escape(part)
            for part in (tail_keys if cell_len(tail_keys) <= room else keys,
                        computing, cache, db) if part)
        status = self.query_one("#status", Static)
        if cell_len(line) <= room:
            self._strip_rows = 1
            status.update(line)
        else:
            # Too wide for one row: the keys keep the first row whole and the
            # data cost gets the second, in the order a reader needs it -- is
            # it still computing, how old is it, where did it come from. A
            # member leaves only when the second row cannot hold them, and
            # then it is the least actionable one that goes.
            first = tail_keys if cell_len(tail_keys) <= room else keys
            for parts in ((computing, cache, db), (computing, cache),
                          (cache, db), (computing,), (cache,), ()):  # noqa: B007
                second = "  ".join(escape(part) for part in parts if part)
                if cell_len(second) <= room:
                    break
            self._strip_rows = 2
            status.update(Text.from_markup(f"{escape(first)}\n{second}"))
        # The strip's height is now decided, and the panes are sized against the
        # space left below the frame -- so this is the moment to hand them back
        # any row the frame was drawing over.
        self._fit_panes()

    def _fit_panes(self) -> None:
        """Hand every pane back the rows the frame was drawing over.

        A TabPane is measured against its container, not against the widgets
        mounted above it inside that container -- the tab strip, and the shared
        range strip this app re-parents into the tab widget. The pane it draws
        therefore reaches one or two rows PAST the space the screen kept for it,
        and the status strip paints over the difference. Losing a row silently
        would be bad enough; the real damage is that the pane believes its
        viewport is that much taller, so a table clipped by the screen does not
        know it is clipped and never scrolls the hidden rows into view.

        Measured rather than hard-coded: the range strip is hidden on the three
        panes that own their own window (Stats, Quota, Pricing), so the overlap
        belongs to the frame as it stands, not to the widget tree.
        """
        # The row the strip owns is WORKED OUT, not waited for. Measuring it asks
        # the layout for the frame as it stands, and a resize arrives before that
        # layout: the handler would measure the strip where it USED to sit, hand
        # the panes the old frame's height, and conclude that nothing needed
        # doing. The strip is the screen's last widget and it is written with a
        # known number of rows, so the row is the screen's height minus those.
        measured = self.query_one("#status", Static).region.y
        rows = self._strip_rows
        limit = (self._pane_screen_height() - rows) if rows else measured
        if limit <= 0:
            return  # nothing is laid out yet; a resize or the next paint retries
        # What the LAST fit saw, panes included: a height asked of a widget is
        # not the height it has until the next layout, and the rows that land
        # then are the rows a body has to be re-fitted to. Each pane's STARTING
        # row rides along, not just its height -- the chrome above the panes (the
        # range strip, the dates input it grows) changes height without any
        # terminal resize, and a pane that slid down a row while keeping its
        # height is a pane that quietly lost a row of figures.
        stamp = (self._pane_screen_width(), self._pane_screen_height(), limit,
                 tuple(sorted((pane.region.y, pane.region.height)
                              for pane in self.query(TabPane))))
        moved = stamp != self._fit_stamp
        self._fit_stamp = stamp
        changed = False
        for pane in self.query(TabPane):
            if pane.region.height == 0:
                continue  # an inactive pane has no geometry to measure
            # An EXPLICIT height, not padding: padding leaves the pane's own box
            # where it was and only moves its contents, so the next measurement
            # would disagree with the last and the two would chase each other.
            # A set height is honoured to the row, which makes the second call
            # compute exactly what the first one set.
            room = max(1, limit - pane.region.y)
            # The height ASKED counts as well as the one laid out. A strip that
            # wraps to two rows and back before the next layout leaves a request
            # for the shorter pane behind it; measured by the laid-out height
            # alone, the second pass reads "already right" and lets that request
            # land. With the re-measures spent, nothing takes the row back.
            asked = pane.styles.height
            if (pane.region.height != room or asked is None or not asked.is_cells
                    or int(asked.value) != room):
                pane.styles.height = f"{room}"
                changed = True
        if self._fit_forced:
            # Whatever this pass found, it found it in a frame that had just moved
            # under it. Take the rows it wrote as a guess and measure once more;
            # the bound below is what stops a widget that resizes its neighbour
            # from chasing it forever.
            self._fit_forced = False
            changed = True
        if not (changed or moved):
            self._fit_rearms = 0  # settled: a later disagreement starts fresh
        elif not self._fit_pending and self._fit_rearms < self._FIT_MAX_REARMS:
            # Either a pane's height moved, or the frame this fit measured is not
            # the frame that got laid out: the strip's row, and so every pane's
            # height, is only known once the frame has been laid out with the text
            # just written into it. Re-measure after the refresh -- a settled frame
            # takes the branch above, so the chain stops. The bound is the brake on
            # two widgets that each resize the other.
            self._fit_pending = True
            self._fit_rearms += 1
            self.call_after_refresh(self._fit_after_refresh)

    def _speed_series_stale(self) -> bool:
        """Has Speed's series box changed size since the picture was drawn?

        Guarded by the number the paint recorded, so the repaint that fixes it
        settles rather than chasing itself: the second reading equals the first.
        """
        if self._active_pane() != "speed":
            return False
        stamp = self._speed_series_stamp()
        return self._sp_series_painted != (-1,) and stamp != self._sp_series_painted

    def _refit_speed_series(self) -> None:
        """Size the picture against the box the layout actually gave it.

        Runs on the refresh after the paint that had to guess, and stops as soon
        as the box agrees with the picture: the repaint records the same number
        it just read, so the second pass finds nothing to do. Waiting for the
        status tick instead would leave the pane showing a picture that overflows
        its container -- and the rows it stole are the table's -- for up to a
        second after every mode change, resize and reload.
        """
        if self._active_pane() == "speed" and self._speed_series_stale():
            self._repaint_pane("speed")

    def _fit_after_refresh(self) -> None:
        self._fit_pending = False
        self._fit_panes()
        # Rows handed back to a pane are rows its body was never fitted to: the
        # card grid, the table's rows and the band at the foot of the scroll were
        # all sized for the shorter box.
        self._refit_body()

    def _tick_status(self) -> None:
        # The interval outlives the page on the way out: Textual tears the
        # screens down before it stops the app's timers, and a tick in between
        # would ask for a status strip that is already gone.
        if self._load_started and self.is_running:
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
        self._stop_speed_follow()  # any watched build belongs to the old window
        self._paint_rangebar()
        for pane in RANGE_PANES:
            if pane == self._active_pane():
                self._started[pane] = True
                self._stranded.discard(pane)
                self._reload_pane(pane)
            elif self._started[pane]:
                # Visited, now hidden: mark it out of date and let the tab hook
                # pay when the user actually looks at it. Pressing "]" used to
                # fan out across every pane ever opened -- Speed's read can
                # queue a timing build and Sessions' fans out one request per
                # source -- so ordinary date stepping did background work for
                # views nobody is reading.
                self._stranded.add(pane)
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
        # Revealed or put away, that input sits ABOVE the panes, so the page just
        # gained or lost a row with no resize event to say so.
        self._page_moved()

    def action_pick_more(self) -> None:
        """``p`` -- the row's own chooser: more ranges, or more years."""
        if self._active_pane() in RANGE_PANES:
            self.push_screen(PresetPicker(self._today, self._win), self._on_preset)
            return
        self.action_stats_year(-1)

    def action_stats_year(self, delta: int = -1) -> None:
        """``p`` an earlier year, ``P`` a later one, back up to trailing 365 days.

        The ladder runs: trailing 365 days, this calendar year, then earlier
        years. It used to jump past the year the reader is standing in and had no
        way back, so "show me 2026" was unreachable and one key could only run
        away from the present.
        """
        if self._active_pane() != "stats":
            return
        moved = self._stats_win.step(1 if delta > 0 else -1, self._today)
        if moved == self._stats_win:
            return
        self._set_stats_window(moved)

    def _set_stats_window(self, window) -> None:
        self._stats_win = window
        # The new window has its own months. Carrying the old page number over
        # lands on some unrelated month of the new year (or off the end of it).
        self._stats_month = None
        self._stats_selected = None
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
        """``h`` — pick which sources this pane reads.

        A chooser, not a blind cycle: with ten supported tools, cycling is a
        hundred keystrokes and no way to see where you are. Sessions fans out
        over its own selection and Speed filters by its own; the two stay
        separate on purpose, so filtering one does not silently filter the other.
        """
        pane = self._active_pane()
        if pane == "sessions":
            options: list[tuple[str, object]] = [
                (f"All supported tools ({len(SESSION_TOOLS)})", ""),
            ]
            options += [
                (f"{label}  (tool={tool})", tool)
                for tool, label in (
                    (tool, tool_label(tool, TOOL_LABELS)) for tool in SESSION_TOOLS
                )
            ]
            active = "" if set(self._sn_sources) == set(SESSION_TOOLS) else (
                self._sn_sources[0] if len(self._sn_sources) == 1 else None
            )
            self.push_screen(ChoicePicker("Tools to read", options, active), self._on_sources)
        elif pane == "speed":
            # The same chooser Sessions uses, over the scan's OWN inventory. A
            # blind cycle was the earlier answer and it stranded readers two
            # options deep: once a filter is in place the response answers for
            # ONE source, so a walk built from what is on screen can only
            # alternate between that source and All. The list is also the only
            # place that shows the sources that were never measured at all.
            self._choose_speed_source()

    def _choose_speed_source(self) -> None:
        """``h``, or the labelled Source button: the whole list, in one pick."""
        if self._active_pane() != "speed":
            return
        options = [("All measured sources", "")]
        options += [(f"only {source}", source) for source in self._speed_inventory()]
        self.push_screen(
            ChoicePicker("Source to measure", options, self._sp_source or ""),
            self._on_speed_source,
        )

    def _on_sources(self, value: object) -> None:
        if value is None:
            return
        self._sn_sources = tuple(SESSION_TOOLS) if value == "" else (str(value),)
        self._sn_selected_key = None
        self._gen += 1
        self._started["sessions"] = True
        self._load_sessions(False)

    def _on_speed_source(self, value: object) -> None:
        if value is None:
            return
        self._sp_source = None if value == "" else str(value)
        self._restart_speed()

    def action_cycle_speed_source(self) -> None:
        """``H`` — All, then one source at a time, then back to All.

        The chooser (``h``) is the way to get somewhere specific; this is the way
        to compare what is on screen with what comes next without leaving the
        keyboard. Both read the same inventory, so a source that is only ever
        reachable through one of them does not exist.
        """
        if self._active_pane() != "speed":
            return
        order = ["", *self._speed_inventory()]
        current = self._sp_source or ""
        try:
            position = order.index(current)
        except ValueError:
            position = 0
        nxt = order[(position + 1) % len(order)]
        self._on_speed_source(nxt)

    def _restart_speed(self, *, cache_only: bool = True) -> None:
        """Re-read the timing snapshot for a new view, lens, source or timing set.

        Mode, lens, source and the whole-request toggle all change WHICH rows and
        which merged populations exist, so each one drops the exact key / merged
        model it was charting (that selection may not survive the change), stops
        watching any build queued for the old one, and re-reads. Repainting the
        previous payload is what made a lens switch look like a working view.
        """
        self._sp_key = None
        self._sp_model = None
        self._sp_gaveup = None
        self._stop_speed_follow()
        self._gen += 1
        self._started["speed"] = True
        self._load_speed(False, cache_only)

    def _speed_inventory(self) -> tuple[str, ...]:
        """Every source the timing scan knows, not just the rows on screen.

        A filtered response answers for ONE source, so deriving the inventory
        from the displayed rows made every other source unreachable after the
        first pick. Unavailable and hidden-by-filter sources count too: the
        reader is entitled to ask what they measured.
        """
        seen = set(self._sp_inventory)
        payload = self._sp_payload or {}
        for row in payload.get("rows") or []:
            if row.get("source"):
                seen.add(str(row["source"]))
        for entry in payload.get("source_status") or []:
            if isinstance(entry, dict) and entry.get("source"):
                seen.add(str(entry["source"]))
        for entry in payload.get("hidden_request_windows") or []:
            if isinstance(entry, dict) and entry.get("source"):
                seen.add(str(entry["source"]))
        self._sp_inventory = tuple(sorted(x for x in seen if x))
        return self._sp_inventory

    def _speed_model_inventory(self) -> list:
        """Every merged model this scope can answer for, from either payload.

        Trend mode holds the trend response and Hour mode holds its own copy of
        one; only one of the two slots is ever filled, so reading a single slot
        made ``k`` a dead key the moment the reader left the Trend view.
        """
        seen: list = []
        for payload in (self._sp_trend, self._sp_hours):
            for entry in ((payload or {}).get("model_series") or []):
                model = entry.get("model")
                if model and model not in seen:
                    seen.append(model)
        return seen

    def _speed_key_inventory(self) -> list:
        """Every exact (source, model, kind, basis) this scope offers.

        The trend response names the comparison keys; the across-models snapshot
        carries the same populations as flat rows, and that snapshot is what is
        in hand when the reader is on Hour and never opened Trend.
        """
        keys = [
            tuple(k)
            for k in views.speed_trend_rows(self._sp_trend or {}).get("keys") or []
        ]
        seen = set(keys)
        for row in (self._sp_payload or {}).get("rows") or []:
            key = (
                row.get("source"),
                row.get("model"),
                row.get("measurement_kind"),
                row.get("token_basis"),
            )
            if any(key) and key not in seen:
                seen.add(key)
                keys.append(key)
        return keys

    def _speed_selection_model_label(self) -> str:
        """Model (and timing word) of the series the ROWS measured, for the picker.

        Read from the loaded payload through `_speed_selection_label`'s own
        selection, so a delayed or failed series read cannot name the model that
        was merely asked for.
        """
        if self._sp_mode == "trend":
            key = self._speed_trend_selection()[0]
            if isinstance(key, (tuple, list)):
                key = (list(key) + [None] * 4)[1]  # the model of the key
        elif self._sp_mode == "hour":
            sel = (self._sp_hours or {}).get("selected_key") or {}
            model = sel.get("model")
            kind = sel.get("measurement_kind")
            if not model:
                return ""
            kind = views.TIMING_KIND_LABELS.get(str(kind), str(kind)) if kind else ""
            return f"{model} / {kind.split(' ')[0]}" if kind else str(model)
        else:
            return ""
        return str(key) if key else ""

    @staticmethod
    def _speed_key_label(key: object) -> str:
        """One comparison key in the pane's own words, not its wire ids.

        The Across table already calls a timing kind "response window" and a
        basis "output + reasoning". A chooser that picks the SAME key has to say
        the same words, or the two surfaces read like two different measurements.
        """
        source, model, kind, basis = (list(key) + [None, None, None, None])[:4]
        parts = [
            str(part) for part in (
                source,
                model,
                views.TIMING_KIND_LABELS.get(str(kind), str(kind)) if kind else "",
                views.TOKEN_BASIS_LABELS.get(str(basis), str(basis)) if basis else "",
            ) if part
        ]
        return " / ".join(parts) or EM_DASH

    def action_choose_series(self) -> None:
        if self._active_pane() != "speed" or self._sp_mode == "across":
            return
        if self._sp_lens == "combined":
            # A merged model IS the selection in this lens: one identity, not a
            # four-part key. Kept as a string end to end so nothing downstream
            # has to guess which lens produced it.
            options = [(str(model), str(model)) for model in self._speed_model_inventory()]
            active = self._sp_model
        else:
            options = [
                (self._speed_key_label(key), tuple(key))
                for key in self._speed_key_inventory()
            ]
            active = self._sp_key
        if not options:
            return
        self.push_screen(SpeedKeyPicker(options, active), self._on_series_picked)

    def _on_series_picked(self, value) -> None:
        if value is None:
            return
        if self._sp_lens == "combined":
            self._sp_model = str(value)
        else:
            self._sp_key = tuple(value)
        self._gen += 1
        # The rows on screen are still the previous series until this read lands.
        self._sp_series_pending = True
        self._sp_series_error = ""
        if self._sp_mode == "hour":
            self._load_hours_start()
        else:
            self._load_speed_trend_only()

    def action_toggle_windows(self) -> None:
        """``x`` — widen the measurement set to whole request windows.

        Some sources time the whole request rather than the decode window, so
        with the decode-only default they can have usage and no measured speed.
        The choice belongs to the user and is stated on screen every time,
        because the two sets are different populations, not a bigger sample.
        """
        if self._active_pane() != "speed":
            return
        self._sp_include_windows = not self._sp_include_windows
        self._sync_toggle_states()  # the button shows the set now in force
        self._sp_key = None
        self._sp_model = None
        self._sp_gaveup = None  # a different population may need its own build
        self._stop_speed_follow()
        self._gen += 1
        self._started["speed"] = True
        # A plain read: the first look at the widened set is allowed to queue the
        # work that computes it, and the subscription reports when it lands.
        self._load_speed(False)

    def _load_hours_start(self) -> None:
        self._gen += 1
        gen = self._gen

        async def _run() -> None:
            self._sp_series_error = ""
            try:
                await self._load_hours(gen)
            except Exception as exc:  # noqa: BLE001 — a failed series is a note
                if gen == self._gen:
                    self._sp_series_pending = False
                    self._sp_series_error = _series_error_text(exc)
                    self._paint_speed()
                return
            if gen == self._gen:
                self._sp_series_pending = False
                self._paint_speed()  # the note loses its "still loading" clause

        # Its OWN group. An exclusive worker with no group lands in Textual's
        # "default" group, where it cancels every ungrouped worker -- including
        # the open build subscription, whose reader is a thread worker. Choosing
        # a series is a view change, not a reason to drop a live stream.
        self.run_worker(_run(), group="speed-series", exclusive=True, exit_on_error=False)

    def _load_speed_trend_only(self) -> None:
        gen = self._gen

        async def _run() -> None:
            self._sp_series_error = ""
            try:
                await self._load_trend(gen)
            except Exception as exc:  # noqa: BLE001 — a failed series is a note
                if gen == self._gen:
                    self._sp_series_pending = False
                    self._sp_series_error = _series_error_text(exc)
                    self._paint_speed()
                return
            if gen == self._gen:
                self._sp_series_pending = False
                self._paint_speed()

        self.run_worker(_run(), group="speed-series", exclusive=True, exit_on_error=False)

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
                # The list is where the reader already was; the hand-off may put
                # the keys there, but it may not move the page to find them.
                self.query_one(table, DataTable).focus(scroll_visible=False)
            return
        if pane == "overview":
            self._ov_selected = None
            self._paint_overview()
        elif pane == "sessions":
            self._sn_detail_key = None
            self._sn_detail = None
            self._paint_sessions()
            # The detail and its turn table just went away; focus has to land
            # back on the list rather than on whatever the hidden table was --
            # and the list is already where the reader is looking, so the page
            # must not be dragged to prove it.
            self.query_one("#sn-table", DataTable).focus(scroll_visible=False)
        elif pane == "speed":
            self._sp_detail_kind = None
            self._sp_detail_key = None
            self.query_one("#sp-detail", Static).display = False
        elif pane == "quota":
            self._quota_detail = None
            self._paint_quota()

    def action_open_row(self) -> None:
        focused = self.focused
        if not isinstance(focused, DataTable):
            return
        pane = self._active_pane()
        if focused.id != PANE_TABLE.get(pane):
            # Focus is on a table this pane shows SECONDARILY (the open
            # session's turns). Keys mean the row under the cursor, so the
            # action has to know which table it is standing in.
            return
        key = _row_key_at(focused, focused.cursor_row)
        if key is None:
            return
        if pane == "overview":
            if (self._ov_subview == "breakdown" and isinstance(key, tuple)
                    and key[0] == "tool"):
                # Enter on a section header opens the tool in the Tools view,
                # where its models already get a table and a window of their
                # own; the section's own rows stay where they are.
                self._ov_subview = "harnesses"
                self._ov_selected = key[1]
                self._sync_toggle_states()
            else:
                self._ov_selected = key
                if self._ov_subview == "breakdown":
                    # Only an Enter pans the page -- a repaint of a caption that
                    # was already open must leave the scroll where it was.
                    self._detail_panned = False
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
            self._show_speed_row(focused, str(key))  # dispatches on the view
        elif pane == "quota":
            identity = self._quota_idents.get(str(key))
            if identity is None:
                # The blank spacer between provider groups. It is a row because a
                # DataTable cannot space rows any other way; it names no account.
                return
            self._quota_detail = quota_detail_rows(
                self._quota_state_val or {}, identity,
                now_ts=time.time(), labels=TOOL_LABELS,
            )
            self._paint_quota()

    def _show_speed_row(self, table: DataTable, key: str) -> None:
        """What the row under the cursor measured, in the view's own terms.

        The three Speed views key their rows differently: a comparison identity
        for Across models, a server bucket for Trend, an hour of the day for Hour.
        Looking every key up as the first kind is what told a reader that a valid
        bucket or hour "is no longer in this snapshot", so the view decides which
        resolver runs.
        """
        # The same tests the painter applies to choose its table: while a Trend or
        # Hour read is still in flight the pane is showing the Across-models rows,
        # and those keys are comparison identities.
        if self._sp_mode == "trend" and self._sp_trend is not None:
            self._show_speed_bucket(str(key))
        elif self._sp_mode == "hour" and self._sp_hours is not None:
            self._show_speed_hour(str(key))
        else:
            self._show_speed_comparison(str(key))

    def _reveal_cursor(self, table: DataTable) -> None:
        """Scroll the row under the cursor back into the table's own viewport.

        The detail a row opens is written UNDER the table, and Speed's pane does
        not scroll, so the rows the detail adds come off the bottom of the
        table's viewport -- which is exactly where the row the reader is standing
        on sits. Asked for synchronously the scroll would run against the layout
        that has not happened yet, so it runs once the new rows are counted.
        """
        def reveal() -> None:
            if table.row_count:
                # The cursor stays where it is; the widget's own scroll is what
                # moves, dragging the viewport back onto the visible row.
                table.move_cursor(row=table.cursor_row)

        self.call_after_refresh(reveal)

    def _speed_detail_rows(self, lines: list[list[Run]], *, cap: int = 0) -> list[list[Run]]:
        """Fit the explanation of an opened row into the rows this terminal has.

        Every line written here is a row taken from the picture and the table
        above it, and at 24 rows the arithmetic runs out fast -- which is how a
        table ended up drawn below the screen. The facts are PACKED onto fewer
        rows, never dropped: the denominator sentence is the reason Enter was
        pressed. A line that is carrying a warning keeps its own row, because a
        warning that shares a line with prose is a warning nobody reads.
        """
        room = self._text_width("#sp-detail", fallback=self._pane_width("speed") - 2)

        def wrap(line: list[Run], limit: int, style: str) -> list[list[Run]]:
            """One authored line, in as many rows as its width asks for.

            The RUNS are the units of the break, not the words: an authored line
            is "what the number is divided by", " · what overlaps", " · what does
            not exist", and a row that ends "count once · no" is a sentence
            broken at the column rather than at the thought. A run wider than the
            surface is the one thing that still gets word-wrapped.
            """
            # The LINE decides its own emphasis when it authored one. Forcing the
            # first row of every write to the header tone made the standing
            # caption -- three muted facts under the table -- arrive in the same
            # voice as the identity of a row the reader had opened.
            use = ("warn" if any(run.style == "warn" for run in line)
                   else style if not any(run.style for run in line)
                   else next((run.style for run in line
                              if run.style in ("warn", "header")), "muted"))
            units: list[Run] = [piece for run in line if run.text
                                for piece in (Run(part, use)
                                              for part in _wrapped(run.text, room))]
            rows: list[list[Run]] = []
            current: list[Run] = []
            used = 0
            for unit in units:
                width = cell_len(unit.text)
                if current and used + width > room:
                    rows.append(current)
                    current, used = [unit], width
                else:
                    current.append(unit)
                    used += width
            if current:
                rows.append(current)
            return rows or [[Run("", use)]]

        head = lines[:2]  # the row's identity, then its figures
        rest = lines[2:]
        quiet = [line for line in rest if not any(run.style == "warn" for run in line)]
        loud = [line for line in rest if any(run.style == "warn" for run in line)]
        out: list[list[Run]] = []
        for index, line in enumerate(head):
            out += wrap(line, 2, "header" if index == 0 else "muted")
        for line in loud:
            out += wrap(line, 2, "warn")
        # Four rows of prose under a table is a second pane, and every one of
        # them is a row taken from the picture and the rows above. The opened
        # row's own facts come first; the rest is packed, and what cannot be
        # packed is already in the strip above or on the row itself.
        limit = cap or (3 if self._compact else 4)
        # The most rows this block may spend. An opened row is allowed past the
        # standing caption's budget (see below) and the ledger subtracts whatever
        # it takes, so the cost lands on the table above rather than on the
        # status strip's row.
        ceiling = limit if not cap else limit + (2 if self._compact else 4)
        budget = max(1, limit - len(out))
        parts = ["".join(run.text for run in line) for line in quiet]
        # Packed with no limit of its own: the row count is decided here, in one
        # place, and only in whole facts. A limit inside the packer cut a fact in
        # half and said nothing about the half it took.
        packed = _fact_rows(parts, room, limit=len(parts) * 4, tail=False)
        if cap and len(packed) > budget:
            # The standing caption and the detail an OPENED row asks for do not
            # share a budget. The caption is a footnote and pays for itself out of
            # the picture's rows; the detail is the reason Enter was pressed, and
            # halving it is how "summed tokens over the UNION of busy time"
            # arrived as "summed tokens over the" -- a rate left standing with no
            # denominator, in the one view that may not have one.
            budget = min(len(packed), max(budget, ceiling - len(out)))
        out += [[Run(part, "muted")] for part in packed[:budget]]
        return self._fit_detail_rows(out, ceiling)

    @staticmethod
    def _fit_detail_rows(rows: list[list[Run]], ceiling: int) -> list[list[Run]]:
        """Cut the block to ``ceiling`` rows without ever cutting a warning.

        A plain slice was the last thing to run, and it cut from the bottom -- which
        is where the amber line sits. At the width that line matters most the
        identity sentence above it wraps to two rows, the slice lands on the warning,
        and what is left reads as a finished caption over rows that are NOT the
        series the reader asked for: the misattribution the line exists to prevent.
        So the block gives way from the bottom, a warn row is passed over, and only a
        block made of nothing but warnings is allowed to stand past its ceiling.
        """
        if len(rows) <= ceiling:
            return rows
        kept = list(rows)
        for index in range(len(kept) - 1, -1, -1):
            if len(kept) <= ceiling:
                break
            if not any(run.style == "warn" for run in kept[index]):
                del kept[index]
        return kept[:ceiling]

    #: Times a pane re-fit may re-measure before the frame is accepted as final.
    #: Three is what a resize needs: the pass that measures the old frame, the
    #: pass that hands the panes their rows, and the pass that agrees they settled.
    _FIT_MAX_REARMS = 3

    #: Rows the opened-row explanation may spend. The painter and the sizing
    #: pass must agree on this number, or the table is measured against a
    #: caption of a height it never got.
    _SPEED_DETAIL_CAP: int = 6

    def _write_speed_detail(self, lines: list[list[Run]]) -> None:
        """Paint the opened row's detail, then put the pane back in balance.

        The detail sits BELOW the series box, so the rows it takes are taken from
        the box: measured after the write, the picture is redrawn for the height
        that is left rather than the one there was before Enter was pressed.
        """
        detail = self.query_one("#sp-detail", Static)
        rows = self._speed_detail_rows(lines, cap=self._SPEED_DETAIL_CAP)
        detail.display = True
        detail.styles.height = str(len(rows))
        detail.update(emit_markup(rows))
        # Enter spends rows the box was sized without: the refit on the next
        # refresh re-divides the pane, and this is the number it divides against.
        self._sp_detail_wanted = len(rows)
        self.call_after_refresh(self._refit_speed_series)
        self._reveal_cursor(self.query_one("#sp-table", DataTable))

    def _held_key(self, table: DataTable) -> object:
        """The row key the cursor is on, before a repaint wipes it.

        Rebuilding a table is how a pane repaints, and a rebuild parks the cursor
        on row zero: the reader arrows down twenty buckets, the snapshot finishes
        building, and they are back at the top with no warning. The key is what
        survives the rebuild, so the key is what gets held.
        """
        return _row_key_at(table, table.cursor_row)

    def _show_speed_bucket(self, key: str) -> None:
        """Open one Trend bucket, and remember that THIS row is what is open."""
        self._sp_detail_kind = "bucket"
        self._sp_detail_key = key
        lines = self._speed_bucket_lines(key)
        if lines is None:
            self._speed_detail_gone(key, "bucket")
            return
        self._write_speed_detail(lines)

    def _speed_detail_gone(self, key: object, what: str) -> None:
        """The opened row left the snapshot between the keypress and the paint."""
        self._sp_detail_kind = None
        self._sp_detail_key = None
        self._write_speed_detail([[
            Run(f"selected {key} · {what} is no longer in this snapshot", "warn")
        ]])

    def _speed_open_detail(self) -> list[list[Run]] | None:
        """The open row's explanation, re-derived from the payload in hand.

        Speed repaints on a snapshot update, a resize and the row-ledger refit.
        Each of those used to write the VIEW's caption over the row the reader had
        opened -- the figures stayed, the explanation of what they were divided by
        vanished, and at the next repaint an open row could even be reported as
        missing. The open row is state, so it is repainted as state.
        """
        if self._sp_detail_kind is None:
            return None
        if self._sp_detail_kind == "bucket":
            return self._speed_bucket_lines(str(self._sp_detail_key))
        if self._sp_detail_kind == "hour":
            return self._speed_hour_lines(str(self._sp_detail_key))
        if self._sp_detail_kind == "comparison":
            return self._speed_comparison_lines(str(self._sp_detail_key))
        return None

    def _speed_bucket_lines(self, key: str) -> list[list[Run]] | None:
        """One Trend bucket: the series, its window, and the overlap it measured."""
        series, label = self._speed_trend_selection()
        trend = views.speed_trend_rows(self._sp_trend or {}, key=series, lens=self._sp_lens)
        row = next((r for r in trend["rows"] if r["key"] == key), None)
        if row is None:
            return None
        rate = row["per_stream"] if self._sp_lens == "stream" else row["combined"]
        # The bucket's OWN key leads: at day resolution two adjacent buckets share
        # a window, so a detail that named only the window could not say which
        # bucket the cursor was on -- and "which one" is why the key was pressed.
        lines = [[Run(f"{label or EM_DASH} · {key}", "header")]]
        if row["span"]:
            lines.append([Run("window ", "muted"), Run(row["span"])])
        if row["gap"]:
            lines.append([Run("no measured response in this bucket · a gap, not a zero", "warn")])
        lines.append([
            Run("rate ", "muted"), Run(rate),
            Run("  · responses ", "muted"), Run(row["calls"]),
            Run("  · parallelism ", "muted"), Run(row["parallelism"]),
        ])
        if self._sp_lens == "stream":
            lines.append([
                Run("per-stream ", "muted"), Run(row["per_stream"]),
                Run("  · combined ", "muted"), Run(row["combined"]),
                Run("  · p10 ", "muted"), Run(row["p10"]),
                Run("  · p90 ", "muted"), Run(row["p90"]),
            ])
        lines.append([Run(trend["spread_note"], "muted")])
        lines.append([Run(
            "one bucket of the series named above · b changes the resolution, k the series",
            "muted")])
        return lines

    def _show_speed_hour(self, hour: str) -> None:
        """Open one hour of the day, and remember that THIS row is what is open."""
        self._sp_detail_kind = "hour"
        self._sp_detail_key = hour
        lines = self._speed_hour_lines(hour)
        if lines is None:
            self._speed_detail_gone(hour, "hour")
            return
        self._write_speed_detail(lines)

    def _speed_hour_lines(self, hour: str) -> list[list[Run]] | None:
        """One hour of the day: the rate, the responses behind it, and its reach."""
        combined = self._sp_lens == "combined"
        hours = views.speed_hour_rows(self._sp_hours or {}, lens=self._sp_lens)
        row = next((r for r in hours["rows"] if r["hour"] == hour), None)
        if row is None:
            return None
        key = hours.get("selected_key") or {}
        if combined:
            who = " ".join(
                part for part in (
                    f"merged model {key.get('model')}" if key.get("model") else "",
                    "sources " + ", ".join(hours["sources"]) if hours.get("sources") else "",
                    "timing " + views._kinds(hours["kinds"]) if hours.get("kinds") else "",
                ) if part
            )
        else:
            who = self._speed_key_label(
                (key.get("source"), key.get("model"),
                 key.get("measurement_kind"), key.get("token_basis"))
            )
        lines = [[Run(f"{who or EM_DASH} · {hour}", "header")]]
        if row["gap"]:
            lines.append([Run("nothing measured in this hour · a gap, not a zero", "warn")])
        cells = [
            Run("rate ", "muted"), Run(row["rate"]),
            Run("  · responses ", "muted"), Run(row.get("calls", EM_DASH)),
        ]
        if combined:
            cells += [Run("  · parallelism ", "muted"), Run(row.get("parallelism", EM_DASH))]
        else:
            cells += [
                Run("  · eligible ", "muted"), Run(row.get("eligible", EM_DASH)),
                Run("  · coverage ", "muted"), Run(row.get("coverage", EM_DASH)),
            ]
        lines.append(cells)
        lines.append([Run(hours["note"], "muted")])
        return lines

    def _show_speed_comparison(self, key: str) -> None:
        """One Across-models row: what the rate was divided by.

        A tok/s figure means nothing without its comparison key, its basis and
        how much of the window it actually covers, so Enter shows those rather
        than repeating the number the table already shows.
        """
        self._sp_detail_kind = "comparison"
        self._sp_detail_key = key
        lines = self._speed_comparison_lines(key)
        if lines is None:
            # The snapshot moved under the cursor between the keypress and here.
            source, model, _timing = (key.split("|") + ["", ""])[:3]
            self._speed_detail_gone(model or key, "row")
            return
        self._write_speed_detail(lines)

    def _speed_comparison_lines(self, key: str) -> list[list[Run]] | None:
        """Build one Across-models row's explanation, or say the row is gone."""
        source, _model, _timing = (key.split("|") + ["", ""])[:3]
        rows = views.speed_rows(
            self._sp_payload or {}, lens=self._sp_lens, labels=TOOL_LABELS
        )["rows"]
        row = next((r for r in rows if f"{r.get('harness') or r.get('sources')}|{r['model']}|{r['timing']}" == key), None)
        if row is None:
            return None
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
        # The two lenses divide by DIFFERENT things, and Enter is where that gets
        # read. A merged rate divides the same tokens by the UNION of the busy
        # intervals, so two overlapping one-second responses of 100 tokens are
        # 200 tok/s combined and 100 tok/s per stream; saying "summed duration"
        # over a combined rate claims the second number while showing the first.
        lines.append([Run(
            "combined = summed tokens over the UNION of busy time · responses in flight at the "
            "same moment share that time, so this is wall-clock throughput, not a per-response rate"
            if self._sp_lens == "combined" else
            "per stream = summed tokens over the SUMMED response durations · each response is "
            "divided by its own time, whatever else was in flight", "muted")])
        lines.append([Run(
            "coverage is measured/eligible calls, not continuous days observed", "muted")])
        return lines

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
        self._note_poll("polling…")
        self._poll_quota()

    def _note_poll(self, text: str) -> None:
        """Say what the poll said, and go on saying it until a read contradicts it."""
        self._quota_note_hold = text
        self.query_one("#quota-note", Static).update(text)

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
        if button_id == "sn-tool-all":
            self._on_sources("")
            return
        if button_id == "sn-tool-more":
            self.action_choose_sources()
            return
        if button_id.startswith("sn-tool-"):
            self._on_sources(button_id[len("sn-tool-"):])
            return
        if button_id in ("ov-tab-harnesses", "ov-tab-models", "ov-tab-breakdown"):
            self._ov_subview = {
                "ov-tab-harnesses": "harnesses",
                "ov-tab-models": "models",
                "ov-tab-breakdown": "breakdown",
            }[button_id]
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
            self._restart_speed()  # re-read the snapshot for the new view
            return
        if button_id == "sp-pick-source":
            self._choose_speed_source()
            return
        if button_id == "sp-pick-series":
            self.action_choose_series()
            return
        if button_id == "sp-pick-bucket":
            self.action_cycle_bucket()
            return
        if button_id == "sp-windows":
            # The button and the x key are one control; a test that drives only
            # the key cannot see that the button on screen did nothing.
            self.action_toggle_windows()
            return
        if button_id.startswith("sp-lens-"):
            self._sp_lens = "stream" if button_id.endswith("stream") else "combined"
            self._sync_toggle_states()
            self._restart_speed()  # a new lens is a new population, not a repaint
            return

    def _sync_toggle_states(self) -> None:
        for key, active in (
            ("ov-tab-harnesses", self._ov_subview == "harnesses"),
            ("ov-tab-models", self._ov_subview == "models"),
            ("ov-tab-breakdown", self._ov_subview == "breakdown"),
            ("rp-tab-summary", self._rp_section == "summary"),
            ("rp-tab-when", self._rp_section == "when"),
            ("rp-tab-projects", self._rp_section == "projects"),
            ("sp-mode-across", self._sp_mode == "across"),
            ("sp-mode-trend", self._sp_mode == "trend"),
            ("sp-mode-hour", self._sp_mode == "hour"),
            ("sp-lens-stream", self._sp_lens == "stream"),
            ("sp-lens-combined", self._sp_lens == "combined"),
            # The whole-request setting is an ON/OFF preference, not a fourth
            # lens: it changes WHICH measurements are in the population, so it
            # reads as a ticked box and never as a pressed tab.
            ("sp-windows", self._sp_include_windows),
        ):
            try:
                self.query_one(f"#{key}", Button).set_class(active, "-selected")
            except Exception:  # noqa: BLE001 — before compose, nothing to sync
                pass
        # A checkbox needs a check mark: -selected is a PRESSED look, which on a
        # preference says "this tab is open" rather than "this box is ticked".
        try:
            self.query_one("#sp-windows", Button).label = (
                "Include whole-request timing"
                if not self._sp_include_windows else "☑ Include whole-request timing"
            )
        except Exception:  # noqa: BLE001
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
        # Each size change gets the settle budget afresh. The bound is there to
        # stop two widgets resizing EACH OTHER forever inside one settle; it was
        # never meant to ration a session, and on a CI runner -- where every
        # layout pass costs more, and where the first resize often arrives while
        # the reads from the last one are still landing -- the leftovers from a
        # previous resize were exactly what the chain needed and did not have.
        self._fit_rearms = 0
        self._repaint_chrome()
        self._fit_panes()  # the strip's share of the screen just changed shape
        # Guarded, for the same reason the deferred pass below is: a pane with a
        # load in flight is showing "computing…", and a repaint here would put
        # the previous window's figures back over a table its reload had just
        # cleared. A pane that IS painted re-fits to the new size -- which at
        # this instant means NO re-fit, since nothing has moved yet; the honest
        # re-fit is the one that runs once the layout lands.
        self._refit_body()
        # And the same again once the layout has landed. The resize event
        # arrives with the app's new size, but every measurement below it -- the
        # tab row's key chips, the strip's window names, the card band's row
        # count, a table's column set -- is taken from widgets that still have
        # the geometry being replaced. Measured on the live tmux session: the
        # nav row's letters only came back when something else happened to
        # repaint them, and a 120x36 Overview squeezed to 80 columns kept 112
        # cells of card inside a 78-cell widget, so "450.0K" arrived as "450".
        #
        # What follows is GUARDED at every step, never an unconditional second
        # pass: a pane painted at the size it was actually given is left alone.
        # An unconditional repaint would repaint the Speed pane for every resize
        # -- a repaint the panes that name the series a reader picked (rather
        # than the one a snapshot lists first) cannot tell from the relabel they
        # refuse. tests/test_tui_speed_series.py pins that.
        self.call_after_refresh(self._refit_after_layout)

    def _refit_after_layout(self) -> None:
        """Re-take the three things that are fitted to a SIZE, not to content.

        Runs after the refresh, because the settled measurements are the only
        honest ones: the labels (a tab row that chose its key chips while the
        window was narrow), the status strip (whose key tail is dropped at eighty
        columns, and whose second row then costs every pane a row it did not
        lose), and the body, whose card grid and column set were chosen at
        whatever width the widgets had the last time they were painted.

        The strip is the fiddly one: rewriting it moves the row that every pane
        height is measured against, and that lands on the NEXT layout pass. The
        chain is therefore carried by _fit_panes, which re-measures until the
        frame stops moving and hands the settled rows straight back here.
        """
        self._repaint_chrome()
        self._update_status()  # re-wraps the strip, then re-fits the panes
        self._refit_body()

    def _page_moved(self) -> None:
        """Say that the frame the page was last measured against is not real.

        Nothing here repaints a figure or starts a read: it re-arms the guarded
        fit pass, which hands back the rows the frame was drawing over and then
        re-fits the body against the rows it actually got. Used when a chooser
        goes away and when the chrome above the panes changes height, both of
        which move the page without any Resize event reaching the App.
        """
        self._fit_stamp = None
        self._fit_rearms = 0
        self._fit_forced = True
        self.call_after_refresh(self._refit_after_layout)

    def _screen_changed(self, _screen) -> None:
        """Re-take the page's fit when a resized chooser is dismissed.

        A pushed screen COVERS the page rather than moving it, so a terminal
        resize that arrives while a chooser is open is answered against the
        chooser: the fit chain measures widgets that are not on screen, settles,
        and the page keeps the layout of a width it no longer has. Measured at
        120x36 shrunk to 80x24 under the range picker: 118 cells of card inside a
        78-cell pane, so "450.0K" arrived as "450" -- and in the other direction
        the page stayed on three-across cards with room for six.

        Nothing on the way back asks again. ScreenResume does not bubble to the
        App, and the deferred refit the resize queued had already run against the
        chooser. The app's screen-change signal is published on push AND on pop
        with the screen that is now active, so this covers every chooser through
        the shared lifecycle instead of only the range picker's success callback.
        """
        if len(self.screen_stack) > 1:
            # A chooser going ON covers a page that has not moved. Note the fact,
            # because the refit is owed when -- and only when -- it goes away.
            self._chooser_was_up = True
            return
        if not self._chooser_was_up:
            return  # the app's first screen, or a mode change: nothing was covered
        self._chooser_was_up = False
        # The chain may have settled against the chooser, so it is told to measure
        # the page again rather than trust a stamp taken while nothing was visible.
        self._page_moved()

    def _refit_body(self) -> None:
        """Refit the page on screen, but only if its box is not the one it fitted.

        Two things stop it. A pane that never painted has no stamp to compare;
        and a pane with a LOAD IN FLIGHT is showing "computing…", not a payload --
        its state still says "ok" (that is how the loading copy knows it is a
        re-load rather than a first run), so the state is not the question. The
        load is. Repainting across it would pour the retained payload back over a
        table the reload had just cleared, which is the one thing a reader can
        see: figures under a "computing" line, for a window that has not answered.
        """
        pane = self._active_pane()
        if len(self.screen_stack) > 1:
            # A chooser is on top: the page is measuring the frame being replaced.
            # It is owed the pass the chooser prevented, and _screen_changed pays
            # it on the way back.
            self._refit_owed.add(pane)
            return
        if self._open_loads.get(pane):
            self._refit_owed.add(pane)  # _end_load re-arms this when it lands
            return
        if self._state[pane] != "ok":
            return
        self._refit_owed.discard(pane)  # this pass IS the refit it was owed
        if self._body_fitted.get(pane) == self._body_stamp(pane):
            return
        self._repaint_pane(pane)

    def _repaint_chrome(self) -> None:
        """Re-choose the labels whose shape depends on the screen's size.

        The tab row and the strip both compare the form they are about to wear
        with the one they are wearing, so a pass that changes nothing writes
        nothing. This never repaints a pane's figures.
        """
        self._apply_density()
        self._paint_tabs()
        self._paint_rangebar()

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
        # scroll_visible=False: this is a hand-off of the KEYBOARD, not a trip to
        # somewhere the reader asked to see, and focusing a widget scrolls it into
        # view -- with an ANIMATED scroll, so the page is still moving afterwards
        # for anything that measures it. On a short terminal that opened Overview
        # with its card band scrolled off the top, eight rows down, before a key
        # had been pressed. Where the table needs to be seen, the cursor movement
        # that follows brings it on screen.
        self.query_one(body).focus(scroll_visible=False)

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
                self._sp_follow_job = str(cache["job_id"])
                self._sp_follow_epoch = self._sp_follow_token
                self._follow_speed_job(str(cache["job_id"]))
        elif self._sp_following:
            # A hidden pane holds no subscription. The build goes on server-side
            # and the next look at Speed re-reads it (or re-subscribes); a stream
            # kept open behind another view only paints a pane nobody sees.
            self._stop_speed_follow()
        self._update_status()

    # ------------------------------------------------------------ painting --

    def _pane_width(self, pane_id: str) -> int:
        width = self.query_one(f"#pane-{pane_id}").content_region.width
        return width if width > 0 else 80

    def _text_width(self, selector: str, *, fallback: int = 80) -> int:
        """Cells a widget actually has for its own text.

        A line packed to the PANE's width instead of the WIDGET's is a line that
        wraps by exactly that widget's padding. In a frame of fixed rows that is
        not cosmetic: the wrapped half is a row the page loses, and the row it
        loses is always the one at the bottom -- the caption, the explanation,
        the last table line.
        """
        for widget in self.query(selector):
            if widget.region.width:
                return max(8, widget.content_size.width)
        return fallback

    def _paint_brand(self) -> None:
        """Name the palette the terminal actually agreed to.

        A design made of tints is the worst place to discover that the terminal
        refused them: the reader has no way to tell "the dashboard is grey" from
        "there is nothing to show", and both look like a broken tool. The answer
        is one short readout, on the row that is always on screen, that says
        which ceiling this terminal gave and, inside tmux, what would raise it.

        The word comes from the decision this process made (``terminal.py``),
        but NO_COLOR is checked against the App itself as well: Textual reads
        that variable when the App is BUILT, so an app that was built with it set
        has to say "off" whatever the shared readout last recorded. Saying
        "truecolour" over a grey screen is the exact lie this row exists to
        prevent.
        """
        state = terminal_palette()
        ceiling = str(state.get("ceiling") or "truecolor")
        word = {"truecolor": "truecolour", "256": "256 colours",
                CEILING_MONO: "off"}.get(ceiling, ceiling)
        if self.no_color:
            word = "off (NO_COLOR)"
        hint = ""
        if word.startswith("off"):
            # The one thing a reader with a grey screen can still do.
            hint = "  ·  TOKDASH_TUI_COLOR=always"
        elif "tmux" in str(state.get("reason") or ""):
            # Short on purpose: the row is one line, right-aligned, and a long
            # note eats the tail -- which is where the instruction was.
            hint = "  ·  tmux: set terminal-features truecolour"
        note = self.query_one("#brand-note", Static)
        note.update(escape(f"colour: {word}{hint}"))

    def _paint_tabs(self) -> None:
        """Put F1..F7 on the tabs, or take them off when the row cannot hold them.

        The key belongs where the choice is made, and the approved design says so
        by printing it in the tab. But a navigation row that scrolls sideways is
        worse than one without the hints, so the letters are the first thing the
        row spends: at eighty columns the seven names are all it can carry.
        """
        tabs = list(self.query(Tabs).first().query(Tab)) if self.query(Tabs) else []
        if len(tabs) != len(PANI):
            return
        room = self._pane_screen_width() - _TABS_PAD
        plain = sum(cell_len(PANE_TITLES[pane]) + _TAB_CHROME for pane in PANI)
        hinted = plain + sum(len(VIEW_FKEYS[pane]) + 1 for pane in PANI)
        hint = hinted <= room and plain <= room
        active = ""
        try:
            active = str(self.query_one("#tabs", TabbedContent).active or "")
        except Exception:  # noqa: BLE001 — before the panes exist (early paint)
            pass
        state = (hint, active)
        if self._tab_hint == state:
            return
        self._tab_hint = state
        for tab, pane in zip(tabs, PANI):
            name = PANE_TITLES[pane]
            if not hint:
                tab.label = Text(name)
                continue
            # The chosen tab's own name keeps CSS's colour (lavender ground, dark
            # ink) and only the KEY box inverts, so the reader can see which key
            # they are on without the row losing its focus and hover states.
            tab.label = Text.assemble(
                self._key_chip(VIEW_FKEYS[pane], selected=f"pane-{pane}" == active),
                name,
            )

    def _pane_screen_width(self) -> int:
        """Cells available to a full-width strip (the rangebar), never 0."""
        width = self.screen.size.width if self.screen is not None else 0
        return width if width > 0 else 80

    def _pane_screen_height(self) -> int:
        """Rows the app was given. Rows it did NOT get are the ones that clip."""
        height = self.screen.size.height if self.screen is not None else 0
        return height if height > 0 else 24

    def _mono(self) -> bool:
        return bool(self.no_color)

    #: Rows a terminal has to be given before a table row can afford a second
    #: line. The approved design gives every row more air than one line of
    #: digits, and a terminal has no line spacing, so the air IS a second line.
    #: The row it is bought from is the one below the fold: the page scrolls, and
    #: a table the reader can scan row by row is worth more than a page that
    #: squeezes seven rows of figures into seven lines to keep a chart in sight.
    #: ``-compact`` (thirty-one rows and under) still buys the dense grid, because
    #: there the second line costs a figure the reader came to read.
    _AIRY_ROWS = 32

    @property
    def _airy(self) -> bool:
        """Whether this screen can give every table row a second line."""
        return not self._compact and self._pane_screen_height() >= self._AIRY_ROWS

    @property
    def _row_air(self) -> int:
        """Lines each Overview row gets.

        The approved design gives a table row more air than one line of digits.
        A terminal has no line spacing, so the air is bought as a second row.
        Measured from the screen rather than from the table's own geometry,
        because the first paint of a pane has no geometry to measure and a page
        must not look like two different designs.
        """
        return 2 if self._airy else 1

    @staticmethod
    def _more_cells() -> int:
        """Cells the permanent "More" button costs, key hint included."""
        return len(MORE_KEY) + 1 + len("More") + _BUTTON_CHROME

    def _visible_presets(
        self, bar_width: int, dates_cells: int, *, hint: bool, roomy: bool
    ) -> set[str]:
        """The strip buttons that fit on one line WITH their dates.

        The exact dates are not negotiable -- a shortcut row that hides which
        window is live is how a dashboard lies -- so the buttons yield space to
        them, and anything that no longer fits goes behind "More". The five
        shortcuts go first, in their own order; a window reached from the chooser
        rides along only in whatever room is left.

        Only STRIP_PRESETS are candidates, and they are the WHOLE row: t, d, w,
        m and the door. Ten shortcuts on a row is a menu, and a menu the reader
        has to read is slower than the one key they meant. A window reached from
        the chooser does not buy itself a button here either -- the exact dates
        at the right of the row already name whatever is live, and a strip whose
        membership changes with the window is a row the reader cannot learn.
        """
        # What the strip actually has: its own padding, then the dates (with the
        # left padding the Static adds) and the typed-date box when it is open,
        # then the buttons -- and "More", which is here whether or not anything
        # is hidden behind it, because it is the door and the key that opens it.
        inner = max(0, bar_width - _RANGEBAR_PAD)
        room = max(0, inner - dates_cells - (_RANGE_INPUT_CELLS if self._custom_mode else 0))
        room -= self._more_cells()
        shown: set[str] = set()
        used = 0
        for preset in STRIP_PRESETS:
            cost = self._strip_cells(preset, hint=hint, roomy=roomy)
            if used + cost > room:
                break
            shown.add(preset.key)
            used += cost
        return shown

    def _paint_rangebar(self) -> None:
        """Highlight the preset that names the CURRENT window, and print the exact
        inclusive dates beside it. A shifted window matches no button, which is
        the point: the dates are the truth, the buttons are shortcuts."""
        if not self.query("#rangebar"):
            # The first resize can arrive before on_mount has put the strip in its
            # slot; a strip that is not on screen yet has nothing to highlight.
            return
        bar = self.query_one("#rangebar", Horizontal)
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
        tight = named(self._win.short_window())
        # The row is the same four shortcuts and their dates at every width, so
        # what a narrow terminal gives up is chosen rather than fallen into. In
        # order of willingness: the roomy wording, the exact dates in long form,
        # and only THEN the key letters, which are the whole point of the row.
        # The form that shows the MOST shortcuts wins; between two that show the
        # same number, the one earlier in this list wins. A button behind "More"
        # is the last resort, and the dates are the last thing to be shortened.
        candidates = (
            (True, True, False),   # hints, full names, dates with their day count
            (True, True, True),    # hints, full names, dates without it
            (True, False, False),  # hints, abbreviated names
            (True, False, True),
            (False, True, True),   # no hints: the last thing the row gives up
        )
        strip_keys = {preset.key for preset in STRIP_PRESETS}
        best = None
        for order, (hint, form, wide_dates) in enumerate(candidates):
            shown = self._visible_presets(
                screen, cells(tight if wide_dates else roomy), hint=hint, roomy=form
            )
            score = len(shown & strip_keys)
            if best is None or score > best[0]:
                best = (score, order, hint, form, wide_dates, shown)
        _, _, hint, form, wide_dates, shown = best
        dates_text = tight if wide_dates else roomy
        self.query_one("#range-dates", Static).update(escape(dates_text))
        # The window on screen, by preset: what the strip must name, and what its
        # key chip has to look chosen for. A typed pair of dates belongs to no
        # preset, which is why "More" -- the door that holds that form too -- is
        # the thing that looks chosen then.
        live = (
            "" if self._win.custom
            else next(
                (p.key for p in PRESETS
                 if p.key == self._win.preset and p.shift == self._win.shift),
                self._win.preset,
            )
        )
        # Relabel only when the form or the chosen window changed: Button.label is
        # a Reactive, and writing it every repaint (every resize, every load,
        # every keystroke that moves the cursor) spends a relayout.
        if self._range_form != (hint, form, live):
            self._range_form = (hint, form, live)
            for preset in PRESETS:
                self.query_one(f"#range-{preset.key}", Button).label = (
                    self._strip_label(
                        preset, hint=hint, roomy=form, selected=preset.key == live
                    )
                )
            self.query_one("#range-more", Button).label = self._more_label(
                selected=bool(self._win.custom)
            )
        for preset in PRESETS:
            button = self.query_one(f"#range-{preset.key}", Button)
            button.display = preset.key in shown
            _fit_button(button)
            button.set_class(
                # Textual 8 takes the flag first: set_class(bool, "class").
                self._win.preset == preset.key
                and self._win.shift == preset.shift
                and not self._win.custom,
                "-selected",
            )
        # Always on screen: it is the door to every window the row does not
        # hold, and the key hint that says "p" opens it.
        more = self.query_one("#range-more", Button)
        more.display = True
        _fit_button(more)

    def _paint_overview(self) -> None:
        self._sync_toggle_states()
        # The sectioned view bounds its own table (see CSS), so the pane says
        # which shape it is showing rather than the table guessing from its row
        # count -- a Tools list of the same length must keep its unbounded grid.
        try:
            self.query_one("#pane-overview").set_class(
                self._ov_subview == "breakdown", "-sectioned"
            )
        except Exception:  # noqa: BLE001 -- before compose, nothing to class
            pass
        if not self._scope_ok("overview"):
            self._paint_pending("overview")
            return
        usage = self._ov_usage or {}
        self._paint_scope("overview")
        self._set_alert(
            "overview",
            views.status_note(
                usage, self._ov_active, usage_status=self._cache_status.get("overview", "hit")
            ),
        )
        cards = views.kpi_cards(usage, self._ov_active, window=self._win.describe())
        width = self._pane_width("overview")
        # Six across, everywhere. Three across puts the six figures on TWO rows
        # of the band, and at a thirty-row screen those rows came straight off
        # the table and the activity picture underneath -- the headline read as
        # two stacked strips, which is the one shape the approved cards are not.
        # Sixteen cells per card carries the longest label here; below that the
        # cards switch to their short wording (TOTAL MESSAGES becomes MESSAGES),
        # because a clipped label is worse than a plainer one.
        per_row = _cards_per_row(width)
        self.query_one("#ov-cards", Static).update(
            _card_block(
                cards, per_row, width, self._mono(), air=0 if self._compact else 1
            )
        )
        # The sort lives here, next to the table it reorders, rather than in a
        # paragraph under it: "s" is invisible until the reader knows what it does.
        sort_note = self.query_one("#ov-sort", Static)
        if self._ov_subview in ("models", "breakdown"):
            sort_note.display = True
            label = dict(views.MODEL_SORTS).get(self._ov_sort, self._ov_sort)
            sort_note.update(escape(f"sorted by {label} · s changes it"))
        else:
            sort_note.display = False
            sort_note.update("")
        if self._ov_subview == "models":
            self._paint_models(usage, width)
        elif self._ov_subview == "breakdown":
            self._paint_breakdown(usage, width)
        elif self._ov_selected is not None:
            self._paint_harness_detail(usage)
        else:
            self._paint_harnesses(usage, width)
        self._paint_activity(usage)

    def _set_alert(
        self, pane: str, lines: list[list[Run]] | str | None, *, style: str = "warn"
    ) -> None:
        """Write (or clear) the pane's ONE status strip.

        ``None``, "" or an empty line list HIDES it: an empty strip is a tinted
        band across the page that says nothing, which is worse than no band.

        A warning in the strip is said in the strip's own amber, not in terminal
        yellow: the band runs the width of the screen, and the loudest colour on
        the page should be the one that says WHICH FIGURE IS WRONG, not that
        something is worth reading. A failure ("bad") keeps its red.
        """
        def softened(line: list[Run]) -> list[Run]:
            return [
                run if run.style != "warn" else Run(run.text, "amber") for run in line
            ]

        body = None
        if isinstance(lines, Text):
            if str(lines).strip():
                body = lines
        elif isinstance(lines, str):
            if lines.strip():
                body = emit_markup([[Run(
                    lines, "amber" if style == "warn" else style
                )]])
        elif lines:
            kept = [softened(line) for line in lines if line]
            if kept:
                body = emit_markup(kept)
        widget = self.query_one(PANE_ALERT[pane], Static)
        if body is None:
            widget.display = False
            widget.update("")
            return
        widget.display = True
        widget.update(body)

    def _paint_scope(self, pane: str) -> None:
        """Print what this pane's numbers cover, in the page head.

        Deliberately NOT the dates: the range strip one row above already names
        the window, and the same text twice on a 24-row screen is one more thing
        to check and one row of content lost. Stats is the exception because the
        shared strip is hidden on that pane.
        """
        if pane == "overview":
            if self._ov_subview == "models":
                text = "Normalized models · every tool"
            elif self._ov_subview == "breakdown":
                text = "Each tool's own models · not merged"
            elif self._ov_selected:
                label = TOOL_LABELS.get(self._ov_selected, self._ov_selected)
                text = f"{label} · this tool's models only"
            else:
                text = "All tools"
        elif pane == "speed":
            text = ("Combined output during busy clock time" if self._sp_lens == "combined"
                    else "Output per measured stream-second")
        elif pane == "sessions":
            asked = len(self._sn_sources)
            text = ("All session tools" if asked == len(SESSION_TOOLS)
                    else f"{asked} of {len(SESSION_TOOLS)} session tools")
        elif pane == "stats":
            text = self._stats_win.describe(self._today)
        elif pane == "report":
            text = "All tools"
        elif pane == "quota":
            text = "Current snapshots · all providers"
        else:
            text = "Current effective rates"
        widget_id = {
            "overview": "#ov-scope", "sessions": "#sn-scope", "stats": "#st-scope",
            "report": "#rp-scope", "speed": "#sp-scope", "quota": "#quota-scope",
            "pricing": "#pr-scope",
        }[pane]
        try:
            self.query_one(widget_id, Static).update(escape(text))
        except Exception:  # noqa: BLE001 — head not composed yet (early paint)
            pass

    @staticmethod
    def _shape(
        table: DataTable,
        spec: tuple[tuple[str, str, int, bool], ...],
        rows: list[tuple] | None = None,
        budget: int | None = None,
        reserve: int = 0,
        name_index: int = 0,
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
        # ``reserve``: cells the viewport will lose AFTER the fit. A table taller
        # than its box grows a vertical scrollbar, and that scrollbar takes a
        # column the fit had already spent -- which is how a "Responses" column
        # that measured perfectly on paper arrived on screen as "22".
        budget = (_table_budget(table) - reserve) if budget is None else budget
        budget = max(12, budget)
        table.clear(columns=True)
        for key, label, width in fit_columns(sized, budget, name_index=name_index):
            # A column narrower than its own label prints "Hour (server clock" --
            # a clipped header is a wrong header, so the label always fits and
            # the FIT above is what gives room to the columns that matter more.
            table.add_column(label, key=key, width=max(width, len(label) + 1))

    @staticmethod
    def _add_row(
        table: DataTable, cells: list[tuple[str, object]], key=None, height: int = 1
    ) -> None:
        """Add one row by COLUMN KEY, so a fitted (shorter) spec cannot misalign.

        Every cell is named, and the row is assembled in the order of the columns
        the table currently has. When ``fit_columns`` drops an optional column at
        a narrow terminal the numbers stay under their own headers instead of
        sliding one column left, which is the failure mode of positional rows.

        Words a column cannot hold are cut with a visible ellipsis. DataTable
        clips without a trace otherwise, and "after first token, respo" reads
        exactly like a complete timing kind.

        ``height`` is the row's own height in terminal rows, and it is how this
        design gets the approved layout's air: a terminal cannot space its lines,
        so a row that should breathe is a row that is two lines tall. Cells that
        carry a line break (a session's name over its id) are what fills the
        second line; a single-line cell sits on the first, which is the line the
        eye scans.
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
            table.add_row(*row, height=height)
        else:
            table.add_row(*row, key=key, height=height)

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
        spec = [("harness", "Tool", 22, False), ("tokens", "Tokens", 8, False),
                ("share", "Share", 6, False), ("cost", "Cost", 10, False)]
        if wide:
            spec += [("input", "Input", 8, True), ("output", "Output", 8, True),
                     ("cache", "Cache", 8, True), ("hit", "Hit", 7, True),
                     ("msgs", "Msgs", 8, True)]
        spec.append(("time", "Est. time", 14, False))
        # reserve=2: the tool list is capped in height, and the vertical
        # scrollbar it grows takes a column off the viewport AFTER the fit has
        # spent it -- which is how "20h 35m" reached the screen as "20h 35".
        self._shape(table, tuple(spec), reserve=2)
        selected = self._ov_selected
        for row in rows:
            self._add_row(table, [
                ("harness", _source_cell(row["tool"], row["harness"])),
                ("tokens", _num_cell(row["tokens"])),
                ("share", _num_cell(usage_share(usage, row["tokens_sort"]))),
                ("cost", _num_cell(row["cost"])),
                ("input", _num_cell(row.get("input", EM_DASH))),
                ("output", _num_cell(row.get("output", EM_DASH))),
                ("cache", _num_cell(row.get("cache", EM_DASH))),
                ("hit", _num_cell(row.get("hit", EM_DASH))),
                ("msgs", _num_cell(row.get("messages", EM_DASH))),
                ("time", _num_cell(row["agent_time"])),
            ], key=row["tool"], height=self._row_air)
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
        self._shape(table, tuple(spec), reserve=2)
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
            ], key=row["name"], height=self._row_air)
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
            "merged across every tool · reasoning is not part of this figure · "
            f"rows listed cover {result['coverage']} of the reported total",
        ]
        detail.display = True
        detail.update(emit_markup([[Run(line, "header" if i == 0 else None)] for i, line in enumerate(lines)]))

    def _paint_breakdown(self, usage: dict, width: int) -> None:
        """Every tool's OWN models, one section per tool (Tools&Models).

        The dashboard's "Apps & Models Breakdown", drawn in a widget that has no
        group heading: the row that NAMES a tool carries that tool's own totals
        in the same columns as the models under it, so a section reads top-down
        and a header stays comparable with the same tool's row in the Tools
        view. Nothing here is merged -- a model two tools used is two rows,
        because the question this table answers is what ONE tool spent. That is
        also why it carries no Share-of-everything column: the denominator a
        model row owns is its own tool.

        A tool with no model list of its own gets no section, exactly as the
        web leaves it out, and a dash in an Input/Output/Cache cell means that
        source does not report the figure, not that it recorded a zero.
        """
        table = self.query_one("#ov-table", DataTable)
        wide = width >= 104
        result = views.tool_model_sections(
            usage, sort=self._ov_sort, search=self._ov_search, labels=TOOL_LABELS
        )
        label = "Tool · model"
        if result["searched"]:
            tools = f"{result['sections_listed']} of {result['sections_total']} tools"
            models = f"{result['models_listed']} model"
            if result["models_listed"] != 1:
                models += "s"
            label = f"Tool · model · {tools} · {models}"
        spec = [("model", label, 30 if wide else 40, False),
                ("tokens", "Tokens", 8, False), ("cost", "Cost", 10, False)]
        if wide:
            spec += [("input", "Input", 8, True), ("output", "Output", 8, True),
                     ("cache", "Cache", 8, True), ("hit", "Hit", 7, True),
                     ("msgs", "Msgs", 8, True)]
        self._shape(table, tuple(spec), reserve=2)
        for section in result["sections"]:
            # The header row is keyed by the TOOL, so Enter can open it; the
            # model rows are keyed by (tool, name) because the same name rides
            # under two tools here and a key has to name one row.
            self._add_row(table, [
                ("model", _source_cell(section["tool"], section["harness"])),
                ("tokens", _head_cell(section["tokens"])),
                ("cost", _head_cell(section["cost"])),
                ("input", _head_cell(section["input"])),
                ("output", _head_cell(section["output"])),
                ("cache", _head_cell(section["cache"])),
                ("hit", _head_cell(section["hit"])),
                ("msgs", _head_cell(section["messages"])),
            ], key=("tool", section["tool"]), height=self._row_air)
            for row in section["models"]:
                self._add_row(table, [
                    ("model", escape(f"  {row['name']}")),
                    ("tokens", _num_cell(row["tokens"])),
                    ("cost", _num_cell(row["cost"])),
                    ("input", _num_cell(row["input"])),
                    ("output", _num_cell(row["output"])),
                    ("cache", _num_cell(row["cache"])),
                    ("hit", _num_cell(row["hit"])),
                    ("msgs", _num_cell(row["messages"])),
                ], key=("model", section["tool"], row["name"]),
                    height=self._row_air)
        self._restore_cursor(table, self._ov_selected)
        self._paint_breakdown_detail(result)

    def _paint_breakdown_detail(self, result: dict) -> None:
        """The opened row of the sectioned view, in the view's own terms.

        A model row is one tool's figure for that model and says so: the merged
        figure a reader may be looking for lives in the Models view, and the
        two numbers are allowed to differ because they answer different
        questions.
        """
        detail = self.query_one("#ov-detail", Static)
        key = self._ov_selected
        section = None
        if isinstance(key, tuple) and key[0] in ("model", "tool"):
            section = next(
                (s for s in result["sections"] if s["tool"] == key[1]), None
            )
        if section is None:
            self._detail_panned = False
            if not result["sections"]:
                detail.display = True
                detail.update(emit_markup([[
                    Run("this window's sources recorded no per-tool model lists",
                        "muted")]]))
                return
            detail.display = False
            detail.update("")
            return
        if key[0] == "tool":
            lines = [
                section["harness"],
                f"tokens {section['tokens']} · cost {section['cost']} · "
                f"messages {section['messages']} · hit {section['hit']}",
                "enter a model below for its figures · enter this row to open "
                "the tool in the Tools view",
            ]
        else:
            model = next(
                (m for m in section["models"] if m["name"] == key[2]), None
            )
            if model is None:
                detail.display = False
                detail.update("")
                return
            lines = [
                f"{model['name']} · {section['harness']}",
                f"tokens {model['tokens']} · share of this tool {model['share']} · "
                f"cost {model['cost']} · messages {model['messages']}",
                f"input {model['input']} · output {model['output']} · "
                f"cache read {model['cache']} · hit {model['hit']}",
                "this tool's own figure · the Models view merges this model "
                "across every tool",
            ]
        detail.display = True
        detail.update(emit_markup([
            [Run(line, "header" if i == 0 else None)]
            for i, line in enumerate(lines)
        ]))
        if not self._detail_panned:
            # The reader pressed Enter to see THIS row, so the page goes to it:
            # the sectioned table is taller than most terminals, and a caption
            # that opened below the fold is a caption that never opened. Same
            # two hops Sessions uses for its turn table -- the first refresh
            # measures the pane as it was before the caption re-wrapped.
            self._detail_panned = True
            self.call_after_refresh(
                lambda: self.call_after_refresh(detail.scroll_visible)
            )

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
            ], key=row["name"], height=self._row_air)
        detail = self.query_one("#ov-detail", Static)
        scope = (
            "this source's models · the figures above stay global"
            if tool in SESSION_TOOLS
            else "this source's models · no session list for this source"
        )
        detail.display = True
        detail.update(emit_markup([[Run(scope, "muted")]]))

    def _paint_activity(self, usage: dict) -> None:
        """The window's shape, drawn from the buckets THIS response carries.

        ``usage.sparkline`` is global -- totals over the window, one series -- so
        this is the only activity picture the payload supports, and there is no
        per-harness version of it to draw. Past the resolver's bound a range
        carries no buckets at all; the honest answer is then the sentence that
        says so, not a line guessed from the totals.
        """
        widget = self.query_one("#ov-activity", Static)
        # Three cells of slack under the measured width: the caption is packed to
        # this number and then drawn by a widget whose width the NEXT layout pass
        # decides, and a caption packed to the frame being replaced loses its
        # tail -- "peak 612.0K tokens ·" -- right where the peak was. The pane
        # itself grows a vertical scrollbar once the table pushes past the fold,
        # and that bar takes the column the caption was packed into.
        room_width = max(24, self._text_width(
            "#ov-activity", fallback=self._pane_width("overview") - 4) - 3)
        strip = views.activity_strip(
            usage.get("sparkline"),
            width=max(8, min(28, room_width // 5)),
            glyphs=_safe_glyphs(),
            max_rows=6 if room_width >= 96 else 3,
        )
        widget.display = True
        if not strip["available"]:
            # One line, and it says why there is no picture: a range past the
            # resolver's bound carries no buckets, and a blank box reads as a
            # broken one.
            self._activity_painted = self._activity_stamp(widget)
            widget.update(emit_markup([[Run(strip["note"], "muted")]]))
            return
        entries = views.chart_entries(strip["all_rows"], "value", text_key="tokens")
        title = f"Token activity · {strip['bucket_count']} {strip['unit']} buckets"
        # The pane scrolls and this band is the last thing in it, so "rows left
        # on the first screen" is a smaller number than "rows the band may have":
        # seven rows of tools leave five, which buys a title and two words and no
        # picture at all. Refusing the chart on that arithmetic is what left a
        # hole of navy under a short table -- and a reader who scrolls to the
        # band expects to find the picture THERE, not a sentence about it. The
        # floor is the shortest assembly that is still a picture: title, the
        # value over each column, three rows of bar, the axis, two rows of facts.
        room = max(self._rows_above_strip(widget), _ACTIVITY_MIN_ROWS)
        self._activity_painted = self._activity_stamp(widget)
        # Shorten the PICTURE before shortening anything that says a number, and
        # stop at the first height whose WHOLE assembly -- title, the value over
        # each column, the bars, the axis, the caption -- fits the rows the pane
        # has left. A chart whose caption is below the fold is a picture without
        # units; a chart whose columns are below the fold is a picture of nothing.
        lines: list[list[Run]] = []
        spare_caption: list[str] = []
        # Four rows held back, not three: title, axis and the caption's SECOND
        # row. At three the caption arrived with a row missing and the packer
        # spent it telling the reader to widen the window.
        want = min(6, max(2, room - 4)) if room else 2
        for height in range(want, 0, -1):
            drawn = column_chart(
                entries, width=max(24, room_width), height=height,
                glyphs=_safe_glyphs(), style="series", unit="tokens",
            )
            if not drawn["available"]:
                widget.display = False
                widget.update("")
                return
            lines = [[Run(title, "bold")], *drawn["lines"]]
            # The bucket keys go here in FULL: the axis under the bars has to fit
            # a column, this line has the whole surface. Peak on the right when
            # both fit on one line; when they do not, two lines that break between
            # facts rather than inside one.
            # Each fact on its own, so the packer breaks BETWEEN them: joined
            # into one string, "peak 612.0K tokens · 1 bucket measured 0 tokens"
            # is a single long fact and wraps at the column, arriving as
            # "... tokens · 1" over "bucket measured 0 tokens".
            # Ordered by what only this band can say: the span, how much of it
            # was measured, the stubs on the baseline that are measurements of
            # zero rather than gaps, and the peak. The peak is the fact the table
            # can also read out, so it is the last to be paid for.
            facts = [
                f"{drawn['measured']} of {drawn['columns']} buckets measured",
            ]
            if drawn.get("zeros"):
                facts.append(f"{drawn['zeros']} bucket"
                             f"{'s' if drawn['zeros'] != 1 else ''} measured 0 tokens")
            # The span LAST: the axis under the bars and the strip at the top of
            # the screen both already say which days these are, so it is the fact
            # that gives way when the band is down to one row.
            facts.append(str(strip["note"]))
            right = (f"peak {drawn['peak_text']} tokens at {drawn['peak_key']}"
                     if drawn.get("peak_text") else "")
            left = " · ".join(fact for fact in facts if fact)
            if cell_len(left) + 3 + cell_len(right) <= room_width:
                caption = [_two_ends(left, right, room_width)]
            else:
                caption = [fact for fact in facts + [right] if fact]
            # Kept for the branch that paints the facts with NO picture: every
            # height can be refused below for want of a row, and the band must
            # still say what the window held. A title alone over four blank rows
            # is the hole this branch exists to avoid.
            spare_caption = list(caption)
            spare = room - len(lines)
            if spare <= 0:
                continue
            # notice=False: the facts that gave way are already under the
            # reader's cursor in the table above; a tally of them is a row of
            # furniture in the one band that shows the numbers.
            packed = _packed(caption, room_width, limit=spare, notice=False)
            # A caption that lost a fact to the row budget buys that row back
            # from the PICTURE, not from the facts: "1 bucket measured 0 tokens"
            # and the span are said by this band and nowhere else on the page,
            # while two bars at six rows and two bars at four say the same
            # shape. Only the shortest picture is allowed to arrive with words
            # it could not fit.
            full = _packed(caption, room_width, limit=len(caption),
                           notice=False)
            if len(packed) < len(full) and height > 1:
                continue
            lines += [[Run(part, "muted")] for part in packed]
            if len(lines) <= room:
                break
        else:
            # No picture fits. That is not the same as no activity: the panel
            # keeps its title and the sentence that says what the window held
            # (buckets measured, the zeros, the peak), which is the part a
            # reader cannot recover from the table. Hiding the panel instead
            # leaves a hole of navy where the window's shape used to be, and a
            # hole reads as a broken chart rather than a short terminal.
            #
            # This branch sizes itself, so it is measured against the rows there
            # really are and not against the floor: a sentence packed to eight
            # rows in a page holding four is the same spill as a chart that
            # needed them, and Windows CI caught it here -- the band nine rows
            # tall, four of them underneath the status strip.
            given = self._rows_above_strip(widget)
            if given < 2:
                widget.display = False
                widget.update("")
                return
            widget.styles.height = "auto"
            widget.update(emit_markup(
                [[Run(title, "bold")]]
                + [[Run(part, "muted")] for part in _packed(
                    spare_caption, room_width, limit=max(1, given - 1),
                    notice=False)]
            ))
            self.call_after_refresh(self._refit_activity)
            return
        # And then it is measured AGAIN, against the rows there really are. The
        # floor above buys a picture where a short neighbour has left five rows,
        # which is the right trade -- but a picture that needs eight of them draws
        # three of itself through the status strip and off the terminal, and an
        # auto-height surface does not complain about being that tall. The rows the
        # page leaves are the ceiling: the band may fill them, and may not pass
        # them. Given fewer, it keeps its title and its sentence, which is what
        # the no-picture branch below has always been for.
        given = self._rows_above_strip(widget)
        if len(lines) > given:
            if given < 2:
                # One row is not an assembly; it is a heading. Say which band this
                # is and leave the geometry stable -- hiding it here would make the
                # next measurement disagree, and the picture would flicker.
                widget.styles.height = "1"
                widget.update(emit_markup([[Run(title, "bold")]]))
                self.call_after_refresh(self._refit_activity)
                return
            widget.styles.height = "auto"
            widget.update(emit_markup(
                [[Run(title, "bold")]]
                + [[Run(part, "muted")] for part in _packed(
                    spare_caption, room_width, limit=max(1, given - 1),
                    notice=False)]
            ))
            self.call_after_refresh(self._refit_activity)
            return
        widget.update(emit_markup(lines))
        # The band is drawn to the rows the pane is actually leaving, not to the
        # rows the picture happened to need. A surface that stops where its text
        # stops leaves a hole of page background under the chart, and the page
        # reads as a strip of content floating above an empty terminal.
        widget.styles.height = str(max(len(lines), given))
        # The rows this was sized against are the ones the PREVIOUS paint left
        # behind; once the frame settles, redraw for the rows there really are.
        self.call_after_refresh(self._refit_activity)

    def _activity_stamp(self, widget: Static) -> tuple:
        """What the last activity picture was divided against.

        The pane's width decides how many bucket keys fit under the bars, the rows
        under the strip decide how tall the picture may be, and the density flag
        decides whether the sections above it carry a blank row. Anything else
        that moves -- new data, a different subview -- is a real repaint anyway.
        """
        return (widget.region.width, self._rows_above_strip(widget), self._compact)

    def _refit_activity(self) -> None:
        """Size the activity picture against the rows the layout really gave it.

        The first paint of a pane runs before Textual has any geometry, so the
        picture is sized from a fallback; without this pass the estimate sticks,
        and an over-tall picture pushes its own caption -- and at 24 rows the
        table above it -- below the fold. It settles by construction: the repaint
        records the number it just read, so the next look finds nothing to do.
        """
        if self._active_pane() != "overview":
            return
        widget = self.query_one("#ov-activity", Static)
        if self._activity_painted == (-1,):
            return
        if self._activity_stamp(widget) != self._activity_painted:
            self._repaint_pane("overview")

    def _rows_above_strip(self, widget: Static) -> int:
        """Rows between a widget's own top and the row the status strip owns.

        The top is set by everything ABOVE the widget, never by its own height,
        so sizing a chart from this number cannot feed back into itself.
        """
        # The strip's row is WORKED OUT, exactly as _fit_panes works it out, and
        # for the same reason: a measurement asks the layout for the frame as it
        # stands, and in the tick a resize or a late footer re-wrap lands, the
        # strip has no height yet. A measured zero used to fall through to the
        # full-height default below, which is how a picture got sized for twelve
        # rows in a frame holding nine and drew the rest of itself -- four rows of
        # it -- underneath the status strip. Windows CI measured that; the local
        # runner lays out the strip in time and never sees the gap.
        rows = self._strip_rows
        measured = self.query_one("#status", Static).region.y
        limit = (self._pane_screen_height() - rows) if rows else measured
        top = widget.region.y
        if limit <= 0 or top <= 0:
            return 12  # nothing is laid out yet: take the full-height default
        # What the ASSEMBLY may spend is the box minus the box's own vertical
        # padding. A panel's padding is rows too: size a picture to the outside
        # of the panel and the panel lands one row under the status strip, which
        # buys a scrollbar, and a scrollbar takes a column off every table on the
        # page. Measured from the widget rather than assumed, because the surfaces
        # do not all carry the same padding.
        inset = max(0, widget.content_region.y - widget.region.y) + max(
            0, widget.region.bottom - widget.content_region.bottom
        )
        # One row in hand: the pane ends the row BEFORE the strip begins.
        return max(0, limit - top - 1 - inset)

    def _sessions_grouped(self, rows: list[dict]) -> bool:
        """Whether the list has more than one tool in it, and so is grouped."""
        return len({row.get("tool") for row in rows}) > 1

    def _sessions_by_tool(self, rows: list[dict]) -> list[dict]:
        """Session rows grouped by tool, in the order the chips above list them.

        One tool at a time is what makes the list readable at a glance: sorted by
        the stamp alone, six tools interleave on every screen and the question
        the pane gets asked -- what did CODEX do this week -- needs a read of
        every row. The sort in force still rules WITHIN a tool (the sort is
        stable, and this is the only reordering), and a tool the inventory has
        never heard of keeps its first-appearance place at the end, so no row is
        ever dropped or invented.
        """
        if not self._sessions_grouped(rows):
            return rows
        known = {tool: index for index, tool in enumerate(self._sn_inventory)}
        first_seen: dict[object, int] = {}
        for row in rows:
            first_seen.setdefault(row.get("tool"), len(first_seen))

        def rank(row: dict) -> tuple[int, int, int]:
            tool = row.get("tool")
            if tool in known:
                return (0, known[tool], 0)
            return (1, 0, first_seen.get(tool, 0))

        return sorted(rows, key=rank)

    def _paint_tool_chips(self, counts: dict[str, int], unfiltered: bool) -> None:
        """The tool chips, thinned to what the row can actually hold.

        The chip row IS the filter, so it shows the tools that have sessions in
        this window, busiest first -- twenty buttons in a fixed order would put
        the one the reader wants behind nineteen they have never used. The
        inventory is only refreshed on an unfiltered read: filtered to one tool,
        the rows say nothing about the others, and a chip row that forgets them
        cannot put them back.

        Like the range strip, the row yields space to the choice it must not
        hide: the tool that is currently in force always keeps a slot, so what
        is on screen always names itself.
        """
        if unfiltered and counts:
            order = {tool: index for index, tool in enumerate(SESSION_TOOLS)}
            self._sn_inventory = tuple(
                sorted(counts, key=lambda tool: (-counts[tool], order.get(tool, 99)))
            )
        chosen = self._sn_sources
        all_on = set(chosen) == set(SESSION_TOOLS)
        picked = chosen[0] if len(chosen) == 1 and not all_on else ""
        shown = [tool for tool in self._sn_inventory if tool in set(SESSION_TOOLS)]
        # The count rides on the chip on an UNFILTERED read: "6" next to a tool's
        # own tint is how the row says which tools this window is actually made
        # of, which is the question the chips answer. On a filtered read the
        # counts belong to a window the reader is no longer looking at, so they
        # come off rather than mislead.
        def chip_text(tool: str) -> str:
            name = f"{_BADGE_EDGE}{tool_label(tool, TOOL_LABELS)}"
            if not unfiltered:
                return name
            held = counts.get(tool)
            return f"{name} {held}" if held else name

        room = self._pane_width("sessions") - _CHIP_LABEL_CELLS - _chip_cells("All")
        room -= _chip_cells("More")
        used = 0
        visible: list[str] = []
        for tool in shown:
            cost = _chip_cells(chip_text(tool))
            if used + cost > room:
                break
            visible.append(tool)
            used += cost
        if picked and picked not in visible:
            if visible:
                visible.pop()
            else:
                visible.append(picked)
        held = set(visible)
        for tool in SESSION_TOOLS:
            button = self.query_one(f"#sn-tool-{tool}", Button)
            button.display = tool in held
            button.set_class(tool == picked, "-selected")
            # The tool's own accent, as the edge the row of chips is scanned by:
            # the same tint the table's Tool column wears, so a chip and its rows
            # match at a glance. The NAME keeps the button's colour, which is
            # what changes on hover and on the chosen chip.
            button.label = Text(chip_text(tool), style=tool_tint(tool))
        self.query_one("#sn-tool-all", Button).set_class(all_on, "-selected")
        # "More" is the door to the tools this row had no room for -- and, unlike
        # the range strip's, it is also the door to every tool this window holds
        # NONE of, which is the answer to "where is my other agent".
        self.query_one("#sn-tool-more", Button).display = (
            len(visible) < len(SESSION_TOOLS)
        )
        row = self.query_one("#sn-tools", Horizontal)
        row.display = any(
            self.query_one(f"#sn-tool-{tool}", Button).display for tool in SESSION_TOOLS
        ) or all_on

    def _paint_sessions(self) -> None:
        # The head names what the rows cover even when the body below has no rows
        # yet, so an empty pane still says what an empty pane is empty OF.
        self._paint_scope("sessions")
        if not self._scope_ok("sessions"):
            self._paint_pending("sessions")
            return
        table = self.query_one("#sn-table", DataTable)
        # A roomy pane can hold the session id beside the name; a tighter one
        # pays for MAIN MODEL first, because "which model" is the question the
        # list is asked most and the id is one Enter away.
        pane_width = self._pane_width("sessions")
        wide = pane_width >= 104
        result = views.session_rows(
            self._sn_payloads,
            search=self._sn_search,
            sort=self._sn_sort,
            labels=TOOL_LABELS,
            narrow=not wide,
            failed=self._sn_failed,
        )
        asked = len(self._sn_sources)
        answered = len(result["sources"])
        # The CAPTION under the table states the rows: how many, in what order,
        # how fresh. Above the table there is a strip for that, and a strip that
        # is always full is a strip that never means anything -- so the strip
        # below gets kept for the two things that are news: a fan-out still in
        # the air, and a tool that did not answer.
        plural = "" if result["count"] == 1 else "s"
        caption = (
            f"{result['count']} session{plural} matching"
            if self._sn_search else f"{result['count']} session{plural}"
        )
        caption += " · sorted by " + SESSION_SORT_LABELS[result["sort"]]
        if self._sessions_grouped(result["rows"]):
            # Said out loud because it changes what "sorted by" means here: the
            # order is per tool, and the tools are in the order the chips above
            # list them. A caption that promised a window-wide sort the list does
            # not have would be a caption the reader has to disbelieve.
            caption += " · grouped by tool"
        if result.get("loading"):
            caption = f"{answered} of {asked} tools loaded · {caption} so far"
        elif answered != asked:
            caption = f"{answered} of {asked} tools answered · {caption}"
        caption += self._sn_stamp_note
        caption += " · review sessions " + (
            "server default" if self._sn_review is None
            else ("included" if self._sn_review else "excluded")
        )
        self.query_one("#sn-meta", Static).update(
            emit_markup([[Run(caption, "muted")]])
        )
        if self._sn_failed:
            # One strip, and it says the whole of the bad news: that the pane is
            # partial, how much of the fan-out is missing, WHICH tool, and the
            # key that asks again. Split over two places it is two hints.
            missing = ", ".join(str(tool) for tool in self._sn_failed[:3])
            more = "" if len(self._sn_failed) <= 3 else f" +{len(self._sn_failed) - 3}"
            self._set_alert(
                "sessions",
                f"Partial · {answered} of {asked} tools answered · "
                f"{missing}{more} unavailable · r Retry",
            )
        else:
            self._set_alert("sessions", None)
        counts: dict[str, int] = {}
        for row in result["rows"]:
            counts[row["tool"]] = counts.get(row["tool"], 0) + 1
        self._paint_tool_chips(counts, set(self._sn_sources) == set(SESSION_TOOLS))
        # The six columns the approved design shows, with the two identity
        # columns OPTIONAL so the fit drops them one at a time. Gating both on
        # one width is what lost the stamp and the model together at 98
        # columns, where there was room for the stamp on its own.
        # Fourteen cells is the minute-resolution stamp ("2026-10-18 11:40" is 16;
        # the header "Last recorded" is 13, and a header always fits). Asking for
        # the seconds here is what cost the MODEL column at a hundred columns: the
        # fit gave the stamp 20 cells and took 22 from the model, and the two
        # columns swapped places depending on how the arithmetic landed.
        spec = [("harness", "Tool", 13, False),
                ("session", "Project / session",
                 38 if pane_width >= 118 else 34 if pane_width >= 108 else 26, False),
                # Twelve is "10-18 11:40" whole. Asking for sixteen at a laptop
                # terminal is what cost the MODEL column: the fit gave the stamp
                # the year and the seconds it had been told to want, and paid for
                # them by dropping the one column that says what was run. The
                # fuller stamp comes back on a pane wide enough to hold both.
                ("last", "Last recorded", 16 if pane_width >= 124 else 12, True),
                # Eighteen, not twenty: a hundred columns is the width a laptop
                # terminal actually has, and at that width the declared widths
                # decide whether the reader sees WHICH MODEL at all. Model and
                # stamp are the two identity columns of this list, and the model
                # is the one a reader cannot recover from anywhere else on the
                # pane, so it asks for its shortest honest width first.
                ("model", "Main model", 20 if pane_width >= 118 else 18, True),
                ("tokens", "Tokens", 8, False),
                ("cost", "Cost", 10 if pane_width >= 108 else 9, False)]
        # Index 1 is the column that may be cropped. With the default of 0 the fit
        # tried to narrow the TOOL chip -- already at its label's width, so it
        # could not move -- and paid for the spare cells by deleting the recorded
        # time and then the model, which are the two columns that tell one row
        # from another.
        self._shape(table, tuple(spec), reserve=2, name_index=1)
        # The id is the disambiguator the approved design puts under the name. A
        # table cell is one line, so it rides AFTER the name and is dropped
        # whole when the column cannot hold both: a cropped id reads like a
        # different id, and the name is what the eye is scanning.
        session_width = int(
            (table.columns.get("session") and table.columns["session"].width) or 0
        )
        # The stamp takes the FULLEST form its column can hold whole. Seconds
        # first, then the year, and only when neither fits does the row give up
        # the column -- which the fit already decided by dropping it.
        last_width = int(
            (table.columns.get("last") and table.columns["last"].width) or 0
        )
        stamp = ("last_seen_shown" if last_width >= 19
                 else "last_seen_minute" if last_width >= 16
                 else "last_seen_clock")
        # The resolution the column is actually showing, said out loud. Without
        # it a reader who needs the seconds cannot know whether there are none or
        # whether the screen simply has no room for them.
        self._sn_stamp_note = (
            "" if stamp == "last_seen_shown" else " · times to the minute"
        )
        # Two lines per row where the terminal has rows to spare (the design's
        # air, paid for with the session id's own line); one line where it does
        # not, with the id after the name as it was. The session column's width
        # decides whether the id is shown at all, in either shape.
        tall = not self._compact
        # ONE TOOL AFTER ANOTHER, in the order the chips above list them. A list
        # sorted purely by the stamp interleaves six tools on every screen, and
        # the one question this pane gets asked -- what did CODEX do this week --
        # can only be answered by reading every row. Within a tool the sort in
        # force still rules, so "s" keeps doing exactly what it says.
        for row in self._sessions_by_tool(result["rows"]):
            self._add_row(table, [
                ("harness", _source_cell(row["tool"], row["harness"])),
                ("session", (_session_cell_tall if tall else _session_cell)(
                    row["display"], row["session_id"], session_width)),
                ("last", escape(row[stamp])),
                ("model", escape(row["model"])),
                ("tokens", _num_cell(row["tokens"])),
                ("cost", _num_cell(row["cost"])),
            ], key=(row["tool"], row["session_id"]), height=2 if tall else 1)
        self._restore_cursor(table, self._sn_selected_key)
        detail = self.query_one("#sn-detail", Static)
        turns = self.query_one("#sn-turns", DataTable)
        pane = self.query_one("#pane-sessions")
        pane.set_class(self._sn_detail_key is not None, "-session-open")
        if self._sn_detail_key is None:
            detail.display = False
            turns.display = False
            turns.clear()
            return
        detail.display = True
        # include_turns=False: #sn-turns below paints the same rows as a table
        # with a keyboard cursor, and a Static list of them would push the
        # timing block (and the table itself) out of the pane.
        detail.update(emit_markup(session_detail_lines(
            self._sn_detail, self._sn_detail_speed, self._sn_detail_note,
            self._sn_detail_key, self._win, TOOL_LABELS, include_turns=False,
        )))
        self._paint_turns()

    def _paint_turns(self) -> None:
        """EVERY turn the whole-session response carried, oldest first.

        Sizing the viewport is the painter's job; dropping rows is not. With the
        turns in a table the reader can arrow to turn 1 of 400, which is the
        reading that explains where a session's tokens went.
        """
        turns = self.query_one("#sn-turns", DataTable)
        was_shown = turns.display
        rows = views.session_detail_rows(self._sn_detail or {})
        pane_width = self._pane_width("sessions")
        wide = pane_width >= 104
        # Which column gives when the pane is tight: the stamp. The turn index
        # already carries the order; the model is the reason the reader opened
        # the session at all -- it is where the tokens went.
        spec = [("index", "Turn", 7, False), ("at", "Recorded", 20, True),
                ("model", "Model", 28, False), ("tokens", "Tokens", 9, False)]
        if wide:
            spec.append(("output", "Output", 9, True))
        spec.append(("cost", "Cost", 9, False))
        self._shape(
            turns,
            tuple(spec),
            # A table that has never been shown has no measured region, and the
            # generic fallback is narrower than the pane it is about to fill --
            # which used to drop a whole column on the first look at a session.
            None,
            None if turns.content_region.width else pane_width - 4,
        )
        for row in rows:
            self._add_row(turns, [
                ("index", escape(f"#{row['index']}")),
                ("at", escape(row["timestamp"])),
                ("model", escape(row["model"])),
                ("tokens", _num_cell(row["tokens"])),
                ("output", _num_cell(row["output"])),
                ("cost", _num_cell(row["cost"])),
            ], key=row["index"])
        turns.display = bool(rows)
        if rows and not was_shown and isinstance(self.focused, DataTable) \
                and self.focused.id == "sn-table":
            # The reader pressed Enter on a session to see it; the keyboard
            # follows the thing they asked for. Anywhere else (search box, a
            # repaint after sorting) focus stays exactly where it was.
            turns.focus()
            # ...and the pane scrolls to meet it.
            self._reveal_turns()

    def _reveal_turns(self) -> None:
        """Scroll the Sessions pane until the open session's turns are all on screen.

        The table only just became visible, so focus's own scroll-into-view
        measured a zero-height widget; at 80x24 that left the focused rows under
        the fold, with the arrow keys busy somewhere the reader could not see.
        Two hops because the summary block above this table re-wraps when the
        session's own text arrives, and the first refresh measures the pane as
        it was BEFORE that re-wrap -- one hop stops a row short. The session's
        timing comes from a fetch of its own and grows the summary again
        whenever it lands, so _load_session_detail asks for this once more then.
        """
        turns = self.query_one("#sn-turns", DataTable)
        self.call_after_refresh(
            lambda: self.call_after_refresh(turns.scroll_visible)
        )

    def _paint_stats(self) -> None:
        self._paint_scope("stats")
        if not self._scope_ok("stats"):
            self._paint_pending("stats")
            return
        payload = getattr(self, "_stats_payload", None) or {}
        start, end = self._stats_win.bounds(self._today)
        # The strip under the title is for NEWS only. This pane's window is
        # already named at the right of the title row -- Stats owns its own
        # window, so that is where a scope belongs, exactly as "All tools" sits
        # there on every other page -- and repeating it here cost a row and gave
        # the reader the same sentence twice, which is what the design review
        # called "dates, page names and instructions on several tight lines".
        self._set_alert("stats", None)
        # /api/stats answers in three blocks: summary (web-card shape),
        # contributions (the calendar) and stats (the annual figures). The annual
        # headline lives in ``stats`` -- reading the envelope instead turns every
        # figure into a dash.
        summary = views.stats_summary(
            payload.get("stats") or {},
            today=self._today.isoformat(),
            window_days=(end - start).days + 1,
        )
        # The figures are CARDS, not a paragraph. Six values in running text is
        # something the reader has to parse; six values in six boxes is
        # something they can scan and compare. The denominators that the
        # paragraph carried by hand stay underneath, where a wrong reading of a
        # card can be checked without leaving the page.
        width = self._pane_width("stats")
        per_row = _cards_per_row(width)
        self.query_one("#st-cards", Static).update(
            _card_block(
                views.stats_cards(summary), per_row, width, self._mono(),
                air=0 if self._compact else 1,
            )
        )
        lines = [
            [Run(f"coverage {summary['coverage']} of this window "
                 f"({summary['span_note']})", "muted")],
            [Run("annual window only · messages count assistant and user turns, "
                 "not sessions · no model or project tables here", "muted")],
        ]
        if summary["current_note"]:
            lines.insert(0, [Run(summary["current_note"], "warn")])
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
        self._paint_scope("report")
        self._sync_toggle_states()
        if not self._scope_ok("report"):
            self._paint_pending("report")
            return
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
            markup = emit_markup(_report_pane_segments(build_report_segments(
                self._rp_usage,
                self._rp_insights,
                self._rp_active,
                window=window,
                width=width,
                glyphs=_safe_glyphs(),
                sections=("summary",),
            )))
        elif self._rp_section == "when":
            markup = emit_markup(self._report_when_lines(width))
        else:
            markup = emit_markup(self._report_projects_lines(width))
        sort_note = (
            " · projects ranked by " + ("cost" if self._rp_project_sort == "cost" else "tokens")
            if self._rp_section == "projects" else " · s sorts projects"
        )
        # The strip above already names the window; what this line still has to
        # say is how wide the text was laid out for -- a report is wrapped, and
        # the wrap is the one thing the reader cannot otherwise see.
        self.query_one("#rp-hints", Static).update(
            emit_markup([[Run(f"wrapped to {width} columns{sort_note}", "muted")]])
        )
        self.query_one("#rp-body", Static).update(markup)

    def _report_pending(self) -> list[list[Run]]:
        """The scan is still running: say so, and never paint a zero for it."""
        return [[Run("analytics scan still running — this section fills in", "warn")]]

    def _report_warnings(self) -> list[list[Run]]:
        return [[Run(text, "warn")] for text in self._rp_warnings]

    def _report_scan_failed(self) -> list[list[Run]]:
        """The facet scan answered with a failure: name it, never wait for it.

        "Still running" is a promise that waiting pays off. When the read raised,
        no amount of waiting fills the section, and the real reason -- sitting in
        ``_rp_warnings`` -- stayed off the screen while the section looked busy.
        """
        detail = next((w for w in self._rp_warnings if w.startswith("analytics")), "")
        lines = [[Run("analytics scan failed — this section has no facet to draw", "bad")]]
        if detail:
            lines.append([Run(detail, "warn")])
        lines.append([Run("r Reload re-runs the scan for this window", "muted")])
        return lines

    def _report_facet_pending_or_failed(self) -> list[list[Run]]:
        return self._report_scan_failed() if self._rp_insights_failed else self._report_pending()

    def _report_when_lines(self, width: int) -> list[list[Run]]:
        """All 24 hours and all seven weekdays, with the denominator named.

        The one-shot report prints the three busiest hours and one weekday
        because a terminal report is a summary; a PANE is where the rest of the
        facet lives, and the reader who wants to know whether 03:00 was quiet or
        simply not recorded has to be able to ask. ``views.report_when`` already
        builds the whole grid from ``insights.hourly`` / ``.weekday`` over
        ``insights.totals`` -- the same denominator every other facet divides by
        -- so this paints it instead of the summary version of it.
        """
        if self._rp_insights is None:
            return self._report_facet_pending_or_failed()
        glyphs = _safe_glyphs()
        two_columns = width >= 92
        bar = max(4, (width - (79 if two_columns else 40)) // (2 if two_columns else 1))
        when = views.report_when(self._rp_insights, width=bar, glyphs=glyphs)
        lines: list[list[Run]] = [[
            Run("Tokens by hour of day  ", "header"),
            Run(f"({when['timezone']})", "muted"),
        ]]
        lines.append([
            Run("shares divide the analytics scan total ", "muted"),
            Run(when["denominator"], "muted"),
            Run(" · a quiet hour is a zero, a facet that never answered is a dash", "muted"),
        ])
        head = []
        if when["peak_hour"] is not None:
            head.append(Run(f"peak hour {when['peak_hour']}", "muted"))
        if when["peak_weekday"]:
            head.append(Run(f" · busiest weekday {when['peak_weekday']}", "muted"))
        if when["night_share"] is not None:
            head.append(
                # The facet answers with a RATIO (night / total), so it goes
                # through the same percent formatter the CLI report uses; a
                # bare "%" suffix turned 50% into 0.5%.
                Run(f" · night share {fmt_hit(when['night_share'])}"
                    f" ({when['night_label']})", "muted")
            )
        if head:
            lines.append(head)
        # No rule between the two facets: the week starts with a heading of its
        # own, and the rows the reader is tracking are the hours, not a line of
        # middots above them. Those two rows belong to the weekday names.
        if when["missing_hours"]:
            lines.append([Run("no hourly facet in this scan", "warn")])
        else:
            hours = when["hours"]
            per = (len(hours) + 1) // 2 if two_columns else len(hours)
            for row in range(per):
                cells: list[Run] = []
                for column in range(2 if two_columns else 1):
                    entry = hours[row + column * per]
                    if entry is None:
                        cells.append(Run(" " * min(width, 40)))
                        continue
                    cells += [
                        Run(f"{entry['hour']} ", "muted"),
                        # A fixed bar cell: the bar is the only variable-length
                        # thing in the row, and un-padded it slid the figures --
                        # and the whole second column -- a few cells per row.
                        Run(entry["bar"].ljust(bar), "accent"),
                        Run(" ", "muted"),
                        Run(f"{entry['tokens']:>9}", "muted"),
                        Run(f"{entry['share']:>7}", "muted"),
                        Run(f"{entry['cost']:>8}", "muted"),
                    ]
                    if column == 0 and two_columns:
                        cells.append(Run("   ", "muted"))
                lines.append(cells)
        if when["missing_weekdays"]:
            lines.append([Run("no weekday facet in this scan", "warn")])
        else:
            # SEVEN ROWS, each with its own bar. A week spread over one row of
            # names loses the bar -- the only thing in this facet that shows a
            # Tuesday that is three times a Wednesday -- and it splits one day's
            # three figures over three rows, which the eye has to stitch back
            # together by counting columns. The hours above already use two
            # columns to stay on screen; the week is seven rows and does not
            # pretend otherwise.
            lines.append([Run("Tokens by weekday", "header")])
            for entry in when["weekdays"]:
                lines.append([
                    Run(f"{entry['day']:<9}", "muted"),
                    Run(entry["bar"].ljust(bar), "accent"),
                    Run(" ", "muted"),
                    Run(f"{entry['tokens']:>9}", "muted"),
                    Run(f"{entry['share']:>7}", "muted"),
                ])
        lines += self._report_warnings()
        return lines

    def _report_projects_lines(self, width: int) -> list[list[Run]]:
        """Project attribution, ALL of it, ranked by the sort in force.

        Two things the summary version could not do: honour ``s`` (the shared
        renderer hardcodes tokens, so the key did nothing), and stop at ten
        without saying the list went on. The pane scrolls, so the whole ranking
        is what belongs here -- with the unattributed residual OUTSIDE the
        ranked rows, never as a fake project.
        """
        if self._rp_insights is None:
            return self._report_facet_pending_or_failed()
        projects = views.report_projects(
            self._rp_insights,
            sort=self._rp_project_sort,
            anonymous=not bool(
                ((self._rp_insights or {}).get("projects") or {}).get("names_included", True)
            ),
        )
        name_width = max(18, width - 41)
        leader = "cost" if projects["sort"] == "cost" else "tokens"
        lines: list[list[Run]] = [[
            Run("Projects  ", "header"),
            Run(f"ranked by {leader} · s sorts", "muted"),
        ], [
            Run("shares divide the analytics scan total ", "muted"),
            Run(projects["totals"], "muted"),
        ], [
            Run(f"{'project':<{name_width}}", "muted"),
            Run(f"{'tokens':>9}", "muted"),
            Run(f"{'share':>7}", "muted"),
            Run(f"{'cost':>9}", "muted"),
            Run(f"{'msgs':>6}", "muted"),
        ]]
        if not projects["rows"]:
            lines.append([Run("no projects in this scan", "warn")])
        for row in projects["rows"]:
            lines.append([
                Run(_fit(row["project"], name_width).ljust(name_width), "muted"),
                Run(f"{row['tokens']:>9}", "muted"),
                Run(f"{row['share']:>7}", "muted"),
                Run(f"{row['cost']:>9}", "muted"),
                Run(f"{row['messages']:>6}", "muted"),
            ])
        un = projects["unattributed"]
        lines.append([
            Run(f"{'unattributed':<{name_width}}", "muted"),
            Run(f"{un['tokens']:>9}", "muted"),
            Run(f"{un['share']:>7}", "muted"),
            Run(f"{un['cost']:>9}", "muted"),
            Run("", "muted"),
        ])
        if projects["coverage_note"]:
            lines.append([Run(projects["coverage_note"], "muted")])
        if projects["reason"]:
            lines.append([Run(f"attribution unavailable: {projects['reason']}", "warn")])
        lines += self._report_warnings()
        return lines

    def _paint_speed(self) -> None:
        self._sync_toggle_states()
        self._paint_scope("speed")
        if not self._scope_ok("speed"):
            self._paint_pending("speed")
            return
        payload = self._sp_payload or {}
        self._paint_speed_selectors(payload)
        self._set_alert("speed", self._speed_note_lines(payload))
        if self._sp_mode == "trend" and self._sp_trend is not None:
            self._paint_speed_trend()
            return
        if self._sp_mode == "hour" and self._sp_hours is not None:
            self._paint_speed_hours()
            return
        self._paint_speed_chart_hidden()
        self._sp_chart_wanted = False
        self._sp_chart_buckets = 0
        table = self.query_one("#sp-table", DataTable)
        result = views.speed_rows(payload, lens=self._sp_lens, labels=TOOL_LABELS)
        width = self._pane_width("speed")
        # A roomy pane spells the measurement out; a close one spells it
        # smaller; none of them cuts it. "response window / outpu…" is not a
        # shortened measurement, it is a truncated one wearing the table's
        # authority, and the column is exactly as wide as its longest value
        # anyway -- so the WORDING is the only dial that can make it fit.
        if width >= 92:
            method, method_width = "timing_short", 24
        else:
            method, method_width = "timing_kind", 11
        if self._sp_lens == "combined":
            # The merged lens is the one place the FULL method label is kept:
            # a row here stands for two or more methods at once, and "first
            # token, respon" is a method that does not exist. When the column
            # cannot hold the words, _clip_text crops them with a trace.
            method, method_width = "timing", 24
            spec = [("model", "Model (merged)", 30, False),
                    ("sources", "Sources", 14, False),
                    ("timing", "Timing kinds", method_width, False)]
        else:
            spec = [("model", "Model", 26, False),
                    # Harness and timing are the row's IDENTITY, not decoration:
                    # two rows both reading "gpt-5.1-codex" 88.4 / 41.0 are only
                    # two different measurements while the screen says which. They
                    # stay in the spec at every width, and the merged
                    # measured/eligible column is what pays for them.
                    # 10 for the longest name here plus 2 for the badge's edge
                    # and its gap: without them "qwen_code" arrives "qwen_co…".
                    ("harness", "Source", 12, False),
                    ("timing", "Timing / basis", method_width, False)]
        # The merged label is the one header worth shortening rather than
        # clipping: a header printed "Measured / eligibl" is a header that has
        # already lost the column it names, and both figures stay in the cell.
        measured_label = ("Measured / eligible" if width >= 112 else "Meas./eligible")
        # Coverage is the one figure here that is ARITHMETIC on another
        # (measured over eligible), so it is the one that goes when the terminal
        # cannot hold the row. It is last for the same reason: fit_columns drops
        # optional columns from the right, and identity is not for sale.
        spec += [("rate", "tok/s", 8, False),
                 ("measured", measured_label, 15, False),
                 ("coverage", "Coverage", 9, True)]
        if width < 92:
            spec = [col for col in spec if col[0] != "coverage"]
        held = self._held_key(table)
        # The rows go into the FIT as well as into the table. Sized from the
        # spec alone, a column gets the width its author guessed and the fit
        # takes it away again -- which is how "3,902 / 4,154" arrived as
        # "3,902 / 4,15" with a horizontal scrollbar in its place.
        self._shape(table, tuple(spec), rows=[
            (row["model"],
             _source_text(row.get("harness") or row.get("sources") or ""),
             str(row[method]),
             str(row["rate"]),
             f"{row['measured']} / {row['eligible']}",
             str(row["coverage"]))
            for row in result["rows"]
        ], budget=self._speed_table_budget(reserve=2))
        for row in result["rows"]:
            self._add_row(table, [
                ("model", escape(row["model"])),
                # ID for the colour, LABEL for the word. Handing the palette the
                # display name made "Codex" hash to another tool's hue, so the
                # same harness wore mint in Overview and amber here -- and the
                # colour is the one thing that lets the eye follow a source from
                # one pane to the next.
                ("harness", _source_cell(row.get("source_id"), row.get("harness"))),
                ("sources", _source_cell(
                    str(row.get("sources") or "").split(",")[0] or None,
                    row.get("sources"),
                )),
                ("timing", escape(row[method])),
                ("rate", _num_cell(row["rate"])),
                ("measured", self._measured_cell(row)),
                ("coverage", _num_cell(row["coverage"])),
            ], key=f"{row.get('harness') or row.get('sources')}|{row['model']}|{row['timing']}")
        self._restore_cursor(table, held)
        open_lines = self._speed_open_detail()
        if open_lines is not None:
            # The reader is standing on an open row; a repaint for a fresh
            # snapshot, a resize or the row ledger keeps explaining THAT row --
            # at the height the row ASKED for. Sized with the caption's budget
            # instead, the repaint shrank the explanation back to three rows and
            # the denominator sentence went missing again on the next tick.
            self._speed_size_series(open_lines, opened=True)
            self._reveal_cursor(table)
            return
        available = payload.get("available_measurement_range")
        if not result["rows"]:
            caption = [[
                Run("no measured rows for this window", "warn"),
                Run(" · try all sources"
                    + (f" · measurements exist {available['from']} .. {available['to']}"
                       if available else "")
                    + ("" if self._sp_include_windows
                       else " · or press x to include whole-request timing"),
                    "muted"),
            ]]
        elif self._sp_lens == "combined":
            caption = [[Run("combined = summed tokens over the UNION of busy time "
                           "the server measured", "muted"),
                        Run(" · overlapping responses count once", "muted"),
                        Run(" · no pooled per-stream rate exists for a merged model", "muted")]]
        else:
            caption = [[Run("one row per source / model / timing kind / token basis", "muted"),
                        Run(" — separate methods stay separate rows", "muted")]]
        self._speed_size_series(caption)
        self._reveal_cursor(table)

    # -- Speed: the parts of the pane that are not the table ---------------



    @staticmethod
    def _measured_cell(row: dict) -> Text:
        """Measured and eligible calls in ONE cell: "973 / 1,073".

        The two figures only mean something together -- a rate from 12 of 1,000
        calls is not the same measurement as one from 973 of them -- and side by
        side in one cell they cost one column instead of two, which is what
        buys the row its source and timing back at a hundred columns.
        """
        return Text(f"{row['measured']} / {row['eligible']}", justify="right", style="muted")

    def _speed_note_lines(self, payload: dict) -> list[list[Run]]:
        """The pane's ONE strip: what is wrong first, then the quiet facts.

        Everything here is a fact the figures depend on -- which series the rows
        measured, which sources answered, whether the snapshot is still being
        built, what the timing filter is hiding. What it is NOT is the window:
        the range strip one row above already prints those dates, and a figure
        repeated in two places is two places that can disagree.

        Amber marks the lines that mean "what is on screen is not what you asked
        for". The rest is muted on the line below it, so a healthy pane with a
        long list of qualifiers does not look like a warning.
        """
        cache = payload.get("cache") or {}
        state = str(cache.get("state") or "unknown")
        warn: list[str] = []
        facts: list[str] = []
        series_note, series_bad = "", False
        if self._sp_mode in ("trend", "hour"):
            # Which ONE thing the rows measure. Both views answer for a single
            # comparison key or merged model, and it names the LOADED series --
            # naming the last pick instead would print model two's name over
            # model one's rates whenever a read was slow or failed.
            selection = self._speed_selection_label()
            if selection:
                facts.append(f"measuring {selection}")
            series_note, series_bad = self._speed_series_state()
            if series_note:
                (warn if series_bad else facts).append(series_note)
        if self._sp_job_note:
            warn.append(self._sp_job_note)
        facts.append(f"source {self._sp_source or 'all measured'}")
        # Moved ahead of the cache state: a group the timing filter is hiding is
        # a fact about the ROWS, and "ready" is a fact about the plumbing.
        # The box two rows below reads "[ ] Whole-request timing", so saying that
        # again would cost the table a row to learn nothing. What the box CANNOT
        # say is what the WIDENED set includes -- that is a different population
        # of measurements, and it is said here the moment it is true.
        if self._sp_include_windows:
            facts.append("timing includes whole request (the initial wait counts)")
        hidden = payload.get("hidden_request_windows") or []
        if hidden:
            facts.append(
                f"{len(hidden)} group{'s' if len(hidden) != 1 else ''}"
                f" hidden by the timing filter · press x"
            )
        status = payload.get("source_status") or []
        if status:
            facts.append(
                "no measured speed: "
                + ", ".join(f"{s.get('source')} ({s.get('status')})" for s in status[:4])
            )
        # LAST, because it is the one fact here about the plumbing rather than
        # about the rows: when the strip has room for a single line, a group the
        # timing filter is hiding beats a word that says "ready".
        facts.append(f"cache {state}")
        room = self._text_width("#sp-note", fallback=self._pane_width("speed") - 2)
        # A short terminal cannot spare the rows: a third strip line is a row the
        # TABLE loses, and once the pane needs to scroll its scrollbar takes a
        # column off the table's right edge -- which is how a measured figure
        # loses its last digit. Facts give way first; warnings keep their lines.
        # Two rows is the most a pane may spend saying how its data was earned
        # when the terminal is 24 rows tall: everything below the strip -- the
        # picture, the rows, the explanation of the row that was opened -- is
        # what the reader came for, and each of them loses a row to each of
        # these.
        # TWO rows, at every size, and they are the same two rows a review asked
        # for: the first says what is on screen (or what went wrong with it), the
        # second is the quiet context that the figures depend on. Anything the
        # two cannot hold is the sentence UNDER the table and, on an open row,
        # the detail -- not a third and fourth line above the picture.
        lines: list[list[Run]] = []
        if warn:
            lines += [[Run(part, "warn")]
                      for part in _fact_rows(warn, room, limit=2, tail=False)]
        lines += [[Run(part, "muted")]
                  for part in _fact_rows(facts, room, limit=2, tail=False)]
        return lines

    def _paint_speed_selectors(self, payload: dict) -> None:
        """Label every control with the value it HOLDS, and grey out what cannot apply.

        A button that reads "Series" cannot tell you what is selected, and what a
        reader is most likely checking is exactly that. These are also the only
        way to see a series choice at all: a picker opened by a blind keypress
        leaves nothing behind once it closes.

        The row cannot scroll sideways, so the budget is spent in order -- source,
        bucket, the timing preference, and only then the series name, which is the
        one label long enough to need cutting. A cut here shows itself as a cut.
        """
        lens = self._sp_lens
        mode = self._sp_mode
        # Model first. The button lives in a row that cannot scroll sideways, so
        # a source-led label ("codex / gpt-5.1-codex / response window / output
        # + reasoning") spends its whole budget on the part a reader has already
        # got from the Source button two cells to the left, and the model -- the
        # thing they are checking -- arrives as "codex / g…".
        series_label = (self._speed_selection_model_label()
                        or self._speed_selection_label()
                        or self._speed_requested_label() or "not chosen")
        bucket_shown = mode == "trend" or (mode == "hour" and lens == "combined")
        source_label = (
            f"Source: {TOOL_LABELS.get(self._sp_source, self._sp_source)}" if self._sp_source
            else "Source: All sources"
        )
        timing_label = ("[x] Whole-request timing" if self._sp_include_windows
                        else "[ ] Whole-request timing")
        fixed = [source_label, f"Bucket: {self._sp_bucket}" if bucket_shown else "",
                 timing_label]
        # 3 cells per button: a space of label padding each side and a cell of
        # margin. The row cannot scroll sideways, so the last button in it gets
        # cut off instead -- which is why the sum here keeps a cell of slack
        # rather than spending the pane down to its last column.
        # A button costs its label plus four cells of padding and border, plus a
        # cell of margin (measured, not guessed). The fill between the group and
        # the preference is 1fr, so it gives way -- but only to zero, and a row
        # that is still too wide cuts whatever sits at its right edge, which is
        # the preference. Two cells of slack keep that from ever being close.
        shown = [f for f in fixed if f]
        room = (self._pane_width("speed") - 5 * (len(shown) + 1)
                - sum(cell_len(f) for f in shown) - 2)
        wanted = f"Series: {series_label}" if mode != "across" else "Series"
        for wid, label, shown, usable in (
            ("sp-pick-source", source_label, True, True),
            ("sp-pick-series", _fit(wanted, max(14, room)),
             mode != "across", bool(self._speed_key_inventory() or self._speed_model_inventory())),
            ("sp-pick-bucket", f"Bucket: {self._sp_bucket}", bucket_shown, bucket_shown),
            # A PREFERENCE, not a fourth lens: it changes which measurements are
            # in the population. It reads as a ticked box for the same reason a
            # filled tab would say "this view is open" about something that is
            # not a view.
            ("sp-windows", timing_label, True, True),
        ):
            button = self.query_one(f"#{wid}", Button)
            button.display = shown
            # A checkbox is drawn with brackets, and brackets are markup.
            button.label = escape(label)
            button.set_class(not (usable and shown), "-off")

    def _paint_speed_chart_hidden(self) -> None:
        chart = self.query_one("#sp-chart", Static)
        chart.display = False
        chart.update("")

    def _speed_chart_geometry(self) -> tuple[bool, int]:
        """(side_by_side, cells the chart may draw into).

        Beside the table when the table still has room to name its own columns
        after the chart takes its share AND the picture can stand its buckets in
        that share; above it otherwise. The estimate is made here rather than
        asked of the layout because the paint runs BEFORE the layout pass that
        would answer: asking now returns the width of the arrangement being
        replaced.

        The bucket count is the second half of the question, and a month of daily
        buckets is what asks it: thirty of them want fifty-nine cells (one per
        bucket plus the air between them), and a pane 120 wide has room for the
        rows and about fifty for the picture. Drawing them anyway is not a
        picture that fits -- it either pushes the numeric table sideways, where
        the responses behind a rate fall off the screen, or it merges, drops or
        re-buckets buckets, and the gaps between measurements (the one thing only
        the picture says well) go with them. Above the rows the same thirty
        columns have the whole pane to stand in, so that is where they go.
        """
        pane = self._pane_width("speed")
        # What the rows need to name a bucket, its rate, the responses behind it
        # and the coverage under the rate; anything less and the picture would be
        # bought with a table that cannot identify its own rows.
        split = pane >= (_SPEED_TABLE_CELLS + 44)
        if split and self._sp_chart_buckets:
            # The same estimate the renderer will be handed: the box loses three
            # cells of its own padding to its text (see _paint_speed_chart).
            beside = max(16, max(24, pane - _SPEED_TABLE_CELLS) - 3)
            if column_chart_needs(self._sp_chart_buckets) > beside:
                split = False
        return split, max(24, pane - (_SPEED_TABLE_CELLS if split else 4))

    def _speed_table_budget(self, *, reserve: int = 0) -> int:
        """Cells the Speed columns may spend, in the arrangement being drawn.

        Measuring the widget instead is a race the pane loses in the
        side-by-side arrangement: the paint runs BEFORE the layout, so the table
        still reports the width of the stacked arrangement it is replacing, the
        fit happily spends it, and the settled frame -- half as wide, with a
        vertical scrollbar taking two more -- answers with a horizontal scrollbar
        across its own last column. Numbers the reader has to slide sideways to
        finish are numbers the screen did not show.
        """
        split = self._sp_chart_wanted and self._speed_chart_geometry()[0]
        cells = _SPEED_TABLE_CELLS if split else self._pane_width("speed")
        return max(12, cells - _TABLE_INSET - reserve)

    #: Rows a table needs to be arrowed usefully: its header and three rows.
    _SERIES_TABLE_MIN = 4
    #: Rows a table needs before it is worth the rows a picture is asking for.
    #: Header plus seven: a 24-hour table showing four hours is a scrollbar, and
    #: a two-row bar chart is decoration, so below this the TABLE keeps the rows.
    _SERIES_TABLE_READABLE = 8
    #: Rows the picture wants when it can have them, and the floor below which it
    #: is two stubs and a line rather than a picture. Under the floor the picture
    #: gives its rows back: the note strip and the table say what a stub would.
    _SERIES_CHART_WANT = 6
    _SERIES_CHART_WANT_COMPACT = 6
    #: Below this a "chart" is two stubs and a line rather than a picture: three
    #: rows of bar is the least that shows a series rising and falling, and both
    #: the note strip and the table already carry the figures a stub would.
    _SERIES_CHART_MIN = 5

    #: Rows the Speed page spends above the series box on a terminal that has
    #  never been laid out: head, one strip line, two tool rows.
    _SPEED_ABOVE_UNKNOWN = 5

    def _speed_rows_above(self) -> int:
        """Rows the page spends ABOVE the series box.

        Read off where the box actually sits rather than counted by hand: the
        head, the strip and the two tool rows are all laid out by the time Speed
        paints, and none of them moves when the picture changes size -- unlike
        the box itself, whose height is exactly what this paint is deciding.
        """
        box = self.query_one("#sp-series")
        pane = self.query_one("#pane-speed")
        if pane.region.height == 0 or box.region.y == 0:
            return self._SPEED_ABOVE_UNKNOWN
        return max(0, box.region.y - pane.region.y)

    def _speed_series_budget(self, detail_rows: int, split: bool,
                             chart: bool = True) -> tuple[int, int]:
        """(rows for the table, rows for the picture) in the space the pane has left.

        The pane is the only honest ledger. The strip above can be one line or
        three, the detail below can be open or closed, and both take rows from
        whatever is left -- so the rows are divided here, in the open, and the two
        widgets are then GIVEN those heights instead of each asking the layout for
        itself. Asking for itself is how a picture and a table once added up to
        more rows than the screen had, and the table was drawn below the terminal
        with a caption on top of it.

        Beside the rows the table gets all of them: the picture spends the columns
        it was given, not the height the table is standing in.
        """
        pane = self.query_one("#pane-speed")
        want = self._SERIES_CHART_WANT_COMPACT if self._compact else self._SERIES_CHART_WANT
        # The margin BELOW the box (the caption's own blank row) belongs to the
        # caption, and a picture that was never drawn -- Across models has no
        # picture -- reserves nothing. Both were once unpaid, and the row they
        # took came off the bottom of the pane, where the caption and the status
        # strip are drawn on top of each other.
        left = max(0, pane.region.height - self._speed_rows_above() - max(0, detail_rows)
                   - self._speed_detail_margin())
        if not chart:
            return max(self._SERIES_TABLE_MIN, left), 0
        if split:
            # Beside the rows the picture spends COLUMNS, not the rows the table
            # is standing in, so the box's whole height is on offer to it. It
            # draws what it can inside that (and its caption is packed into what
            # the bars leave), which is what makes a seven-bucket series read as
            # a chart rather than as a row of stubs over an empty panel.
            return max(self._SERIES_TABLE_MIN, left), left
        # Stacked, the two children share the box, and the row of air between the
        # picture and the header is a row of the box like any other.
        table = max(self._SERIES_TABLE_MIN, left - want - (1 if want else 0))
        if table < self._SERIES_TABLE_READABLE and want:
            # The picture is asking for rows the table needs more. It keeps what
            # is spare above a readable table, and goes below -- a series the
            # reader cannot arrow through is not a series.
            table = max(self._SERIES_TABLE_MIN,
                        min(left - 1 - self._SERIES_CHART_MIN,
                            self._SERIES_TABLE_READABLE))
        chart_rows = max(0, left - table - 1)
        if chart_rows < self._SERIES_CHART_MIN:
            chart_rows, table = 0, max(left, self._SERIES_TABLE_MIN)
        return table, chart_rows

    def _speed_detail_margin(self) -> int:
        """The blank row the stylesheet puts above the caption line.

        It is a row of the pane like any other. In dense mode the rule is off
        (see #tabs.-compact), which is the only reason the same arithmetic serves
        both a 36-row terminal and a 24-row one.
        """
        detail = self.query_one("#sp-detail", Static)
        if not detail.display or self._compact:
            return 0
        return 1

    def _speed_size_series(
        self, detail_lines: list[list[Run]] | None, *, opened: bool = False
    ) -> int:
        """Rows below the table written, then the rows that are left divided.

        Write what goes under the table, then divide the rows that are left; the
        return value is the rows the picture may draw. The detail is written FIRST
        because its height is one of the numbers in the division: opening a row
        takes rows from the box above it, and a picture sized before that is
        written is a picture that no longer fits.

        `self._sp_chart_wanted` says whether THIS mode draws a picture at all.
        Asking instead whether a caption was passed once cost Across models six
        rows of screen for a chart that pane never draws.
        """
        detail = self.query_one("#sp-detail", Static)
        if detail_lines is None:
            detail_rows = self._sp_detail_wanted if detail.display else 0
            detail.styles.height = "auto" if detail.display else "0"
        else:
            rows = self._speed_detail_rows(
                detail_lines, cap=self._SPEED_DETAIL_CAP if opened else 0
            )
            detail_rows = len(rows)
            # The height is PAID FOR, so it is also SET. Left on auto the widget
            # answers to its own content, and one wrapped caption row -- the
            # width it was packed at is the width of the frame being replaced --
            # put the whole pane one row past the clip, footer drawn over the
            # sentence that explains the figures.
            detail.display = True
            detail.styles.height = str(detail_rows)
            detail.update(emit_markup(rows))
        self._sp_detail_wanted = detail_rows
        split, _cells = self._speed_chart_geometry()
        table_rows, chart_rows = self._speed_series_budget(
            detail_rows, split, chart=self._sp_chart_wanted)
        self.call_after_refresh(self._refit_speed_series)
        box = self.query_one("#sp-series")
        table = self.query_one("#sp-table", DataTable)
        # Explicit heights, not 1fr: the arithmetic above is the layout, and a
        # second authority deciding the same two boxes is what the pane had before.
        # A table is as tall as its rows. Sized at the budget it was GIVEN, ten
        # rows of data in a thirty-row terminal left seven empty ones between the
        # last figure and the sentence under it -- a hole in the middle of the
        # page. The budget stays the ceiling (it is what keeps a 24-hour table on
        # screen and scrolling) and the leftover air moves below the caption,
        # where it belongs to the pane instead of punching through the content.
        wanted = 1 + table.row_count if table.row_count else table_rows
        table_used = max(self._SERIES_TABLE_MIN, min(table_rows, wanted))
        # Stacked, the picture's own margin-bottom (#sp-chart.-stacked) is a row
        # of the box like any other. It was left out of the box's height, so the
        # table drew one row PAST its container and the caption painted over the
        # last hour -- which is also why the row under the cursor sat below the
        # table's own fold and the widget stopped scrolling for it.
        box.styles.height = (f"{max(table_used, chart_rows)}" if split
                             else f"{table_used + chart_rows + (1 if chart_rows else 0)}")
        table.styles.height = f"{table_used}"
        self._sp_table_used = table_used
        self._sp_chart_budget = chart_rows
        self._sp_series_painted = self._speed_series_stamp()
        return chart_rows

    #: Rows #sp-table was last GIVEN, so the picture can shrink the box to the
    #: taller of the two without asking the ledger twice.
    _sp_table_used: int = 0

    def _speed_series_stamp(self) -> tuple:
        """What the last division of the rows was based on."""
        return (self._speed_rows_above(),
                self._sp_detail_wanted,
                self.query_one("#pane-speed").region.height,
                self._compact,
                self._speed_chart_geometry()[0],
                # The columns are fitted to the table's OWN width, and in Trend
                # and Hour that width changes when the picture moves beside them
                # -- without it here, the fit kept the budget of the arrangement
                # being replaced, and the table answered the new, narrower one
                # with a horizontal scrollbar over its own last column.
                self.query_one("#sp-table", DataTable).content_region.width)

    def _apply_density(self) -> None:
        """One blank row between sections is right at 36 rows and wrong at 24.

        The air gives way all at once, by one flag, rather than one widget at a
        time: the page keeps its order and its surfaces at every size, and loses
        only the space between them. Toggling it on resize is what stops a window
        dragged small from keeping a layout it outgrew.
        """
        # Thirty-one rows is the line. The roomier page spends eight rows of its
        # own above the first table (two of air under the strip, a three-row head,
        # the card band's air), and a thirty-six row terminal can pay for them and
        # still leave the activity band a picture. A thirty row terminal cannot:
        # measured, the same page at one hundred columns lost the window's shape
        # entirely, which is a worse trade than a tighter title. The card band
        # keeps the air BETWEEN a figure's name and the figure at every density,
        # because that is what makes the number the headline.
        compact = self._pane_screen_height() <= 31
        tabs = self.query("#tabs")
        if not tabs:
            return
        self._compact = compact
        self.query_one("#tabs").set_class(compact, "-compact")

    def _paint_speed_chart(
        self, *, rows: int, title: str, bucket_note: str, entries: list[dict],
        lens_note: str,
    ) -> None:
        """The picture of the buckets the route returned, beside or above the rows.

        It exists for the one thing a table says badly: WHERE the series is empty.
        A blank column is an unmeasured bucket, a stub on the baseline is a
        measured zero, and the two must never look alike. The exact figures stay
        in the table, which is why the value over a bar is dropped rather than
        shortened when it does not fit its column.

        BESIDE the table there is room to name the series twice, so the title
        line stays. ABOVE it, every row comes out of the table's height, and the
        strip over there has already said which series is being measured.
        """
        split, drawn_width = self._speed_chart_geometry()
        # The box loses its own right padding to its text; the estimate is still
        # made before the layout (the measured width would be the arrangement
        # this paint is replacing), so the padding is subtracted rather than read.
        width = max(16, drawn_width - 3)
        chart = self.query_one("#sp-chart", Static)
        self.query_one("#sp-series").set_class(split, "-split")
        chart.set_class(not split, "-stacked")
        budget = rows
        if budget < self._SERIES_CHART_MIN:
            # No honest picture fits here. Say nothing rather than drawing two
            # stubs over the rows the exact figures need.
            self._sp_chart_rows = 0
            chart.display = False
            chart.update("")
            return
        lines: list[list[Run]] = []
        drawn: dict = {}

        def caption_of(shape: dict) -> list[str]:
            """The picture's own facts, once its columns are known.

            Ordered by what only the picture can say: where the series is empty
            and how much of it exists at all. The bucket size and the lens are
            already on the strip above the table, so they are the facts that give
            way when the caption is down to its last row."""
            return [
                "gaps are unmeasured",
                f"{shape['measured']} of {shape['columns']} buckets measured",
                # The peak names its bucket: "peak 88.4" over a picture of 24 of
                # them is a figure the reader has to go and find.
                f"peak {shape['peak_text']} tok/s at {shape['peak_key']}"
                if shape["peak_text"] else "",
                bucket_note,
                lens_note,
            ]

        # Shorten the PICTURE before shortening anything that says a number:
        # the bars show where the series is empty, and both the note strip and
        # the table already carry the figures a caption would repeat. So the
        # loop walks the picture DOWN and takes the first height that leaves a
        # row for the caption -- a column of bars with no line under it is a
        # shape that never had to say whether its blanks were gaps. A picture
        # with no room to say anything at all is held back as the last answer,
        # used only when no shorter one can pay for its own explanation.
        tall: list[list[Run]] | None = None
        for height in range(
            min(4 if self._compact else 12, max(2, budget - 2)), 1, -1
        ):
            drawn = column_chart(
                entries, width=width, height=height,
                # "series", the same lavender Overview's activity bars wear: the
                # approved palette reserves this tone for "a measured series",
                # and a second colour for the same kind of mark would leave the
                # reader checking which blue meant what.
                glyphs=_safe_glyphs(), style="series", unit="tok/s",
            )
            if not drawn["available"]:
                self._sp_chart_rows = 0
                chart.display = False
                chart.update("")
                return
            lines = []
            if split or not self._compact:
                lines.append([Run(_fit(title, width), "bold")])
            lines += drawn["lines"]
            spare = budget - len(lines)
            if spare > 0:
                # No "N more lines" tally here: what gives way is the bucket size
                # and the lens, both of which the strip above the table already
                # prints, so the count would be noise about facts the reader is
                # not missing.
                lines += [[Run(part, "muted")]
                          for part in _packed(caption_of(drawn), width,
                                              limit=spare, notice=False)]
            if len(lines) > budget:
                continue
            if spare <= 0:
                if tall is None:
                    tall = lines
                continue
            break
        else:
            if tall is None:
                self._sp_chart_rows = 0
                chart.display = False
                chart.update("")
                return
            lines = tall
        self._sp_chart_rows = len(lines)
        chart.display = True
        chart.update(emit_markup(lines))
        # The box was divided BEFORE the picture knew its own height: the bars
        # stop at a cap, the captions stop at what the columns hold, and the rows
        # they did not spend stayed inside the box as a navy gap under the last
        # bar. Now that the height is known, the box takes it -- and only in split
        # mode, where the picture rather than the table decides how tall the row
        # of the page is.
        if split and self._sp_table_used:
            self.query_one("#sp-series").styles.height = str(
                max(len(lines), self._sp_table_used))

    def _speed_rate_label(self, table: DataTable, others: list[tuple], lens: str) -> str:
        """The rate header, spelled out only when the table can pay for the words.

        "tok/s per stream" names which denominator the column divided -- the one
        thing a table of buckets cannot say for itself, and the difference between
        two figures that otherwise look identical. It also costs eight cells more
        than "tok/s", and those cells come out of the columns that identify a row:
        beside the picture at 120 columns the spelled-out header pushed the
        Responses-behind-the-rate column out of the table. So the spelling is
        bought only when nothing has to be dropped to pay for it.
        """
        long = "tok/s per stream" if lens == "stream" else "tok/s combined"
        # The scrollbar these tables grow takes cells the fit has to leave room
        # for, and the budget is the arrangement's, not the widget's (the fit
        # below has to be asked the same question the table will be laid out at).
        # The reserve is the SHAPE's reserve as well: probed at one and painted
        # at two, this probe once promised a fourth column the table then
        # dropped, which is how the hour view lost its parallelism column.
        budget = self._speed_table_budget(reserve=2)

        def kept(label: str) -> int:
            spec = [("rate", label, max(8, cell_len(label) + 1), False), *others]
            sized = [(key, text, max(int(width), cell_len(text) + 1), optional)
                     for key, text, width, optional in spec]
            return len(fit_columns(sized, budget))

        return long if kept(long) == kept("tok/s") else "tok/s"

    def _paint_speed_trend(self) -> None:
        self._sp_chart_wanted = True
        table = self.query_one("#sp-table", DataTable)
        # The renderer and the note above it resolve the series together, so the
        # heading can name model two only when the rows measure model two.
        series, _label = self._speed_trend_selection()
        trend = views.speed_trend_rows(self._sp_trend or {}, key=series, lens=self._sp_lens)
        stream = self._sp_lens == "stream"
        # Told to the arrangement before the columns are fitted: the buckets the
        # route returned decide whether the picture can stand BESIDE these rows
        # at all, and the fit has to be asked at the width that answer implies.
        self._sp_chart_buckets = len(trend["rows"])
        # The column budget depends on whether the picture stands BESIDE these
        # rows or above them, and the paint runs before the layout that would
        # answer -- so it is asked through _speed_table_budget, which is where
        # the fit below actually gets its cells.
        held = self._held_key(table)
        # BESIDE the picture the table has roughly half the pane, and what it
        # must never lose is the bucket, the rate the bars are drawn from and the
        # number of responses behind it -- which is the preview's own three.
        # "Window" repeats the bucket whenever a bucket IS a day, and its long
        # span was the column that pushed the rate header off the screen.
        # The span column costs 24 cells, and at 80 columns the table cannot pay
        # for it AND its own vertical scrollbar: the fit sized the columns to a
        # width the scrollbar then took back, and the rate arrived clipped. A
        # wide-enough pane buys it back.
        span_ok = self._pane_width("speed") >= 92
        spec = [("bucket", "Bucket", 16, False)]
        if span_ok:
            spec.append(("span", "Window", 24, True))
        spec += [("calls", "Responses", 10, not span_ok),
                 ("par", "Parallelism", 12, True)]
        if stream:
            spec += [("p10", "p10", 8, True), ("p90", "p90", 8, True)]
        # The rate goes SECOND, once the rest of the row is known: the header
        # names the denominator ("per stream" / "combined") when the table can
        # hold the words, and shortens to "tok/s" when the only thing they would
        # cost is a column that identifies the row.
        spec.insert(1, ("rate", self._speed_rate_label(table, spec, self._sp_lens),
                        8, False))
        self._shape(table, tuple(spec), rows=[
            (row["key"], row["span"], row["per_stream"] if stream else row["combined"],
             row["calls"], row["parallelism"], row["p10"], row["p90"])
            for row in trend["rows"]
        ], budget=self._speed_table_budget(reserve=2))  # a month of buckets scrolls
        for row in trend["rows"]:
            self._add_row(table, [
                ("bucket", escape(row["key"])),
                *([("span", escape(row["span"]))] if span_ok else []),
                ("rate", _num_cell(row["per_stream"] if stream else row["combined"])),
                ("calls", _num_cell(row["calls"])),
                ("par", _num_cell(row["parallelism"])),
                ("p10", _num_cell(row["p10"])),
                ("p90", _num_cell(row["p90"])),
            ], key=row["key"])
        self._restore_cursor(table, held)
        gran = str(trend.get("granularity") or "auto")
        # The caption first: its height is part of the division below, so the
        # picture can only be sized after it is known.
        lines = [
            [Run(f"series {trend['selected'] or 'none'} · {trend['note']}", "muted")],
            [Run(f"latest measured point: {trend['latest']} · {trend['spread_note']}"
                 f" · buckets offered: {', '.join(trend['offered']) or 'none'}", "muted")],
        ]
        lines += self._speed_series_warning_lines()
        open_lines = self._speed_open_detail()
        rows = self._speed_size_series(
            open_lines or lines, opened=open_lines is not None)
        self._paint_speed_chart(
            rows=rows,
            title=f"{gran} buckets · {trend['selected'] or 'no series'}",
            bucket_note=f"{gran} buckets",
            entries=views.chart_entries(
                trend["rows"],
                "per_stream_value" if stream else "combined_value",
                text_key=("per_stream" if stream else "combined"),
            ),
            lens_note=("output per measured stream-second" if stream
                       else "combined output over the busy clock"),
        )
        self.call_after_refresh(self._refit_speed_series)

    def _paint_speed_hours(self) -> None:
        self._sp_chart_wanted = True
        table = self.query_one("#sp-table", DataTable)
        hours = views.speed_hour_rows(self._sp_hours or {}, lens=self._sp_lens)
        combined = self._sp_lens == "combined"
        self._sp_chart_buckets = len(hours["rows"])
        held = self._held_key(table)
        spec = [
            # The hour axis IS the identity column, so it never goes; the second
            # column is the rate the bars are drawn from, and the third says how
            # many responses stand behind it (an hour built from one response is
            # not an hour of throughput).
            ("hour", "Hour (server clock)", 18, False),
            # Not optional: beside the picture this is the whole table, and an
            # hour built from one response is not an hour of throughput.
            ("calls", "Responses", 10, False),
        ]
        spec.append(("par", "Parallelism", 12, True) if combined
                    else ("coverage", "Coverage", 9, True))
        spec.insert(1, ("rate", self._speed_rate_label(table, spec, self._sp_lens),
                        8, False))
        self._shape(table, tuple(spec), rows=[
            (row["hour"], row["rate"], row.get("calls", EM_DASH),
             row.get("parallelism", EM_DASH) if combined else row.get("coverage", EM_DASH))
            for row in hours["rows"]
        ], budget=self._speed_table_budget(reserve=2))  # 24 hours do not fit
        for row in hours["rows"]:
            cells = [
                ("hour", escape(row["hour"])),
                ("rate", _num_cell(row["rate"])),
                ("calls", _num_cell(row.get("calls", EM_DASH))),
            ]
            cells.append(("par", _num_cell(row.get("parallelism", EM_DASH))) if combined
                         else ("coverage", _num_cell(row.get("coverage", EM_DASH))))
            self._add_row(table, cells, key=row["hour"])
        self._restore_cursor(table, held)
        if combined:
            model = (hours.get("selected_key") or {}).get("model") or EM_DASH
            merged = " · ".join(
                part for part in (
                    f"sources {', '.join(hours['sources'])}" if hours.get("sources") else "",
                    "timing " + views._kinds(hours["kinds"]) if hours.get("kinds") else "",
                ) if part
            )
            lines = [[Run(f"merged model {model}" + (f" · {merged}" if merged else ""), "muted")]]
            if hours.get("empty_reason"):
                lines.append([Run(str(hours["empty_reason"]), "warn")])
            lines.append([Run(
                "combined = summed tokens over the UNION of busy time · "
                "an hour that measured nothing is a gap, not a zero", "muted")])
        else:
            key = hours.get("selected_key") or {}
            # Every part of the key in the pane's own words. Printing the raw
            # field value put "output_including_reasoning" on the screen under
            # a table whose own rows say "out+reason" for the same thing.
            label = " / ".join(
                views._kinds([v]) if k == "measurement_kind"
                else views._basis(v) if k == "token_basis"
                else str(v)
                for k, v in key.items() if v
            )
            lines = [
                [Run(f"selection {label or EM_DASH} · {hours['note']}", "muted")],
                [Run(f"days with measurements: {hours.get('days_with_measurements', EM_DASH)} · "
                     "an hour that measured nothing is a gap, not a zero", "muted")],
            ]
        lines += self._speed_series_warning_lines()
        open_lines = self._speed_open_detail()
        rows = self._speed_size_series(
            open_lines or lines, opened=open_lines is not None)
        self._paint_speed_chart(
            rows=rows,
            title=f"Hour of day · {self._speed_selection_label() or 'no series'}",
            bucket_note="hours use the server's local clock",
            entries=views.chart_entries(hours["rows"], "rate_value", text_key="rate"),
            lens_note=("combined output over the busy clock" if combined
                       else "output per measured stream-second"),
        )
        self.call_after_refresh(self._refit_speed_series)

    def _paint_quota(self) -> None:
        self._paint_scope("quota")
        banner, rows, note, credit_lines, row_keys = quota_table(
            self._quota_state_val or {},
            self._quota_history_val,
            now_ts=time.time(),
            glyphs=_safe_glyphs(),
            labels=TOOL_LABELS,
        )
        # While a poll is running the note belongs to the POLL. A repaint -- a
        # resize, a re-fit landing after a load, a tab coming back -- used to write
        # the state banner over "polling…" and over a poll's own result a moment
        # after it landed, which on a slow runner meant the one piece of feedback
        # a poll gives was gone before the reader looked. The table and the detail
        # below are still repainted; only the sentence a repaint cannot know is
        # left alone, and the reload a successful poll starts writes it back.
        segs = list(banner)
        if note:
            segs.append([Run(note, "muted")])
        # The note is the one line on this pane a repaint does not own. What the
        # reader pressed "u" to learn is whether the POLLS worked -- "polling…",
        # "poll ok", a failure, consent missing -- and none of it is derivable
        # from the snapshot being painted. So a repaint re-applies the poll's own
        # sentence for as long as it stands, and lets the derived banner back the
        # moment a read lands that makes it stale. Before this, any repaint at all
        # -- a resize, a re-fit landing after a load, the tab coming back -- could
        # put the pre-poll banner over it, and on a slow runner the one piece of
        # feedback a poll gives was gone before the reader looked.
        note_w = self.query_one("#quota-note", Static)
        note_w.update(emit_markup(segs) if self._quota_note_hold is None
                      else self._quota_note_hold)
        table = self.query_one("#quota-table", DataTable)
        # Every quota row is a handful of words ("62.3% left", "in 1d 23h") that
        # cannot be abbreviated and should not be cropped: a cut "unlimi…" reads
        # like a value, not like a column that is too narrow. So the columns are
        # measured against the rows they are about to hold, and fit_columns drops
        # a whole optional column rather than cutting a word in half.
        self._shape(table, QUOTA_COLUMNS, list(rows), reserve=2)
        self._quota_idents = {}
        for index, (row, identity) in enumerate(zip(rows, row_keys)):
            cells = list(row)
            status = str(cells[-1]) if cells else ""
            if status:
                cells[-1] = Text(status, style="green" if status == "ok" else "red")
            if identity and cells and str(cells[0]).strip():
                # The Provider column is the row's source identity, and the
                # account rows are exactly where two accounts of one provider
                # have to be told apart at a glance.
                cells[0] = _source_cell(identity.get("provider"), cells[0])
            row_key = _quota_row_key(identity, index)
            self._quota_idents[row_key] = identity
            self._add_row(
                table,
                [(key, escape(str(cell)) if not isinstance(cell, Text) else cell)
                 for key, cell in zip([c[0] for c in QUOTA_COLUMNS], cells)],
                key=row_key,
            )
        credits = self.query_one("#quota-credits", Static)
        credits.display = bool(credit_lines)
        if credit_lines:
            credits.update(emit_markup(credit_lines))
        detail = self.query_one("#quota-detail", Static)
        if self._quota_detail:
            detail.display = True
            detail.update(emit_markup(self._quota_detail))
        else:
            detail.display = False
            detail.update("")

    def _paint_pricing(self) -> None:
        self._paint_scope("pricing")
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
        ), reserve=2)
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
# The approved preview's six KPI tones, keyed by the FIGURE rather than by
# position: "sorted by cost" moves the columns, and a colour that moved with
# them would stop meaning anything.
_CARD_COLORS = {
    # Not #b9a7ff: that lavender belongs to the thing the keyboard is ON. A
    # figure wearing the selection colour reads as a figure you can act on.
    "tokens": "#adb8ff",
    "cost": "#7ee7c3",
    "messages": "#c4a1ff",
    "agent": "#86bdff",
    "cache": "#8cdeeb",
    "model": "#f2d07e",
    # Stats' annual band. A streak is a count of days, so it takes the day blue
    # and the mint of a run that is still going; none of them borrows the
    # lavender that means "tokens" everywhere else on the screen.
    "days": "#86bdff",
    "streak": "#79dfbd",
    "longest": "#c4a1ff",
}
_CARD_TINT_DEFAULT = "#a8b1ff"
# The card's label is the smallest voice in the block: the approved design prints
# it small and grey under a figure twice its weight, and the two steps of dimming
# below are as close as a fixed-grid terminal gets to that ratio.
_CARD_LABEL = "#8b99b0"


# A DataTable charges itself two cells of padding per column on top of the
# width it was given (measured, not guessed: 10 columns of the shipped widths
# need 20 cells more than their sum).
_TABLE_CELL_PAD = 2
# DataTable { padding: 0 2 }: two cells at each end, INSIDE the widget's width,
# so they come off a budget before any column is measured.
_TABLE_INSET = 4
# Cells the Speed rows keep when the picture sits BESIDE them: the bucket, the
# rate the bars are drawn from, the responses behind it and the coverage under
# the rate. This is not a second guess at the arrangement --
# _speed_chart_geometry hands the REST of the pane to the picture, and both
# halves are read from this number.
#
# Sized to the WIDEST set those rows must carry, not the average one: Hour
# (server clock) 19 + tok/s 8 + Responses 10 + Parallelism 12, plus two cells of
# padding each. At sixty-two that arithmetic was one cell short, and the fit paid
# for it by dropping the optional column -- Parallelism in the combined hour
# view, the one figure that says how many responses overlapped -- so the pane
# printed a dash for a measurement it had while spare cells sat under the
# picture.
_SPEED_TABLE_CELLS = 66

#: Rows the Overview's activity band is allowed to ask for even when the first
#: screen has fewer left. Title, one row of figures over the columns, four rows
#: of bar, the axis and two rows of facts: the shortest assembly that reads as a
#: picture of the window rather than a caption about one.
_ACTIVITY_MIN_ROWS = 8


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
    name_index: int = 0,
) -> list[tuple[str, str, int]]:
    """Trim a column spec until it fits ``width`` cells.

    Order of concessions, deliberately: narrow the name column, then drop columns
    the view can live without (the detail line still carries them), then narrow
    the numeric ones. Every floor holds, so no figure is ever squeezed into a
    value that reads wrong.

    ``name_index`` says WHICH column may be cropped, because it is not always the
    first one: Sessions leads with the tool chip and its long column is the
    session name at index 1. Left at 0 the name column could not give ground, so
    a hundred-column terminal dropped the recorded time and the model -- the two
    facts a reader uses to tell rows apart -- to protect spare width on a label.
    """
    cols = [[key, label, int(width_), bool(optional)] for key, label, width_, optional in spec]

    def need(items: list[list]) -> int:
        return sum(item[2] for item in items) + _TABLE_CELL_PAD * len(items)

    # The name column is the only one allowed to be cropped -- the detail line
    # always carries the full text -- so it gives ground first and every measured
    # number keeps its home for as long as one can be found.
    name_at = name_index if 0 <= name_index < len(cols) else 0
    # The name column's own header is part of its floor: _shape re-applies
    # "at least the label" when it adds the column, so a fit that crops past the
    # header is a fit whose arithmetic the table silently undoes -- and the
    # columns it measured then arrive one cell wider than the budget, which is a
    # horizontal scrollbar across the last figure.
    name_min = max(name_floor, cell_len(str(cols[name_at][1])) + 1)
    while (
        len(cols) > 2
        and need(cols) > width
        and cols[name_at][2] > name_min
        and len(cols) > 1  # the crop must not be the whole column set
    ):
        cols[name_at][2] -= 1
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
    if isinstance(cell, Text):
        if width <= 1 or len(cell.plain) <= width:
            return cell
        if "\n" in cell.plain:
            # A cell that carries its own line break (a session's name over its
            # id) is cropped LINE by line: the plain text of the whole cell is
            # longer than either of its lines, so cropping it as one string would
            # put an ellipsis in the middle of the first line and lose the second.
            lines = cell.split("\n")
            return Text("\n").join(
                Text(_fit(line.plain, width), style=line.style) for line in lines
            )
        return Text(_fit(cell.plain, width), style=cell.style)
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


def _quota_row_key(identity: dict | None, index: int) -> str:
    """A stable row key that carries the row\'s identity across a repaint.

    The table shows display text with the Provider cell deliberately blank on
    continuation rows, so what a reader sees cannot be turned back into
    (provider, account, window). The key is where that identity lives instead:
    a pipe-joined string, because Textual wraps whatever it is given and
    everything downstream reads it back as text.
    """
    if not identity:
        return f"quota-gap-{index}"
    if identity.get("status_only"):
        return f"quota|{identity.get('provider') or ''}|status"
    return (f"quota|{identity.get('provider') or ''}"
            f"|{identity.get('account') or 'default'}"
            f"|{identity.get('bucket') or ''}")


def session_detail_lines(
    detail: dict | None,
    speed: dict | None,
    note: str,
    key: tuple[str, str],
    window: DateWindow,
    labels: dict,
    *,
    include_turns: bool = True,
) -> list[list[Run]]:
    """The whole-session panel: scope first, then turns, then timing.

    ``include_turns`` keeps the promise this function's name makes: the text
    form covers the WHOLE session, every turn in it, because a caller that has
    no table widget has nothing else. The Sessions pane passes False -- the same
    rows are on screen underneath as #sn-turns, where they can be arrowed, and
    printing them twice would put the timing block past the bottom of the pane.
    Either way nothing is truncated: there is no "last eight turns" here.

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
            Run("Entire session — every turn it recorded, not only the days "
                "in the range · opened from ", "muted"),
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
    total_turns = len(detail.get("turns") or [])
    # The turns themselves are a table, not a paragraph. This used to print the
    # last eight of twelve into a Static with no way back, which made the
    # earlier work behind the session total unreadable; the count and the
    # pointers stay here, the data goes to #sn-turns where every row is reachable.
    if total_turns:
        lines.append([
            Run(f"{fmt_int(total_turns)} turn{'' if total_turns == 1 else 's'} recorded", "muted"),
            Run(include_turns and ", oldest first"
                or " · listed below, oldest first · arrows walk them", "muted"),
        ])
        if include_turns:
            for turn in views.session_detail_rows(detail):
                lines.append([
                    Run(f"  turn {turn['index']}", "muted"),
                    Run(" \u00b7 ", "muted"), Run(turn["timestamp"]),
                    Run(" \u00b7 ", "muted"), Run(turn["model"]),
                    Run(" \u00b7 ", "muted"), Run(turn["tokens"]),
                    Run(" \u00b7 ", "muted"), Run(turn["cost"]),
                ])
    else:
        lines.append([Run("no turns recorded for this session", "muted")])
    if speed is None:
        lines.append([Run("timing: not requested yet", "muted")])
    else:
        summary = speed.get("summary") or {}
        scope = speed.get("scope") or {}
        state = (speed.get("cache") or {}).get("state") or "unknown"
        covered = str(scope.get("kind") or "").replace("_", " ")
        lines.append([
            Run("timing · ", "muted"),
            Run(f"{fmt_int(speed.get('total_responses'))} responses · "
                f"{fmt_int(summary.get('speed_calls'))} measured · "
                f"{views._rate(summary.get('output_tok_per_s'))} tok/s · "
                f"cache {state}"
                + (f" · measured over the {covered}" if covered else "")),
        ])
        if speed.get("truncated"):
            lines.append([Run("  series truncated to the point budget; aggregates still cover the session", "muted")])
    if note:
        lines.append([Run(note, "warn")])
    return lines


_CARD_SURFACE = "#121924"
#: The plate under a card's figure. The approved design prints the figure about
#: twice the height of its label; a terminal has one row height, so the figure
#: gets the next best emphasis -- a plate of its own, one step above the card,
#: with the number in bold on top of it.
_CARD_PLATE = "#18212f"


_CAL_BG = ("#161e2b", "#2f2a4d", "#4a3f80", "#6d59bd", "#b9a7ff")
_CAL_WEEKDAYS = "MTWTFSS"
# States a build stream may report that mean the build is OVER. Anything else
# and a closed stream is a lost connection, not a completion.
_SP_JOB_DONE = frozenset({"ready", "complete", "done"})

# The rangebar's own horizontal padding, and the left padding #range-dates adds
# in front of the dates. Measured from the CSS above, not guessed.
_RANGEBAR_PAD = 2
# The dates get their own pad because the strip is right-aligned and does not
# wrap: a budget one cell short loses the LAST digit of the end date rather than
# a shortcut button. The buttons give way first, and they only do so if the
# dates ask for more than the minimum.
_RANGE_DATES_PAD = 4
# A Button's full cell cost: the label, the two cells of padding the stylesheet
# gives it, and the one-cell margin that separates it from the next shortcut.
_BUTTON_CHROME = 3
# The width _fit_button pins a button to. It equals _BUTTON_CHROME minus the
# 1-cell margin: padding plus label, nothing spare. The extra gutter Textual used
# to be given (and happily spent in the middle of the row) is what made a five
# shortcut strip read as a spread of islands instead of a row of keys.
_BUTTON_LABEL_PAD = 2
# The typed-date box: #range-dates-input is 34 cells of input inside a round
# border, and it only exists while "c" has it open -- but while it is open it
# takes the room the shortcut buttons would have spent.
_RANGE_INPUT_CELLS = 36
# A tab costs its label plus the Tab rule's padding (0 1). The Tabs row itself is
# padded 0 1, which is what comes off the screen.
_TAB_CHROME = 2
_TABS_PAD = 2
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


def _series_error_text(exc: BaseException) -> str:
    """One clause naming why a series read failed, for a note rather than a crash.

    A superseded read is not a failed one: an exclusive worker group cancels the
    load that a newer choice replaced, and a red line there would blame the reader
    for the refresh they just asked for. An empty string means "do not report".
    """
    err = getattr(exc, "error", exc)
    if isinstance(err, (WorkerCancelled, asyncio.CancelledError)):
        return ""
    text = " ".join(str(err).split()) or type(err).__name__
    return text if len(text) <= 120 else text[:117] + "..."


def _report_warning(name: str, exc: Exception) -> str:
    """The report body's words for a facet scan that did not answer.

    The one-shot report already says "analytics scan failed: ..." on stderr and
    in its warnings; the interactive pane says the same sentence in the same
    place, so neither surface can turn a missing scan into a quiet zero.
    """
    err = getattr(exc, "error", exc)
    return f"{name} failed: {err}"  # emit_markup escapes any literal bracket


def _wrapped(text: str, width: int) -> list[str]:
    """One fact, broken at WORD boundaries when it is wider than the surface.

    _packed breaks BETWEEN facts and never inside one, which is right up to the
    point where a single fact -- "measuring codex / gpt-5.1-codex / response
    window / output + reasoning" -- is longer than the pane. Left alone the
    WIDGET wraps it at whatever column runs out, so a row the ledger paid for
    costs two rows of screen, and the last line of the caption is the one that
    falls off the bottom.
    """
    if not text:
        return []
    if width < 12 or cell_len(text) <= width:
        return [text]
    parts: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = f"{current} {word}" if current else word
        if current and cell_len(candidate) > width:
            parts.append(current)
            current = word
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def _fact_rows(items: list[str], width: int, *, sep: str = " · ", limit: int = 3,
               tail: bool = True) -> list[str]:
    """Facts to rows: packed whole, and wrapped whole when a fact cannot be whole.

    The row count is the contract -- the pane budgets its height on it -- so a
    too-long fact is broken HERE, where the break is chosen, rather than by the
    widget afterwards, where it is a surprise. Facts that no longer fit are
    counted on a trailing line unless ``tail`` is off, which is what a ONE-line
    strip needs: its overflow is not lost, it is the sentence under the table.
    """
    rows: list[str] = []
    current = ""
    for item in (i for i in items if i):
        pieces = _wrapped(item, width)
        for index, piece in enumerate(pieces):
            candidate = f"{current}{sep}{piece}" if current else piece
            # index > 0: a continuation of a fact already begun. It may not join
            # another fact's line, or the sentence arrives with a middot in it.
            if current and (index or cell_len(candidate) > width):
                rows.append(current)
                current = piece
            else:
                current = candidate
    if current:
        rows.append(current)
    if len(rows) <= limit:
        return rows
    kept = rows[:max(0, limit - 1)]
    missing = [i for i in items if i and not any(i in row for row in kept)]
    if not missing:
        return rows[:limit]
    if not tail:
        return kept or rows[:limit]
    tail = f"{len(missing)} more detail line{'s' if len(missing) != 1 else ''} (widen to read)"
    return kept + [tail]


def _packed(items: list[str], width: int, *, sep: str = " · ",
            limit: int = 3, notice: bool = True) -> list[str]:
    """Pack whole facts into lines that break BETWEEN them, never inside one.

    Wrapping left to the widget breaks wherever the column runs out, which is how
    "kimi (timing_unavailable)" used to arrive as two lines that matched nothing
    and read as two halves. Facts that no longer fit are counted on the last line
    rather than dropped quietly: a qualifier that had to give way is a qualifier
    the reader should know is missing.
    """
    kept = [item for item in items if item]
    lines: list[str] = []
    current = ""
    skipped = 0
    for index, item in enumerate(kept):
        candidate = f"{current}{sep}{item}" if current else item
        if current and cell_len(candidate) > width:
            if len(lines) >= limit - 1:
                skipped = len(kept) - index
                break
            lines.append(current)
            current = item
        else:
            current = candidate
    if skipped and notice:
        tail = f"{skipped} more detail line{'s' if skipped != 1 else ''} (widen to read)"
        current = f"{current}{sep}{tail}" if current else tail
    if current:
        lines.append(current)
    return lines[:limit]


def _two_ends(left: str, right: str, width: int) -> str:
    """Left text, right text, one row -- or one honest " · " between them.

    Right-alignment inside a Static is arithmetic, so the arithmetic is allowed
    to fail: when the two halves cannot both fit, they sit next to each other
    instead of the right-hand figure wrapping under the chart it describes.
    """
    room = max(0, width - 2)
    gap = room - cell_len(left) - cell_len(right)
    if gap >= 2:
        return f"{left}{' ' * gap}{right}"
    return f"{left} · {right}" if left and right else (left or right)


def _fit_button(button: Button) -> None:
    """Pin a shortcut button to the width of the label it is wearing.

    The row is a Horizontal of auto-width buttons, and when the row runs out of
    room Textual shrinks SOME of them -- not the last one, which is what a reader
    (and my arithmetic) expects -- so "d Yesterday" arrives as "d Ye". The strip
    decides which buttons are on the row; the layout is then told the exact width
    of each, and the row cannot quietly eat a shortcut that was paid for.
    """
    button.styles.width = cell_len(str(button.label)) + _BUTTON_LABEL_PAD


def _cards_per_row(pane_width: int) -> int:
    """How many cards fit across a pane this wide: six, or three when six is thin.

    One rule for both bands (Overview's usage, Stats' year), because two panes
    that reflow the same six figures differently read as two different products.

    The break is where a card stops being able to hold a figure WHOLE, not where
    a row of six stops fitting: fourteen cells carries "gpt-5-codex", "$567.66"
    and (in its short form) "2d 3h" with their padding, and sixty of those plus
    the gutters is ninety-two columns. Below that the band goes to two rows of
    three, because a name cropped to "gpt-5-c…" is a different name and the
    reader has no way to know it.
    """
    return 6 if pane_width >= 92 else 3


def _card_block(
    cards: list[dict], per_row: int, pane_width: int, mono: bool, *, air: int = 1
) -> Text:
    """Six figures as coloured blocks, reflowed to the pane's real width.

    Card width comes from the pane and the row count, never from the longest
    value, so a long model name wraps inside its own column instead of pushing a
    neighbour's digits off the screen. Separation is the tinted surface and the
    gap between blocks -- the approved design has no rules and no borders.

    ``air`` is the blank row above and below each block. A terminal has no font
    size, so the only way a headline figure can be made to READ larger is the
    space around it: the approved cards spend more panel on empty navy than on
    ink, and a three-line strip of tight text is the one thing it definitively is
    not. On a 24-row terminal the air is the row the table needs, so the caller
    takes that back (--compact) -- but the blank row BETWEEN a card's label and
    its figure stays at every size, because it is the one thing that makes the
    figure the card's headline rather than its subtitle.

    In a monochrome terminal the tints cannot carry the hierarchy, so the layout
    and the words carry it instead: short delta wording, no surface fill, and the
    same block geometry.
    """
    # Two cells of gutter when the band is roomy, ONE when it is not. The cards
    # are separated by their own surfaces, so the second cell of gutter is the
    # first thing the band can give back -- and at a hundred columns the cell it
    # gives back is the difference between "gpt-5.1-codex" and "gpt-5.1-cod…".
    gutter = 2
    plain = (max(40, pane_width) - gutter * (per_row - 1)) // per_row
    if plain < 17:
        gutter = 1
        plain = (max(40, pane_width) - gutter * (per_row - 1)) // per_row
    # Seventeen cells is where the decoration starts paying for itself. A roomy
    # band spends one cell on a right gutter (a figure flush with the edge of the
    # screen reads as a figure the screen cut off) and one on the card's tint
    # edge; a tight band spends both on the figures instead, because at fourteen
    # cells an accent is what makes "gpt-5-codex" arrive as "gpt-5-co…".
    fancy = plain >= 17
    width = max(11, plain - (1 if fancy else 0))
    inner = max(6, width - 2)
    short = inner < 24  # "+12.0% vs prior window" needs 22 cells to say itself
    surface = "" if mono else f"on {_CARD_SURFACE}"
    blank = f" {_pad('', inner)}"
    # label, air, figure, note: the air is INSIDE the block, between the name of
    # the figure and the figure, so the eye lands on the number first.
    # The extra row is bought once, not once per card ROW: at eighty columns the
    # band is two rows of three, and air inside both of them spends eight rows of
    # a twenty-four row terminal on the headline. A single row of six, which is
    # the shape the approved design draws, is the shape that gets the space.
    plan = (["air"] * air) + ["label"] + (["air"] if per_row >= 6 else []) + [
        "value", "note"] + (["air"] * air)

    body = Text()
    # The band ends at its last painted row. A newline after it is a row of the
    # Static's height spent on nothing, and at thirty rows a screen that row
    # comes straight off the table underneath.
    total_lines = -(-len(cards) // max(1, per_row)) * len(plan)
    painted = 0
    for start in range(0, len(cards), per_row):
        row = cards[start:start + per_row]
        for slot in plan:
            for column, card in enumerate(row):
                tint = _CARD_COLORS.get(card.get("key"), _CARD_TINT_DEFAULT)
                edge = fancy and not mono
                if slot == "air":
                    if edge:
                        body.append(" ", style=f"on {tint}")
                    body.append(_pad(blank, width - (1 if edge else 0)), style=surface)
                    if column < len(row) - 1:
                        body.append(" " * gutter)
                    continue
                # Tint by WHAT the figure is, not by where it landed: cost is
                # mint and tokens lavender in every pane and every row count, so
                # a colour seen twice is the same kind of number twice.
                if slot == "label":
                    # The long name of the figure when there is room for it, the
                    # short one when there is not, and a clipped label never: an
                    # ellipsis on a heading asks the reader to guess which figure
                    # they are looking at.
                    label = str(card["label"])
                    short_label = str(card.get("short_label") or "")
                    if cell_len(label) > inner and short_label:
                        label = short_label
                    text, style = _fit(label.upper(), inner), f"{_CARD_LABEL} {surface}"
                elif slot == "value":
                    # Abbreviate the unit before cropping the figure. "2d 3h" is
                    # the same length of time as "2 days 3 hours"; "2 days 3 ho…"
                    # is a number with a piece missing.
                    value = str(card["value"])
                    short_value = str(card.get("short_value") or "")
                    if cell_len(value) > inner and short_value:
                        value = short_value
                    text = _fit(value, inner)
                    # The plate is the emphasis a terminal can actually give a
                    # number: it cannot print the figure twice the size of its
                    # label, so the figure gets its own ground instead.
                    plate = surface if mono else f"on {_CARD_PLATE}"
                    style = (f"bold {tint} {plate}").strip() if not mono else surface
                else:  # the note under the figure: a delta, or the caveat
                    delta = card.get("delta")
                    if delta is not None:
                        text = _delta_text(delta, short=short)
                    else:
                        detail = str(card.get("detail", ""))
                        if short or cell_len(detail) > inner:
                            detail = str(card.get("short_detail") or detail)
                        # Prose is not a figure: a third of "prompt-token weighted"
                        # is not a shorter truth, it is a broken sentence. If the
                        # card cannot hold one, it shows none, and the pane's
                        # caption underneath still carries the caveat.
                        text = detail if cell_len(detail) <= inner else ""
                    style = f"#7f8da3 {surface}"
                if edge:
                    body.append(" ", style=f"on {tint}")
                body.append(
                    _pad(f" {_pad(text, inner)}", width - (1 if edge else 0)),
                    style=style,
                )
                if column < len(row) - 1:
                    body.append(" " * gutter)
            painted += 1
            if painted < total_lines:
                body.append("\n")
    return body
