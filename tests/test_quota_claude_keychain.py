from __future__ import annotations

import json
import subprocess
import sys

import pytest

from tokdash.sources.quota import claude

_BLOB = {
    "claudeAiOauth": {
        "accessToken": "sk-ant-oat01-test",
        "expiresAt": 4_000_000_000_000,
        "subscriptionType": "max",
        "rateLimitTier": "default_claude_max_5x",
    }
}


def _isolate(monkeypatch, tmp_path):
    """No credentials file, no env token — only the (mocked) Keychain remains."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)


def test_keychain_fallback_supplies_token_and_plan(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(claude, "_is_macos", lambda: True)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(_BLOB) + "\n", stderr="")

    monkeypatch.setattr(claude.subprocess, "run", fake_run)

    token, meta = claude._read_credentials()

    assert token == "sk-ant-oat01-test"
    assert meta["credential_path"] == claude._KEYCHAIN_LABEL
    assert calls[0][:4] == ["security", "find-generic-password", "-s", claude.CLAUDE_KEYCHAIN_SERVICE]

    plan = claude.read_claude_plan()
    assert plan["status"] == "ok"
    assert plan["plan"] == "Max 5x"
    assert plan["credential_path"] == claude._KEYCHAIN_LABEL


def test_keychain_not_consulted_off_macos(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(claude, "_is_macos", lambda: False)

    def boom(cmd, **kwargs):
        raise AssertionError("security must not run off macOS")

    monkeypatch.setattr(claude.subprocess, "run", boom)

    token, meta = claude._read_credentials()
    assert token is None
    assert meta["error"] == "credentials_not_found"
    assert claude.read_claude_plan()["status"] == "unavailable"


def test_keychain_locked_or_missing_degrades_to_unavailable(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(claude, "_is_macos", lambda: True)
    # errSecItemNotFound / locked keychain: security exits non-zero with nothing on stdout.
    monkeypatch.setattr(
        claude.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 44, stdout="", stderr="could not be found"),
    )

    token, meta = claude._read_credentials()
    assert token is None
    assert meta["error"] == "credentials_not_found"


def test_env_token_beats_keychain(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-env")
    monkeypatch.setattr(claude, "_is_macos", lambda: True)

    def boom(cmd, **kwargs):
        raise AssertionError("explicit env token must short-circuit the Keychain")

    monkeypatch.setattr(claude.subprocess, "run", boom)

    token, meta = claude._read_credentials()
    assert token == "sk-ant-oat01-env"
    assert meta["credential_path"] == "CLAUDE_CODE_OAUTH_TOKEN"


def test_read_claude_plan_env_token_short_circuits_keychain(monkeypatch, tmp_path):
    # Same precedence as _read_credentials: with the env override set, read_claude_plan
    # must not touch the credentials file's siblings nor spawn the security subprocess
    # (which could stall up to its timeout or trigger a Keychain prompt on macOS).
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-env")
    monkeypatch.setattr(claude, "_is_macos", lambda: True)

    def boom(cmd, **kwargs):
        raise AssertionError("read_claude_plan must not call security when the env token is set")

    monkeypatch.setattr(claude.subprocess, "run", boom)

    plan = claude.read_claude_plan()
    assert plan["status"] == "ok"
    assert plan["credential_path"] == "CLAUDE_CODE_OAUTH_TOKEN"
    assert plan["plan"] is None  # the env var carries no plan metadata


def test_credentials_file_beats_keychain(monkeypatch, tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-file"}}), encoding="utf-8"
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(claude, "_is_macos", lambda: True)

    def boom(cmd, **kwargs):
        raise AssertionError("existing credentials file must short-circuit the Keychain")

    monkeypatch.setattr(claude.subprocess, "run", boom)

    token, _meta = claude._read_credentials()
    assert token == "sk-ant-oat01-file"


@pytest.mark.skipif(sys.platform != "darwin", reason="requires the macOS security(1) binary")
def test_keychain_read_end_to_end_with_real_security_binary(tmp_path):
    # Seeds a throwaway keychain with the Claude Code service name and reads it back
    # through the production helper — verifies the actual security(1) interaction
    # (arg handling, -w output shape, JSON parse) on the macOS CI runner.
    kc = str(tmp_path / "tokdash-test.keychain-db")
    subprocess.run(["security", "create-keychain", "-p", "tokdash-test", kc], check=True)
    try:
        subprocess.run(["security", "unlock-keychain", "-p", "tokdash-test", kc], check=True)
        subprocess.run(
            [
                "security",
                "add-generic-password",
                "-s",
                claude.CLAUDE_KEYCHAIN_SERVICE,
                "-a",
                "tokdash-ci",
                "-w",
                json.dumps(_BLOB),
                kc,
            ],
            check=True,
        )
        data = claude._read_keychain_credentials(keychain=kc)
    finally:
        subprocess.run(["security", "delete-keychain", kc], check=False)

    assert data is not None
    assert data["claudeAiOauth"]["accessToken"] == "sk-ant-oat01-test"
