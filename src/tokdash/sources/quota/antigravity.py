from __future__ import annotations

import base64
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError
import urllib.request
import time

from ... import clientpaths
from .codex import _parse_time
from .types import QuotaSnapshot

BASE_URL = "https://daily-cloudcode-pa.googleapis.com"
_USER_AGENT = f"antigravity/1.0.0 {platform.system().lower()}/{platform.machine().lower()}"


_SAFE_TOKEN_META_KEYS = ("email", "auth_method", "expiry", "expires_at", "expiry_date")
# agy CLI (Go) stores the oauth blob via zalando/go-keyring. Observed 2026-09-24:
# service ``gemini``, account ``antigravity``, payload ``go-keyring-base64:`` + JSON.
KEYCHAIN_SERVICE = "gemini"
KEYCHAIN_ACCOUNT = "antigravity"
_KEYCHAIN_LABEL = "macOS Keychain (gemini/antigravity)"
_GO_KEYRING_PREFIX = "go-keyring-base64:"


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _jwt_payload(token: Any) -> dict[str, Any]:
    """Decode unverified identity claims used only for the local account label."""
    if not isinstance(token, str):
        return {}
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _safe_token_meta(data: dict[str, Any], path: str) -> dict[str, Any]:
    meta: dict[str, Any] = {"path": path}
    # An OAuth blob may gain new credential fields. Copy only the account and
    # expiry fields we deliberately expose in failure snapshots.
    for key in _SAFE_TOKEN_META_KEYS:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, (str, int, float, bool)) or value is None:
            meta[key] = value
    token_obj = data.get("token") if isinstance(data.get("token"), dict) else {}
    for key in ("expiry", "expires_at", "expiry_date"):
        value = token_obj.get(key)
        if key in token_obj and (isinstance(value, (str, int, float)) or value is None):
            meta[key] = value
    if not meta.get("email"):
        email = _jwt_payload(data.get("id_token")).get("email")
        if isinstance(email, str) and email:
            meta["email"] = email
    return meta


def _parse_token_text(text: str, source: str) -> tuple[str | None, dict[str, Any]]:
    """Parse a file or Keychain blob into (access_token, redacted meta)."""
    text = (text or "").strip()
    if text.startswith(_GO_KEYRING_PREFIX):
        try:
            text = base64.b64decode(text[len(_GO_KEYRING_PREFIX) :]).decode("utf-8")
        except Exception as exc:
            return None, {"error": "token_invalid", "message": str(exc), "path": source}
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        return text or None, {"path": source}
    token_obj = data.get("token") if isinstance(data.get("token"), dict) else {}
    token = data.get("access_token") or token_obj.get("access_token")
    return str(token) if token else None, _safe_token_meta(data, source)


def _read_keychain_blob(keychain: str | None = None) -> str | None:
    """Read the agy CLI oauth blob from the macOS Keychain.

    Read-only, via ``security`` with an argument list (never a shell). Returns
    ``None`` off-macOS, when the item is missing, or when the keychain is locked.
    The first read from a new binary may show a one-time permission prompt; the
    timeout keeps an unanswered prompt from wedging a poll cycle.
    """
    if not _is_macos():
        return None
    cmd = [
        "security",
        "find-generic-password",
        "-s",
        KEYCHAIN_SERVICE,
        "-a",
        KEYCHAIN_ACCOUNT,
        "-w",
    ]
    if keychain:
        cmd.append(keychain)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    text = (result.stdout or "").strip()
    return text or None


def _read_token() -> tuple[str | None, dict[str, Any]]:
    # Verified against the shipped AGY 1.2.3 binary on 2026-09-15. Keep the
    # original product-home path for installs that predate the Jetski rename.
    # Current agy CLI on macOS often has no file at either path and keeps the
    # same JSON in the login Keychain instead (go-keyring, service=gemini).
    paths = clientpaths.antigravity_oauth_token_paths()
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8").strip()
            break
        except FileNotFoundError:
            continue
        except Exception as exc:
            return None, {"error": "token_invalid", "message": str(exc), "path": str(path)}
    else:
        blob = _read_keychain_blob()
        if blob:
            return _parse_token_text(blob, _KEYCHAIN_LABEL)
        return None, {
            "error": "token_not_found",
            "path": str(paths[0]),
            "legacy_path": str(paths[1]),
        }
    return _parse_token_text(text, str(path))


def _status_snapshot(status: str, captured_at: int, raw: dict[str, Any]) -> QuotaSnapshot:
    return QuotaSnapshot("antigravity", str(raw.get("email") or "default"), "api", "Antigravity API", None, None, None, captured_at, "antigravity_api", status, raw)


def _post_json(url: str, token: str, payload: dict[str, Any], opener, timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Probed 2026-07-02: the v1internal endpoints return HTTP 403 for requests
            # without an antigravity-style User-Agent (Python-urllib default is rejected).
            "User-Agent": _USER_AGENT,
        },
        method="POST",
    )
    last_error: HTTPError | None = None
    for attempt in range(2):
        try:
            with opener(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except HTTPError as exc:
            last_error = exc
            if exc.code not in {500, 502, 503, 504} or attempt == 1:
                raise
            time.sleep(0.2)
    else:
        assert last_error is not None
        raise last_error
    return data if isinstance(data, dict) else {}


def collect_antigravity_api_snapshots(
    *,
    opener=urllib.request.urlopen,
    now: int | None = None,
    timeout: float = 15.0,
) -> list[QuotaSnapshot]:
    captured_at = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    token, meta = _read_token()
    if not token:
        return [_status_snapshot("unavailable", captured_at, meta)]
    try:
        assist = _post_json(f"{BASE_URL}/v1internal:loadCodeAssist", token, {}, opener, timeout)
        # Probed 2026-07-02: the project id arrives as "cloudaicompanionProject" and the
        # models call requires it under the "project" key — {} or "projectId" gets HTTP 403.
        project_id = (
            assist.get("cloudaicompanionProject") or assist.get("projectId") or assist.get("project_id")
        )
        models = _post_json(
            f"{BASE_URL}/v1internal:fetchAvailableModels",
            token,
            {"project": project_id} if project_id else {},
            opener,
            timeout,
        )
    except HTTPError as exc:
        status = "stale_token" if exc.code in {401, 403} else "fetch_error"
        return [_status_snapshot(status, captured_at, {**meta, "error": f"HTTP {exc.code}: {exc.reason}"})]
    except Exception as exc:
        return [_status_snapshot("fetch_error", captured_at, {**meta, "error": str(exc)})]

    account = str(meta.get("email") or "default")
    items = _model_items(models)
    out: list[QuotaSnapshot] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        quota = item.get("quotaInfo") if isinstance(item.get("quotaInfo"), dict) else item.get("quota_info")
        # Guard the snake_case fallback too: if quota_info is present but not a dict (schema
        # drift), fall back to {} so the .get calls below don't raise AttributeError. This
        # used to be swallowed by a broad `except Exception` before the reset-time hoist.
        if not isinstance(quota, dict):
            quota = {}
        reset = _parse_time(quota.get("resetTime") or quota.get("reset_time"))
        try:
            remaining = float(quota.get("remainingFraction"))
        except (TypeError, ValueError):
            remaining = None
        if remaining is None:
            # remainingFraction is null/absent. With a resetTime this is an exhausted
            # window (e.g. the weekly limit is hit, resetting at `reset`) - treat it as
            # 0% remaining so the pool still surfaces the binding window instead of going
            # stale. Without a resetTime the model carries no quota signal, so skip it.
            if reset is None:
                continue
            remaining = 0.0
        out.append(
            QuotaSnapshot(
                "antigravity",
                account,
                str(item.get("name") or item.get("model") or "model"),
                str(item.get("displayName") or item.get("display_name") or item.get("name") or "Model"),
                round((1.0 - remaining) * 100.0, 4),
                reset,
                None,
                captured_at,
                "antigravity_api",
                "ok",
                {"model": item},
            )
        )
    return out or [_status_snapshot("unavailable", captured_at, {**meta, "error": "no_models"})]


def _model_items(models: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("models", "availableModels"):
        raw = models.get(key)
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        if isinstance(raw, dict):
            # Dict-shaped responses key items by the stable model id; keep it as the
            # bucket id when the item itself carries no "name".
            return [
                {**item, "name": item.get("name") or model_key}
                for model_key, item in raw.items()
                if isinstance(item, dict)
            ]
    return []
