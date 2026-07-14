from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

from tokdash.sources.quota import antigravity, claude, codex
from tokdash.usage_store import _codex_window_used_percent_from_raw

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "quota"


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _jwt(payload: dict) -> str:
    def part(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{part({'alg':'none'})}.{part(payload)}.sig"


def _header(req, name: str) -> str | None:
    for key, value in req.header_items():
        if key.lower() == name.lower():
            return value
    return None


def test_codex_api_collects_usage_and_reset_credits(monkeypatch, tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    token = _jwt({"exp": 4_000_000_000, "https://api.openai.com/auth": {"chatgpt_account_id": "acct_123"}})
    (codex_home / "auth.json").write_text(json.dumps({"tokens": {"access_token": token}}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    urls = []
    account_headers = []

    def opener(req, timeout=15):
        urls.append(req.full_url)
        account_headers.append(_header(req, "ChatGPT-Account-Id"))
        if req.full_url.endswith("/wham/usage"):
            return FakeResponse(
                {
                    "plan_type": "pro",
                    "rate_limit": {"used_percent": 25, "resets_at": "2026-07-01T13:00:00Z"},
                    "additional_rate_limits": [
                        {"used_percent": 40, "resets_at": 1783467600, "window_minutes": 10080},
                    ],
                }
            )
        return FakeResponse(
            {
                "available_count": 2,
                "credits": [
                    {"id": "credit-a", "expires_at": "2026-07-04T00:00:00Z"},
                    {"id": "credit-b", "expires_at": 1783296000},
                ],
            }
        )

    snapshots = codex.collect_codex_api_snapshots(opener=opener, now=1_782_907_200)

    assert {s.bucket for s in snapshots} == {"5h", "7d", "reset_credits"}
    assert [s.used_percent for s in snapshots if s.bucket == "5h"] == [25.0]
    assert [s.used_percent for s in snapshots if s.bucket == "reset_credits"] == [2.0]
    assert all(s.account == "acct_123" for s in snapshots)
    assert urls == [
        "https://chatgpt.com/backend-api/wham/usage",
        "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits",
    ]
    assert account_headers == ["acct_123", "acct_123"]


def test_codex_api_unwraps_nested_additional_rate_limits(monkeypatch, tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    token = _jwt({"exp": 4_000_000_000, "https://api.openai.com/auth": {"chatgpt_account_id": "acct_123"}})
    (codex_home / "auth.json").write_text(json.dumps({"tokens": {"access_token": token}}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    def opener(req, timeout=15):
        if req.full_url.endswith("/wham/usage"):
            return FakeResponse(
                {
                    "plan_type": "pro",
                    "rate_limit": {"used_percent": 25, "resets_at": "2026-07-01T13:00:00Z"},
                    "additional_rate_limits": [
                        {
                            "name": "weekly",
                            "rate_limit": {
                                "used_percent": 40,
                                "resets_at": 1783467600,
                                "window_minutes": 10080,
                            },
                        }
                    ],
                }
            )
        return FakeResponse({"available_count": 0, "credits": []})

    snapshots = codex.collect_codex_api_snapshots(opener=opener, now=1_782_907_200)

    assert [s.used_percent for s in snapshots if s.bucket == "7d"] == [40.0]


def test_codex_api_resets_at_only_one_percent_is_not_scaled_to_full(monkeypatch, tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    token = _jwt({"exp": 4_000_000_000})
    (codex_home / "auth.json").write_text(json.dumps({"tokens": {"access_token": token}}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    def opener(req, timeout=15):
        if req.full_url.endswith("/wham/usage"):
            return FakeResponse(
                {
                    "plan_type": "prolite",
                    "rate_limit": {"used_percent": 1, "resets_at": "2026-07-10T13:10:55Z"},
                }
            )
        return FakeResponse({"available_count": 0, "credits": []})

    snapshots = codex.collect_codex_api_snapshots(opener=opener, now=1_783_674_889)

    primary = next(s for s in snapshots if s.bucket == "5h")
    assert primary.used_percent == 1.0


def test_codex_api_omits_account_header_when_account_unresolved(monkeypatch, tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    token = _jwt({"exp": 4_000_000_000})
    (codex_home / "auth.json").write_text(json.dumps({"tokens": {"access_token": token}}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    account_headers = []

    def opener(req, timeout=15):
        account_headers.append(_header(req, "ChatGPT-Account-Id"))
        if req.full_url.endswith("/wham/usage"):
            return FakeResponse({"rate_limit": {"used_percent": 10, "resets_at": 1_782_910_800}})
        return FakeResponse({"available_count": 0, "credits": []})

    snapshots = codex.collect_codex_api_snapshots(opener=opener, now=1_782_907_200)

    assert any(s.bucket == "5h" and s.account == "default" for s in snapshots)
    assert account_headers == [None, None]


def test_codex_api_uses_tokens_account_id_fallback(monkeypatch, tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    token = _jwt({"exp": 4_000_000_000})
    (codex_home / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": token, "account_id": "acct_from_tokens"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    account_headers = []

    def opener(req, timeout=15):
        account_headers.append(_header(req, "ChatGPT-Account-Id"))
        if req.full_url.endswith("/wham/usage"):
            return FakeResponse({"rate_limit": {"used_percent": 10, "resets_at": 1_782_910_800}})
        return FakeResponse({"available_count": 0, "credits": []})

    snapshots = codex.collect_codex_api_snapshots(opener=opener, now=1_782_907_200)

    assert all(s.account == "acct_from_tokens" for s in snapshots)
    assert account_headers == ["acct_from_tokens", "acct_from_tokens"]


def test_codex_api_expired_token_still_attempts_call_and_401_yields_stale_snapshot(monkeypatch, tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": _jwt({"exp": 10, "https://api.openai.com/auth.chatgpt_account_id": "acct"})}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    calls = {"n": 0}

    def opener(_req, timeout=15):
        calls["n"] += 1
        raise HTTPError("https://chatgpt.com/backend-api/wham/usage", 401, "Unauthorized", {}, None)

    snapshots = codex.collect_codex_api_snapshots(opener=opener, now=1000)

    assert calls["n"] == 1  # the call is attempted despite a locally-expired exp claim
    assert len(snapshots) == 1
    assert snapshots[0].status == "stale_token"
    assert snapshots[0].bucket == "api"


def test_claude_api_parses_limits_shape(monkeypatch, tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "token",
                    "expiresAt": 4_000_000_000_000,
                    "subscriptionType": "max",
                    "rateLimitTier": "default_claude_max_5x",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))

    def opener(req, timeout=15):
        assert req.full_url == "https://api.anthropic.com/api/oauth/usage"
        return FakeResponse(
            {
                "limits": [
                    {"kind": "session", "percent": 75, "resets_at": "2026-07-01T15:00:00Z", "is_active": True},
                    {
                        "kind": "weekly_scoped",
                        "percent": 0.5,
                        "resets_at": 1783467600,
                        "scope": {"model": {"display_name": "Opus"}},
                    },
                ]
            }
        )

    snapshots = claude.collect_claude_api_snapshots(opener=opener, now=1_782_907_200)

    assert [(s.bucket, s.bucket_label, s.used_percent) for s in snapshots] == [
        ("session", "Session", 75.0),
        ("weekly_scoped_opus", "Opus", 50.0),
    ]
    assert all(s.plan == "max/default_claude_max_5x" for s in snapshots)


def test_antigravity_api_normalizes_model_quota(monkeypatch, tmp_path):
    ag_dir = tmp_path / ".gemini" / "antigravity-cli"
    ag_dir.mkdir(parents=True)
    (ag_dir / "antigravity-oauth-token").write_text(
        json.dumps(
            {
                "auth_method": "oauth",
                "token": {
                    "access_token": "ya29.token",
                    "refresh_token": "secret-refresh",
                    "expiry": "2096-10-02T07:06:40Z",
                },
                "email": "h@example.com",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(antigravity.clientpaths, "antigravity_cli_dir", lambda: ag_dir)
    authorizations = []

    def opener(req, timeout=15):
        authorizations.append(_header(req, "Authorization"))
        if req.full_url.endswith(":loadCodeAssist"):
            return FakeResponse({"projectId": "project-1"})
        assert req.full_url.endswith(":fetchAvailableModels")
        return FakeResponse(
            {
                "models": {
                    "gemini-3-pro": {
                        "name": "models/gemini-3-pro",
                        "displayName": "Gemini 3 Pro",
                        "quotaInfo": {"remainingFraction": 0.2, "resetTime": "2026-07-02T00:00:00Z"},
                    }
                }
            }
        )

    snapshots = antigravity.collect_antigravity_api_snapshots(opener=opener, now=1_782_907_200)

    assert len(snapshots) == 1
    assert authorizations == ["Bearer ya29.token", "Bearer ya29.token"]
    assert snapshots[0].account == "h@example.com"
    assert snapshots[0].bucket == "models/gemini-3-pro"
    assert snapshots[0].bucket_label == "Gemini 3 Pro"
    assert snapshots[0].used_percent == 80.0
    assert "secret-refresh" not in json.dumps(snapshots[0].raw)
    assert "ya29.token" not in json.dumps(snapshots[0].raw)


def test_antigravity_nested_expired_token_still_attempts_call_and_401_is_stale_without_secret_raw(monkeypatch, tmp_path):
    ag_dir = tmp_path / ".gemini" / "antigravity-cli"
    ag_dir.mkdir(parents=True)
    (ag_dir / "antigravity-oauth-token").write_text(
        json.dumps(
            {
                "auth_method": "oauth",
                "token": {
                    "access_token": "ya29.token",
                    "refresh_token": "secret-refresh",
                    "expiry": "2020-01-01T00:00:00Z",
                },
                "email": "h@example.com",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(antigravity.clientpaths, "antigravity_cli_dir", lambda: ag_dir)
    calls = {"n": 0}

    def opener(_req, timeout=15):
        calls["n"] += 1
        raise HTTPError("https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist", 401, "Unauthorized", {}, None)

    snapshots = antigravity.collect_antigravity_api_snapshots(opener=opener, now=1_782_907_200)

    assert calls["n"] == 1
    assert snapshots[0].status == "stale_token"
    assert snapshots[0].account == "h@example.com"
    raw = json.dumps(snapshots[0].raw)
    assert "secret-refresh" not in raw
    assert "ya29.token" not in raw


def test_antigravity_http_401_is_stale_token(monkeypatch, tmp_path):
    ag_dir = tmp_path / ".gemini" / "antigravity-cli"
    ag_dir.mkdir(parents=True)
    (ag_dir / "antigravity-oauth-token").write_text(json.dumps({"access_token": "ya29.token"}), encoding="utf-8")
    monkeypatch.setattr(antigravity.clientpaths, "antigravity_cli_dir", lambda: ag_dir)

    def opener(_req, timeout=15):
        raise HTTPError("https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist", 401, "Unauthorized", {}, None)

    snapshots = antigravity.collect_antigravity_api_snapshots(opener=opener, now=1_782_907_200)

    assert snapshots[0].status == "stale_token"


def test_codex_http_403_is_stale_token(monkeypatch, tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    token = _jwt({"exp": 4_000_000_000, "https://api.openai.com/auth.chatgpt_account_id": "acct_123"})
    (codex_home / "auth.json").write_text(json.dumps({"tokens": {"access_token": token}}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    def opener(_req, timeout=15):
        raise HTTPError("https://chatgpt.com/backend-api/wham/usage", 403, "Forbidden", {}, None)

    snapshots = codex.collect_codex_api_snapshots(opener=opener, now=1_782_907_200)

    assert snapshots[0].status == "stale_token"


def test_codex_retries_transient_http_error_once(monkeypatch, tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    token = _jwt({"exp": 4_000_000_000, "https://api.openai.com/auth.chatgpt_account_id": "acct_123"})
    (codex_home / "auth.json").write_text(json.dumps({"tokens": {"access_token": token}}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    calls = {"usage": 0}

    def opener(req, timeout=15):
        if req.full_url.endswith("/wham/usage"):
            calls["usage"] += 1
            if calls["usage"] == 1:
                raise HTTPError(req.full_url, 500, "Server Error", {}, None)
            return FakeResponse({"rate_limits": {"primary": {"used_percent": 10, "resets_at": 1_782_910_800, "limit_window_seconds": 18000}}})
        return FakeResponse({"available_count": 0, "credits": []})

    snapshots = codex.collect_codex_api_snapshots(opener=opener, now=1_782_907_200)

    assert calls["usage"] == 2
    assert any(s.bucket == "5h" and s.status == "ok" for s in snapshots)


def test_antigravity_retries_transient_http_error_once(monkeypatch, tmp_path):
    ag_dir = tmp_path / ".gemini" / "antigravity-cli"
    ag_dir.mkdir(parents=True)
    (ag_dir / "antigravity-oauth-token").write_text(json.dumps({"access_token": "ya29.token"}), encoding="utf-8")
    monkeypatch.setattr(antigravity.clientpaths, "antigravity_cli_dir", lambda: ag_dir)
    calls = {"load": 0}

    def opener(req, timeout=15):
        if req.full_url.endswith(":loadCodeAssist"):
            calls["load"] += 1
            if calls["load"] == 1:
                raise HTTPError(req.full_url, 429, "Too Many Requests", {}, None)
            return FakeResponse({"projectId": "project-1"})
        return FakeResponse(
            {
                "models": [
                    {
                        "name": "models/gemini-3-pro",
                        "displayName": "Gemini 3 Pro",
                        "quotaInfo": {"remainingFraction": 0.2, "resetTime": "2026-07-02T00:00:00Z"},
                    }
                ]
            }
        )

    snapshots = antigravity.collect_antigravity_api_snapshots(opener=opener, now=1_782_907_200)

    assert calls["load"] == 2
    assert snapshots[0].status == "ok"


def _load_quota_fixture(name: str) -> dict:
    path = _FIXTURE_DIR / name
    if not path.exists():
        pytest.skip(f"frozen fixture {path} not present (run scripts/probe_quota_endpoints.py)")
    return json.loads(path.read_text(encoding="utf-8"))


def test_codex_usage_frozen_fixture_parses(monkeypatch, tmp_path):
    usage = _load_quota_fixture("codex_usage.json")
    credits = _load_quota_fixture("codex_reset_credits.json")
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    token = _jwt({"exp": 4_000_000_000})
    (codex_home / "auth.json").write_text(json.dumps({"tokens": {"access_token": token}}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    def opener(req, timeout=15):
        return FakeResponse(usage if req.full_url.endswith("/wham/usage") else credits)

    snapshots = codex.collect_codex_api_snapshots(opener=opener, now=1_782_907_200)

    by_bucket = {s.bucket: s for s in snapshots if s.bucket in {"5h", "7d"}}
    assert by_bucket["5h"].used_percent == 99.0
    assert by_bucket["7d"].used_percent == 40.0
    assert all(s.plan == "prolite" for s in snapshots if s.bucket in {"5h", "7d"})
    spark_5h = next(s for s in snapshots if s.bucket == "codex_bengalfox_5h")
    assert spark_5h.bucket_label == "GPT-5.3-Codex-Spark · 5-hour"
    assert spark_5h.used_percent == 6.0
    spark_7d = next(s for s in snapshots if s.bucket == "codex_bengalfox_7d")
    assert spark_7d.bucket_label == "GPT-5.3-Codex-Spark · 7-day"
    assert spark_7d.used_percent == 2.0
    reset = next(s for s in snapshots if s.bucket == "reset_credits")
    assert reset.used_percent == 3.0
    assert all(s.status != "fetch_error" for s in snapshots)


def test_codex_usage_nested_primary_secondary_windows_parse_inline(monkeypatch, tmp_path):
    """Pins the real wham/usage nested-window parsing contract independent of the fixture file."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    token = _jwt({"exp": 4_000_000_000})
    (codex_home / "auth.json").write_text(json.dumps({"tokens": {"access_token": token}}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    def opener(req, timeout=15):
        if req.full_url.endswith("/wham/usage"):
            return FakeResponse(
                {
                    "plan_type": "pro",
                    "rate_limit": {
                        "allowed": True,
                        "limit_reached": False,
                        "primary_window": {
                            "limit_window_seconds": 18000,
                            "reset_after_seconds": 100,
                            "reset_at": 1_782_910_800,
                            "used_percent": 55,
                        },
                        "secondary_window": {
                            "limit_window_seconds": 604800,
                            "reset_after_seconds": 200,
                            "reset_at": 1_783_467_600,
                            "used_percent": 33,
                        },
                    },
                }
            )
        return FakeResponse({"available_count": 0, "credits": []})

    snapshots = codex.collect_codex_api_snapshots(opener=opener, now=1_782_907_200)

    by_bucket = {s.bucket: s for s in snapshots if s.bucket in {"5h", "7d"}}
    assert by_bucket["5h"].used_percent == 55.0
    assert by_bucket["5h"].resets_at == 1_782_910_800
    assert by_bucket["7d"].used_percent == 33.0
    assert by_bucket["7d"].resets_at == 1_783_467_600
    assert all(s.plan == "pro" for s in by_bucket.values())


def test_codex_usage_classifies_single_weekly_primary_by_duration(monkeypatch, tmp_path):
    """A temporary weekly-only response must not synthesize or mislabel a 5h window."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    token = _jwt({"exp": 4_000_000_000})
    (codex_home / "auth.json").write_text(json.dumps({"tokens": {"access_token": token}}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    def opener(req, timeout=15):
        if req.full_url.endswith("/wham/usage"):
            return FakeResponse(
                {
                    "plan_type": "prolite",
                    "rate_limit": {
                        "primary_window": {
                            "limit_window_seconds": 604800,
                            "reset_at": 1_784_365_006,
                            "used_percent": 61,
                        },
                        "secondary_window": None,
                    },
                    "additional_rate_limits": [
                        {
                            "limit_name": "GPT-5.3-Codex-Spark",
                            "metered_feature": "codex_bengalfox",
                            "rate_limit": {
                                "primary_window": {
                                    "limit_window_seconds": 604800,
                                    "reset_at": 1_784_399_038,
                                    "used_percent": 32,
                                },
                                "secondary_window": None,
                            },
                        }
                    ],
                }
            )
        return FakeResponse({"available_count": 0, "credits": []})

    snapshots = codex.collect_codex_api_snapshots(opener=opener, now=1_783_880_575)
    by_bucket = {snapshot.bucket: snapshot for snapshot in snapshots}

    assert "5h" not in by_bucket
    assert by_bucket["7d"].used_percent == 61.0
    assert by_bucket["7d"].resets_at == 1_784_365_006
    assert "codex_bengalfox_5h" not in by_bucket
    assert by_bucket["codex_bengalfox_7d"].used_percent == 32.0
    assert by_bucket["codex_bengalfox_7d"].resets_at == 1_784_399_038


@pytest.mark.parametrize("with_duration", [False, True])
def test_codex_usage_plural_single_primary_is_weekly(monkeypatch, tmp_path, with_duration):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    token = _jwt({"exp": 4_000_000_000})
    (codex_home / "auth.json").write_text(json.dumps({"tokens": {"access_token": token}}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    weekly = {"used_percent": 61, "resets_at": 1_784_365_006}
    if with_duration:
        weekly["limit_window_seconds"] = 604800

    def opener(req, timeout=15):
        if req.full_url.endswith("/wham/usage"):
            return FakeResponse({"plan_type": "prolite", "rate_limits": {"primary": weekly}})
        return FakeResponse({"available_count": 0, "credits": []})

    snapshots = codex.collect_codex_api_snapshots(opener=opener, now=1_783_880_575)
    by_bucket = {snapshot.bucket: snapshot for snapshot in snapshots}

    assert "5h" not in by_bucket
    assert by_bucket["7d"].used_percent == 61.0


def test_codex_metered_single_primary_without_duration_is_weekly(monkeypatch, tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    token = _jwt({"exp": 4_000_000_000})
    (codex_home / "auth.json").write_text(json.dumps({"tokens": {"access_token": token}}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    def opener(req, timeout=15):
        if req.full_url.endswith("/wham/usage"):
            return FakeResponse(
                {
                    "plan_type": "prolite",
                    "rate_limit": {"used_percent": 5, "resets_at": 1_783_898_575},
                    "additional_rate_limits": [
                        {
                            "limit_name": "GPT-5.3-Codex-Spark",
                            "metered_feature": "codex_bengalfox",
                            "rate_limit": {
                                "primary_window": {"used_percent": 32, "resets_at": 1_784_399_038},
                                "secondary_window": None,
                            },
                        }
                    ],
                }
            )
        return FakeResponse({"available_count": 0, "credits": []})

    snapshots = codex.collect_codex_api_snapshots(opener=opener, now=1_783_880_575)
    by_bucket = {snapshot.bucket: snapshot for snapshot in snapshots}

    assert by_bucket["5h"].used_percent == 5.0
    assert "codex_bengalfox_5h" not in by_bucket
    assert by_bucket["codex_bengalfox_7d"].used_percent == 32.0


def test_codex_usage_nested_one_percent_is_not_scaled_to_full(monkeypatch, tmp_path):
    """Real wham/usage uses a 0-100 percent scale; 1 means 1%, not a unit fraction."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    token = _jwt({"exp": 4_000_000_000})
    (codex_home / "auth.json").write_text(json.dumps({"tokens": {"access_token": token}}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    def opener(req, timeout=15):
        if req.full_url.endswith("/wham/usage"):
            return FakeResponse(
                {
                    "plan_type": "prolite",
                    "rate_limit": {
                        "primary_window": {
                            "limit_window_seconds": 18000,
                            "reset_after_seconds": 13975,
                            "reset_at": 1_783_689_055,
                            "used_percent": 1,
                        },
                        "secondary_window": {
                            "limit_window_seconds": 604800,
                            "reset_after_seconds": 600775,
                            "reset_at": 1_784_275_855,
                            "used_percent": 0,
                        },
                    },
                }
            )
        return FakeResponse({"available_count": 0, "credits": []})

    snapshots = codex.collect_codex_api_snapshots(opener=opener, now=1_783_674_889)

    primary = next(s for s in snapshots if s.bucket == "5h")
    assert primary.used_percent == 1.0


def test_antigravity_models_frozen_fixture_parses(monkeypatch, tmp_path):
    assist = _load_quota_fixture("antigravity_loadcodeassist.json")
    models = _load_quota_fixture("antigravity_models.json")
    ag_dir = tmp_path / ".gemini" / "antigravity-cli"
    ag_dir.mkdir(parents=True)
    (ag_dir / "antigravity-oauth-token").write_text(json.dumps({"access_token": "ya29.token"}), encoding="utf-8")
    monkeypatch.setattr(antigravity.clientpaths, "antigravity_cli_dir", lambda: ag_dir)

    def opener(req, timeout=15):
        return FakeResponse(assist if req.full_url.endswith(":loadCodeAssist") else models)

    snapshots = antigravity.collect_antigravity_api_snapshots(opener=opener, now=1_782_907_200)

    assert snapshots
    assert all(s.status != "fetch_error" for s in snapshots)


def test_claude_plan_label_normalized(monkeypatch, tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    cases = [
        ({"subscriptionType": "max", "rateLimitTier": "default_claude_max_5x"}, "Max 5x"),
        ({"subscriptionType": "max", "rateLimitTier": "default_claude_max_20x"}, "Max 20x"),
        ({"subscriptionType": "pro"}, "Pro"),
    ]
    for oauth, expected in cases:
        (claude_dir / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "token", **oauth}}), encoding="utf-8"
        )
        assert claude.read_claude_plan()["plan"] == expected, expected


def test_claude_api_tolerates_non_dict_scope(monkeypatch, tmp_path):
    # Regression: a string (or otherwise non-dict) scope must not raise AttributeError and
    # escape the collector as a 500 — the entry falls back to its kind label and other
    # well-formed limits still parse.
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "token", "subscriptionType": "max"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))

    def opener(req, timeout=15):
        return FakeResponse(
            {
                "limits": [
                    {"kind": "session", "percent": 60, "scope": "everything"},  # scope is a str
                    {"kind": "weekly", "percent": 20, "scope": {"model": "not-a-dict"}},
                ]
            }
        )

    snapshots = claude.collect_claude_api_snapshots(opener=opener, now=1_782_907_200)

    assert [(s.bucket, s.used_percent) for s in snapshots] == [("session", 60.0), ("weekly", 20.0)]


def test_codex_api_keeps_windows_when_reset_credits_fails(monkeypatch, tmp_path):
    # Regression: a failing reset-credits call must NOT discard the usage windows already
    # fetched. The cycle degrades to "no reset_credits snapshot", keeping 5h/7d.
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    token = _jwt({"exp": 4_000_000_000, "https://api.openai.com/auth": {"chatgpt_account_id": "acct_123"}})
    (codex_home / "auth.json").write_text(json.dumps({"tokens": {"access_token": token}}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    def opener(req, timeout=15):
        if req.full_url.endswith("/wham/usage"):
            return FakeResponse(
                {
                    "plan_type": "pro",
                    "rate_limits": {
                        "primary": {"used_percent": 25, "resets_at": 1_783_024_796},
                        "secondary": {"used_percent": 50, "resets_at": 1_783_421_214},
                    },
                }
            )
        raise HTTPError(req.full_url, 500, "Server Error", {}, None)

    snapshots = codex.collect_codex_api_snapshots(opener=opener, now=1_782_907_200)

    buckets = {s.bucket for s in snapshots}
    assert "5h" in buckets and "7d" in buckets  # windows preserved despite credits failure
    assert "reset_credits" not in buckets


_CODEX_W5H = {"used_percent": 11, "resets_at": 1_782_910_800, "limit_window_seconds": 18000}
_CODEX_W7D = {"used_percent": 61, "resets_at": 1_784_365_006, "limit_window_seconds": 604800}
_CODEX_W5H_ND = {"used_percent": 12, "resets_at": 1_782_910_800}
_CODEX_WEIRD = {"used_percent": 33, "resets_at": 1_782_910_800, "limit_window_seconds": 99999}


def _codex_api_window_roundtrip_cases() -> list:
    def usage(rate_limit_fields: dict) -> dict:
        return {"plan_type": "pro", **rate_limit_fields}

    return [
        pytest.param(
            usage({"rate_limits": {"primary": dict(_CODEX_W5H), "secondary": dict(_CODEX_W7D)}}),
            id="plural_both_normal",
        ),
        pytest.param(
            usage({"rate_limits": {"primary": dict(_CODEX_W7D), "secondary": dict(_CODEX_W5H)}}),
            id="plural_both_swapped_by_duration",
        ),
        pytest.param(
            usage({"rate_limits": {"primary": dict(_CODEX_W5H)}}),
            id="plural_single_primary_5h_duration",
        ),
        pytest.param(
            usage({"rate_limits": {"primary": dict(_CODEX_W5H_ND)}}),
            id="plural_single_primary_no_duration",
        ),
        pytest.param(
            usage({"rate_limits": {"secondary": dict(_CODEX_W7D)}}),
            id="plural_single_secondary_7d",
        ),
        pytest.param(
            usage({"rate_limits": {"primary": dict(_CODEX_W5H), "secondary": dict(_CODEX_WEIRD)}}),
            id="plural_one_recognized_one_unknown_duration",
        ),
        pytest.param(
            usage({"rate_limit": {"primary_window": dict(_CODEX_W5H), "secondary_window": dict(_CODEX_W7D)}}),
            id="nested_single_both",
        ),
        pytest.param(
            usage({"rate_limit": {"primary_window": dict(_CODEX_W5H_ND)}}),
            id="nested_single_primary_only_no_duration",
        ),
        pytest.param(
            usage({"rate_limit": {"used_percent": 7, "resets_at": 1_782_910_800}}),
            id="flat_legacy_rate_limit",
        ),
        pytest.param(
            usage(
                {
                    "rate_limit": {"used_percent": 5, "resets_at": 1_783_898_575},
                    "additional_rate_limits": [
                        {
                            "limit_name": "Spark",
                            "metered_feature": "codex_bengalfox",
                            "rate_limit": {
                                "primary_window": dict(_CODEX_W5H),
                                "secondary_window": dict(_CODEX_W7D),
                            },
                        }
                    ],
                }
            ),
            id="metered_both",
        ),
        pytest.param(
            usage(
                {
                    "rate_limit": {"used_percent": 5, "resets_at": 1_783_898_575},
                    "additional_rate_limits": [
                        {
                            "limit_name": "Spark",
                            "metered_feature": "codex_bengalfox",
                            "rate_limit": {
                                "primary_window": dict(_CODEX_W5H_ND),
                                "secondary_window": None,
                            },
                        }
                    ],
                }
            ),
            id="metered_single_primary_no_duration",
        ),
        pytest.param(
            usage(
                {
                    "rate_limit": {"used_percent": 5, "resets_at": 1_783_898_575},
                    "additional_rate_limits": [
                        {
                            "limit_name": "Spark",
                            "metered_feature": "codex_bengalfox",
                            "rate_limit": {
                                "primary_window": dict(_CODEX_W5H),
                                "secondary_window": None,
                            },
                        }
                    ],
                }
            ),
            id="metered_single_primary_5h",
        ),
    ]


@pytest.mark.parametrize("usage_payload", _codex_api_window_roundtrip_cases())
def test_codex_api_window_used_percent_round_trips_from_raw(monkeypatch, tmp_path, usage_payload):
    """The WRITE path (collect_codex_api_snapshots) and the RE-DERIVE path
    (_codex_window_used_percent_from_raw) must classify every live-usage window into the
    same bucket. If they disagree, a store re-derive after replay would silently diverge
    from what was written, which is exactly the shape of bug that causes double-counting.

    NOTE: deliberately excludes the "legacy unsuffixed additional_rate_limits" shape (a
    metered item with a top-level used_percent and no primary_window/secondary_window).
    That shape synthesizes the main "7d" bucket's value from additional_rate_limits, which
    usage_store.py documents as NOT re-derivable from a snapshot's own raw ("A 7d row's
    value came from additional_rate_limits, so it cannot be re-derived from here"). Asserting
    round-trip equality there would fail on that intended, documented behavior rather than a
    real bug, so it is left out of this parametrization on purpose.
    """
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    token = _jwt({"exp": 4_000_000_000})
    (codex_home / "auth.json").write_text(json.dumps({"tokens": {"access_token": token}}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    def opener(req, timeout=15):
        if req.full_url.endswith("/wham/usage"):
            return FakeResponse(usage_payload)
        return FakeResponse({"available_count": 0, "credits": []})

    snapshots = codex.collect_codex_api_snapshots(opener=opener, now=1_782_907_200)

    saw = 0
    for snap in snapshots:
        if snap.source != "codex_api" or snap.status != "ok" or snap.bucket == "reset_credits":
            continue
        if snap.used_percent is None:
            continue
        assert _codex_window_used_percent_from_raw(snap.bucket, json.dumps(snap.raw)) == snap.used_percent
        saw += 1

    # Guards against a future refactor that silently stops producing snapshots (e.g. an
    # opener/bucket-filter mismatch) making this test vacuously pass with zero assertions.
    assert saw >= 1
