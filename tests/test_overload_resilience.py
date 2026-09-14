"""Concurrency/resilience tests for the API response cache.

These cover the overload-hang fix: single-flight refresh, stale-while-revalidate,
clean handling of a failed compute, and the global heavy-compute semaphore that
keeps a burst of distinct cold keys from saturating the worker pool.
"""
import threading
import time
from datetime import datetime

import pytest
from fastapi import HTTPException

import tokdash.api as api


@pytest.fixture(autouse=True)
def _reset_cache():
    api._clear_cache()
    with api._cache_guard:
        api._key_locks.clear()
        api._inflight_fills.clear()
        api._force_refresh_join_keys.clear()
    yield
    api._clear_cache()
    with api._cache_guard:
        api._key_locks.clear()
        api._inflight_fills.clear()
        api._force_refresh_join_keys.clear()


def test_fresh_hit_returns_cached_without_recomputing():
    calls = []

    def fetch():
        calls.append(1)
        return "v1"

    assert api.get_cached_or_fetch("k", fetch) == "v1"
    assert api.get_cached_or_fetch("k", fetch) == "v1"  # served from cache
    assert len(calls) == 1


def test_force_refresh_recomputes_fresh_hit_and_updates_cache():
    calls = []

    def fetch():
        calls.append(1)
        return f"v{len(calls)}"

    assert api.get_cached_or_fetch("k-force", fetch) == "v1"
    assert api.get_cached_or_fetch("k-force", fetch, force_refresh=True) == "v2"
    assert api.get_cached_or_fetch("k-force", fetch) == "v2"
    assert len(calls) == 2


def test_force_refresh_joins_an_inflight_refresh_for_the_same_key():
    """The loser of the single-flight race waits for the winner's fresh result.

    Two forced refreshes for one key used to split into "recompute" and "serve the
    stale body at once" — the Refresh button's answer then claimed fresh work while
    showing the old numbers. The loser now joins the in-flight fill instead.
    """
    api._cache["k-force-stale"] = (datetime.now().timestamp(), "cached")
    calls = []
    started = threading.Event()
    release = threading.Event()

    def slow_fetch():
        calls.append(1)
        started.set()
        assert release.wait(timeout=5)
        return "fresh"

    refreshed: dict[str, object] = {}

    def refresher():
        refreshed["r"] = api.get_cached_or_fetch(
            "k-force-stale", slow_fetch, force_refresh=True, return_metadata=True
        )

    rt = threading.Thread(target=refresher)
    rt.start()
    assert started.wait(timeout=5)  # the winner holds the key lock and is computing

    joined: dict[str, object] = {}

    def joiner():
        joined["r"] = api.get_cached_or_fetch(
            "k-force-stale", slow_fetch, force_refresh=True, return_metadata=True
        )

    jt = threading.Thread(target=joiner)
    jt.start()
    # Grace period, like _race() elsewhere: nothing observable distinguishes
    # "parked on the fill" from "not started yet", so give the joiner a moment to
    # reach the wait before asserting it has NOT been answered with "cached".
    time.sleep(0.1)
    assert "r" not in joined, "the losing refresh was answered from the stale body"

    release.set()
    rt.join(timeout=5)
    jt.join(timeout=5)
    assert not rt.is_alive() and not jt.is_alive()
    assert refreshed["r"].value == "fresh"
    assert joined["r"].value == "fresh"
    # "recomputed", not "hit"/"stale": the dashboard labels the refresh as cached
    # whenever served_from_cache is true, so a joined result must not say so.
    assert joined["r"].status == "recomputed"
    assert joined["r"].served_from_cache is False
    assert len(calls) == 1  # still one compute for the key
    assert api._cache["k-force-stale"][1] == "fresh"


def test_cache_fetch_metadata_uses_shallow_copy_without_mutating_cached_dict():
    calls = []

    def fetch():
        calls.append(1)
        return {"value": len(calls)}

    first = api.get_cached_or_fetch("k-meta", fetch, return_metadata=True)
    second = api.get_cached_or_fetch("k-meta", fetch, return_metadata=True)

    assert first.value == {"value": 1}
    assert first.status == "recomputed"
    assert first.served_from_cache is False
    assert second.value == {"value": 1}
    assert second.status == "hit"
    assert second.served_from_cache is True
    assert second.age_seconds is not None
    assert "response_cache" not in api._cache["k-meta"][1]
    assert len(calls) == 1


def test_cold_same_key_waiters_fail_fast_instead_of_blocking_workers():
    """Same cold key -> one compute; concurrent waiters get backpressure."""
    calls = []
    started = threading.Event()
    release = threading.Event()

    def fetch():
        calls.append(1)
        started.set()
        release.wait(timeout=5)
        return "value"

    result: dict[str, str] = {}

    def first():
        result["v"] = api.get_cached_or_fetch("k1", fetch)

    t = threading.Thread(target=first)
    t.start()
    assert started.wait(timeout=5)  # the single in-flight compute has begun

    with pytest.raises(api.CacheBackpressureError):
        api.get_cached_or_fetch("k1", fetch)

    release.set()
    t.join(timeout=5)

    assert len(calls) == 1
    assert result["v"] == "value"
    assert api.get_cached_or_fetch("k1", fetch) == "value"


def test_stale_value_served_while_refresh_in_flight():
    """The first stale reader returns immediately while one daemon refreshes."""
    api._cache["k2"] = (datetime.now().timestamp() - (api.CACHE_TTL + 10), "stale")
    calls = []
    started = threading.Event()
    release = threading.Event()

    def slow_fetch():
        calls.append(1)
        started.set()
        release.wait(timeout=5)
        return "fresh"

    started_at = time.monotonic()
    assert api.get_cached_or_fetch("k2", slow_fetch) == "stale"
    assert time.monotonic() - started_at < 0.2
    assert started.wait(timeout=5)

    # Readers keep getting stale without blocking or triggering another compute.
    assert api.get_cached_or_fetch("k2", slow_fetch) == "stale"

    release.set()
    deadline = time.monotonic() + 5
    while api._cache["k2"][1] != "fresh" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(calls) == 1
    assert api._cache["k2"][1] == "fresh"


def _stale_entry(key: str, value: str) -> None:
    api._cache[key] = (datetime.now().timestamp() - (api.CACHE_TTL + 10), value)


def test_force_refresh_joins_a_background_stale_refresh_daemon():
    """The bug: page load schedules a refresh daemon, Refresh clicks, gets stale back.

    A plain read on a stale key hands the single-flight lock to a refresh daemon.
    A forced refresh arriving while the daemon holds that lock was answered with
    the same stale body it was clicked to replace; it now joins the daemon's fill.
    """
    _stale_entry("k-join", "stale")
    started = threading.Event()
    release = threading.Event()

    def slow_fetch():
        started.set()
        assert release.wait(timeout=5)
        return "fresh"

    assert api.get_cached_or_fetch("k-join", slow_fetch) == "stale"  # schedules the daemon
    assert started.wait(timeout=5)  # the daemon holds the key lock and is computing

    joined: dict[str, object] = {}

    def refresher():
        joined["r"] = api.get_cached_or_fetch(
            "k-join", slow_fetch, force_refresh=True, return_metadata=True
        )

    rt = threading.Thread(target=refresher)
    rt.start()
    time.sleep(0.1)  # grace period: the join must be parked, not answered already
    assert "r" not in joined, "the forced refresh was answered from the stale body"

    release.set()
    rt.join(timeout=5)
    assert not rt.is_alive()
    assert joined["r"].value == "fresh"
    assert joined["r"].status == "recomputed"
    assert joined["r"].served_from_cache is False
    assert api._cache["k-join"][1] == "fresh"


def test_force_refresh_join_accepts_the_fresh_value_read_as_hit():
    """A fill that stored but has not released its lock must not label fresh stale.

    Window: the daemon's _cache_set_if_epoch has run but it still holds the key
    lock when the forced refresh reads `hit` at entry. `hit` then IS the fresh
    entry, so a strictly-newer check can never pass; the fill record's stored
    flag is what lets the join report the value as recomputed.
    """
    key = "k-join-fresh-hit"
    lock, acquired = api._try_key_lock(key)  # simulate the stored-but-unreleased fill
    assert acquired
    # A real fill stores through _cache_set_if_epoch, which marks its record stored.
    assert api._cache_set_if_epoch(key, "fresh", api._cache_epoch_value())

    joined: dict[str, object] = {}

    def refresher():
        joined["r"] = api.get_cached_or_fetch(
            key, lambda: "unused", force_refresh=True, return_metadata=True
        )

    rt = threading.Thread(target=refresher)
    rt.start()
    time.sleep(0.1)  # grace period: the join must be parked on the fill
    assert "r" not in joined

    api._release_key_lock(key, lock)
    rt.join(timeout=5)
    assert not rt.is_alive()
    assert joined["r"].value == "fresh"
    assert joined["r"].status == "recomputed"
    assert joined["r"].served_from_cache is False


def test_force_refresh_join_serves_stale_when_the_inflight_fill_fails():
    """A join must never turn a failed fill into an error for the Refresh clicker."""
    _stale_entry("k-join-fail", "stale")
    started = threading.Event()
    release = threading.Event()

    def failing_fetch():
        started.set()
        assert release.wait(timeout=5)
        raise RuntimeError("boom")

    assert api.get_cached_or_fetch("k-join-fail", failing_fetch) == "stale"
    assert started.wait(timeout=5)

    joined: dict[str, object] = {}

    def refresher():
        joined["r"] = api.get_cached_or_fetch(
            "k-join-fail", failing_fetch, force_refresh=True, return_metadata=True
        )

    rt = threading.Thread(target=refresher)
    rt.start()
    # Grace period, as in the success test: the daemon still holds the key lock
    # until release.set(), so this gives the joiner time to park on the fill.
    time.sleep(0.1)
    assert "r" not in joined

    release.set()
    rt.join(timeout=5)
    assert not rt.is_alive()
    assert joined["r"].value == "stale"
    assert joined["r"].status == "stale"
    assert joined["r"].served_from_cache is True

    # The failed fill wedged nothing: the key is computable again at once.
    assert api.get_cached_or_fetch("k-join-fail", lambda: "recovered", force_refresh=True) == "recovered"


def test_force_refresh_join_never_reports_a_failed_fill_as_recomputed():
    """Fresh cache entry + failed winning fill: the join must not claim fresh work.

    Regression: a join that trusted cache freshness alone would find the
    pre-existing fresh entry still within TTL after the fill failed and return
    it with status "recomputed" — the Refresh button reporting success while
    showing the old numbers. Only a fill that actually stored may be joined.
    """
    key = "k-join-fresh-fail"
    api._cache[key] = (datetime.now().timestamp(), "old-but-fresh")
    started = threading.Event()
    release = threading.Event()

    def failing_fetch():
        started.set()
        assert release.wait(timeout=5)
        raise RuntimeError("boom")

    winner_exc: dict[str, BaseException] = {}

    def winner():
        try:
            api.get_cached_or_fetch(key, failing_fetch, force_refresh=True)
        except BaseException as e:  # noqa: BLE001 - capturing for assertion
            winner_exc["e"] = e

    wt = threading.Thread(target=winner)
    wt.start()
    assert started.wait(timeout=5)  # the winner holds the key lock and is computing

    joined: dict[str, object] = {}

    def joiner():
        joined["r"] = api.get_cached_or_fetch(
            key, failing_fetch, force_refresh=True, return_metadata=True
        )

    jt = threading.Thread(target=joiner)
    jt.start()
    time.sleep(0.1)  # grace period: the join must be parked on the fill
    assert "r" not in joined

    release.set()  # the winning fill now fails and stores nothing
    wt.join(timeout=5)
    jt.join(timeout=5)
    assert not wt.is_alive() and not jt.is_alive()
    assert isinstance(winner_exc.get("e"), RuntimeError)
    assert joined["r"].value == "old-but-fresh"
    assert joined["r"].status == "stale"
    assert joined["r"].served_from_cache is True


def test_force_refresh_join_times_out_and_falls_back_to_stale(monkeypatch):
    """A wedged fill must not park the Refresh request past the join budget."""
    monkeypatch.setattr(api, "_FORCE_REFRESH_JOIN_SECONDS", 0.1)
    _stale_entry("k-join-timeout", "stale")
    started = threading.Event()
    release = threading.Event()

    def slow_fetch():
        started.set()
        assert release.wait(timeout=5)
        return "fresh"

    assert api.get_cached_or_fetch("k-join-timeout", slow_fetch) == "stale"
    assert started.wait(timeout=5)  # daemon computing; the join will out-wait nothing

    started_at = time.monotonic()
    result = api.get_cached_or_fetch(
        "k-join-timeout", slow_fetch, force_refresh=True, return_metadata=True
    )
    elapsed = time.monotonic() - started_at

    assert result.value == "stale"
    assert result.status == "stale"
    assert elapsed >= 0.1, "the join returned without waiting its budget"
    assert elapsed < 2.0, "the join hung instead of falling back to stale"

    release.set()
    deadline = time.monotonic() + 5
    while api._cache["k-join-timeout"][1] != "fresh" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert api._cache["k-join-timeout"][1] == "fresh"


def test_force_refresh_join_allows_one_waiter_per_key():
    """A parked join holds an AnyIO worker, so excess joiners fall back at once.

    Second and later forced refreshes for a key whose fill already has a joiner
    are answered from the stale body immediately rather than parking more
    request workers on the same fill for up to the join budget.
    """
    _stale_entry("k-join-cap", "stale")
    started = threading.Event()
    release = threading.Event()

    def slow_fetch():
        started.set()
        assert release.wait(timeout=5)
        return "fresh"

    assert api.get_cached_or_fetch("k-join-cap", slow_fetch) == "stale"  # schedules the daemon
    assert started.wait(timeout=5)  # the daemon holds the key lock and is computing

    joined: dict[str, object] = {}

    def first_joiner():
        joined["r"] = api.get_cached_or_fetch(
            "k-join-cap", slow_fetch, force_refresh=True, return_metadata=True
        )

    jt = threading.Thread(target=first_joiner)
    jt.start()
    time.sleep(0.1)  # grace period: the first joiner has claimed the waiter slot

    started_at = time.monotonic()
    extra = api.get_cached_or_fetch(
        "k-join-cap", slow_fetch, force_refresh=True, return_metadata=True
    )
    assert time.monotonic() - started_at < 0.2, "an excess joiner parked on the fill"
    assert extra.value == "stale"
    assert extra.status == "stale"

    release.set()
    jt.join(timeout=5)
    assert not jt.is_alive()
    assert joined["r"].value == "fresh"
    assert joined["r"].status == "recomputed"
    assert api._force_refresh_join_keys == set()


def test_a_plain_stale_read_does_not_join_the_inflight_fill():
    """Only the forced refresh pays the join; stale-while-revalidate stays instant."""
    _stale_entry("k-no-join", "stale")
    started = threading.Event()
    release = threading.Event()

    def slow_fetch():
        started.set()
        assert release.wait(timeout=5)
        return "fresh"

    assert api.get_cached_or_fetch("k-no-join", slow_fetch) == "stale"  # schedules the daemon
    assert started.wait(timeout=5)

    started_at = time.monotonic()
    result = api.get_cached_or_fetch("k-no-join", slow_fetch, return_metadata=True)
    assert time.monotonic() - started_at < 0.2, "a plain read parked on the fill"
    assert result.value == "stale"
    assert result.status == "stale"

    release.set()
    deadline = time.monotonic() + 5
    while api._cache["k-no-join"][1] != "fresh" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert api._cache["k-no-join"][1] == "fresh"


def test_inflight_fill_records_are_drained_with_the_locks():
    """The join registry tracks fills, so it must be empty once no fill is running."""
    assert api.get_cached_or_fetch("k-reg-fg", lambda: "v") == "v"

    _stale_entry("k-reg-bg", "old")
    assert api.get_cached_or_fetch("k-reg-bg", lambda: "new") == "old"  # daemon fill
    deadline = time.monotonic() + 5
    while api._cache["k-reg-bg"][1] != "new" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert api._cache["k-reg-bg"][1] == "new"

    assert api._inflight_fills == {}


def test_response_cache_and_idle_key_locks_are_bounded(monkeypatch):
    monkeypatch.setattr(api, "CACHE_MAX_ENTRIES", 2)

    assert api.get_cached_or_fetch("first", lambda: 1) == 1
    assert api.get_cached_or_fetch("second", lambda: 2) == 2
    # Touch first so second is the LRU entry.
    assert api.get_cached_or_fetch("first", lambda: 99) == 1
    assert api.get_cached_or_fetch("third", lambda: 3) == 3

    assert list(api._cache) == ["first", "third"]
    assert len(api._key_locks) <= 2

    api._clear_cache()
    assert not api._cache
    assert not api._key_locks


def test_failed_compute_propagates_and_does_not_poison_cache():
    state = {"fail": True}

    def fetch():
        if state["fail"]:
            raise RuntimeError("boom")
        return "ok"

    with pytest.raises(RuntimeError):
        api.get_cached_or_fetch("k3", fetch)
    assert "k3" not in api._cache  # failure must not be cached

    state["fail"] = False
    assert api.get_cached_or_fetch("k3", fetch) == "ok"  # retry recomputes cleanly


def test_failed_fill_unlock_and_registry_cleanup_are_atomic():
    """A released failed-fill lock cannot be acquired while still registered."""
    released = threading.Event()
    finish_release = threading.Event()
    contender_done = threading.Event()

    class PausingReleaseLock:
        def __init__(self):
            self._lock = threading.Lock()
            self._lock.acquire()

        def acquire(self, blocking=True):
            return self._lock.acquire(blocking=blocking)

        def release(self):
            self._lock.release()
            released.set()
            assert finish_release.wait(timeout=5)

        def locked(self):
            return self._lock.locked()

    key = "failed-fill-release-race"
    lock = PausingReleaseLock()
    with api._cache_guard:
        api._key_locks[key] = lock

    releaser = threading.Thread(target=api._release_key_lock, args=(key, lock))
    contender_result = {}

    def contend():
        contender_result["lock"], contender_result["acquired"] = api._try_key_lock(key)
        contender_done.set()

    contender = threading.Thread(target=contend)
    releaser.start()
    assert released.wait(timeout=5)
    contender.start()
    try:
        # _release_key_lock still owns _cache_guard, so lookup cannot observe
        # the just-unlocked mutex before its failed-fill entry is removed.
        assert not contender_done.wait(timeout=0.2)
    finally:
        finish_release.set()

    releaser.join(timeout=5)
    contender.join(timeout=5)
    assert not releaser.is_alive()
    assert not contender.is_alive()
    assert contender_result["acquired"] is True
    assert contender_result["lock"] is not lock
    api._release_key_lock(key, contender_result["lock"])


def test_inflight_compute_after_cache_clear_does_not_repopulate_stale_value():
    """A pricing-db edit cache clear wins over an older in-flight compute."""
    started = threading.Event()
    release = threading.Event()

    def old_fetch():
        started.set()
        release.wait(timeout=5)
        return "old-price-result"

    result: dict[str, str] = {}

    def first():
        result["first"] = api.get_cached_or_fetch("k-clear", old_fetch)

    t = threading.Thread(target=first)
    t.start()
    assert started.wait(timeout=5)

    api._clear_cache()
    release.set()
    t.join(timeout=5)

    assert result["first"] == "old-price-result"
    assert "k-clear" not in api._cache

    calls = []

    def new_fetch():
        calls.append(1)
        return "new-price-result"

    assert api.get_cached_or_fetch("k-clear", new_fetch) == "new-price-result"
    assert api._cache["k-clear"][1] == "new-price-result"
    assert len(calls) == 1


def test_failed_inflight_compute_does_not_poison_later_retry():
    """A cold waiter fails fast; after the holder fails, a later request recomputes."""
    calls = []
    started = threading.Event()
    release = threading.Event()

    def failing_then_ok():
        n = len(calls)
        calls.append(1)
        if n == 0:
            started.set()
            release.wait(timeout=5)
            raise RuntimeError("boom")
        return "recovered"

    first_exc: dict[str, BaseException] = {}

    def first():
        try:
            api.get_cached_or_fetch("k4", failing_then_ok)
        except BaseException as e:  # noqa: BLE001 - capturing for assertion
            first_exc["e"] = e

    ft = threading.Thread(target=first)
    ft.start()
    assert started.wait(timeout=5)

    with pytest.raises(api.CacheBackpressureError):
        api.get_cached_or_fetch("k4", failing_then_ok)

    release.set()
    ft.join(timeout=5)

    assert isinstance(first_exc.get("e"), RuntimeError)  # holder's failure surfaced
    assert api.get_cached_or_fetch("k4", failing_then_ok) == "recovered"
    assert len(calls) == 2


def test_heavy_compute_semaphore_bounds_concurrency(monkeypatch):
    """Distinct cold keys over the cap queue for a slot; the cap is never exceeded.

    These used to fail fast, which was safe while a cold key almost always had a stale
    value to serve meanwhile. Once a closed date range has to be computed to be correct,
    the Sessions tab's one-request-per-tool fan-out is all cold keys at once, and
    refusing everything past the cap rejected most of the tab. What must still hold is
    the cap itself: waiting may not let a third heavy compute run.
    """
    monkeypatch.setattr(api, "_compute_semaphore", threading.BoundedSemaphore(2))
    counter_lock = threading.Lock()
    state = {"cur": 0, "peak": 0}
    release = threading.Event()

    def fetch():
        with counter_lock:
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
        release.wait(timeout=5)
        with counter_lock:
            state["cur"] -= 1
        return "v"

    results: list[str] = []
    errors: list[BaseException] = []

    def worker(i: int):
        try:
            results.append(api.get_cached_or_fetch(f"k-{i}", fetch))
        except BaseException as e:  # noqa: BLE001 - capturing for assertion
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    time.sleep(0.3)  # let as many as the cap allows enter fetch concurrently
    assert state["peak"] == 2  # the cap is reached but never exceeded
    release.set()
    for t in threads:
        t.join(timeout=5)
    assert state["peak"] == 2  # queueing must not raise the ceiling
    assert results == ["v"] * 6, "a cold fan-out must drain rather than be refused"
    assert errors == []


def test_positive_int_env_defaults_and_validation(monkeypatch):
    monkeypatch.delenv("TOKDASH_TEST_KNOB", raising=False)
    assert api._positive_int_env("TOKDASH_TEST_KNOB", 2) == 2
    monkeypatch.setenv("TOKDASH_TEST_KNOB", "")
    assert api._positive_int_env("TOKDASH_TEST_KNOB", 2) == 2
    monkeypatch.setenv("TOKDASH_TEST_KNOB", "bad")
    assert api._positive_int_env("TOKDASH_TEST_KNOB", 2) == 2
    monkeypatch.setenv("TOKDASH_TEST_KNOB", "0")
    assert api._positive_int_env("TOKDASH_TEST_KNOB", 2) == 2
    monkeypatch.setenv("TOKDASH_TEST_KNOB", "-1")
    assert api._positive_int_env("TOKDASH_TEST_KNOB", 2) == 2
    monkeypatch.setenv("TOKDASH_TEST_KNOB", "3")
    assert api._positive_int_env("TOKDASH_TEST_KNOB", 2) == 3


def test_default_cache_ttl_covers_dashboard_auto_refresh_interval():
    assert api.CACHE_TTL >= 5 * 60


def test_api_routes_return_503_when_cold_compute_cap_is_full(monkeypatch):
    monkeypatch.setattr(api, "_compute_semaphore", threading.BoundedSemaphore(0))

    with pytest.raises(HTTPException) as exc:
        api.get_usage()

    assert exc.value.status_code == 503
    assert "Too many cold requests" in exc.value.detail
