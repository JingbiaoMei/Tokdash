"""tokdash.tui.data: cache-key parity with the API routes, window mapping,
backpressure retry, and the disabled-DB no-ops.

The core of the module's contract is that the TUI/report keys are BYTE-IDENTICAL
to what the FastAPI routes build — same helpers, same arguments — so the TUI
reuses the routes' single-flight/TTL/stale-while-revalidate/day-pin semantics
instead of running a parallel cache that drifts from the warmers.
"""
from __future__ import annotations

import datetime

import pytest

import tokdash.api as api
from tokdash.tui import data, remote
from tokdash.usage_store import UsageDatabaseSchemaTooNewError

# 2026-09-22 is a Tuesday: the Monday-week window is 2026-09-21 → 2026-09-22.
TUESDAY = datetime.date(2026, 9, 22)

from tokdash.tui import remote  # noqa: E402  (round-2 remote-delegation tests)


@pytest.fixture(autouse=True)
def _fresh_remote_state():
    """The probe verdict is module-global by design (one probe per run); reset it
    so a latched "gone"/RemoteService never leaks across tests."""
    data.reset_remote()
    yield
    data.reset_remote()


class _Recorder:
    """Stand-in for get_cached_or_fetch: records the call, hands back metadata."""

    def __init__(self, status="hit", age_seconds=1.5):
        self.calls: list[tuple] = []
        self.kwargs: list[dict] = []
        self._status = status
        self._age = age_seconds

    def __call__(self, key, fetch_fn, **kwargs):
        self.calls.append((key, fetch_fn))
        self.kwargs.append(kwargs)
        return api.CacheFetchResult(
            value={"payload": key}, status=self._status, age_seconds=self._age
        )


# ---------------------------------------------------------------------------
# Cache-key parity with the routes (the core test)
# ---------------------------------------------------------------------------

def test_usage_key_parity_with_route(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(data, "get_cached_or_fetch", rec)
    # ASYMMETRIC dates on purpose: symmetric (d, d) would let a swapped
    # date_from/date_to inside data.py's key call pass parity unnoticed.
    d_from, d_to = "2026-09-01", "2026-09-20"
    outcome = data.fetch_usage("today", d_from, d_to)
    assert len(rec.calls) == 1
    # Same helpers, same args → byte-identical keys (pricing fold + day-pin
    # included: _window_cache_key runs on both sides of this assert).
    assert rec.calls[0][0] == api._usage_cache_key("today", d_from, d_to)
    assert outcome.status == "hit" and outcome.age_seconds == 1.5
    # Metadata + refresh plumbing reach the cache intact.
    assert rec.kwargs[0] == {"force_refresh": False, "return_metadata": True}
    data.fetch_usage("today", d_from, d_to, refresh=True)
    assert rec.kwargs[1]["force_refresh"] is True


def test_usage_fetch_closure_calls_route_function(monkeypatch):
    rec = _Recorder()
    seen = []
    monkeypatch.setattr(data, "get_cached_or_fetch", rec)
    monkeypatch.setattr(
        data,
        "compute_usage_with_comparison",
        lambda *a: seen.append(a) or {"ok": True},
    )
    data.fetch_usage("today", "2026-09-01", "2026-09-20")
    rec.calls[0][1]()  # run the stored fetch closure
    assert seen == [("today", "2026-09-01", "2026-09-20")]


def test_active_time_key_parity_and_review_flag_splits_keys(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(data, "get_cached_or_fetch", rec)
    # Asymmetric dates so a swapped date_from/date_to in data.py is visible
    # to the key comparison (see the usage-parity test for why).
    d_from, d_to = "2026-09-01", "2026-09-20"
    data.fetch_active_time("today", d_from, d_to, True)
    data.fetch_active_time("today", None, None, None)
    assert rec.calls[0][0] == api._active_time_cache_key("today", d_from, d_to, True)
    assert rec.calls[1][0] == api._active_time_cache_key("today", None, None, None)
    # Overview (None = route default) and Report (True) are different keys on
    # purpose; they must never silently collide.
    assert rec.calls[0][0] != rec.calls[1][0]


def test_active_time_fetch_goes_through_payload_augmentation(monkeypatch):
    # The payload — not get_active_time_data — is what the route and the
    # warmer store; bypassing it loses the route-only "range" field.
    rec = _Recorder()
    seen = []
    monkeypatch.setattr(data, "get_cached_or_fetch", rec)
    monkeypatch.setattr(
        data,
        "_active_time_payload",
        lambda *a: seen.append(a) or {"ok": True},
    )
    data.fetch_active_time("today", "2026-09-01", "2026-09-22", True)
    rec.calls[0][1]()
    assert seen == [("today", "2026-09-01", "2026-09-22", True)]


def test_insights_key_parity_and_facets_identity(monkeypatch):
    rec = _Recorder()
    key_seen = []
    monkeypatch.setattr(data, "get_cached_or_fetch", rec)

    def key_rec(*args):
        key_seen.append(args)
        return api._insights_cache_key(*args)

    monkeypatch.setattr(data, "_insights_cache_key", key_rec)
    data.fetch_insights("year", "2026-01-01", "2026-09-22")
    # The default facet string reaches the key as the SAME object the routes
    # use — reordered or interpolated facets are a key nobody warmed.
    assert key_seen == [("year", "2026-01-01", "2026-09-22", api.REPORT_FACETS, True)]
    assert key_seen[0][3] is api.REPORT_FACETS
    assert rec.calls[0][0] == api._insights_cache_key(
        "year", "2026-01-01", "2026-09-22", api.REPORT_FACETS, True
    )


def test_stats_key_parity_with_route_expression(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(data, "get_cached_or_fetch", rec)
    data.fetch_stats(None)
    data.fetch_stats(2026)
    # Byte-for-byte the route expression at api.py:2330-2332:
    # _window_cache_key(f"stats_{year}", None, f"{year}-12-31" if year else None)
    assert rec.calls[0][0] == api._window_cache_key("stats_None", None, None)
    assert rec.calls[1][0] == api._window_cache_key("stats_2026", None, "2026-12-31")
    # The route tests truthiness, not `is not None`: year=0 behaves as None.
    data.fetch_stats(0)
    assert rec.calls[2][0] == api._window_cache_key("stats_0", None, None)


def test_outcome_passes_status_and_age_through(monkeypatch):
    rec = _Recorder(status="stale", age_seconds=42.0)
    monkeypatch.setattr(data, "get_cached_or_fetch", rec)
    outcome = data.fetch_usage("today")
    assert isinstance(outcome, data.FetchOutcome)
    assert (outcome.status, outcome.age_seconds) == ("stale", 42.0)
    assert outcome.value == {"payload": rec.calls[0][0]}


# ---------------------------------------------------------------------------
# resolve_report_period / report_windows
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("token", "idx", "expected_from"),
    [("week", 0, "2026-09-21"), ("month", 1, "2026-09-01"), ("year", 2, "2026-01-01")],
)
def test_calendar_periods_use_report_windows(token, idx, expected_from):
    period, date_from, date_to = data.resolve_report_period(token, today=TUESDAY)
    # The route period becomes "today" — the warmer's key family.
    assert (period, date_from, date_to) == ("today", expected_from, "2026-09-22")
    # Cross-check against _report_windows itself, not a hand-computed calendar.
    assert (date_from, date_to) == api._report_windows(TUESDAY)[idx]


def test_calendar_periods_case_insensitive():
    assert data.resolve_report_period("WEEK", today=TUESDAY) == data.resolve_report_period(
        "week", today=TUESDAY
    )


@pytest.mark.parametrize("token", ["today", "3days", "7d", "14d", "2w", "30", "1y", "all"])
def test_rolling_tokens_pass_through(token):
    assert data.resolve_report_period(token, today=TUESDAY) == (token, None, None)


def test_unknown_period_raises_value_error():
    with pytest.raises(ValueError, match="Unknown period 'banana'"):
        data.resolve_report_period("banana", today=TUESDAY)


def test_report_windows_delegates():
    assert data.report_windows(TUESDAY) == api._report_windows(TUESDAY)
    assert len(data.report_windows()) == 3  # no today → _local_today()


# ---------------------------------------------------------------------------
# Period shift ( [ ] / 0 ) — WHOLE calendar periods; warm parity at shift 0
# ---------------------------------------------------------------------------

SEP_SUN = datetime.date(2026, 9, 20)  # a SUNDAY (fixture app's "today")


def test_shift_zero_is_the_exact_unshifted_triple():
    # Warm-key parity law: shift=0 must return BYTE-EQUAL results to no-shift
    # for every token and day — the shifted resolver may never re-key today's
    # warm windows. Cross-checked against _report_windows itself.
    for day in (TUESDAY, LEAP_DAY, SEP_SUN, datetime.date(2026, 1, 1)):
        for token, idx in [("week", 0), ("month", 1), ("year", 2)]:
            warm = api._report_windows(day)[idx]
            for fn in (data.resolve_report_period, data.resolve_overview_period):
                assert fn(token, today=day, shift=0)[1:] == warm
                assert fn(token, today=day) == fn(token, today=day, shift=0)
    assert data.resolve_overview_period("today", today=SEP_SUN, shift=0)[1:] == (
        "2026-09-20", "2026-09-20")


def test_month_shift_returns_full_previous_months():
    # From ANY September day: one step = the FULL Aug 1 -> Aug 31 (never a
    # "Sep 1 pulled one day shorter"), year-crossing steps included.
    for shift, pair in [(1, ("2026-08-01", "2026-08-31")),
                        (2, ("2026-07-01", "2026-07-31")),
                        (9, ("2025-12-01", "2025-12-31"))]:
        assert data.resolve_overview_period(
            "month", today=SEP_SUN, shift=shift)[1:] == pair
        assert data.resolve_report_period(
            "month", today=SEP_SUN, shift=shift)[1:] == pair


def test_month_shift_lands_on_februarys_true_end():
    # Month-end clamp, both ways: stepping March back lands on Feb's ACTUAL
    # last day — common year 28, leap year 29 (never the nonexistent Feb 31).
    assert data.resolve_overview_period(
        "month", today=datetime.date(2026, 3, 31), shift=1)[1:] == (
        "2026-02-01", "2026-02-28")
    assert data.resolve_overview_period(
        "month", today=datetime.date(2028, 3, 31), shift=1)[1:] == (
        "2028-02-01", "2028-02-29")


def test_week_shift_returns_full_previous_weeks():
    # SEP_SUN is its week's LAST day; one step back is the whole week before.
    assert data.resolve_overview_period(
        "week", today=SEP_SUN, shift=1)[1:] == ("2026-09-07", "2026-09-13")
    assert data.resolve_overview_period(
        "week", today=SEP_SUN, shift=2)[1:] == ("2026-08-31", "2026-09-06")
    # A mid-week "today" (TUE 9.22 sits in week 9.21→9.27) steps to THAT
    # week's predecessor whole — the step counts weeks, not days from today.
    assert data.resolve_report_period(
        "week", today=TUESDAY, shift=1)[1:] == ("2026-09-14", "2026-09-20")


def test_year_shift_returns_full_previous_years_even_from_leap_day():
    assert data.resolve_overview_period(
        "year", today=SEP_SUN, shift=1)[1:] == ("2025-01-01", "2025-12-31")
    assert data.resolve_overview_period(
        "year", today=LEAP_DAY, shift=1)[1:] == ("2023-01-01", "2023-12-31")
    # A Feb-29 "today" stepping into a common year: endpoints come from the
    # year NUMBER, so there is no Feb-31-style clamp to explode on.
    assert data.resolve_report_period(
        "year", today=datetime.date(2028, 2, 29), shift=1)[1:] == (
        "2027-01-01", "2027-12-31")


def test_today_shift_steps_single_days():
    # The "today" token's period IS a day — its steps stay day-by-day, and
    # they cross year boundaries on plain date arithmetic.
    assert data.resolve_overview_period(
        "today", today=SEP_SUN, shift=1)[1:] == ("2026-09-19", "2026-09-19")
    assert data.resolve_overview_period(
        "today", today=datetime.date(2026, 1, 5), shift=10)[1:] == (
        "2025-12-26", "2025-12-26")


@pytest.mark.parametrize("token", ["all", "7d", "3days", "1y"])
def test_shift_is_inert_on_windowless_tokens(token):
    # No window to step: both resolvers pass through UNCHANGED at any shift
    # (the app never steps these; the resolvers stay honest regardless).
    assert data.resolve_overview_period(token, today=SEP_SUN, shift=3) == (
        token, None, None)
    assert data.resolve_report_period(token, today=SEP_SUN, shift=3) == (
        token, None, None)


# ---------------------------------------------------------------------------
# Backpressure retry (index.html fetchJsonWithRetry parity)
# ---------------------------------------------------------------------------

class _SleepSpy:
    def __init__(self):
        self.delays: list[float] = []

    def __call__(self, seconds):
        self.delays.append(seconds)


def _flaky(failures, result):
    state = {"calls": 0}

    def fn():
        state["calls"] += 1
        if state["calls"] <= failures:
            raise api.CacheBackpressureError("busy")
        return result

    fn.state = state
    return fn


def _patch_sleep_and_jitter(monkeypatch, jitter=0.1):
    sleep = _SleepSpy()
    uniforms = []

    def uniform(lo, hi):
        uniforms.append((lo, hi))
        return jitter

    monkeypatch.setattr(data.time, "sleep", sleep)
    monkeypatch.setattr(data.random, "uniform", uniform)
    return sleep, uniforms


def test_retry_succeeds_after_two_backpressures(monkeypatch):
    sleep, _ = _patch_sleep_and_jitter(monkeypatch)
    ok = data.FetchOutcome(value={"ok": True}, status="recomputed", age_seconds=None)
    fn = _flaky(2, ok)
    assert data._with_backpressure_retry(fn) is ok
    assert fn.state["calls"] == 3
    assert len(sleep.delays) == 2


def test_retry_exhaustion_reraises(monkeypatch):
    sleep, _ = _patch_sleep_and_jitter(monkeypatch)
    fn = _flaky(4, None)
    with pytest.raises(api.CacheBackpressureError):
        data._with_backpressure_retry(fn)
    assert fn.state["calls"] == 3  # 3 total attempts, then stop
    assert len(sleep.delays) == 2


def test_retry_delay_formula(monkeypatch):
    sleep, uniforms = _patch_sleep_and_jitter(monkeypatch, jitter=0.1)
    fn = _flaky(2, data.FetchOutcome(value={}, status="hit", age_seconds=0.0))
    data._with_backpressure_retry(fn)
    # delay = 450 ms * (attempt + 1) + jitter, jitter drawn from [0, 250 ms).
    assert sleep.delays == [pytest.approx(0.45 * 1 + 0.1), pytest.approx(0.45 * 2 + 0.1)]
    assert uniforms == [(0.0, 0.25), (0.0, 0.25)]


def test_retry_never_touches_terminal_or_programming_errors(monkeypatch):
    sleep, _ = _patch_sleep_and_jitter(monkeypatch)
    too_new = UsageDatabaseSchemaTooNewError(path="/tmp/x.sqlite3", found=99, supported=9)

    def raiser(exc):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise exc

        fn.calls = calls
        return fn

    fn = raiser(too_new)
    with pytest.raises(UsageDatabaseSchemaTooNewError):
        data._with_backpressure_retry(fn)
    assert fn.calls["n"] == 1  # terminal by design: never retry

    fn = raiser(ValueError("bad"))
    with pytest.raises(ValueError):
        data._with_backpressure_retry(fn)
    assert fn.calls["n"] == 1

    assert sleep.delays == []


# ---------------------------------------------------------------------------
# Disabled-DB no-ops (TOKDASH_USAGE_DB=0 must not touch the filesystem)
# ---------------------------------------------------------------------------

def _forbid_store(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("UsageEntryStore constructed under TOKDASH_USAGE_DB=0")

    monkeypatch.setattr(data, "UsageEntryStore", boom)


def test_ensure_db_compatible_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")
    _forbid_store(monkeypatch)
    called = []
    monkeypatch.setattr(
        data, "raise_if_usage_db_incompatible", lambda *a: called.append(a)
    )
    data.ensure_usage_db_compatible()
    assert called == []


def test_ensure_db_compatible_calls_check_when_enabled(monkeypatch):
    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")
    _forbid_store(monkeypatch)  # the preflight must check, never construct
    called = []
    monkeypatch.setattr(
        data, "raise_if_usage_db_incompatible", lambda *a: called.append(a)
    )
    data.ensure_usage_db_compatible()
    assert called == [()]


def test_ensure_db_compatible_reraises_schema_too_new(monkeypatch):
    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")

    def boom(*a):
        raise UsageDatabaseSchemaTooNewError(path="/tmp/x.sqlite3", found=99, supported=9)

    monkeypatch.setattr(data, "raise_if_usage_db_incompatible", boom)
    with pytest.raises(UsageDatabaseSchemaTooNewError):
        data.ensure_usage_db_compatible()


def test_db_summary_disabled(monkeypatch):
    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")
    _forbid_store(monkeypatch)
    assert data.db_summary() == "usage db disabled (TOKDASH_USAGE_DB=0) — live parsing"


def test_db_summary_enabled(monkeypatch):
    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")

    class FakeStore:
        def status(self):
            return {"path": "/tmp/.tokdash/usage.sqlite3", "usage_entries": 17}

    monkeypatch.setattr(data, "UsageEntryStore", FakeStore)
    assert data.db_summary() == "/tmp/.tokdash/usage.sqlite3 · 17 usage rows"


# ---------------------------------------------------------------------------
# Import discipline (data.py must stay headless)
# ---------------------------------------------------------------------------

def test_data_module_imports_no_textual_or_rich():
    # Headless-safe boundary: the report path imports data.py and must never
    # pull Textual/rich transitively (full meta_path lock: test_tui_import_discipline.py).
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[1] / "src" / "tokdash" / "tui" / "data.py"
    ).read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        assert not stripped.startswith(("import textual", "import rich", "from textual", "from rich"))


# ---------------------------------------------------------------------------
# Round 2 — resolve_overview_period: calendar windows pinned to fixed dates
# ---------------------------------------------------------------------------

# 2026-09-23 is a Wednesday: Monday-week window 2026-09-21 → 2026-09-23.
WEDNESDAY = datetime.date(2026, 9, 23)
# 2026-09-21 IS the Monday — the week window collapses to a single day.
MONDAY = datetime.date(2026, 9, 21)
# Leap day. Thursday; the Monday-week window opens 2024-02-26.
LEAP_DAY = datetime.date(2024, 2, 29)


@pytest.mark.parametrize(
    ("token", "expected_from"),
    [
        ("today", "2026-09-23"),
        ("week", "2026-09-21"),
        ("month", "2026-09-01"),
        ("year", "2026-01-01"),
    ],
)
def test_resolve_overview_period_fixed_wednesday(token, expected_from):
    period, date_from, date_to = data.resolve_overview_period(token, today=WEDNESDAY)
    assert (period, date_from, date_to) == ("today", expected_from, "2026-09-23")


def test_resolve_overview_period_pairs_equal_report_windows():
    # Same source of truth as the warmer, asserted against the windows
    # themselves — never against a hand-computed calendar.
    for token, idx in [("week", 0), ("month", 1), ("year", 2)]:
        _, date_from, date_to = data.resolve_overview_period(token, today=LEAP_DAY)
        assert (date_from, date_to) == api._report_windows(LEAP_DAY)[idx]
    # Leap-day specifics through the shared helper (2024-02-26 = that Monday).
    _, wf, wt = data.resolve_overview_period("week", today=LEAP_DAY)
    assert (wf, wt) == ("2024-02-26", "2024-02-29")
    _, mf, _ = data.resolve_overview_period("month", today=LEAP_DAY)
    assert mf == "2024-02-01"
    _, yf, _ = data.resolve_overview_period("year", today=LEAP_DAY)
    assert yf == "2024-01-01"


def test_resolve_overview_period_monday_week_collapses_to_single_day():
    # On a Monday the week window is (Mon, Mon) — from == to by design; the
    # server's midnight warm skips such keys (skip_single_day) and one cold
    # answer for it is correct, not a bug.
    period, date_from, date_to = data.resolve_overview_period("week", today=MONDAY)
    assert (period, date_from, date_to) == ("today", "2026-09-21", "2026-09-21")


def test_resolve_overview_period_today_pair_equals_warm_target_inputs():
    # "today" becomes the explicit (D, D) pair _usage_warm_target warms.
    period, date_from, date_to = data.resolve_overview_period("today", today=TUESDAY)
    warm_key, _warm_fn = api._usage_warm_target(TUESDAY.isoformat())
    assert warm_key == api._usage_cache_key(period, date_from, date_to)


@pytest.mark.parametrize(
    ("token", "idx"), [("week", 0), ("month", 1), ("year", 2)]
)
def test_resolve_overview_period_usage_keys_equal_report_warm_keys(token, idx):
    # The usage key the Overview asks for is BYTE-IDENTICAL to the Report-tab
    # warm key for the same window: _report_warm_targets lays targets out as
    # (usage, insights, active) per window, so the usage key is at idx*3.
    period, date_from, date_to = data.resolve_overview_period(token, today=WEDNESDAY)
    key = api._usage_cache_key(period, date_from, date_to)
    assert key == api._report_warm_targets(WEDNESDAY)[idx * 3][0]


@pytest.mark.parametrize("token", ["all", "7d", "3days", "30", "1y", "2w"])
def test_resolve_overview_period_rolling_passthrough(token):
    assert data.resolve_overview_period(token, today=TUESDAY) == (token, None, None)


def test_overview_periods_membership_lock():
    # Round-2 cycle order: the calendar tokens replaced the rolling "7d".
    assert data.OVERVIEW_PERIODS == ("today", "week", "month", "year", "all")
    assert data.PERIOD_KEYS == {
        "t": "today",
        "w": "week",
        "m": "month",
        "y": "year",
        "a": "all",
    }


# ---------------------------------------------------------------------------
# Round 2 — quota fetchers (always in-process)
# ---------------------------------------------------------------------------

def test_fetch_quota_state_key_is_route_literal(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(data, "get_cached_or_fetch", rec)
    # The /api/quota route stores its payload under the BARE literal
    # "quota_state" (api.py: `_cached_route("/api/quota", "quota_state", quota_state)`)
    # — no window/pricing folding. Any decoration here is a key the route never fills.
    outcome = data.fetch_quota_state()
    assert rec.calls[0][0] == "quota_state"
    assert rec.kwargs[0] == {"force_refresh": False, "return_metadata": True}
    assert outcome.status == "hit"
    data.fetch_quota_state(refresh=True)
    assert rec.kwargs[1]["force_refresh"] is True


def test_fetch_quota_state_closure_imports_real_collector(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(data, "get_cached_or_fetch", rec)
    seen = []

    def fake_state():
        seen.append("called")
        return {"providers": {}}

    import tokdash.sources.quota as quota_pkg

    monkeypatch.setattr(quota_pkg, "quota_state", fake_state, raising=True)
    data.fetch_quota_state()
    rec.calls[0][1]()  # run the stored closure
    assert seen == ["called"]


def test_fetch_quota_state_goes_through_backpressure_retry(monkeypatch):
    sleep, _ = _patch_sleep_and_jitter(monkeypatch)
    state = {"calls": 0}

    def flaky(key, fetch_fn, **kwargs):
        state["calls"] += 1
        if state["calls"] <= 2:
            raise api.CacheBackpressureError("busy")
        return api.CacheFetchResult(value={"ok": True}, status="hit", age_seconds=None)

    monkeypatch.setattr(data, "get_cached_or_fetch", flaky)
    outcome = data.fetch_quota_state()
    assert outcome.value == {"ok": True}
    assert state["calls"] == 3
    assert len(sleep.delays) == 2


class _HistoryStore:
    """Records the quota_history kwargs; stands in for UsageEntryStore()."""

    def __init__(self):
        self.kwargs = None

    def quota_history(self, **kwargs):
        self.kwargs = kwargs
        return {"series": [{"provider": "codex"}]}


def _forbid_history_store(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("UsageEntryStore constructed under TOKDASH_USAGE_DB=0")

    monkeypatch.setattr(data, "UsageEntryStore", boom)


def test_fetch_quota_history_params_mirror_route(monkeypatch):
    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")
    store = _HistoryStore()
    monkeypatch.setattr(data, "UsageEntryStore", lambda *a, **k: store)
    outcome = data.fetch_quota_history(hours=24, now_ts=1_000_000)
    # Route defaults: providers None, granularity "hour", max_points 300, and
    # the codex gate CLOSED without codex_api consent.
    assert store.kwargs == {
        "providers": None,
        "granularity": "hour",
        "start": 1_000_000 - 24 * 3600,
        "end": 1_000_000,
        "max_points": 300,
        "network_only_providers": set(),
    }
    assert outcome.value == {"series": [{"provider": "codex"}]}
    assert (outcome.status, outcome.age_seconds) == ("hit", None)


def test_fetch_quota_history_network_gate_applied(monkeypatch):
    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")
    store = _HistoryStore()
    monkeypatch.setattr(data, "UsageEntryStore", lambda *a, **k: store)

    import tokdash.sources.quota.config as quota_config

    monkeypatch.setattr(quota_config, "network_enabled", lambda key: key == "codex_api")
    data.fetch_quota_history(hours=6, now_ts=7200)
    assert store.kwargs["network_only_providers"] == {"codex"}
    assert store.kwargs["start"] == 7200 - 6 * 3600
    assert store.kwargs["end"] == 7200


def test_fetch_quota_history_disabled_db_builds_no_store(monkeypatch):
    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")
    _forbid_history_store(monkeypatch)
    rec = _Recorder()
    monkeypatch.setattr(data, "get_cached_or_fetch", rec)
    outcome = data.fetch_quota_history()
    assert (outcome.value, outcome.status, outcome.age_seconds) == ({"series": []}, "hit", None)
    assert rec.calls == []  # not cached either — there is nothing to cache


# ---------------------------------------------------------------------------
# Round 2 — remote delegation seams
# ---------------------------------------------------------------------------

def _enable_fake_service(monkeypatch, remote_get_fn):
    data.reset_remote()
    monkeypatch.setattr(
        remote, "probe_service", lambda **kw: remote.RemoteService("http://svc.test:1", "0")
    )
    monkeypatch.setattr(remote, "remote_get", remote_get_fn)


def test_remote_answer_wins_and_local_closure_untouched(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(data, "get_cached_or_fetch", rec)
    seen = {}

    def remote_get(svc, path, params, **kw):
        seen["path"] = path
        seen["params"] = params
        return {"answer": 1}, {"status": "stale", "age_seconds": 3.5}

    _enable_fake_service(monkeypatch, remote_get)
    outcome = data.fetch_usage("today", "2026-09-01", "2026-09-20")
    assert outcome.value == {"answer": 1}
    assert (outcome.status, outcome.age_seconds) == ("stale", 3.5)  # server meta wins
    assert rec.calls == []  # the local cache/closure never ran
    assert seen["path"] == "/api/usage"
    assert seen["params"] == {
        "period": "today",
        "date_from": "2026-09-01",
        "date_to": "2026-09-20",
    }


def test_remote_none_meta_synthesizes_hit(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(data, "get_cached_or_fetch", rec)
    # /api/stats has no injected metadata — status synthesized as "hit", no age.
    _enable_fake_service(monkeypatch, lambda svc, path, params, **kw: ({"y": 1}, None))
    outcome = data.fetch_stats(2026)
    assert (outcome.status, outcome.age_seconds) == ("hit", None)


def test_service_gone_falls_back_once_and_latches(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(data, "get_cached_or_fetch", rec)
    probes = {"n": 0}
    gone = {"n": 0}

    def counting_probe(**kw):
        probes["n"] += 1
        return remote.RemoteService("http://svc.test:1", "0")

    def gone_get(svc, path, params, **kw):
        gone["n"] += 1
        raise remote.ServiceGone("dead")

    data.reset_remote()
    monkeypatch.setattr(remote, "probe_service", counting_probe)
    monkeypatch.setattr(remote, "remote_get", gone_get)

    data.fetch_usage("today")  # probe → gone → local
    assert len(rec.calls) == 1
    assert data._REMOTE == "gone"

    data.fetch_usage("today")  # latched: still local, NO re-probe
    assert len(rec.calls) == 2
    assert probes["n"] == 1
    assert gone["n"] == 1

    data.reset_remote()
    data.fetch_usage("today")  # re-probe → gone again
    assert probes["n"] == 2


def test_refresh_param_sent_where_route_accepts_it(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(data, "get_cached_or_fetch", rec)
    calls: list[tuple[str, dict]] = []

    def remote_get(svc, path, params, **kw):
        calls.append((path, dict(params)))
        return {"ok": True}, None

    _enable_fake_service(monkeypatch, remote_get)
    data.fetch_usage("today", refresh=True)
    data.fetch_active_time("today", "2026-09-01", "2026-09-20", None, refresh=True)
    data.fetch_insights("year", "2026-01-01", "2026-09-20", refresh=True)
    data.fetch_stats(2026, refresh=True)  # the route has NO refresh param
    assert calls[0][1].get("refresh") is True
    assert calls[1][1].get("refresh") is True
    assert "include_review_sessions" not in calls[1][1]  # None → omitted (route default)
    assert calls[2][1].get("refresh") is True
    assert calls[2][1]["facets"] is api.REPORT_FACETS
    assert calls[2][1]["include_project_names"] is True
    assert "refresh" not in calls[3][1]
    assert calls[3][1] == {"year": 2026}


def test_remote_false_forces_local_even_with_service(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(data, "get_cached_or_fetch", rec)

    def boom(svc, path, params, **kw):
        raise AssertionError("remote_get called with remote=False")

    _enable_fake_service(monkeypatch, boom)
    data.fetch_usage("today", refresh=True, remote=False)
    assert len(rec.calls) == 1
    assert rec.kwargs[0]["force_refresh"] is True  # local refresh survives the seam


def test_kill_switch_leaves_local_path_byte_identical(monkeypatch):
    # TOKDASH_TUI_NO_REMOTE=1 (the suite-wide session default in conftest) must
    # make every fetch answer exactly as it did pre-delegation: key, closure,
    # and kwargs byte-for-byte what round 1 sent.
    monkeypatch.setenv("TOKDASH_TUI_NO_REMOTE", "1")
    rec = _Recorder()
    monkeypatch.setattr(data, "get_cached_or_fetch", rec)

    def boom(svc, path, params, **kw):
        raise AssertionError("remote_get ran despite TOKDASH_TUI_NO_REMOTE=1")

    monkeypatch.setattr(remote, "remote_get", boom)
    outcome = data.fetch_usage("today", "2026-09-01", "2026-09-20")
    assert rec.calls[0][0] == api._usage_cache_key("today", "2026-09-01", "2026-09-20")
    assert rec.kwargs[0] == {"force_refresh": False, "return_metadata": True}
    assert outcome.status == "hit"
