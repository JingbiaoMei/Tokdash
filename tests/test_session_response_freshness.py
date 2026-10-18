"""Response caches must not mask changes detected by the session loaders."""
from __future__ import annotations

import pytest

from tokdash import api, sessions


@pytest.fixture(autouse=True)
def clean_cache(monkeypatch):
    # Exercise immediate invalidation independently of the inventory scan TTL.
    monkeypatch.setattr(api, "_SIG_TTL", 0)
    api._clear_cache()
    yield
    api._clear_cache()


@pytest.mark.parametrize("tool", sessions.SESSION_TOOLS)
def test_source_changes_invalidate_only_that_tools_responses(monkeypatch, tool):
    versions = {name: 0 for name in sessions.SESSION_TOOLS}
    monkeypatch.setattr(api, "session_source_signature", lambda name: (versions[name],))
    monkeypatch.setattr(api, "get_sessions_data", lambda name, *a, **kw: {"version": versions[name]})
    other_tool = "codex" if tool != "codex" else "hermes"
    other = api.get_sessions(other_tool)
    first = api.get_sessions(tool, date_from="2025-01-01", date_to="2025-01-02")
    assert api.get_sessions(tool, date_from="2025-01-01", date_to="2025-01-02") is first
    versions[tool] += 1
    updated = api.get_sessions(tool, date_from="2025-01-01", date_to="2025-01-02")
    assert updated == {"version": 1}
    assert updated is not first
    assert api.get_sessions(other_tool) is other
    assert api.get_sessions(tool, date_from="2025-01-01", date_to="2025-01-02") is updated


def test_codex_alias_and_refresh_use_the_same_freshness_rules(monkeypatch):
    signature = [0]
    calls = []
    monkeypatch.setattr(api, "session_source_signature", lambda tool: tuple(signature))

    def fetch(*args, **kwargs):
        calls.append(1)
        return {"compute": len(calls)}

    monkeypatch.setattr(api, "get_sessions_data", fetch)
    monkeypatch.setattr(api, "get_codex_sessions_data", fetch)
    first = api.get_sessions(" Codex ")
    assert api.get_codex_sessions() is first
    signature[0] += 1
    assert api.get_codex_sessions() == {"compute": 2}
    assert api.get_sessions("codex", refresh=True) == {"compute": 3}
    assert api.get_codex_sessions(refresh=True) == {"compute": 4}


def test_codex_log_append_creation_and_removal_change_signature(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions.clientpaths, "codex_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(sessions.clientpaths, "codex_archived_sessions_dir", lambda: tmp_path / "archive")
    monkeypatch.setattr(sessions, "_codex_state_signature", lambda: ())
    empty = sessions.session_source_signature("codex")
    log = tmp_path / "rollout.jsonl"
    log.write_text("{}\n")
    created = sessions.session_source_signature("codex")
    assert created != empty
    log.write_text('{}\n{"more":true}\n')
    assert sessions.session_source_signature("codex") != created
    log.unlink()
    assert sessions.session_source_signature("codex") == empty


def test_transient_discovery_failure_uses_loader_fallback_without_caching(monkeypatch):
    def rotated(*args):
        raise FileNotFoundError("rotated segment")

    monkeypatch.setattr(sessions, "qoder_cli_file_signatures", rotated)
    assert api.get_sessions("qoder_cli", "all")["sessions"] == []
    assert not api._cache

    monkeypatch.setattr(sessions, "qoder_cli_file_signatures", lambda roots: ())
    recovered = {"sessions": [{"session_id": "recovered"}]}
    monkeypatch.setattr(api, "get_sessions_data", lambda *args, **kwargs: recovered)
    assert api.get_sessions("qoder_cli", "all") is recovered
    assert api.get_sessions("qoder_cli", "all") is recovered
    assert len(api._cache) == 1


def test_signature_failure_does_not_abort_other_startup_warm_targets(monkeypatch):
    def signature(tool):
        if tool == "qoder_cli":
            raise FileNotFoundError("rotated segment")
        return ()

    monkeypatch.setattr(api, "session_source_signature", signature)
    targets = api._session_warm_targets("2026-10-18")
    assert len(targets) == len(sessions.SESSION_TOOLS)  # other tools + active time
    assert all(key is not None for key, _fetch in targets)
    assert not any(key.startswith("sessions_qoder_cli_") for key, _fetch in targets)


def test_expired_tree_inventory_refreshes_off_request_path(monkeypatch):
    monkeypatch.setattr(api, "_SIG_TTL", 5.0)
    version = [0]
    scans = []
    pending = []

    def scan(tool):
        scans.append(version[0])
        return (version[0],)

    def schedule(*args, **kwargs):
        pending.append(args)
        return True

    monkeypatch.setattr(api, "session_source_signature", scan)
    monkeypatch.setattr(api, "get_sessions_data", lambda *a, **kw: {"version": version[0]})
    monkeypatch.setattr(api, "_schedule_stale_refresh", schedule)
    first = api.get_sessions("codex")
    assert api.get_sessions("codex") is first
    assert scans == [0]
    inventory_key = "session_source_inventory_codex"
    stamp, inventory = api._cache[inventory_key]
    api._cache[inventory_key] = (stamp - 6, inventory)
    version[0] = 1
    try:
        assert api.get_sessions("codex") is first
        assert api.get_sessions("codex") is first
        assert scans == [0], "a warm request must not scan even at inventory expiry"
        assert len(pending) == 1, "concurrent hits must share one inventory refresh"
    finally:
        # Run the scheduled worker deterministically; no sleeps or thread leaks.
        api._refresh_stale_in_background(*pending.pop())
    updated = api.get_sessions("codex")
    assert updated == {"version": 1}
    assert api.get_sessions("codex") is updated
    assert scans == [0, 1]


def test_refresh_bypasses_fresh_tree_inventory(monkeypatch):
    monkeypatch.setattr(api, "_SIG_TTL", 5.0)
    version = [0]
    monkeypatch.setattr(api, "session_source_signature", lambda tool: (version[0],))
    monkeypatch.setattr(api, "get_sessions_data", lambda *a, **kw: {"version": version[0]})
    first = api.get_sessions("codex")
    version[0] = 1
    assert api.get_sessions("codex") is first
    assert api.get_sessions("codex", refresh=True) == {"version": 1}
    assert api.get_sessions("codex") == {"version": 1}


def test_failed_background_inventory_scan_keeps_last_good_value_and_retries(monkeypatch):
    monkeypatch.setattr(api, "_SIG_TTL", 5.0)
    unavailable = [False]
    pending = []

    def scan(tool):
        if unavailable[0]:
            raise FileNotFoundError("rotated segment")
        return (1,)

    def schedule(*args, **kwargs):
        pending.append(args)
        return True

    monkeypatch.setattr(api, "session_source_signature", scan)
    monkeypatch.setattr(api, "get_sessions_data", lambda *a, **kw: {"sessions": []})
    monkeypatch.setattr(api, "_schedule_stale_refresh", schedule)
    first = api.get_sessions("codex")
    key = "session_source_inventory_codex"
    stamp, inventory = api._cache[key]
    api._cache[key] = (stamp - 6, inventory)
    unavailable[0] = True
    assert api.get_sessions("codex") is first
    api._refresh_stale_in_background(*pending.pop())
    assert api._cache[key] == (stamp - 6, inventory)
    unavailable[0] = False
    assert api.get_sessions("codex") is first
    assert len(pending) == 1
    api._refresh_stale_in_background(*pending.pop())
    assert api._cache[key][0] > stamp


def test_busy_inventory_does_not_abort_other_warm_targets(monkeypatch):
    monkeypatch.setattr(api, "_SIG_TTL", 5.0)
    monkeypatch.setattr(api, "session_source_signature", lambda tool: ())
    original = api.get_cached_or_fetch

    def busy(key, *args, **kwargs):
        if key == "session_source_inventory_codex":
            raise api.CacheBackpressureError("inventory fill in progress")
        return original(key, *args, **kwargs)

    monkeypatch.setattr(api, "get_cached_or_fetch", busy)
    targets = api._session_warm_targets("2026-10-18")
    assert len(targets) == len(sessions.SESSION_TOOLS)
    assert not any(key.startswith("sessions_codex_") for key, _fetch in targets)
    assert not api._startup_warm_events


def test_foreground_request_can_join_startup_inventory_fill(monkeypatch):
    import threading

    monkeypatch.setattr(api, "_SIG_TTL", 5.0)
    entered = threading.Event()
    release = threading.Event()
    joined = []
    failures = []
    original_claim = api._claim_startup_warm_wait

    def scan(tool):
        if tool == "codex":
            entered.set()
            assert release.wait(timeout=5)
        return ()

    def claim(key):
        result = original_claim(key)
        if key == "session_source_inventory_codex" and result is not None:
            joined.append(key)
            release.set()
        return result

    def warm():
        try:
            api._session_warm_targets("2026-10-18")
        except Exception as exc:
            failures.append(exc)

    monkeypatch.setattr(api, "session_source_signature", scan)
    monkeypatch.setattr(api, "_claim_startup_warm_wait", claim)
    monkeypatch.setattr(api, "get_sessions_data", lambda *a, **kw: {"sessions": []})
    worker = threading.Thread(target=warm)
    worker.start()
    try:
        assert entered.wait(timeout=5)
        assert api.get_sessions("codex") == {"sessions": []}
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert not failures
    assert joined == ["session_source_inventory_codex"]
    assert not api._startup_warm_events


def test_overview_warms_before_inventory_discovery(monkeypatch):
    monkeypatch.setattr(api, "_SIG_TTL", 5.0)
    order = []
    monkeypatch.setattr(api, "compute_usage_with_comparison", lambda *a, **kw: order.append("usage") or {})
    monkeypatch.setattr(api, "compute_stats", lambda *a, **kw: order.append("stats") or {})
    monkeypatch.setattr(api, "session_source_signature", lambda tool: order.append("inventory") or ())
    monkeypatch.setattr(api, "get_sessions_data", lambda *a, **kw: {})
    monkeypatch.setattr(api, "_active_time_payload", lambda *a, **kw: {})
    monkeypatch.setattr(api, "get_codex_activity_insights", lambda *a, **kw: {})
    monkeypatch.setattr(api, "_report_warm_targets", lambda *a, **kw: [])
    api._warm_caches()
    assert order[:3] == ["usage", "stats", "inventory"]
