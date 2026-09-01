"""A cold fan-out drains instead of being rejected wholesale.

The Sessions tab issues one request per tool, so switching to a range nothing has
computed yet asks for ~17 distinct cold keys at once. While a stale value was almost
always available, refusing everything past the heavy-compute cap was harmless — the
caller had something to show. Once a closed window must actually be computed to be
correct, that refusal rejected the whole fan-out: measured against a live server, 13 of
15 tools got an instant 503 while the slots they needed freed a second later, and the
browser retries only three times before giving up.

A cold request with nothing to serve now waits briefly for a slot. The cap itself is
unchanged, so the parser stampede and RSS ceiling these tests do not measure stay bounded.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

import pytest

pytest.importorskip("fastapi")

import tokdash.api as api


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setenv("TOKDASH_WARM_ON_START", "0")
    api._clear_cache()
    yield
    api._clear_cache()


def _slow_fetch(seconds: float, value: str):
    def fetch():
        time.sleep(seconds)
        return value
    return fetch


def test_a_cold_fanout_of_distinct_keys_all_succeed(monkeypatch):
    """15 cold keys against 2 slots: every one is served, none refused."""
    monkeypatch.setattr(api, "_compute_semaphore", threading.BoundedSemaphore(2))
    monkeypatch.setattr(api, "_COMPUTE_WAIT_SECONDS", 30.0)

    results: dict[str, object] = {}
    errors: list[Exception] = []

    def worker(n: int):
        try:
            results[f"k{n}"] = api.get_cached_or_fetch(f"k{n}", _slow_fetch(0.05, f"v{n}"))
        except Exception as exc:  # noqa: BLE001 - the assertion below reports it
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"a cold fan-out was refused: {errors!r}"
    assert len(results) == 15
    assert results["k7"] == "v7"


def test_concurrency_cap_is_still_enforced_while_waiting(monkeypatch):
    """Waiting must not let more heavy computes run at once than the cap allows."""
    monkeypatch.setattr(api, "_compute_semaphore", threading.BoundedSemaphore(2))
    monkeypatch.setattr(api, "_COMPUTE_WAIT_SECONDS", 30.0)

    live = 0
    peak = 0
    guard = threading.Lock()

    def fetch():
        nonlocal live, peak
        with guard:
            live += 1
            peak = max(peak, live)
        time.sleep(0.05)
        with guard:
            live -= 1
        return "v"

    threads = [
        threading.Thread(target=lambda n=n: api.get_cached_or_fetch(f"cap{n}", fetch))
        for n in range(10)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert peak <= 2, f"{peak} heavy computes ran at once against a cap of 2"


def test_a_refresh_over_a_cached_value_never_waits_for_a_slot(monkeypatch):
    """The `wait=latest is None` guard, on the one path that actually reaches it.

    An ordinary stale read returns from the background-refresh branch long before the
    cold path, so it exercises nothing here. Only `refresh=1` — the Refresh button —
    skips that branch and arrives with a cached value in hand, and it must hand that
    value back rather than park a worker for a slot it does not need.
    """
    monkeypatch.setattr(api, "_compute_semaphore", threading.BoundedSemaphore(1))
    monkeypatch.setattr(api, "_COMPUTE_WAIT_SECONDS", 30.0)
    api._cache["refresh-key"] = (datetime.now().timestamp(), "cached")

    assert api._compute_semaphore.acquire(blocking=False)  # hold the only slot
    try:
        started = time.monotonic()
        value = api.get_cached_or_fetch(
            "refresh-key", _slow_fetch(5, "new"), force_refresh=True
        )
        elapsed = time.monotonic() - started
    finally:
        api._compute_semaphore.release()

    assert value == "cached"
    assert elapsed < 2.0, "a caller holding a value must not wait for a compute slot"


def test_an_ordinary_stale_read_still_returns_immediately(monkeypatch):
    """Guards the pre-existing fast path the test above used to be mistaken for."""
    monkeypatch.setattr(api, "_compute_semaphore", threading.BoundedSemaphore(1))
    monkeypatch.setattr(api, "_COMPUTE_WAIT_SECONDS", 30.0)
    api._cache["stale-key"] = (datetime.now().timestamp() - (api.CACHE_TTL + 10), "old")

    assert api._compute_semaphore.acquire(blocking=False)
    try:
        started = time.monotonic()
        assert api.get_cached_or_fetch("stale-key", _slow_fetch(5, "new")) == "old"
        assert time.monotonic() - started < 2.0
    finally:
        api._compute_semaphore.release()


def test_a_cold_request_gives_up_after_the_wait_and_reports_backpressure(monkeypatch):
    """The timed acquire timing out — the new failure path.

    The waiter-cap test short-circuits before `acquire(timeout=...)` is ever called,
    so without this the give-up branch is unexercised.
    """
    monkeypatch.setattr(api, "_compute_semaphore", threading.BoundedSemaphore(1))
    monkeypatch.setattr(api, "_COMPUTE_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(api, "_COMPUTE_MAX_WAITERS", 4)

    assert api._compute_semaphore.acquire(blocking=False)  # never released in time
    try:
        started = time.monotonic()
        with pytest.raises(api.CacheBackpressureError):
            api.get_cached_or_fetch("never-free", _slow_fetch(0.01, "v"))
        elapsed = time.monotonic() - started
    finally:
        api._compute_semaphore.release()

    assert elapsed >= 0.3, "it must actually wait the budget before giving up"
    assert elapsed < 5.0


def test_the_waiter_cap_still_fails_fast(monkeypatch):
    """A pathological burst must not park unbounded worker threads."""
    monkeypatch.setattr(api, "_compute_semaphore", threading.BoundedSemaphore(1))
    monkeypatch.setattr(api, "_COMPUTE_WAIT_SECONDS", 30.0)
    monkeypatch.setattr(api, "_COMPUTE_MAX_WAITERS", 0)

    assert api._compute_semaphore.acquire(blocking=False)
    try:
        with pytest.raises(api.CacheBackpressureError):
            api.get_cached_or_fetch("burst", _slow_fetch(0.01, "v"))
    finally:
        api._compute_semaphore.release()


def test_the_waiter_count_returns_to_zero_after_a_timeout(monkeypatch):
    """Without the decrement the counter ratchets until every cold request is refused.

    The waiter-cap test cannot catch that: it sets the cap to 0, which returns before
    the counter is ever touched. Here the cap is 1, so a leaked increment would make
    the SECOND attempt bail out instantly instead of waiting its budget.
    """
    monkeypatch.setattr(api, "_compute_semaphore", threading.BoundedSemaphore(1))
    monkeypatch.setattr(api, "_COMPUTE_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(api, "_COMPUTE_MAX_WAITERS", 1)
    assert api._compute_waiters == 0

    assert api._compute_semaphore.acquire(blocking=False)  # never freed
    try:
        for attempt in range(3):
            started = time.monotonic()
            with pytest.raises(api.CacheBackpressureError):
                api.get_cached_or_fetch(f"ratchet-{attempt}", _slow_fetch(0.01, "v"))
            assert time.monotonic() - started >= 0.2, (
                f"attempt {attempt} was refused without waiting: "
                "the waiter count did not return to zero"
            )
    finally:
        api._compute_semaphore.release()
    assert api._compute_waiters == 0


def test_a_background_stale_refresh_never_waits(monkeypatch):
    """The opportunistic refresh already has a value to serve, so it must not park."""
    monkeypatch.setattr(api, "_compute_semaphore", threading.BoundedSemaphore(1))
    monkeypatch.setattr(api, "_COMPUTE_WAIT_SECONDS", 30.0)
    assert api._compute_semaphore.acquire(blocking=False)
    try:
        started = time.monotonic()
        assert api._acquire_compute_slot(wait=False) is False
        assert time.monotonic() - started < 1.0
    finally:
        api._compute_semaphore.release()


def test_the_default_concurrency_scales_with_cores_and_is_bounded(monkeypatch):
    for cores, expected in [(1, 2), (2, 2), (4, 2), (8, 4), (16, 8), (24, 8), (128, 8)]:
        monkeypatch.setattr(api, "_available_cpus", lambda cores=cores: cores)
        assert api._default_compute_concurrency() == expected, cores


def test_available_cpus_prefers_the_affinity_mask_over_the_host_count(monkeypatch):
    """os.cpu_count() reports the whole host — wrong for a pinned process."""
    monkeypatch.setattr(api.os, "process_cpu_count", None, raising=False)
    monkeypatch.setattr(
        api.os, "sched_getaffinity", lambda _pid: set(range(4)), raising=False
    )
    monkeypatch.setattr(api.os, "cpu_count", lambda: 64)
    monkeypatch.setattr(api, "_cgroup_cpu_quota", lambda: None)
    assert api._available_cpus() == 4


def test_available_cpus_falls_back_when_there_is_no_affinity_mask(monkeypatch):
    """os.sched_getaffinity is Linux-only; macOS and Windows must still get a count.

    The other tests inject the symbol with raising=False, so without this the
    AttributeError fallback in _available_cpus would only ever run on the platforms
    where the tests are least likely to be looked at.
    """
    monkeypatch.setattr(api.os, "process_cpu_count", None, raising=False)
    monkeypatch.delattr(api.os, "sched_getaffinity", raising=False)
    monkeypatch.setattr(api.os, "cpu_count", lambda: 12)
    monkeypatch.setattr(api, "_cgroup_cpu_quota", lambda: None)
    assert api._available_cpus() == 12


def test_a_cgroup_quota_caps_the_cpu_count(monkeypatch):
    """The docstring's own case: a 2-CPU container on a big host.

    A quota is not an affinity mask, so without this the container would report the
    host's cores and take 8 concurrent parses.
    """
    monkeypatch.setattr(api.os, "process_cpu_count", None, raising=False)
    monkeypatch.setattr(
        api.os, "sched_getaffinity", lambda _pid: set(range(64)), raising=False
    )
    monkeypatch.setattr(api.os, "cpu_count", lambda: 64)
    monkeypatch.setattr(api, "_cgroup_cpu_quota", lambda: 2)
    assert api._available_cpus() == 2


def test_a_quota_lowers_the_resulting_concurrency(monkeypatch):
    """The quota must change the ANSWER, not just the CPU count.

    Asserting concurrency == 2 under a 2-CPU quota proves nothing: the max(2, ...)
    floor returns 2 for any count <= 4 whether the quota was respected or ignored.
    An 8-CPU quota giving 4 IS distinguishable from the 8 an unquota'd host returns.
    """
    monkeypatch.setattr(api.os, "process_cpu_count", None, raising=False)
    monkeypatch.setattr(
        api.os, "sched_getaffinity", lambda _pid: set(range(64)), raising=False
    )
    monkeypatch.setattr(api.os, "cpu_count", lambda: 64)

    monkeypatch.setattr(api, "_cgroup_cpu_quota", lambda: None)
    assert api._default_compute_concurrency() == 8, "unquota'd 64-core host"

    monkeypatch.setattr(api, "_cgroup_cpu_quota", lambda: 8)
    assert api._default_compute_concurrency() == 4, "the quota must lower it"


def _cgroup_tree(tmp_path, root_cpu_max: str, leaf_cpu_max: str | None = None):
    """A fake cgroup v2 root, with the process in /system.slice/tokdash.service."""
    root = tmp_path / "cgroup"
    leaf = root / "system.slice" / "tokdash.service"
    leaf.mkdir(parents=True)
    (root / "cpu.max").write_text(root_cpu_max, encoding="utf-8")
    if leaf_cpu_max is not None:
        (leaf / "cpu.max").write_text(leaf_cpu_max, encoding="utf-8")
    proc = tmp_path / "self-cgroup"
    proc.write_text("0::/system.slice/tokdash.service\n", encoding="utf-8")
    return root, proc


def test_a_quota_on_the_processes_own_subcgroup_is_found(tmp_path):
    """The systemd CPUQuota= case: the root says "max", the unit's own cgroup caps it.

    `tokdash setup` installs a systemd unit, so this is a real deployment shape, not a
    hypothetical — reading only the root would miss it entirely.
    """
    root, proc = _cgroup_tree(tmp_path, "max 100000\n", "400000 100000\n")
    assert api._cgroup_cpu_quota(root, proc) == 4


def test_the_most_restrictive_cgroup_level_wins(tmp_path):
    root, proc = _cgroup_tree(tmp_path, "800000 100000\n", "200000 100000\n")
    assert api._cgroup_cpu_quota(root, proc) == 2


def test_a_namespaced_container_quota_at_the_root_is_found(tmp_path):
    """docker --cpus=2: the cgroup is namespaced, so the root carries the limit."""
    root, proc = _cgroup_tree(tmp_path, "200000 100000\n")
    assert api._cgroup_cpu_quota(root, proc) == 2


def test_an_unlimited_cgroup_reports_no_quota(tmp_path):
    root, proc = _cgroup_tree(tmp_path, "max 100000\n", "max 100000\n")
    assert api._cgroup_cpu_quota(root, proc) is None


def test_a_missing_cgroup_tree_reports_no_quota(tmp_path):
    missing = tmp_path / "absent"
    assert api._cgroup_cpu_quota(missing, missing) is None


def test_a_v1_quota_on_the_processes_own_subcgroup_is_found(tmp_path):
    """v1 has the same sub-cgroup shape, so the walk must apply there too."""
    missing = tmp_path / "absent"
    base = tmp_path / "cpu"
    unit = base / "system.slice" / "tokdash.service"
    unit.mkdir(parents=True)
    (base / "cpu.cfs_quota_us").write_text("-1\n", encoding="utf-8")  # root unlimited
    (base / "cpu.cfs_period_us").write_text("100000\n", encoding="utf-8")
    (unit / "cpu.cfs_quota_us").write_text("300000\n", encoding="utf-8")
    (unit / "cpu.cfs_period_us").write_text("100000\n", encoding="utf-8")
    proc = tmp_path / "self-cgroup"
    proc.write_text("4:cpu,cpuacct:/system.slice/tokdash.service\n", encoding="utf-8")

    assert api._cgroup_cpu_quota(missing, proc, base) == 3


def test_the_legacy_v1_quota_is_still_read(tmp_path):
    missing = tmp_path / "absent"
    v1 = tmp_path / "v1"
    v1.mkdir()
    (v1 / "cpu.cfs_quota_us").write_text("400000\n", encoding="utf-8")
    (v1 / "cpu.cfs_period_us").write_text("100000\n", encoding="utf-8")
    assert api._cgroup_cpu_quota(missing, missing, v1) == 4

    (v1 / "cpu.cfs_quota_us").write_text("-1\n", encoding="utf-8")  # unlimited
    assert api._cgroup_cpu_quota(missing, missing, v1) is None


def test_the_waiter_allowance_is_derived_from_the_concurrency():
    """Computing + parked threads share one budget, so an override cannot starve
    AnyIO's pool — the failure the compute cap exists to prevent.

    Checked as a function of an explicit budget rather than against the imported
    constants: those depend on the ambient machine and on whatever TOKDASH_* the runner
    exports, so an assertion over them passes under a wrong formula too.
    """
    assert api._default_max_waiters(2, 32) == 30
    assert api._default_max_waiters(8, 32) == 24
    assert api._default_max_waiters(31, 32) == 1
    assert api._default_max_waiters(32, 32) == 0
    assert api._default_max_waiters(64, 32) == 0, "never negative"


def test_the_wait_budget_rejects_infinity_and_clamps(monkeypatch):
    monkeypatch.setenv("TOKDASH_COMPUTE_WAIT_SECONDS", "inf")
    assert api._bounded_float_env("TOKDASH_COMPUTE_WAIT_SECONDS", 15.0, maximum=120.0) == 15.0
    monkeypatch.setenv("TOKDASH_COMPUTE_WAIT_SECONDS", "nan")
    assert api._bounded_float_env("TOKDASH_COMPUTE_WAIT_SECONDS", 15.0, maximum=120.0) == 15.0
    monkeypatch.setenv("TOKDASH_COMPUTE_WAIT_SECONDS", "9999")
    assert api._bounded_float_env("TOKDASH_COMPUTE_WAIT_SECONDS", 15.0, maximum=120.0) == 120.0
    monkeypatch.setenv("TOKDASH_COMPUTE_WAIT_SECONDS", "2.5")
    assert api._bounded_float_env("TOKDASH_COMPUTE_WAIT_SECONDS", 15.0, maximum=120.0) == 2.5


def test_the_daily_warm_minute_honours_zero_and_rejects_out_of_range(monkeypatch):
    """Midnight-exactly is a legitimate setting, and a bad value must not wrap."""
    monkeypatch.setenv("TOKDASH_DAILY_WARM_MINUTE", "0")
    assert api._daily_warm_minute() == 0
    monkeypatch.setenv("TOKDASH_DAILY_WARM_MINUTE", "1445")
    assert api._daily_warm_minute() == 5, "1445 must not wrap to 00:05's neighbour"
    monkeypatch.setenv("TOKDASH_DAILY_WARM_MINUTE", "90")
    assert api._daily_warm_minute() == 90
    monkeypatch.setenv("TOKDASH_DAILY_WARM_MINUTE", "-1")
    assert api._daily_warm_minute() == 5


def test_an_explicit_env_override_still_wins(monkeypatch):
    monkeypatch.setenv("TOKDASH_COMPUTE_CONCURRENCY", "3")
    assert api._positive_int_env("TOKDASH_COMPUTE_CONCURRENCY", api._default_compute_concurrency()) == 3


# --- the scheduled warm -------------------------------------------------------

def test_the_daily_warm_fires_just_after_local_midnight():
    for now, expected in [
        ("2026-08-31 23:50", "2026-09-01 00:05"),
        ("2026-08-31 00:01", "2026-08-31 00:05"),
        ("2026-08-31 12:00", "2026-09-01 00:05"),
    ]:
        current = datetime.fromisoformat(now).astimezone()
        fires_at = current + timedelta(seconds=api._seconds_until_daily_warm(current))
        assert fires_at.strftime("%Y-%m-%d %H:%M") == expected


def test_the_daily_warm_never_returns_a_zero_delay():
    """A zero would spin the loop; the target is always pushed to the next day."""
    at_target = datetime.fromisoformat("2026-08-31 00:05").astimezone()
    assert api._seconds_until_daily_warm(at_target) > 1.0


def test_the_daily_warm_fills_yesterdays_keys_and_not_todays(monkeypatch):
    today = api._local_today()
    yesterday = (today - timedelta(days=1)).isoformat()

    monkeypatch.setattr(api, "compute_usage_with_comparison", lambda p, f, t: {"d": t})
    monkeypatch.setattr(api, "get_sessions_data", lambda tool, p, f, t, **kw: {"tool": tool})
    monkeypatch.setattr(api, "get_active_time_data", lambda p, f, t, **kw: {"d": t})

    api._warm_previous_day()

    for key, _ in api._day_warm_targets(yesterday):
        assert key in api._cache, f"yesterday's key was not warmed: {key}"
    # Today holds almost nothing at 00:05; warming it would put a near-empty
    # snapshot in front of the morning's first request.
    for key, _ in api._day_warm_targets(today.isoformat()):
        assert key not in api._cache


def test_a_request_after_the_daily_warm_is_served_from_cache(monkeypatch):
    """Pin the contract through the route, not by re-deriving keys with the helpers.

    Asserting the warmer's own key helpers match the warmer proves nothing; what
    matters is that a real Yesterday request lands on what was warmed, so a drift
    between warmer and route shows up here as a miss.
    """
    from fastapi.testclient import TestClient

    yesterday = (api._local_today() - timedelta(days=1)).isoformat()
    computes: list[tuple] = []

    def fake_usage(period, date_from, date_to):
        computes.append((date_from, date_to))
        return {"total_tokens": 1}

    monkeypatch.setattr(api, "compute_usage_with_comparison", fake_usage)
    monkeypatch.setattr(api, "get_sessions_data", lambda tool, p, f, t, **kw: {"tool": tool})
    monkeypatch.setattr(api, "get_active_time_data", lambda p, f, t, **kw: {"d": t})

    api._warm_previous_day()
    assert computes == [(yesterday, yesterday)]

    with TestClient(api.app) as client:
        body = client.get(f"/api/usage?date_from={yesterday}&date_to={yesterday}").json()

    assert body["response_cache"]["status"] == "hit", "the warm did not land on the route's key"
    assert computes == [(yesterday, yesterday)], "the request recomputed despite the warm"


def test_stats_is_warmed_second_behind_the_overview_usage_key(monkeypatch):
    """Composing by name must keep the dashboard's order; slicing let it drift."""
    order: list[str] = []
    monkeypatch.setattr(api, "_run_warmers", lambda warmers, **kw: order.extend(k for k, _ in warmers))
    monkeypatch.setattr(api, "compute_stats", lambda _year=None: {})

    api._warm_caches()

    today = api._local_today().isoformat()
    assert order[0] == api._usage_warm_target(today)[0]
    assert order[1] == api._window_cache_key("stats_None", None, None)
    assert order[2:-1] == [key for key, _ in api._session_warm_targets(today)]
    assert order[-1] == api._day_scoped_key(api.ACTIVITY_INSIGHTS_CACHE_KEY)


def test_the_daily_warm_join_is_shorter_than_the_startup_one():
    """A warm on a live server must not impose the startup join on a racing request.

    That request pays the join AND then up to _COMPUTE_WAIT_SECONDS for a slot, so the
    startup allowance on top would outlast most browser timeouts.
    """
    assert api.DAILY_WARM_JOIN_SECONDS < api.STARTUP_WARM_JOIN_SECONDS


def test_a_warm_carries_its_own_join_budget():
    event = api._begin_startup_warm("budget-key", 3.0)
    try:
        claimed = api._claim_startup_warm_wait("budget-key")
        assert claimed is not None
        assert claimed[0] is event
        assert claimed[1] == 3.0
    finally:
        api._finish_startup_warm("budget-key", event)
    assert api._claim_startup_warm_wait("budget-key") is None


def test_a_testclient_block_leaves_no_warm_thread_running():
    """conftest's no_background_warmers must hold: _lifespan has no shutdown side, so a
    thread started here would outlive the block with the test's patches long gone."""
    from fastapi.testclient import TestClient

    before = {t.name for t in threading.enumerate()}
    with TestClient(api.app) as client:
        client.get("/health")
    leaked = {t.name for t in threading.enumerate()} - before
    assert not {name for name in leaked if name.startswith("tokdash-")}, leaked
