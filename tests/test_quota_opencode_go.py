from __future__ import annotations

import json
from urllib.error import HTTPError

from tokdash.sources.quota import opencode_go


class FakeResponse:
    def __init__(self, raw: bytes):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, n: int = -1) -> bytes:
        return self.raw if n is None or n < 0 else self.raw[:n]


def _json_opener(payload, hook=None):
    def opener(request, timeout=0):
        assert request.full_url == "https://opencode.ai/zen/go/v1/usage"
        assert request.get_header("Authorization").startswith("Bearer ")
        assert request.get_header("Accept") == "application/json"
        if hook is not None:
            hook(request)
        return FakeResponse(json.dumps(payload).encode("utf-8"))

    return opener


def _error_opener(code, body: bytes | dict, reason="Error"):
    def opener(request, timeout=0):
        fp = FakeFP(body if isinstance(body, bytes) else json.dumps(body).encode("utf-8"))
        raise HTTPError(request.full_url, code, reason, {}, fp)

    return opener


class FakeFP:
    def __init__(self, raw: bytes):
        self.raw = raw

    def read(self, n: int = -1) -> bytes:
        return self.raw

    def close(self) -> None:
        pass


def _auth_file(tmp_path, monkeypatch, entry):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"opencode-go": entry}), encoding="utf-8")
    monkeypatch.setattr(opencode_go.clientpaths, "opencode_data_dir", lambda: tmp_path)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    return auth_path


def test_collects_rolling_weekly_monthly_windows(monkeypatch, tmp_path):
    _auth_file(tmp_path, monkeypatch, {"type": "api", "key": "sk-file"})
    payload = {
        "usage": {
            "rolling": {"status": "ok", "percent": 25.0, "resetsAt": "2026-09-01T02:15:00Z"},
            "weekly": {"status": "ok", "percent": 50.0, "resetsAt": "2026-09-05T00:00:00Z"},
            "monthly": {"status": "ok", "percent": 10.0, "resetsAt": "2026-10-01T00:00:00Z"},
        }
    }
    seen = {}
    snapshots = opencode_go.collect_opencode_go_api_snapshots(
        opener=_json_opener(payload, lambda req: seen.setdefault("auth", req.get_header("Authorization"))),
        now=1_787_900_000,
    )

    assert [(s.bucket, s.used_percent) for s in snapshots] == [
        ("rolling", 25.0),
        ("weekly", 50.0),
        ("monthly", 10.0),
    ]
    assert [s.resets_at for s in snapshots] == [1_788_228_900, 1_788_566_400, 1_790_812_800]
    assert all(
        s.provider == "opencode_go" and s.plan == "Go" and s.source == "opencode_go_api" and s.status == "ok"
        for s in snapshots
    )
    assert seen["auth"] == "Bearer sk-file"


def test_env_overrides_auth_file_without_reading_or_rewriting_it(monkeypatch, tmp_path):
    auth_path = _auth_file(tmp_path, monkeypatch, {"type": "api", "key": "sk-file"})
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-env")
    before = auth_path.read_bytes()

    def unexpected_file_read():
        raise AssertionError("auth.json read despite explicit environment key")

    monkeypatch.setattr(opencode_go, "_auth_file_key", unexpected_file_read)

    seen = {}
    opencode_go.collect_opencode_go_api_snapshots(
        opener=_json_opener({"usage": {}}, lambda req: seen.setdefault("auth", req.get_header("Authorization"))),
        now=1,
    )

    assert seen["auth"] == "Bearer sk-env"
    assert auth_path.read_bytes() == before


def test_env_key_when_no_auth_file(monkeypatch, tmp_path):
    monkeypatch.setattr(opencode_go.clientpaths, "opencode_data_dir", lambda: tmp_path / "missing")
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-env")

    seen = {}
    snapshots = opencode_go.collect_opencode_go_api_snapshots(
        opener=_json_opener({"usage": {"weekly": {"status": "ok", "percent": 7.0}}}, lambda req: seen.setdefault("auth", req.get_header("Authorization"))),
        now=1,
    )

    assert seen["auth"] == "Bearer sk-env"
    assert [(s.bucket, s.used_percent) for s in snapshots] == [("weekly", 7.0)]


def test_blank_env_falls_back_to_auth_file(monkeypatch, tmp_path):
    _auth_file(tmp_path, monkeypatch, {"type": "api", "key": "sk-file"})
    monkeypatch.setenv("OPENCODE_API_KEY", "   ")

    credential = opencode_go.read_opencode_go_key()

    assert credential is not None and credential.token == "sk-file"


def test_oauth_entry_is_not_used_as_an_api_key(monkeypatch, tmp_path):
    _auth_file(tmp_path, monkeypatch, {"type": "oauth", "key": "oauth-key"})

    credential = opencode_go.read_opencode_go_key()

    assert credential is None


def test_missing_credentials_reports_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(opencode_go.clientpaths, "opencode_data_dir", lambda: tmp_path / "missing")
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)

    snapshots = opencode_go.collect_opencode_go_api_snapshots(
        opener=lambda request, timeout=0: (_ for _ in ()).throw(AssertionError("network called")),
        now=1,
    )

    assert len(snapshots) == 1
    assert snapshots[0].bucket == "api"
    assert snapshots[0].status == "unavailable"
    assert snapshots[0].raw["error"] == "credentials_not_found"


def test_empty_usage_reports_no_windows(monkeypatch, tmp_path):
    _auth_file(tmp_path, monkeypatch, {"type": "api", "key": "sk-file"})

    snapshots = opencode_go.collect_opencode_go_api_snapshots(opener=_json_opener({"usage": {}}), now=1)

    assert len(snapshots) == 1
    assert snapshots[0].status == "unavailable"
    assert snapshots[0].raw["error"] == "no_windows"


def test_percent_clamped_and_reset_in_sec_accepted(monkeypatch, tmp_path):
    _auth_file(tmp_path, monkeypatch, {"type": "api", "key": "sk-file"})
    payload = {
        "usage": {
            "rolling": {"status": "ok", "percent": 140.0, "resetInSec": 3600},
            "weekly": {"status": "ok", "percent": -5.0},
        }
    }

    snapshots = opencode_go.collect_opencode_go_api_snapshots(opener=_json_opener(payload), now=1_000_000)

    assert [(s.bucket, s.used_percent, s.resets_at) for s in snapshots] == [
        ("rolling", 100.0, 1_003_600),
        ("weekly", 0.0, None),
    ]


def test_float_reset_offset_accepted(monkeypatch, tmp_path):
    _auth_file(tmp_path, monkeypatch, {"type": "api", "key": "sk-file"})
    payload = {"usage": {"rolling": {"status": "ok", "percent": 10.0, "resetInSec": "3600.5"}}}

    snapshots = opencode_go.collect_opencode_go_api_snapshots(opener=_json_opener(payload), now=1_000_000)

    assert [(s.bucket, s.resets_at) for s in snapshots] == [("rolling", 1_003_600)]


def test_401_marks_key_stale_with_refresh_hint(monkeypatch, tmp_path):
    _auth_file(tmp_path, monkeypatch, {"type": "api", "key": "sk-file"})
    opener = _error_opener(401, {"error": {"type": "AuthError", "message": "Missing API key."}})

    snapshots = opencode_go.collect_opencode_go_api_snapshots(opener=opener, now=1)

    assert len(snapshots) == 1
    assert snapshots[0].status == "stale_token"
    assert "HTTP 401" in snapshots[0].raw["error"]
    assert snapshots[0].raw["hint"].startswith("Run '/connect'")


def test_403_entitlement_error_reports_unavailable(monkeypatch, tmp_path):
    _auth_file(tmp_path, monkeypatch, {"type": "api", "key": "sk-file"})
    opener = _error_opener(403, {"error": {"type": "EntitlementError", "message": "OpenCode Go subscription required."}})

    snapshots = opencode_go.collect_opencode_go_api_snapshots(opener=opener, now=1)

    assert len(snapshots) == 1
    assert snapshots[0].bucket == "api"
    assert snapshots[0].status == "unavailable"
    assert snapshots[0].raw["error"] == "subscription_required"


def test_subscription_loss_and_recovery_update_stored_quota_status(monkeypatch, tmp_path):
    from tokdash import api
    from tokdash.sources import quota
    from tokdash.sources.quota import config
    from tokdash.usage_store import UsageEntryStore

    _auth_file(tmp_path, monkeypatch, {"type": "api", "key": "sk-file"})
    config.set_quota_consent({"credential_scan": True, "opencode_go_api": True})
    monkeypatch.setattr(quota, "collect_local_snapshots", lambda store=None: [])
    monkeypatch.setattr(quota, "_CURRENT_SNAPSHOTS", [])
    monkeypatch.setattr(quota, "_LAST_POLL_AT", None)
    store = UsageEntryStore()
    payload = {"usage": {"weekly": {"status": "ok", "percent": 25, "resetsAt": "2026-09-20T00:00:00Z"}}}

    def poll(opener, now):
        monkeypatch.setattr(
            quota, "collect_opencode_go_api_snapshots",
            lambda: opencode_go.collect_opencode_go_api_snapshots(opener=opener, now=now),
        )
        quota.poll_quota(store)
        api._clear_cache()
        return api.get_quota()["providers"]["opencode_go"]

    provider = poll(_json_opener(payload), 1_789_344_000)
    assert provider["status"] == "ok"
    assert provider["status_detail"] is None
    assert provider["buckets"][0]["remaining_percent"] == 75.0

    provider = poll(
        _error_opener(403, {"error": {"type": "EntitlementError", "message": "OpenCode Go subscription required."}}),
        1_789_344_100,
    )
    assert provider["status"] == "unavailable"
    assert provider["status_detail"] == "unavailable"
    assert provider["status_at"] == 1_789_344_100
    assert provider["buckets"][0]["captured_at"] == 1_789_344_000
    assert store.quota_history(providers=["opencode_go"])["series"][0]["points"] == [
        {"captured_at": 1_789_344_000, "used_percent": 25.0},
    ]

    payload["usage"]["weekly"]["percent"] = 30
    provider = poll(_json_opener(payload), 1_789_344_200)
    assert provider["status"] == "ok"
    assert provider["status_detail"] is None
    assert provider["status_at"] is None
    assert provider["buckets"][0]["remaining_percent"] == 70.0


def test_403_other_marks_key_stale(monkeypatch, tmp_path):
    _auth_file(tmp_path, monkeypatch, {"type": "api", "key": "sk-file"})
    opener = _error_opener(403, {"error": {"type": "Forbidden", "message": "Nope."}})

    snapshots = opencode_go.collect_opencode_go_api_snapshots(opener=opener, now=1)

    assert len(snapshots) == 1
    assert snapshots[0].status == "stale_token"


def test_error_without_message_falls_back_to_body(monkeypatch, tmp_path):
    _auth_file(tmp_path, monkeypatch, {"type": "api", "key": "sk-file"})
    opener = _error_opener(403, {"error": {"type": "OtherError"}})

    snapshots = opencode_go.collect_opencode_go_api_snapshots(opener=opener, now=1)

    assert len(snapshots) == 1
    assert snapshots[0].status == "stale_token"
    assert "None" not in snapshots[0].raw["error"]


def test_cdn_error_page_truncated_to_fetch_error(monkeypatch, tmp_path):
    _auth_file(tmp_path, monkeypatch, {"type": "api", "key": "sk-file"})
    opener = _error_opener(502, b"<html>" + b"x" * 2000, reason="Bad Gateway")

    snapshots = opencode_go.collect_opencode_go_api_snapshots(opener=opener, now=1)

    assert len(snapshots) == 1
    assert snapshots[0].status == "fetch_error"
    assert len(snapshots[0].raw["error"]) <= 230


def test_malformed_json_is_fetch_error(monkeypatch, tmp_path):
    _auth_file(tmp_path, monkeypatch, {"type": "api", "key": "sk-file"})

    def opener(request, timeout=0):
        return FakeResponse(b"not json{")

    snapshots = opencode_go.collect_opencode_go_api_snapshots(opener=opener, now=1)

    assert len(snapshots) == 1
    assert snapshots[0].status == "fetch_error"


def test_plain_string_and_underscore_key_entries_accepted(monkeypatch, tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"opencode_go": "sk-underscore", "other": {"apiKey": "sk-other"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(opencode_go.clientpaths, "opencode_data_dir", lambda: tmp_path)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)

    credential = opencode_go.read_opencode_go_key()

    assert credential is not None and credential.token == "sk-underscore"


def test_detection_reads_key_content_only_with_credential_scan(monkeypatch, tmp_path):
    from tokdash.sources import quota

    _auth_file(tmp_path, monkeypatch, {"type": "api", "key": "sk-file"})
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)

    monkeypatch.setattr(quota.config, "credential_scan_enabled", lambda: True)
    assert "opencode_go" in quota._detected_local_providers([])

    (tmp_path / "auth.json").write_text(json.dumps({"other": {"key": "x"}}), encoding="utf-8")
    assert "opencode_go" not in quota._detected_local_providers([])

    # Pre-consent detection is shallow (file presence) and never parses content.
    monkeypatch.setattr(quota.config, "credential_scan_enabled", lambda: False)
    _auth_file(tmp_path, monkeypatch, {"type": "api", "key": "sk-file"})
    assert "opencode_go" in quota._detected_local_providers([])
    (tmp_path / "auth.json").unlink()
    assert "opencode_go" not in quota._detected_local_providers([])
