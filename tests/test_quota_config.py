from __future__ import annotations

import json

import pytest

from tokdash.sources.quota import config


def test_quota_network_consent_defaults_off_and_round_trips(tmp_path):
    assert config.read_quota_config() == {"codex_api": False, "claude_api": False, "antigravity_api": False}

    updated = config.set_quota_consent({"codex_api": True, "claude_api": True})

    assert updated == {"codex_api": True, "claude_api": True, "antigravity_api": False}
    assert config.read_quota_config() == updated


def test_quota_kill_switch_disables_network_even_with_saved_consent(monkeypatch):
    config.set_quota_consent({"codex_api": True, "claude_api": True, "antigravity_api": True})

    monkeypatch.setenv("TOKDASH_QUOTA_POLL", "0")

    assert config.enabled_network_sources() == []
    assert config.network_enabled("codex_api") is False


def test_quota_config_preserves_unrelated_config_keys():
    config.set_quota_consent({"codex_api": True})

    path = config.config_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    data["update_check"] = True
    path.write_text(json.dumps(data), encoding="utf-8")

    config.set_quota_consent({"codex_api": False, "antigravity_api": True})

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["update_check"] is True
    assert saved["quota"] == {"codex_api": False, "claude_api": False, "antigravity_api": True}


def test_poll_interval_precedence_env_over_config_over_default(monkeypatch):
    # Default when nothing is set.
    assert config.effective_poll_interval() == (1800, "default")

    config.set_poll_interval_minutes(60)
    assert config.effective_poll_interval() == (3600, "config")
    assert config.read_poll_interval_minutes() == 60

    # Env override wins and is floored at 300 s.
    monkeypatch.setenv("TOKDASH_QUOTA_POLL_INTERVAL", "100")
    assert config.effective_poll_interval() == (300, "env")
    monkeypatch.setenv("TOKDASH_QUOTA_POLL_INTERVAL", "5400")
    assert config.effective_poll_interval() == (5400, "env")

    # Malformed env falls through to the config value.
    monkeypatch.setenv("TOKDASH_QUOTA_POLL_INTERVAL", "not-an-int")
    assert config.effective_poll_interval() == (3600, "config")


def test_set_poll_interval_rejects_out_of_range():

    with pytest.raises(ValueError):
        config.set_poll_interval_minutes(45)


def test_master_switch_defaults_on_and_round_trips():
    assert config.quota_config_enabled() is True
    assert config.quota_tracking_enabled() is True

    config.set_quota_enabled(False)
    assert config.quota_config_enabled() is False
    assert config.quota_tracking_enabled() is False

    config.set_quota_enabled(True)
    assert config.quota_tracking_enabled() is True


def test_master_switch_disables_network_sources():
    config.set_quota_consent({"codex_api": True, "claude_api": True})
    assert config.enabled_network_sources() == ["codex_api", "claude_api"]

    config.set_quota_enabled(False)
    assert config.enabled_network_sources() == []
    assert config.network_enabled("codex_api") is False


def test_kill_switch_overrides_config_enabled(monkeypatch):
    config.set_quota_enabled(True)
    monkeypatch.setenv("TOKDASH_QUOTA_POLL", "0")

    assert config.quota_config_enabled() is True  # config preference unchanged
    assert config.quota_tracking_enabled() is False  # kill switch wins


def test_settings_preserve_consent_and_unrelated_keys():
    config.set_quota_consent({"codex_api": True})
    config.set_poll_interval_minutes(120)
    config.set_quota_enabled(False)

    saved = config.read_quota_config()
    assert saved["codex_api"] is True
    assert config.read_poll_interval_minutes() == 120
    assert config.quota_config_enabled() is False


def test_set_quota_consent_preserves_enabled_and_interval():
    # Regression: set_quota_consent used to rebuild the quota block from the consent keys
    # alone, dropping the master switch and interval. Changing consent while tracking is
    # OFF must NOT silently re-enable it or reset the saved interval.
    config.set_quota_enabled(False)
    config.set_poll_interval_minutes(60)

    config.set_quota_consent({"codex_api": True})

    assert config.quota_config_enabled() is False
    assert config.read_poll_interval_minutes() == 60
    assert config.read_quota_config()["codex_api"] is True
