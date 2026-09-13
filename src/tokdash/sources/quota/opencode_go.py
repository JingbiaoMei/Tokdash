from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError
import urllib.request

from ... import clientpaths
from .codex import _parse_time
from .types import QuotaSnapshot

PROVIDER = "opencode_go"
SOURCE = "opencode_go_api"
PLAN = "Go"

# Single fixed endpoint (port of tokscale
# crates/tokscale-cli/src/commands/usage/opencode_go.rs): no per-user base URL,
# no workspace id, no cookies — so no allowlist check is needed before attaching
# the bearer token.
USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
_TIMEOUT_SECONDS = 30.0
_MAX_BODY_BYTES = 1024 * 1024

_AUTH_KEYS = ("opencode-go", "opencode_go")
_ENV_VAR = "OPENCODE_API_KEY"

_WINDOWS = (
    ("rolling", "Rolling (5h)"),
    ("weekly", "Weekly"),
    ("monthly", "Monthly"),
)


@dataclass(frozen=True)
class _Credential:
    token: str
    source: str


def _auth_file_key() -> _Credential | None:
    # Deliberately permissive: auth.json is user- and tool-edited JSON, so the
    # Go key is accepted under both spellings and with any of the token field
    # names OpenCode-compatible tools write. Only these two key names are ever
    # read, so a non-Go credential under another name can never be picked up;
    # a wrong token here fails closed as 401 stale_token, never as bad data.
    try:
        data = json.loads(clientpaths.opencode_auth_path().read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    for auth_key in _AUTH_KEYS:
        entry = data.get(auth_key)
        if isinstance(entry, dict) and str(entry.get("type") or "").strip().lower() == "oauth":
            continue
        token = entry.strip() if isinstance(entry, str) else _entry_token(entry)
        if token:
            return _Credential(token, f"opencode auth.json:{auth_key}")
    return None


def _entry_token(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ""
    for key in ("key", "apiKey", "api_key", "token"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def read_opencode_go_key() -> _Credential | None:
    """Read OPENCODE_API_KEY first, falling back to OpenCode's auth.json."""
    env_token = os.environ.get(_ENV_VAR, "").strip()
    if env_token:
        return _Credential(env_token, _ENV_VAR)
    return _auth_file_key()


def has_credentials() -> bool:
    return read_opencode_go_key() is not None


def _status_snapshot(status: str, captured_at: int, credential: _Credential | None, raw: dict[str, Any]) -> QuotaSnapshot:
    return QuotaSnapshot(
        PROVIDER, "default", "api", "OpenCode Go API", None, None, None,
        captured_at, SOURCE, status,
        {"credential_source": credential.source if credential else None, **raw},
    )


def _window_resets_at(window: dict[str, Any], captured_at: int) -> int | None:
    resets_at = _parse_time(window.get("resetsAt"))
    if resets_at is not None:
        return resets_at
    for key in ("resetInSec", "resetsInSeconds", "reset_in_sec"):
        try:
            offset = int(float(window.get(key)))
        except (TypeError, ValueError):
            continue
        if offset >= 0:
            return captured_at + offset
    return None


def _window_snapshot(window: dict[str, Any], bucket: str, label: str, captured_at: int, credential: _Credential) -> QuotaSnapshot:
    try:
        used = float(window.get("percent") or 0.0)
    except (TypeError, ValueError):
        used = 0.0
    return QuotaSnapshot(
        PROVIDER, "default", bucket, label, round(max(0.0, min(100.0, used)), 4),
        _window_resets_at(window, captured_at), PLAN, captured_at, SOURCE, "ok",
        {"credential_source": credential.source, "window": window},
    )


def _snapshots_from_payload(payload: dict[str, Any], captured_at: int, credential: _Credential) -> list[QuotaSnapshot]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return []
    return [
        _window_snapshot(window, bucket, label, captured_at, credential)
        for bucket, label in _WINDOWS
        if isinstance((window := usage.get(bucket)), dict)
    ]


def _is_entitlement_error(payload: Any) -> bool:
    try:
        return str(payload.get("error", {}).get("type") or "") == "EntitlementError"
    except AttributeError:
        return False


def _read_capped(resp: Any) -> bytes:
    data = resp.read(_MAX_BODY_BYTES + 1)
    if len(data) > _MAX_BODY_BYTES:
        raise ValueError(f"response exceeds {_MAX_BODY_BYTES} byte cap")
    return data


def collect_opencode_go_api_snapshots(*, opener=urllib.request.urlopen, now: int | None = None, timeout: float = _TIMEOUT_SECONDS) -> list[QuotaSnapshot]:
    captured_at = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    credential = read_opencode_go_key()
    if credential is None:
        return [_status_snapshot("unavailable", captured_at, None, {"error": "credentials_not_found"})]

    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {credential.token}",
            "Accept": "application/json",
            "User-Agent": "tokdash/opencode-go-quota",
        },
    )
    try:
        with opener(req, timeout=timeout) as resp:
            payload = json.loads(_read_capped(resp).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("response is not a JSON object")
        snapshots = _snapshots_from_payload(payload, captured_at, credential)
        if snapshots:
            return snapshots
        return [_status_snapshot("unavailable", captured_at, credential, {"error": "no_windows"})]
    except HTTPError as exc:
        try:
            body = exc.read(_MAX_BODY_BYTES + 1)[:_MAX_BODY_BYTES].decode("utf-8", "replace")
        except Exception:
            body = ""
        try:
            error_payload = json.loads(body) if body else None
        except ValueError:
            error_payload = None
        if exc.code == 403 and _is_entitlement_error(error_payload):
            # Record entitlement loss so an older successful reading cannot keep
            # the card healthy. The stored windows remain available as history.
            return [_status_snapshot("unavailable", captured_at, credential, {"error": "subscription_required"})]
        detail = ""
        if isinstance(error_payload, dict):
            err = error_payload.get("error")
            detail = str((err.get("message") if isinstance(err, dict) else err) or "").strip()
        detail = detail or body.strip()[:200]
        if exc.code == 401:
            return [_status_snapshot("stale_token", captured_at, credential, {
                "error": f"HTTP 401: {detail}",
                "hint": "Run '/connect' in OpenCode to refresh, or check OPENCODE_API_KEY.",
            })]
        status = "stale_token" if exc.code == 403 else "fetch_error"
        return [_status_snapshot(status, captured_at, credential, {"error": f"HTTP {exc.code}: {detail}"[:220]})]
    except Exception as exc:
        return [_status_snapshot("fetch_error", captured_at, credential, {"error": str(exc)[:220]})]
