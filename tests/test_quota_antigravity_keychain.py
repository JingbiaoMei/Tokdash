from __future__ import annotations

import base64
import json
import subprocess

from tokdash.sources.quota import antigravity


def _oauth_blob(access_token: str = "ya29.token", email: str = "h@example.com") -> dict:
    return {
        "auth_method": "oauth",
        "token": {"access_token": access_token, "refresh_token": "secret-refresh"},
        "email": email,
    }


def _go_keyring_payload(blob: dict) -> str:
    raw = json.dumps(blob, separators=(",", ":")).encode("utf-8")
    return antigravity._GO_KEYRING_PREFIX + base64.b64encode(raw).decode("ascii")


def _isolate_files(monkeypatch, tmp_path):
    """No oauth token files — only the (mocked) Keychain remains."""
    empty = tmp_path / "gemini-empty"
    empty.mkdir()
    monkeypatch.setattr(antigravity.clientpaths, "antigravity_cli_dir", lambda: empty / "antigravity-cli")


def test_keychain_fallback_supplies_go_keyring_token(monkeypatch, tmp_path):
    _isolate_files(monkeypatch, tmp_path)
    monkeypatch.setattr(antigravity, "_is_macos", lambda: True)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout=_go_keyring_payload(_oauth_blob()) + "\n", stderr="")

    monkeypatch.setattr(antigravity.subprocess, "run", fake_run)

    token, meta = antigravity._read_token()

    assert token == "ya29.token"
    assert meta["path"] == antigravity._KEYCHAIN_LABEL
    assert meta["email"] == "h@example.com"
    assert "secret-refresh" not in json.dumps(meta)
    assert calls[0][:6] == [
        "security",
        "find-generic-password",
        "-s",
        antigravity.KEYCHAIN_SERVICE,
        "-a",
        antigravity.KEYCHAIN_ACCOUNT,
    ]
    assert "-w" in calls[0]


def test_keychain_plain_json_blob_is_accepted(monkeypatch, tmp_path):
    _isolate_files(monkeypatch, tmp_path)
    monkeypatch.setattr(antigravity, "_is_macos", lambda: True)
    monkeypatch.setattr(
        antigravity.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps(_oauth_blob("ya29.plain")) + "\n", stderr=""
        ),
    )

    token, meta = antigravity._read_token()
    assert token == "ya29.plain"
    assert meta["path"] == antigravity._KEYCHAIN_LABEL


def test_keychain_not_consulted_off_macos(monkeypatch, tmp_path):
    _isolate_files(monkeypatch, tmp_path)
    monkeypatch.setattr(antigravity, "_is_macos", lambda: False)

    def boom(cmd, **kwargs):
        raise AssertionError("security must not run off macOS")

    monkeypatch.setattr(antigravity.subprocess, "run", boom)

    token, meta = antigravity._read_token()
    assert token is None
    assert meta["error"] == "token_not_found"


def test_keychain_missing_degrades_to_unavailable(monkeypatch, tmp_path):
    _isolate_files(monkeypatch, tmp_path)
    monkeypatch.setattr(antigravity, "_is_macos", lambda: True)
    monkeypatch.setattr(
        antigravity.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 44, stdout="", stderr="could not be found"),
    )

    token, meta = antigravity._read_token()
    assert token is None
    assert meta["error"] == "token_not_found"


def test_oauth_file_beats_keychain(monkeypatch, tmp_path):
    ag_dir = tmp_path / "antigravity-cli"
    ag_dir.mkdir()
    (ag_dir / "antigravity-oauth-token").write_text(
        json.dumps(_oauth_blob("ya29.file")), encoding="utf-8"
    )
    monkeypatch.setattr(antigravity.clientpaths, "antigravity_cli_dir", lambda: ag_dir)
    monkeypatch.setattr(antigravity, "_is_macos", lambda: True)

    def boom(cmd, **kwargs):
        raise AssertionError("oauth token file must short-circuit the Keychain")

    monkeypatch.setattr(antigravity.subprocess, "run", boom)

    token, meta = antigravity._read_token()
    assert token == "ya29.file"
    assert meta["path"].endswith("antigravity-oauth-token")


def test_collect_uses_keychain_token_without_leaking_secrets(monkeypatch, tmp_path):
    _isolate_files(monkeypatch, tmp_path)
    monkeypatch.setattr(antigravity, "_is_macos", lambda: True)
    monkeypatch.setattr(
        antigravity.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(
            cmd, 0, stdout=_go_keyring_payload(_oauth_blob()) + "\n", stderr=""
        ),
    )
    authorizations: list[str] = []

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return json.dumps(self.payload).encode("utf-8")

    def opener(req, timeout=15):
        authorizations.append(req.get_header("Authorization") or req.headers.get("Authorization"))
        if req.full_url.endswith(":loadCodeAssist"):
            return FakeResponse({"cloudaicompanionProject": "project-1"})
        return FakeResponse(
            {
                "models": {
                    "gemini-3-pro": {
                        "name": "models/gemini-3-pro",
                        "displayName": "Gemini 3 Pro",
                        "quotaInfo": {"remainingFraction": 0.8, "resetTime": "2026-09-24T00:00:00Z"},
                    }
                }
            }
        )

    snapshots = antigravity.collect_antigravity_api_snapshots(opener=opener, now=1_790_237_091)

    assert authorizations == ["Bearer ya29.token", "Bearer ya29.token"]
    assert snapshots[0].status == "ok"
    assert snapshots[0].used_percent == 20.0
    raw = json.dumps(snapshots[0].raw)
    assert "secret-refresh" not in raw
    assert "ya29.token" not in raw


def test_parse_token_text_decodes_go_keyring_prefix():
    blob = _oauth_blob("ya29.direct")
    token, meta = antigravity._parse_token_text(_go_keyring_payload(blob), "unit")
    assert token == "ya29.direct"
    assert meta["email"] == "h@example.com"
