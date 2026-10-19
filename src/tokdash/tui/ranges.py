"""The shared historical range: presets, stepping, validation, and Stats' window.

Pure model, no Textual, no Rich, no I/O -- every rule the Overview, Sessions,
Report and Speed panes share lives here so the four of them cannot disagree
about what "Last Month" meant, and so the whole date surface is testable
without an app.

Two laws hold this together:

1. A selection always resolves to EXPLICIT inclusive date bounds, and the API
   period is always one the resolver recognises ("today", or "all"). Endpoint
   labels like "lastWeek" are never sent as a period: an unrecognised period
   silently resolves to all time, which would quietly widen every number on
   screen. All Time is the one window with no dates, and it says so with
   ``period="all"``.
2. Stepping a window is only meaningful for a window that has one. All Time has
   no bounds to move, and no window may step into the future -- both are inert
   here rather than "best effort" in the UI.

Calendar presets (Last Week, This/Last Month, This/Last Year) step by WHOLE
calendar windows: one back from the partial current month is the COMPLETE
previous month, and forward returns to the partial current month. Rolling
presets (Last 7 Days, Last 365 Days) and a custom pair step by their own
inclusive length, so a seven-day window never turns into a six-day one.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta

ARROW = chr(8594)  # -> the same range arrow the CLI report uses

ALL_PERIOD = "all"
# The period token every dated window is fetched under. The resolver's date pair
# wins over it, so this only names the route's default (and keeps the key the
# same one the dashboard's explicit quick ranges build).
DATED_PERIOD = "today"

WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _month_end(day: date) -> date:
    return (_month_start(day) + timedelta(days=32)).replace(day=1) - timedelta(days=1)


def calendar_window(unit: str, today: date, shift: int) -> tuple[date, date]:
    """The window for a calendar unit ``shift`` whole periods before ``today``.

    Shift 0 is the CURRENT, today-clamped window (what "This Month" means on the
    17th: the 1st through the 17th). Shift >= 1 is a COMPLETE past window -- the
    whole month, week or year, never the current one pulled a day shorter.
    """
    if unit == "day":
        start = today - timedelta(days=shift)
        return start, start
    if unit == "week":
        start = today - timedelta(days=today.weekday() + 7 * shift)
        end = start + timedelta(days=6)
        return start, min(end, today) if shift == 0 else end
    if unit == "month":
        total = today.year * 12 + today.month - 1 - shift
        year, month0 = divmod(total, 12)
        start = date(year, month0 + 1, 1)
        end = _month_end(start)
        return start, min(end, today) if shift == 0 else end
    if unit == "year":
        year = today.year - shift
        start = date(year, 1, 1)
        end = date(year, 12, 31)
        return start, min(end, today) if shift == 0 else end
    raise ValueError(f"unknown calendar unit {unit!r}")


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    short: str  # the compact button label
    unit: str | None  # day/week/month/year; None = rolling or windowless
    rolling_days: int | None  # for rolling presets (inclusive length)
    shift: int  # whole units back from the current one (0 = current window)
    all_time: bool = False


PRESETS: tuple[Preset, ...] = (
    Preset("today", "Today", "Today", "day", None, 0),
    Preset("yesterday", "Yesterday", "Yest.", "day", None, 1),
    Preset("7d", "Last 7 Days", "7d", None, 7, 0),
    Preset("lastweek", "Last Week (Mon-Sun)", "Lwk", "week", None, 1),
    Preset("month", "This Month", "Mth", "month", None, 0),
    Preset("lastmonth", "Last Month", "LMth", "month", None, 1),
    Preset("year", "This Year", "Yr", "year", None, 0),
    Preset("lastyear", "Last Year", "LYr", "year", None, 1),
    Preset("365d", "Last 365 Days", "365d", None, 365, 0),
    Preset("all", "All Time", "All", None, None, 0, all_time=True),
)
PRESET_BY_KEY: dict[str, Preset] = {p.key: p for p in PRESETS}

# Single-keystroke presets (spec 2). "d" is Yesterday, "w" is the rolling
# seven-day window -- NOT the calendar week, which is what the old "w" meant.
# The old meaning is reachable as a named preset, so no window was lost.
RANGE_KEYS: dict[str, str] = {
    "t": "today",
    "d": "yesterday",
    "w": "7d",
    "m": "month",
    "y": "year",
    "a": "all",
}


@dataclass(frozen=True)
class DateWindow:
    """A resolved, inclusive historical window with its provenance attached."""

    preset: str
    label: str
    date_from: date | None
    date_to: date | None
    shift: int = 0
    all_time: bool = False
    custom: bool = False
    # Set for a rolling window that no preset owns ("--period 14"). It keys none
    # of the buttons: the dates are what names such a window.
    rolling_days: int | None = None

    # ------------------------------------------------------------- requests --
    def request(self) -> tuple[str, str | None, str | None]:
        """The (period, date_from, date_to) triple to fetch with.

        Dated windows always travel as explicit dates under the route's default
        period; only All Time travels as a period token. This is the one place
        a user selection becomes an API call.
        """
        if self.all_time:
            return (ALL_PERIOD, None, None)
        if self.date_from is None or self.date_to is None:
            return (ALL_PERIOD, None, None)
        return (DATED_PERIOD, self.date_from.isoformat(), self.date_to.isoformat())

    @property
    def days(self) -> int | None:
        if self.date_from is None or self.date_to is None:
            return None
        return (self.date_to - self.date_from).days + 1

    # ------------------------------------------------------------- stepping --
    def step(self, delta: int, today: date) -> DateWindow | None:
        """Move the window ``delta`` whole steps, or None when it cannot move.

        ``delta`` is negative to look EARLIER (``[``) and positive to come back
        toward today. None is a real answer, not a failure: All Time has no
        bounds, and no window may move into the future -- callers must treat it
        as "inert" and leave their in-flight loads alone.
        """
        if self.all_time:
            return None
        if self.custom:
            length = self.days or 1
            moved_from = self.date_from + timedelta(days=delta * length)
            if moved_from > today:
                return None
            return replace(
                self,
                date_from=moved_from,
                date_to=min(self.date_to + timedelta(days=delta * length), today),
                label="Custom",
                custom=True,
                shift=0,
            )
        if self.rolling_days is not None:
            # A rolling window the CLI handed us (``--period 14``) has no preset
            # to step, so it moves by its own inclusive length like a typed pair.
            new_shift = self.shift - delta
            if new_shift < 0:
                return None
            start, end = _rolling_window(today, self.rolling_days, new_shift)
            return replace(self, date_from=start, date_to=end, shift=new_shift)
        preset = PRESET_BY_KEY.get(self.preset)
        if preset is None or preset.all_time:
            return None
        # One step of ``[`` is one MORE period back, hence the subtraction; the
        # current window (shift below the preset's own) is the future edge.
        new_shift = self.shift - delta
        if new_shift < min(0, preset.shift if preset.unit is None else 0):
            return None
        return _window_from_preset(preset, today, new_shift)

    # -------------------------------------------------------------- display --
    def describe(self) -> str:
        """The exact, inclusive window text. Never a bare preset name."""
        if self.all_time:
            return "All Time (every recorded session)"
        assert self.date_from and self.date_to
        if self.date_from == self.date_to:
            return f"{self.date_from.isoformat()}  (1 day)"
        return (
            f"{self.date_from.isoformat()} " + ARROW + f" {self.date_to.isoformat()}"
            f"  ({self.days} days)"
        )

    def short_window(self) -> str:
        """The same exact window with the separators an 80-column strip needs.

        A narrow terminal still gets real dates: "2026-10-12..2026-10-18", never
        a preset name that leaves the inclusive bounds to the imagination.
        """
        if self.all_time:
            return "All Time"
        assert self.date_from and self.date_to
        if self.date_from == self.date_to:
            return f"{self.date_from.isoformat()} (1 day)"
        return (
            f"{self.date_from.isoformat()}..{self.date_to.isoformat()}"
            f" ({self.days}d)"
        )

    def redate(self, today: date) -> "DateWindow":
        """The same selection anchored to a different "today".

        Only a day-pinned window moves. A typed custom pair is kept exactly as
        typed -- the user asked for those dates, and midnight does not change
        what they meant -- and All Time has no dates to move.
        """
        if self.custom or self.all_time:
            return self
        preset = PRESET_BY_KEY.get(self.preset)
        if preset is None:
            if self.rolling_days is None:
                return self
            start, end = _rolling_window(today, self.rolling_days, 0)
            return replace(self, date_from=start, date_to=end, shift=0)
        return _window_from_preset(preset, today, self.shift)

    @property
    def short_label(self) -> str:
        preset = PRESET_BY_KEY.get(self.preset)
        return preset.short if preset else (self.label or "Custom")


def _rolling_window(today: date, length: int, shift: int) -> tuple[date, date]:
    """The inclusive N-day window ending ``shift`` whole windows back."""
    end = today - timedelta(days=shift * length)
    return end - timedelta(days=length - 1), end


def _label_for(preset: Preset, shift: int) -> str:
    """Name the window the resolved dates actually describe.

    Stepping a preset can land it on another preset's home shift (Yesterday
    pressed ``]`` lands on Today), so the name comes from the shift rather than
    from the preset the user first pressed -- otherwise the label would sit
    under the wrong dates.
    """
    if preset.unit is None:
        if not shift:
            return preset.label
        unit = "window" if shift == 1 else "windows"
        return f"{preset.label} ({shift} {unit} back)"
    names = {
        "day": {0: "Today", 1: "Yesterday"},
        "week": {0: "This Week (Mon-today)", 1: "Last Week (Mon-Sun)"},
        "month": {0: "This Month", 1: "Last Month"},
        "year": {0: "This Year", 1: "Last Year"},
    }[preset.unit]
    if shift in names:
        return names[shift]
    unit = {"day": "day", "week": "week", "month": "month", "year": "year"}[preset.unit]
    return f"{shift} {unit}{'s' if shift != 1 else ''} back"


def _window_from_preset(preset: Preset, today: date, shift: int) -> DateWindow:
    if preset.all_time:
        return DateWindow(preset.key, preset.label, None, None, shift, all_time=True)
    if preset.unit is None:
        start, end = _rolling_window(today, preset.rolling_days or 1, shift)
    else:
        start, end = calendar_window(preset.unit, today, shift)
    return DateWindow(preset.key, _label_for(preset, shift), start, end, shift)


def window_for(token: str, today: date) -> DateWindow:
    """The unshifted window a preset key names (``t``/``w``/``m``/... or a key)."""
    key = RANGE_KEYS.get(str(token or "").strip().lower(), str(token or "").strip().lower())
    preset = PRESET_BY_KEY.get(key)
    if preset is None:
        raise ValueError(f"unknown range preset {token!r}")
    return _window_from_preset(preset, today, preset.shift)


# A CLI period length only borrows a button when the window is IDENTICAL; the
# labels below are the three where compute's arithmetic and ours agree exactly.
ROLLING_PRESET_KEYS = {1: "today", 7: "7d", 365: "365d"}


def window_for_days(days: int, today: date) -> DateWindow:
    """The finite rolling window a period LENGTH names (``--period 14``).

    Kept separate from `window_for`: a length is not a preset. ``14`` and ``30``
    are trailing spans ending today, so they get their own key and can never
    light the "Mth" button, which would claim a calendar month the dates do not
    show.
    """
    length = max(1, int(days))
    key = ROLLING_PRESET_KEYS.get(length)
    if key is not None:
        return window_for(key, today)
    start, end = _rolling_window(today, length, 0)
    return DateWindow(
        f"rolling{length}", f"Last {length} Days", start, end, rolling_days=length
    )


def window_for_period(token: str, today: date) -> DateWindow:
    """The window a CLI period token selects, or Today for anything it does not know.

    ``tokdash tui --period`` speaks compute's period language, so the token is
    translated the way the ROUTES translate it -- including the one period the
    resolver refuses to reduce to a length. ``period_to_range_args`` special-cases
    "month" as the FIRST of the current calendar month, so ``--period month``
    opens October 1..18, not a 30-day rolling window; a TUI that went through
    ``period_to_days`` alone would show September 19 under the same command line.
    Deferred import: this module stays cheap to import, and the startup path
    pays it once.
    """
    from ..compute import period_is_recognized, period_to_days

    text = str(token or "").strip().lower()
    if not text or not period_is_recognized(text):
        return window_for("today", today)
    if text == "month":
        # The resolver's calendar special case, kept rather than re-derived.
        return window_for("month", today)
    days = period_to_days(text)
    if days >= 36500:  # compute's ALL_TIME_DAYS: the one windowless answer
        return window_for("all", today)
    return window_for_days(days, today)


def parse_custom_date(raw: str, today: date) -> tuple[date | None, str | None]:
    """Parse one typed ISO date. Returns (date, error) -- the error keeps the
    typed text intact so a half-entered date is never thrown away."""
    text = str(raw or "").strip()
    if not text:
        return None, "enter a date as YYYY-MM-DD"
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None, f"'{text}' is not a YYYY-MM-DD date"
    if parsed > today:
        return None, f"{text} is in the future"
    return parsed, None


def custom_window(
    raw_from: str, raw_to: str, today: date
) -> tuple[DateWindow | None, str | None]:
    """Validate a typed pair IN PLACE: an error names the problem and leaves both
    fields as typed, so the user fixes the one bad half instead of retyping."""
    start, err_from = parse_custom_date(raw_from, today)
    end, err_to = parse_custom_date(raw_to, today)
    if err_from or err_to:
        return None, err_from or err_to
    assert start and end
    if start > end:
        return None, f"{start.isoformat()} is after {end.isoformat()}"
    return DateWindow("custom", "Custom", start, end, custom=True), None


# ---------------------------------------------------------------------------
# Stats: its own annual selection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatsWindow:
    """Stats answers for one annual window and nothing else.

    ``year=None`` is the route's trailing 365 days -- NOT this calendar year,
    which is the single most common misreading of that endpoint. The Stats view
    therefore names its window out loud and never borrows the shared range.
    """

    year: int | None = None

    @property
    def is_trailing(self) -> bool:
        return self.year is None

    def request(self) -> int | None:
        return self.year

    def bounds(self, today: date) -> tuple[date, date]:
        """The inclusive days this rung covers, never a day that has not happened.

        A NAMED year runs Jan 1 to Dec 31 -- except the current one, which the
        endpoint can only answer through today. Naming a window the data cannot
        reach is the mistake this whole module exists to stop.
        """
        if self.year is None:
            return today - timedelta(days=364), today
        return date(self.year, 1, 1), min(date(self.year, 12, 31), today)

    def describe(self, today: date) -> str:
        start, end = self.bounds(today)
        if self.year is None:
            return f"Trailing 365 Days  ({start.isoformat()} .. {end.isoformat()})"
        if end < date(self.year, 12, 31):
            return (f"{self.year} to date  ({start.isoformat()} .. {end.isoformat()})")
        return f"{self.year}  ({start.isoformat()} .. {end.isoformat()})"

    def step(self, delta: int, today: date) -> StatsWindow:
        """One rung either way: trailing 365 days, then whole calendar years.

        The newest rung is the trailing window and the one below it is THIS
        calendar year, so a reader standing on "last 365 days" can ask for the
        year they are in -- which the old ladder stepped straight past. Past
        "now" there is nothing to return, and delta > 0 walks back up to the
        trailing rung rather than into a year nobody has lived through.
        """
        if self.year is None:
            return StatsWindow(today.year) if delta < 0 else self
        nxt = self.year + delta
        if nxt > today.year:
            return StatsWindow(None)  # newest rung is the trailing year
        return StatsWindow(nxt)
