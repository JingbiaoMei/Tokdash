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
    yield
    api._clear_cache()
    with api._cache_guard:
        api._key_locks.clear()


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


def test_force_refresh_returns_cached_value_under_same_key_contention():
    api._cache["k-force-stale"] = (datetime.now().timestamp(), "cached")
    calls = []
    started = threading.Event()
    release = threading.Event()

    def slow_fetch():
        calls.append(1)
        started.set()
        release.wait(timeout=5)
        return "fresh"

    refreshed: dict[str, str] = {}

    def refresher():
        refreshed["v"] = api.get_cached_or_fetch("k-force-stale", slow_fetch, force_refresh=True)

    rt = threading.Thread(target=refresher)
    rt.start()
    assert started.wait(timeout=5)

    assert api.get_cached_or_fetch("k-force-stale", slow_fetch, force_refresh=True) == "cached"

    release.set()
    rt.join(timeout=5)
    assert refreshed["v"] == "fresh"
    assert len(calls) == 1
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
    """A stale entry is returned immediately to readers while one thread refreshes."""
    api._cache["k2"] = (datetime.now().timestamp() - (api.CACHE_TTL + 10), "stale")
    calls = []
    started = threading.Event()
    release = threading.Event()

    def slow_fetch():
        calls.append(1)
        started.set()
        release.wait(timeout=5)
        return "fresh"

    refreshed: dict[str, str] = {}

    def refresher():
        refreshed["v"] = api.get_cached_or_fetch("k2", slow_fetch)

    rt = threading.Thread(target=refresher)
    rt.start()
    assert started.wait(timeout=5)

    # While the refresh is in flight, a reader gets the stale value without blocking
    # and without triggering a second compute.
    assert api.get_cached_or_fetch("k2", slow_fetch) == "stale"

    release.set()
    rt.join(timeout=5)
    assert refreshed["v"] == "fresh"
    assert len(calls) == 1
    assert api._cache["k2"][1] == "fresh"


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
    """Distinct cold keys over the cap fail fast instead of blocking workers."""
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
    assert state["peak"] == 2
    assert results == ["v"] * 2
    assert len(errors) == 4
    assert all(isinstance(e, api.CacheBackpressureError) for e in errors)


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
