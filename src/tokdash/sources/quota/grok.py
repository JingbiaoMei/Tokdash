"""Grok Build quota and credit tracking provider.

Extracts OAuth credentials from local Grok CLI configuration files
and queries the Grok Build billing API to track credit usage, subscription
tiers, and billing period reset windows.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError
import urllib.request

from ... import clientpaths
from .codex import _parse_time
from .types import QuotaSnapshot


GROK_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"


def _read_auth() -> tuple[str | None, dict[str, Any]]:
    """Extract Grok CLI OAuth credentials from local storage.

    Reads authentication details from ``auth.json`` in the Grok CLI home
    directory (``~/.grok/auth.json`` via ``clientpaths.grok_home()``), filtering
    for xAI OIDC credentials where ``oidc_issuer`` is ``https://auth.x.ai``
    and ``auth_mode`` is ``oidc`` or ``external``. Plain API keys are rejected.

    Returns:
        A tuple of ``(token, metadata)`` if valid credentials are found,
        or ``(None, error_metadata)`` if credentials are missing or invalid.
        Metadata includes credential path, user ID, expiration timestamp,
        and authentication mode.
    """
    path = clientpaths.grok_home() / "auth.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, {"error": "credentials_not_found", "path": str(path)}
    except Exception as exc:
        return None, {"error": "credentials_invalid", "message": str(exc), "path": str(path)}
    if not isinstance(data, dict):
        return None, {"error": "credentials_invalid", "path": str(path)}

    candidates: list[dict[str, Any]] = []
    for value in data.values():
        if not isinstance(value, dict):
            continue
        issuer = str(value.get("oidc_issuer") or "")
        auth_mode = str(value.get("auth_mode") or "").lower()
        # Grok Build's own billing auth gate rejects plain API-key auth. Keep
        # that distinction here rather than sending a PAYG key to consumer billing.
        if issuer.rstrip("/") == "https://auth.x.ai" and auth_mode in {"oidc", "external"}:
            candidates.append(value)
    if not candidates:
        return None, {"error": "xai_oauth_not_found", "path": str(path)}
    auth = candidates[0]
    token = str(auth.get("key") or "").strip()
    if not token:
        return None, {"error": "access_token_not_found", "path": str(path)}
    return token, {
        "path": str(path),
        "user_id": str(auth.get("user_id") or ""),
        "expires_at": auth.get("expires_at"),
        "auth_mode": str(auth.get("auth_mode") or "").lower(),
    }


def _status_snapshot(status: str, captured_at: int, meta: dict[str, Any]) -> QuotaSnapshot:
    """Create a fallback or error QuotaSnapshot for Grok Build.

    Args:
        status: Status string indicating availability or error state
            (e.g., 'unavailable', 'stale_token', 'fetch_error').
        captured_at: Unix epoch timestamp in seconds when the snapshot was recorded.
        meta: Metadata dictionary containing path, error, and expiry details.

    Returns:
        A QuotaSnapshot instance with provider 'grok', account user_id or 'default',
        source 'grok_api', the specified status, and sanitized metadata.
    """
    safe = {
        key: meta.get(key)
        for key in ("path", "expires_at", "auth_mode", "error")
        if meta.get(key) is not None
    }
    return QuotaSnapshot(
        "grok", str(meta.get("user_id") or "default"), "api", "Grok Build billing",
        None, None, None, captured_at, "grok_api", status, safe,
    )


def _cent_value(value: Any) -> float | None:
    """Extract a numeric cent value from a scalar or dictionary wrapper.

    Args:
        value: Direct numeric value or mapping with 'val' or 'value' key.

    Returns:
        Extracted float value in cents, or None if conversion fails.
    """
    if isinstance(value, dict):
        value = value.get("val", value.get("value"))
    try:
        return float(value)
    except Exception:
        return None


def _billing_snapshot(payload: dict[str, Any], meta: dict[str, Any], captured_at: int) -> QuotaSnapshot | None:
    """Parse a Grok billing API response into a QuotaSnapshot.

    Handles proto3-JSON responses where zero-valued usage fields are omitted,
    determines the quota bucket ('7d' weekly, 'month' monthly, or 'credits'),
    and extracts subscription tier, reset timestamps, and raw metadata.

    Args:
        payload: JSON dictionary decoded from the Grok billing endpoint.
        meta: Credential and source metadata from ``_read_auth``.
        captured_at: Unix epoch timestamp in seconds when the snapshot was captured.

    Returns:
        A populated QuotaSnapshot with status 'ok', or None if payload structure
        does not match expected billing formats.
    """
    config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    period = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), dict) else {}
    period_type = str(period.get("type") or "")
    resets_at = _parse_time(period.get("end") or config.get("billingPeriodEnd"))
    period_start = _parse_time(period.get("start") or config.get("billingPeriodStart"))
    # A forward-moving currentPeriod is what marks a known `?format=credits` response
    # (verified against the live endpoint + xAI's own Grok CLI, mirrored by openusage).
    has_period = bool(period_type) and resets_at is not None and (period_start is None or resets_at > period_start)

    # The credits response is proto3-JSON: zero-valued fields are OMITTED. So an absent
    # creditUsagePercent means 0% used, NOT missing data — an idle week must still render a
    # 0% card, not vanish. A present-but-non-numeric value is real schema drift → give up.
    raw_percent = config.get("creditUsagePercent")
    if raw_percent is not None:
        try:
            used_percent = float(raw_percent)
        except (TypeError, ValueError):
            return None
    elif has_period:
        used_percent = 0.0
    else:
        # Not the credits shape — fall back to the plain /v1/billing monthly fields.
        limit = _cent_value(config.get("monthlyLimit"))
        used = _cent_value(config.get("used"))
        if limit is not None and used is not None and limit > 0:
            used_percent = used / limit * 100.0
        else:
            return None
    used_percent = round(max(0.0, min(100.0, used_percent)), 4)

    if "WEEKLY" in period_type:
        bucket, label = "7d", "Weekly"
    elif "MONTHLY" in period_type:
        bucket, label = "month", "Monthly"
    else:
        bucket, label = "credits", "Grok Build usage"
    plan = payload.get("subscriptionTier")
    raw = {
        "config": config,
        "onDemandEnabled": payload.get("onDemandEnabled"),
        "subscriptionTier": plan,
        "credential_path": meta.get("path"),
    }
    return QuotaSnapshot(
        "grok", str(meta.get("user_id") or "default"), bucket, label,
        used_percent, resets_at, str(plan) if plan else None,
        captured_at, "grok_api", "ok", raw,
    )


def collect_grok_api_snapshots(*, opener=urllib.request.urlopen, now: int | None = None, timeout: float = 15.0) -> list[QuotaSnapshot]:
    """Fetch current Grok Build quota and credit usage from the billing API.

    Queries ``GROK_BILLING_URL`` (``https://cli-chat-proxy.grok.com/v1/billing?format=credits``)
    using OAuth credentials discovered from the local Grok CLI configuration.
    Sets headers including Bearer authorization, ``X-XAI-Token-Auth: xai-grok-cli``,
    ``x-userid``, and client identification.

    Args:
        opener: Callable used to issue the HTTP request (defaults to ``urllib.request.urlopen``).
        now: Optional Unix epoch timestamp in seconds overriding current time.
        timeout: Network request timeout in seconds (defaults to 15.0).

    Returns:
        List containing a QuotaSnapshot with current usage and status 'ok',
        or a single error snapshot with status 'unavailable', 'stale_token',
        or 'fetch_error'.
    """
    captured_at = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    token, meta = _read_auth()
    if not token:
        return [_status_snapshot("unavailable", captured_at, meta)]
    expires_at = _parse_time(meta.get("expires_at"))
    if expires_at is not None and expires_at <= captured_at:
        return [_status_snapshot("stale_token", captured_at, {**meta, "error": "oauth_token_expired"})]
    user_id = str(meta.get("user_id") or "")
    if not user_id:
        return [_status_snapshot("unavailable", captured_at, {**meta, "error": "user_id_not_found"})]

    req = urllib.request.Request(
        GROK_BILLING_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "X-XAI-Token-Auth": "xai-grok-cli",
            "x-userid": user_id,
            "x-grok-client-version": "tokdash",
            "Accept": "application/json",
            "User-Agent": "Grok Build",
        },
    )
    try:
        with opener(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("response is not a JSON object")
        snapshot = _billing_snapshot(payload, meta, captured_at)
        return [snapshot] if snapshot is not None else [_status_snapshot("unavailable", captured_at, {**meta, "error": "no_usage"})]
    except HTTPError as exc:
        status = "stale_token" if exc.code in {401, 403} else "fetch_error"
        return [_status_snapshot(status, captured_at, {**meta, "error": f"HTTP {exc.code}: {exc.reason}"})]
    except Exception as exc:
        return [_status_snapshot("fetch_error", captured_at, {**meta, "error": str(exc)})]
