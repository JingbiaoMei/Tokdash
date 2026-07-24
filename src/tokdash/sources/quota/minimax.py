from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError
import urllib.request

from ... import clientpaths
from . import config as quota_config
from .codex import _parse_time
from .credential_sources import discover_external_credentials, endpoint_host_allowed
from .types import QuotaSnapshot

# MiniMax's own global + mainland-China hosts. Exact host + HTTPS is required before the
# bearer token is attached, so a crafted/misconfigured base_url can't exfiltrate it.
_ALLOWED_HOSTS = frozenset({"api.minimax.io", "api.minimaxi.com", "www.minimax.io", "www.minimaxi.com"})


@dataclass(frozen=True)
class _Credential:
    token: str
    region: str
    base_url: str
    source: str
    expires_at: int | None = None


_REGION_BASE_URLS = {
    "global": "https://api.minimax.io",
    "cn": "https://api.minimaxi.com",
}


def _config_path():
    return clientpaths.minimax_cli_root() / "config.json"


def _read_config() -> dict[str, Any]:
    try:
        data = json.loads(_config_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _oauth_expiry(value: Any) -> int | None:
    return _parse_time(value)


def _credentials() -> list[_Credential]:
    """Resolve static Token Plan keys first, then the mmx CLI credential.

    MiniMax runs separate global and mainland-China backends. Region-specific
    environment variables may therefore intentionally produce two accounts.
    The generic ``MINIMAX_API_KEY`` follows mmx's selected/configured region.
    """
    cfg = _read_config()
    configured_region = str(os.environ.get("MINIMAX_REGION") or cfg.get("region") or "global").lower()
    if configured_region not in _REGION_BASE_URLS:
        configured_region = "global"

    out: list[_Credential] = []
    for region, env_name in (
        ("global", "MINIMAX_TOKEN_PLAN_GLOBAL_KEY"),
        ("cn", "MINIMAX_TOKEN_PLAN_CN_KEY"),
    ):
        token = os.environ.get(env_name, "").strip()
        if token:
            out.append(_Credential(token, region, _REGION_BASE_URLS[region], env_name))

    generic = os.environ.get("MINIMAX_API_KEY", "").strip()
    if generic:
        out.append(
            _Credential(
                generic,
                configured_region,
                str(os.environ.get("MINIMAX_BASE_URL") or cfg.get("base_url") or _REGION_BASE_URLS[configured_region]).rstrip("/"),
                "MINIMAX_API_KEY",
            )
        )

    # Match mmx's file precedence: OAuth before the config-file API key.
    oauth = cfg.get("oauth") if isinstance(cfg.get("oauth"), dict) else {}
    access_token = str(oauth.get("access_token") or "").strip()
    if access_token:
        region = str(oauth.get("region") or configured_region).lower()
        if region not in _REGION_BASE_URLS:
            region = configured_region
        out.append(
            _Credential(
                access_token,
                region,
                str(oauth.get("resource_url") or cfg.get("base_url") or _REGION_BASE_URLS[region]).rstrip("/"),
                str(_config_path()),
                _oauth_expiry(oauth.get("expires_at")),
            )
        )
    else:
        token = str(cfg.get("api_key") or "").strip()
        if token:
            out.append(
                _Credential(
                    token,
                    configured_region,
                    str(cfg.get("base_url") or _REGION_BASE_URLS[configured_region]).rstrip("/"),
                    str(_config_path()),
                )
            )

    if quota_config.credential_scan_enabled():
        for candidate in discover_external_credentials("minimax"):
            out.append(
                _Credential(
                    candidate.token,
                    candidate.region or configured_region,
                    candidate.base_url,
                    candidate.source_ref,
                )
            )

    deduped: list[_Credential] = []
    seen: set[tuple[str, str]] = set()
    for credential in out:
        key = (credential.token, credential.base_url)
        if key not in seen:
            seen.add(key)
            deduped.append(credential)
    return deduped


def _status_snapshot(status: str, captured_at: int, credential: _Credential | None, raw: dict[str, Any]) -> QuotaSnapshot:
    region = credential.region if credential else "default"
    meta = {
        "region": region,
        "credential_source": credential.source if credential else None,
        **raw,
    }
    return QuotaSnapshot(
        "minimax", region, "api", "MiniMax Token Plan", None, None, None,
        captured_at, "minimax_api", status, meta,
    )


def _percent(remaining: Any, used: Any, total: Any) -> float | None:
    try:
        total_f = float(total)
        used_f = float(used)
        if total_f > 0:
            return round(max(0.0, min(100.0, used_f / total_f * 100.0)), 4)
    except Exception:
        pass
    try:
        if remaining is not None:
            # The official MiniMax CLI defines this field as a 0–100 remaining
            # percentage. Counts remain the preferred source above because boosted
            # weekly plans can render more than 100% remaining.
            return round(100.0 - max(0.0, min(100.0, float(remaining))), 4)
    except Exception:
        pass
    return None


def _quota_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    return f"{base}/token_plan/remains" if base.endswith("/v1") else f"{base}/v1/token_plan/remains"


def _snapshots_from_payload(payload: dict[str, Any], credential: _Credential, captured_at: int) -> list[QuotaSnapshot]:
    remains = payload.get("model_remains")
    if not isinstance(remains, list):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        remains = data.get("model_remains")
    if not isinstance(remains, list):
        return []

    plan = payload.get("plan_name") or payload.get("current_subscribe_title")
    out: list[QuotaSnapshot] = []
    for item in remains:
        if not isinstance(item, dict):
            continue
        name = str(item.get("model_name") or "general")
        label = name.replace("_", " ").strip().title() or "General"

        if item.get("current_interval_status") != 3:
            used = _percent(
                item.get("current_interval_remaining_percent"),
                item.get("current_interval_usage_count"),
                item.get("current_interval_total_count"),
            )
            if used is not None:
                out.append(
                    QuotaSnapshot(
                        "minimax", credential.region, f"{credential.region}_{name}_5h",
                        f"{label} · 5-hour",
                        used, _parse_time(item.get("end_time")), str(plan) if plan else None,
                        captured_at, "minimax_api", "ok", {"model_remain": item, "region": credential.region},
                    )
                )

        if item.get("current_weekly_status") != 3:
            used = _percent(
                item.get("current_weekly_remaining_percent"),
                item.get("current_weekly_usage_count"),
                item.get("current_weekly_total_count"),
            )
            if used is not None:
                out.append(
                    QuotaSnapshot(
                        "minimax", credential.region, f"{credential.region}_{name}_7d",
                        f"{label} · Weekly",
                        used, _parse_time(item.get("weekly_end_time")), str(plan) if plan else None,
                        captured_at, "minimax_api", "ok", {"model_remain": item, "region": credential.region},
                    )
                )
    return out


def collect_minimax_api_snapshots(*, opener=urllib.request.urlopen, now: int | None = None, timeout: float = 15.0) -> list[QuotaSnapshot]:
    captured_at = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    credentials = _credentials()
    if not credentials:
        return [_status_snapshot("unavailable", captured_at, None, {"error": "credentials_not_found"})]

    out: list[QuotaSnapshot] = []
    for credential in credentials:
        if credential.expires_at is not None and credential.expires_at <= captured_at:
            out.append(_status_snapshot("stale_token", captured_at, credential, {"error": "oauth_token_expired"}))
            continue
        url = _quota_url(credential.base_url)
        if not endpoint_host_allowed(url, _ALLOWED_HOSTS):
            out.append(_status_snapshot("unavailable", captured_at, credential, {"error": "untrusted_endpoint"}))
            continue
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {credential.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "tokdash/minimax-quota",
            },
        )
        try:
            with opener(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("response is not a JSON object")
            base_resp = payload.get("base_resp") if isinstance(payload.get("base_resp"), dict) else {}
            status_code = int(base_resp.get("status_code") or 0)
            if status_code != 0:
                status = "stale_token" if status_code == 1004 else "fetch_error"
                out.append(_status_snapshot(status, captured_at, credential, {"error": base_resp.get("status_msg") or f"status_code_{status_code}"}))
                continue
            snapshots = _snapshots_from_payload(payload, credential, captured_at)
            out.extend(snapshots or [_status_snapshot("unavailable", captured_at, credential, {"error": "no_limits"})])
        except HTTPError as exc:
            status = "stale_token" if exc.code in {401, 403} else "fetch_error"
            out.append(_status_snapshot(status, captured_at, credential, {"error": f"HTTP {exc.code}: {exc.reason}"}))
        except Exception as exc:
            out.append(_status_snapshot("fetch_error", captured_at, credential, {"error": str(exc)}))
    return out
