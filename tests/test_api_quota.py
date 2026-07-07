from __future__ import annotations

import pytest

import tokdash.api as api


def test_get_quota_returns_stored_codex_session_data_without_collecting(monkeypatch):
    from tokdash.sources.quota.types import QuotaSnapshot
    from tokdash.usage_store import UsageEntryStore

    api._clear_cache()
    monkeypatch.setattr(
        "tokdash.sources.quota.collect_local_snapshots",
        lambda: (_ for _ in ()).throw(AssertionError("local collector called")),
    )
    UsageEntryStore().insert_quota_snapshots(
        [
            QuotaSnapshot(
                "codex",
                "default",
                "5h",
                "5-hour window",
                50,
                1_782_909_000,
                "pro",
                1_782_907_200,
                "codex_session",
                "ok",
                {},
            )
        ]
    )

    payload = api.get_quota()

    assert payload["providers"]["codex"]["network_enabled"] is False
    assert payload["providers"]["codex"]["buckets"][0]["bucket"] == "5h"
    assert payload["providers"]["codex"]["buckets"][0]["used_percent"] == 50.0
    assert payload["consent"] == {"codex_api": False, "claude_api": False, "antigravity_api": False}


def test_get_quota_does_not_call_network_collectors(monkeypatch):
    api._clear_cache()
    monkeypatch.setattr("tokdash.sources.quota.collect_network_snapshots", lambda: (_ for _ in ()).throw(AssertionError("network called")))

    payload = api.get_quota()

    assert "providers" in payload


def test_quota_history_route_uses_stored_snapshots(tmp_path):
    from tokdash.sources.quota.types import QuotaSnapshot
    from tokdash.usage_store import UsageEntryStore

    store = UsageEntryStore()
    store.insert_quota_snapshots(
        [
            QuotaSnapshot("codex", "acct", "5h", "5-hour window", 10, None, "pro", 1_782_907_200, "codex_session", "ok", {}),
            QuotaSnapshot("codex", "acct", "5h", "5-hour window", 20, None, "pro", 1_782_910_800, "codex_session", "ok", {}),
        ]
    )

    history = api.get_quota_history(granularity="hour")

    assert history["series"][0]["provider"] == "codex"
    assert history["series"][0]["consumption"] == [{"period_start": 1_782_910_800, "consumed_percent": 10.0}]


def test_quota_consent_route_persists_provider_flags():
    api._clear_cache()
    payload = api.set_quota_consent({"codex_api": True, "claude_api": False})

    assert payload["consent"] == {"codex_api": True, "claude_api": False, "antigravity_api": False}
    assert api.get_quota()["consent"]["codex_api"] is True


def test_quota_refresh_collects_and_stores_network_snapshots(monkeypatch):
    from tokdash.sources.quota.types import QuotaSnapshot

    snapshot = QuotaSnapshot(
        "claude",
        "default",
        "session",
        "Session",
        12.5,
        None,
        "max",
        1_782_907_200,
        "claude_api",
        "ok",
        {"fixture": True},
    )
    monkeypatch.setattr(api, "_try_begin_quota_refresh", lambda: 0.0)
    monkeypatch.setattr("tokdash.sources.quota.collect_enabled_snapshots", lambda include_network=True, store=None: [snapshot])

    payload = api.refresh_quota()

    assert payload["inserted"] == 1
    assert api.get_quota_history(providers="claude")["series"][0]["bucket"] == "session"


def test_quota_refresh_enforces_cooldown(monkeypatch):
    monkeypatch.setattr(api, "_try_begin_quota_refresh", lambda: 42.0)

    with pytest.raises(api.HTTPException) as exc:
        api.refresh_quota()

    assert exc.value.status_code == 429
    assert "42" in exc.value.detail


def test_try_begin_quota_refresh_reserves_atomically(monkeypatch):
    # First caller reserves the slot (remaining == 0); an immediate second caller is blocked
    # (remaining > 0). The check-and-reserve happen in one critical section, so two racing
    # refreshes can never both proceed.
    monkeypatch.setattr(api.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(api, "_quota_last_refresh_monotonic", 0.0)
    assert api._try_begin_quota_refresh() == 0.0
    assert api._try_begin_quota_refresh() > 0


def test_quota_refresh_failure_releases_cooldown(monkeypatch):
    # A refresh that errors after reserving the slot must roll the reservation back —
    # otherwise a 500 locks the user out for the full cooldown with nothing to show for it.
    monkeypatch.setattr(api.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(api, "_quota_last_refresh_monotonic", 0.0)
    monkeypatch.setattr(api, "_quota_prev_refresh_monotonic", 0.0)
    monkeypatch.setattr(
        "tokdash.sources.quota.collect_enabled_snapshots",
        lambda include_network=True, store=None: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with pytest.raises(RuntimeError):
        api.refresh_quota()

    # The slot is free again: the next attempt reserves instead of hitting the cooldown.
    assert api._try_begin_quota_refresh() == 0.0


def test_get_quota_exposes_codex_reset_credit_inventory():
    from tokdash.sources.quota.types import QuotaSnapshot
    from tokdash.usage_store import UsageEntryStore

    api._clear_cache()
    UsageEntryStore().insert_quota_snapshots(
        [
            QuotaSnapshot(
                "codex",
                "acct",
                "reset_credits",
                "Reset credits",
                2,
                None,
                "pro",
                1_782_907_200,
                "codex_api",
                "ok",
                {
                    "reset_credits": {
                        "available_count": 2,
                        "credits": [{"id": "credit-a", "expires_at": "2026-07-04T00:00:00Z"}],
                    }
                },
            )
        ]
    )

    payload = api.get_quota()

    assert payload["providers"]["codex"]["reset_credits"] == {
        "available_count": 2,
        "credits": [{"id": "credit-a", "expires_at": "2026-07-04T00:00:00Z"}],
    }


def test_quota_refresh_keeps_current_state_when_usage_db_disabled(monkeypatch):
    from tokdash.sources.quota.types import QuotaSnapshot

    snapshot = QuotaSnapshot(
        "antigravity",
        "default",
        "models/gemini-3-pro",
        "Gemini 3 Pro",
        80,
        None,
        None,
        1_782_907_200,
        "antigravity_api",
        "ok",
        {},
    )
    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")
    monkeypatch.setattr(api, "_try_begin_quota_refresh", lambda: 0.0)
    monkeypatch.setattr("tokdash.sources.quota.collect_enabled_snapshots", lambda include_network=True, store=None: [snapshot])

    payload = api.refresh_quota()
    state = api.get_quota()

    assert payload["inserted"] == 0
    assert state["providers"]["antigravity"]["buckets"][0]["bucket_label"] == "Gemini 3 Pro"


def test_get_quota_tolerates_malformed_poll_interval(monkeypatch):
    api._clear_cache()
    monkeypatch.setenv("TOKDASH_QUOTA_POLL_INTERVAL", "not-an-int")

    payload = api.get_quota()

    # Malformed env override falls through to the default (30 min) and reports its source.
    assert payload["poll"]["interval"] == 1800
    assert payload["poll"]["interval_source"] == "default"


def test_get_quota_marks_network_enabled_and_last_run_for_api_rows():
    from tokdash.sources.quota.types import QuotaSnapshot
    from tokdash.usage_store import UsageEntryStore

    api._clear_cache()
    api.set_quota_consent({"codex_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            QuotaSnapshot(
                "codex",
                "acct",
                "5h",
                "5-hour window",
                20,
                None,
                "pro",
                1_782_907_200,
                "codex_api",
                "ok",
                {},
            )
        ]
    )

    payload = api.get_quota()

    assert payload["providers"]["codex"]["network_enabled"] is True
    assert payload["poll"]["last_run"] == 1_782_907_200


def test_get_quota_prefers_freshest_bucket_across_accounts():
    from tokdash.sources.quota.types import QuotaSnapshot
    from tokdash.usage_store import UsageEntryStore

    api._clear_cache()
    UsageEntryStore().insert_quota_snapshots(
        [
            QuotaSnapshot(
                "codex",
                "default",
                "5h",
                "5-hour window",
                50,
                None,
                "pro",
                1_782_907_200,
                "codex_session",
                "ok",
                {},
            ),
            QuotaSnapshot(
                "codex",
                "acct_real",
                "5h",
                "5-hour window",
                20,
                None,
                "pro",
                1_782_910_800,
                "codex_api",
                "ok",
                {},
            ),
            QuotaSnapshot(
                "codex",
                "default",
                "7d",
                "7-day window",
                40,
                None,
                "pro",
                1_782_907_200,
                "codex_session",
                "ok",
                {},
            ),
        ]
    )

    buckets = api.get_quota()["providers"]["codex"]["buckets"]

    assert [(bucket["bucket"], bucket["account"], bucket["used_percent"]) for bucket in buckets] == [
        ("5h", "acct_real", 20.0),
        ("7d", "default", 40.0),
    ]


def test_quota_settings_route_sets_enabled_and_interval():
    api._clear_cache()
    payload = api.set_quota_settings({"enabled": False, "poll_interval_minutes": 60})

    assert payload["enabled"] is False
    assert payload["poll_interval_minutes"] == 60
    assert payload["interval"] == 3600

    state = api.get_quota()
    assert state["enabled"] is False
    assert state["poll"]["interval"] == 3600
    assert state["poll"]["interval_source"] == "config"


def test_quota_settings_route_rejects_bad_interval():
    with pytest.raises(api.HTTPException) as exc:
        api.set_quota_settings({"poll_interval_minutes": 45})

    assert exc.value.status_code == 400


def test_quota_refresh_rejected_when_disabled():
    api._clear_cache()
    api.set_quota_settings({"enabled": False})

    with pytest.raises(api.HTTPException) as exc:
        api.refresh_quota()

    assert exc.value.status_code == 409
    assert "disabled" in exc.value.detail.lower()


def test_get_quota_reports_master_switch_and_poll_source():
    api._clear_cache()
    payload = api.get_quota()

    assert payload["enabled"] is True
    assert payload["poll"]["interval"] == 1800
    assert payload["poll"]["interval_source"] == "default"
    assert payload["poll"]["network_enabled"] is False


def test_quota_history_route_bounds_series_to_max_points():
    from tokdash.sources.quota.types import QuotaSnapshot
    from tokdash.usage_store import UsageEntryStore

    UsageEntryStore().insert_quota_snapshots(
        [
            QuotaSnapshot(
                "codex",
                "acct",
                "5h",
                "5-hour window",
                float(i),
                None,
                "pro",
                1_782_907_200 + i * 60,
                "codex_session",
                "ok",
                {},
            )
            for i in range(10)
        ]
    )

    history = api.get_quota_history(max_points=2)

    for series in history["series"]:
        assert len(series["points"]) <= 2


def test_get_quota_exposes_status_detail_for_freshest_api_status_row():
    from tokdash.sources.quota.types import QuotaSnapshot
    from tokdash.usage_store import UsageEntryStore

    api._clear_cache()
    UsageEntryStore().insert_quota_snapshots(
        [
            QuotaSnapshot(
                "antigravity",
                "default",
                "api",
                "Antigravity API",
                None,
                None,
                None,
                1_782_907_200,
                "antigravity_api",
                "stale_token",
                {},
            )
        ]
    )

    payload = api.get_quota()

    assert payload["providers"]["antigravity"]["status_detail"] == "stale_token"
    assert payload["providers"]["antigravity"]["buckets"] == []
    assert payload["providers"]["claude"]["status_detail"] is None


def test_poll_quota_idles_entirely_when_disabled(monkeypatch):
    import tokdash.sources.quota as quota

    quota.config.set_quota_enabled(False)

    def boom(*_args, **_kwargs):
        raise AssertionError("collector invoked while quota tracking is disabled")

    monkeypatch.setattr(quota, "collect_local_snapshots", boom)
    monkeypatch.setattr(quota, "collect_network_snapshots", boom)

    result = quota.poll_quota()

    assert result["disabled"] is True
    assert result["inserted"] == 0
    assert result["snapshots"] == 0


def test_stale_token_banner_clears_after_successful_api_poll():
    """A failure status row (bucket "api") is only written on failure, so it stays the
    newest "api" row forever after recovery — the banner must yield to a newer ok row."""
    from tokdash.sources.quota.types import QuotaSnapshot
    from tokdash.usage_store import UsageEntryStore

    api._clear_cache()
    store = UsageEntryStore()
    store.insert_quota_snapshots(
        [
            QuotaSnapshot(
                "codex", "acct", "api", "Codex API", None, None, None,
                1_782_900_000, "codex_api", "stale_token", {"error": "token_expired"},
            ),
            QuotaSnapshot(
                "codex", "acct", "5h", "5-hour window", 42.0, None, "pro",
                1_782_907_200, "codex_api", "ok", {},
            ),
        ]
    )

    payload = api.get_quota()
    codex = payload["providers"]["codex"]
    assert codex["status_detail"] is None
    assert codex["status"] == "ok"


def test_get_quota_exposes_remaining_percent_alongside_used_percent():
    from tokdash.sources.quota.types import QuotaSnapshot
    from tokdash.usage_store import UsageEntryStore

    api._clear_cache()
    UsageEntryStore().insert_quota_snapshots(
        [
            QuotaSnapshot(
                "codex", "acct", "5h", "5-hour window", 99.0, None, "pro",
                1_782_907_200, "codex_api", "ok", {},
            )
        ]
    )

    bucket = api.get_quota()["providers"]["codex"]["buckets"][0]

    assert bucket["used_percent"] == 99.0
    assert bucket["remaining_percent"] == 1.0


def test_stale_token_banner_shows_when_failure_is_newest():
    from tokdash.sources.quota.types import QuotaSnapshot
    from tokdash.usage_store import UsageEntryStore

    api._clear_cache()
    UsageEntryStore().insert_quota_snapshots(
        [
            QuotaSnapshot(
                "codex", "acct", "5h", "5-hour window", 42.0, None, "pro",
                1_782_900_000, "codex_api", "ok", {},
            ),
            QuotaSnapshot(
                "codex", "acct", "api", "Codex API", None, None, None,
                1_782_907_200, "codex_api", "stale_token", {"error": "token_expired"},
            ),
        ]
    )

    payload = api.get_quota()
    assert payload["providers"]["codex"]["status_detail"] == "stale_token"


def test_codex_plan_label_normalized_in_state_only():
    """Card label shows "Pro Lite"; stored rows keep the raw plan_type."""
    from tokdash.sources.quota.types import QuotaSnapshot
    from tokdash.usage_store import UsageEntryStore

    api._clear_cache()
    store = UsageEntryStore()
    store.insert_quota_snapshots(
        [
            QuotaSnapshot(
                "codex", "acct", "5h", "5-hour window", 40.0, None, "prolite",
                1_782_907_200, "codex_session", "ok", {},
            )
        ]
    )

    payload = api.get_quota()
    assert payload["providers"]["codex"]["plan"] == "Pro Lite"
    assert store.latest_quota_snapshots()[0]["plan"] == "prolite"


def test_get_quota_api_enabled_session_does_not_override_bucket():
    """codex_api enabled: a newer codex_session row must not win the 7d bucket over an
    older codex_api row — the API is the sole oracle once enabled."""
    from tokdash.sources.quota.types import QuotaSnapshot
    from tokdash.usage_store import UsageEntryStore

    api._clear_cache()
    api.set_quota_consent({"codex_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            QuotaSnapshot(
                "codex", "acct", "7d", "7-day window", 70, 1_783_000_000, "pro",
                1_782_907_200, "codex_api", "ok", {},
            ),
            QuotaSnapshot(
                "codex", "default", "7d", "7-day window", 40, 1_783_100_000, "pro",
                1_782_907_260, "codex_session", "ok", {},
            ),
        ]
    )

    buckets = api.get_quota()["providers"]["codex"]["buckets"]

    assert len(buckets) == 1
    bucket = buckets[0]
    assert bucket["bucket"] == "7d"
    assert bucket["source"] == "codex_api"
    assert bucket["used_percent"] == 70.0
    assert bucket["resets_at"] == 1_783_000_000


def test_get_quota_api_enabled_session_only_omits_bucket():
    """codex_api enabled but only session rows exist for a bucket: the bucket is omitted
    rather than falling back to stale session data (accepted empty-card behavior)."""
    from tokdash.sources.quota.types import QuotaSnapshot
    from tokdash.usage_store import UsageEntryStore

    api._clear_cache()
    api.set_quota_consent({"codex_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            QuotaSnapshot(
                "codex", "default", "7d", "7-day window", 40, 1_783_100_000, "pro",
                1_782_907_200, "codex_session", "ok", {},
            ),
        ]
    )

    payload = api.get_quota()
    codex = payload["providers"]["codex"]

    assert [b["bucket"] for b in codex["buckets"]] == []
    assert codex["estimated"] is False


def test_get_quota_api_disabled_shows_session_and_marks_estimated():
    """codex_api disabled: the session row is shown as the bucket, and the codex card is
    marked estimated."""
    from tokdash.sources.quota.types import QuotaSnapshot
    from tokdash.usage_store import UsageEntryStore

    api._clear_cache()
    api.set_quota_consent({"codex_api": False})
    UsageEntryStore().insert_quota_snapshots(
        [
            QuotaSnapshot(
                "codex", "default", "7d", "7-day window", 40, 1_783_100_000, "pro",
                1_782_907_200, "codex_session", "ok", {},
            ),
        ]
    )

    payload = api.get_quota()
    codex = payload["providers"]["codex"]

    assert [b["bucket"] for b in codex["buckets"]] == ["7d"]
    assert codex["buckets"][0]["source"] == "codex_session"
    assert codex["estimated"] is True
