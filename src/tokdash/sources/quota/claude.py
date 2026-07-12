from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError
import urllib.request
import time

from ... import clientpaths
from .codex import _normalize_percent, _parse_time
from .types import QuotaSnapshot

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
_KEYCHAIN_LABEL = f"macOS Keychain ({CLAUDE_KEYCHAIN_SERVICE})"


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _read_keychain_credentials(keychain: str | None = None) -> dict[str, Any] | None:
    """Read the Claude Code credential blob from the macOS Keychain.

    On macOS, Claude Code stores the same JSON that Linux/Windows keep in
    ``.credentials.json`` as a login-Keychain generic password (service
    ``Claude Code-credentials``) — the same source ccstatusline and CodexBar read.
    Read-only, via the ``security`` CLI with an argument list (never a shell). Returns
    ``None`` off-macOS, when the item is missing, when the keychain is locked or access
    is denied, or when the payload is not a JSON object — callers degrade to
    ``unavailable`` plus the ``CLAUDE_CODE_OAUTH_TOKEN`` hint. The first read from a new
    binary may show a one-time Keychain permission prompt; the timeout keeps an
    unanswered prompt from wedging a poll cycle. ``keychain`` narrows the lookup to one
    keychain file (used by the CI integration test); production searches the default
    keychain list.
    """
    if not _is_macos():
        return None
    cmd = ["security", "find-generic-password", "-s", CLAUDE_KEYCHAIN_SERVICE, "-w"]
    if keychain:
        cmd.append(keychain)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    text = (result.stdout or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _env_token() -> str:
    return os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()


def _load_credential_data() -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
    """Shared credential-source resolution: ``.credentials.json``, then the macOS Keychain.

    Callers check ``CLAUDE_CODE_OAUTH_TOKEN`` BEFORE calling this — the explicit override
    must short-circuit both sources, notably the Keychain subprocess and its potential
    permission prompt (it is the documented headless/locked-Keychain escape hatch).
    Returns ``(data, source_label, error_meta)``: ``data`` is the parsed blob or ``None``
    on failure, with ``error_meta`` carrying the error fields.
    """
    path = clientpaths.claude_config_dir() / ".credentials.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw, str(path), {}
        return None, str(path), {"error": "credentials_invalid", "message": "not a JSON object"}
    except FileNotFoundError:
        keychain_data = _read_keychain_credentials()
        if keychain_data is not None:
            return keychain_data, _KEYCHAIN_LABEL, {}
        return None, str(path), {"error": "credentials_not_found"}
    except Exception as exc:
        return None, str(path), {"error": "credentials_invalid", "message": str(exc)}


def read_claude_plan() -> dict[str, Any]:
    # Same source precedence as _read_credentials (env var > file > Keychain): the usage
    # data is fetched with the env token's account when the override is set, so plan/tier
    # must not be read from another source's (possibly different) account — and the
    # Keychain subprocess must not run at all. The env var carries no plan metadata.
    if _env_token():
        return {"status": "ok", "plan": None, "tier": None, "credential_path": "CLAUDE_CODE_OAUTH_TOKEN"}
    data, source, _error = _load_credential_data()
    if data is None:
        return {"status": "unavailable", "plan": None, "tier": None, "credential_path": source}

    oauth = data.get("claudeAiOauth") if isinstance(data.get("claudeAiOauth"), dict) else {}
    plan = oauth.get("subscriptionType") or data.get("subscriptionType")
    tier = oauth.get("rateLimitTier") or data.get("rateLimitTier")
    return {"status": "ok", "plan": _plan_label(plan, tier), "tier": tier, "credential_path": source}


def _plan_label(plan: Any, tier: Any) -> str | None:
    """Human plan label for the card header: "Max 5x" / "Max 20x" / "Pro".

    Display-only — snapshot rows keep the raw subscription/tier strings.
    """
    tier_text = str(tier or "").lower()
    if "max_20x" in tier_text:
        return "Max 20x"
    if "max_5x" in tier_text:
        return "Max 5x"
    if plan:
        return str(plan).replace("_", " ").strip().title() or None
    return None


def _read_credentials() -> tuple[str | None, dict[str, Any]]:
    env_token = _env_token()
    if env_token:
        return env_token, {"plan": None, "tier": None, "credential_path": "CLAUDE_CODE_OAUTH_TOKEN"}
    data, source, error_meta = _load_credential_data()
    if data is None:
        return None, {**error_meta, "credential_path": source}
    oauth = data.get("claudeAiOauth") if isinstance(data.get("claudeAiOauth"), dict) else {}
    token = oauth.get("accessToken")
    plan = oauth.get("subscriptionType") or data.get("subscriptionType")
    tier = oauth.get("rateLimitTier") or data.get("rateLimitTier")
    return str(token) if token else None, {
        "expires_at_ms": oauth.get("expiresAt") or data.get("expiresAt"),
        "plan": "/".join(str(v) for v in (plan, tier) if v) or None,
        "tier": tier,
        "credential_path": source,
    }


def _status_snapshot(status: str, captured_at: int, raw: dict[str, Any]) -> QuotaSnapshot:
    return QuotaSnapshot("claude", "default", "api", "Claude API", None, None, raw.get("plan"), captured_at, "claude_api", status, raw)


def _label_for_limit(limit: dict[str, Any]) -> tuple[str, str]:
    kind = str(limit.get("kind") or "usage")
    # Defensive: the API could return scope/model as something other than a dict (schema
    # drift). isinstance guards keep a string scope from raising AttributeError and 500ing
    # GET /api/quota/refresh — we simply fall back to the kind label.
    scope = limit.get("scope")
    scope = scope if isinstance(scope, dict) else {}
    model_obj = scope.get("model")
    model_obj = model_obj if isinstance(model_obj, dict) else {}
    model = str(model_obj.get("display_name") or "").strip()
    if model:
        slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in model).strip("_")
        return f"{kind}_{slug}", model
    return kind, kind.replace("_", " ").title()


def collect_claude_api_snapshots(
    *,
    opener=urllib.request.urlopen,
    now: int | None = None,
    timeout: float = 15.0,
) -> list[QuotaSnapshot]:
    captured_at = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    token, meta = _read_credentials()
    if not token:
        return [_status_snapshot("unavailable", captured_at, meta)]
    expires_ms = meta.get("expires_at_ms")
    try:
        if expires_ms and int(expires_ms) // 1000 <= captured_at:
            return [_status_snapshot("stale_token", captured_at, meta)]
    except Exception:
        pass
    req = urllib.request.Request(
        CLAUDE_USAGE_URL,
        headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20", "Accept": "application/json"},
    )
    payload: dict[str, Any] | None = None
    try:
        for attempt in range(2):
            try:
                with opener(req, timeout=timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                break
            except HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504} or attempt == 1:
                    raise
                time.sleep(0.2)
    except HTTPError as exc:
        status = "stale_token" if exc.code in {401, 403} else "fetch_error"
        return [_status_snapshot(status, captured_at, {**meta, "error": f"HTTP {exc.code}: {exc.reason}"})]
    except Exception as exc:
        return [_status_snapshot("fetch_error", captured_at, {**meta, "error": str(exc)})]
    if payload is None:
        return [_status_snapshot("fetch_error", captured_at, {**meta, "error": "empty_response"})]
    limits = payload.get("limits") if isinstance(payload.get("limits"), list) else []
    out: list[QuotaSnapshot] = []
    for limit in limits:
        if not isinstance(limit, dict):
            continue
        # A single malformed entry should be skipped, never abort the whole fetch (which
        # would surface as a raw 500 on /api/quota/refresh instead of a fetch_error).
        try:
            used = _normalize_percent(limit.get("percent", limit.get("utilization")))
            if used is None:
                continue
            bucket, label = _label_for_limit(limit)
        except Exception:
            continue
        out.append(
            QuotaSnapshot(
                "claude",
                "default",
                bucket,
                label,
                used,
                _parse_time(limit.get("resets_at")),
                meta.get("plan"),
                captured_at,
                "claude_api",
                "ok",
                {"limit": limit},
            )
        )
    if out:
        return out
    for key, label in (("five_hour", "5-hour window"), ("seven_day", "7-day window")):
        obj = payload.get(key) if isinstance(payload.get(key), dict) else {}
        used = _normalize_percent(obj.get("utilization"))
        if used is not None:
            out.append(QuotaSnapshot("claude", "default", key, label, used, _parse_time(obj.get("resets_at")), meta.get("plan"), captured_at, "claude_api", "ok", {"limit": obj}))
    return out or [_status_snapshot("unavailable", captured_at, {**meta, "error": "no_limits"})]
