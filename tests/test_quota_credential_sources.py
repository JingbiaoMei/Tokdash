from __future__ import annotations

import json
import sqlite3

from tokdash.sources.quota import credential_sources


def test_discovers_minimax_and_kimi_from_opencode_without_exposing_values(monkeypatch, tmp_path):
    data_home = tmp_path / "data"
    config_home = tmp_path / "config"
    auth_path = data_home / "opencode" / "auth.json"
    config_path = config_home / "opencode" / "opencode.jsonc"
    auth_path.parent.mkdir(parents=True)
    config_path.parent.mkdir(parents=True)
    auth_path.write_text(
        json.dumps({
            "minimax": {"type": "api", "key": "mini-secret"},
            "kimi-code": {"type": "api", "key": "kimi-secret"},
        }),
        encoding="utf-8",
    )
    config_path.write_text(
        """
        {
          // Comments are valid in OpenCode config.
          "provider": {
            "minimax": {"options": {"baseURL": "https://api.minimaxi.com/v1"}},
            "kimi-code": {"options": {"baseURL": "https://api.kimi.com/coding/v1"}},
          },
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path / "missing-kimi-code"))
    monkeypatch.setenv("KIMI_SHARE_DIR", str(tmp_path / "missing-kimi"))
    monkeypatch.setenv("MMX_CONFIG_DIR", str(tmp_path / "missing-mmx"))
    monkeypatch.setenv("CC_SWITCH_CONFIG_DIR", str(tmp_path / "missing-switch"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "missing-claude"))

    candidates = credential_sources.discover_external_credentials()

    assert {(item.provider, item.region, item.token) for item in candidates} == {
        ("minimax", "cn", "mini-secret"),
        ("kimi", None, "kimi-secret"),
    }
    summary = credential_sources.discover_provider_sources()
    assert summary["minimax"] == ["opencode"]
    assert summary["kimi"] == ["opencode"]
    assert "secret" not in json.dumps(summary)


def test_opencode_file_reference_is_not_followed(monkeypatch, tmp_path):
    config_path = tmp_path / "opencode.json"
    secret_path = tmp_path / "secret.txt"
    secret_path.write_text("must-not-be-read", encoding="utf-8")
    config_path.write_text(
        json.dumps({
            "provider": {
                "minimax": {
                    "options": {
                        "baseURL": "https://api.minimax.io/v1",
                        "apiKey": f"{{file:{secret_path}}}",
                    }
                }
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "missing-data"))
    monkeypatch.setenv("CC_SWITCH_CONFIG_DIR", str(tmp_path / "missing-switch"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "missing-claude"))

    assert credential_sources.discover_external_credentials("minimax") == []


def test_discovers_cc_switch_provider_records_read_only(monkeypatch, tmp_path):
    root = tmp_path / "cc-switch"
    root.mkdir()
    db_path = root / "cc-switch.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE providers (id TEXT, name TEXT, app_type TEXT, settings_config TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO providers VALUES (?, ?, ?, ?)",
        (
            "mini",
            "MiniMax",
            "claude",
            json.dumps({
                "env": {
                    "ANTHROPIC_BASE_URL": "https://api.minimax.io/anthropic",
                    "ANTHROPIC_AUTH_TOKEN": "mini-secret",
                }
            }),
        ),
    )
    conn.execute(
        "INSERT INTO providers VALUES (?, ?, ?, ?)",
        (
            "kimi",
            "Kimi",
            "opencode",
            json.dumps({
                "options": {
                    "baseURL": "https://api.kimi.com/coding/v1",
                    "apiKey": "kimi-secret",
                }
            }),
        ),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("CC_SWITCH_CONFIG_DIR", str(root))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "missing-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "missing-config"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "missing-claude"))

    candidates = credential_sources.discover_external_credentials()

    assert {(item.provider, item.token, item.source) for item in candidates} == {
        ("minimax", "mini-secret", "cc_switch"),
        ("kimi", "kimi-secret", "cc_switch"),
    }


def test_discovers_active_claude_settings_without_reading_logs(monkeypatch, tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(
        json.dumps({
            "env": {
                "ANTHROPIC_BASE_URL": "https://api.minimaxi.com/anthropic",
                "ANTHROPIC_AUTH_TOKEN": "mini-secret",
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "missing-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "missing-config"))
    monkeypatch.setenv("CC_SWITCH_CONFIG_DIR", str(tmp_path / "missing-switch"))

    candidates = credential_sources.discover_external_credentials("minimax")

    assert len(candidates) == 1
    assert candidates[0].source == "claude_settings"
    assert candidates[0].region == "cn"


def test_discovers_auth_only_key_and_provider_block_without_baseurl(monkeypatch, tmp_path):
    # Mirrors Howard's real machine: the working keys live in auth.json only.
    #  - `kimi-for-coding`  : no opencode.json provider block at all
    #  - `minimax-cn-coding-plan`: a provider block exists but declares NO baseURL
    # The pre-fix loop iterated opencode.json's provider dict and required a baseURL
    # there, so both were missed. Discovery must classify them by provider id.
    data_home = tmp_path / "data"
    config_home = tmp_path / "config"
    auth_path = data_home / "opencode" / "auth.json"
    config_path = config_home / "opencode" / "opencode.json"
    auth_path.parent.mkdir(parents=True)
    config_path.parent.mkdir(parents=True)
    auth_path.write_text(
        json.dumps({
            "kimi-for-coding": {"type": "api", "key": "kimi-secret"},
            "minimax-cn-coding-plan": {"type": "api", "key": "mini-secret"},
        }),
        encoding="utf-8",
    )
    config_path.write_text(
        json.dumps({"provider": {"minimax-cn-coding-plan": {"models": {}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path / "missing-kimi-code"))
    monkeypatch.setenv("KIMI_SHARE_DIR", str(tmp_path / "missing-kimi"))
    monkeypatch.setenv("MMX_CONFIG_DIR", str(tmp_path / "missing-mmx"))
    monkeypatch.setenv("CC_SWITCH_CONFIG_DIR", str(tmp_path / "missing-switch"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "missing-claude"))

    candidates = credential_sources.discover_external_credentials()

    assert {(item.provider, item.region, item.token) for item in candidates} == {
        ("minimax", "cn", "mini-secret"),
        ("kimi", None, "kimi-secret"),
    }
    # The mainland-China host is inferred for the CN plan id.
    minimax = next(c for c in candidates if c.provider == "minimax")
    assert minimax.base_url == "https://api.minimaxi.com"
