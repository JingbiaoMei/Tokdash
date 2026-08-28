from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
import urllib.request

from ... import clientpaths
from . import config as quota_config
from .codex import _parse_time
from .credential_sources import discover_external_credentials, endpoint_host_allowed
from .types import QuotaSnapshot

_QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
_ALLOWED_HOSTS = frozenset({"api.z.ai"})


@dataclass(frozen=True)
class _Credential:
    token: str
    source: str


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _zcode_credentials() -> list[_Credential]:
    path = clientpaths.zcode_home() / "v2" / "config.json"
    providers = _read_json(path).get("provider")
    if not isinstance(providers, dict):
        return []

    out: list[_Credential] = []
    for provider_id, provider in providers.items():
        if not isinstance(provider, dict) or provider.get("enabled") is False:
            continue
        options = provider.get("options") if isinstance(provider.get("options"), dict) else {}
        base_url = str(options.get("baseURL") or options.get("base_url") or "")
        normalized_url = base_url.lower().rstrip("/")
        is_coding_endpoint = normalized_url.startswith(
            ("https://api.z.ai/api/anthropic", "https://api.z.ai/api/coding/")
        )
        if "zai-coding-plan" not in str(provider_id).lower() and not is_coding_endpoint:
            continue
        token = str(options.get("apiKey") or options.get("api_key") or "").strip()
        if token:
            out.append(_Credential(token, f"{path}:provider.{provider_id}"))
    return out


def _credentials() -> list[_Credential]:
    out: list[_Credential] = []
    for name in ("ZAI_API_KEY", "Z_AI_API_KEY"):
        token = os.environ.get(name, "").strip()
        if token:
            out.append(_Credential(token, name))

    anthropic_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    anthropic_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    if anthropic_token and "api.z.ai" in anthropic_url.lower():
        out.append(_Credential(anthropic_token, "ANTHROPIC_AUTH_TOKEN"))

    out.extend(_zcode_credentials())
    if quota_config.credential_scan_enabled():
        for candidate in discover_external_credentials("zai"):
            out.append(_Credential(candidate.token, candidate.source_ref))

    deduped: list[_Credential] = []
    seen: set[str] = set()
    for credential in out:
        if credential.token not in seen:
            seen.add(credential.token)
            deduped.append(credential)
    return deduped


def _status_snapshot(
    status: str,
    captured_at: int,
    credential: _Credential | None,
    raw: dict[str, Any],
) -> QuotaSnapshot:
    return QuotaSnapshot(
        "zai",
        "default",
        "api",
        "Z.ai Coding Plan",
        None,
        None,
        None,
        captured_at,
        "zai_api",
        status,
        {"credential_source": credential.source if credential else None, **raw},
    )


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _used_percent(item: dict[str, Any]) -> float | None:
    percentage = _number(item.get("percentage"))
    if percentage is None:
        total = _number(item.get("usage"))
        current = _number(item.get("currentValue"))
        if current is None and total is not None:
            remaining = _number(item.get("remaining"))
            current = None if remaining is None else total - remaining
        if total is None or total <= 0 or current is None:
            return None
        percentage = current / total * 100.0
    return round(max(0.0, min(100.0, percentage)), 4)


def _bucket(item: dict[str, Any], index: int) -> tuple[str, str]:
    limit_type = str(item.get("type") or "").upper()
    unit = item.get("unit")
    if unit == 3 or limit_type == "TOKENS_LIMIT":
        return "5h", "5-hour window"
    if unit == 6:
        return "7d", "Weekly"
    if limit_type == "TIME_LIMIT":
        return "mcp_monthly", "MCP monthly"
    label = limit_type.replace("_", " ").title() or f"Limit {index + 1}"
    return f"limit_{index + 1}", label


def _snapshots_from_payload(payload: dict[str, Any], captured_at: int) -> list[QuotaSnapshot]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    limits = data.get("limits") if isinstance(data.get("limits"), list) else []
    plan = str(data.get("level") or data.get("planName") or data.get("plan_name") or "").strip()
    plan = plan.title() if plan else None

    out: list[QuotaSnapshot] = []
    for index, item in enumerate(limits):
        if not isinstance(item, dict):
            continue
        used_percent = _used_percent(item)
        if used_percent is None:
            continue
        bucket, label = _bucket(item, index)
        out.append(
            QuotaSnapshot(
                "zai",
                "default",
                bucket,
                label,
                used_percent,
                _parse_time(item.get("nextResetTime") or item.get("resetAt")),
                plan,
                captured_at,
                "zai_api",
                "ok",
                {"limit": item},
            )
        )
    return out


def collect_zai_api_snapshots(
    *,
    opener=urllib.request.urlopen,
    now: int | None = None,
    timeout: float = 15.0,
) -> list[QuotaSnapshot]:
    captured_at = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    credentials = _credentials()
    if not credentials:
        return [_status_snapshot("unavailable", captured_at, None, {"error": "credentials_not_found"})]
    if not endpoint_host_allowed(_QUOTA_URL, _ALLOWED_HOSTS, path_prefix="/api/monitor/usage/"):
        return [_status_snapshot("unavailable", captured_at, None, {"error": "untrusted_endpoint"})]

    failures: list[QuotaSnapshot] = []
    for credential in credentials:
        request = urllib.request.Request(
            _QUOTA_URL,
            headers={
                # Z.ai's official Coding Plan usage plugin sends the API key raw here.
                "Authorization": credential.token,
                "Accept-Language": "en-US,en",
                "Content-Type": "application/json",
                "User-Agent": "tokdash/zai-quota",
            },
        )
        try:
            with opener(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("response is not a JSON object")
            if payload.get("success") is False:
                code = payload.get("code")
                status = "stale_token" if code in {1001, 1002, 401, 403} else "fetch_error"
                failures.append(
                    _status_snapshot(
                        status,
                        captured_at,
                        credential,
                        {"error": str(payload.get("msg") or f"code_{code}")},
                    )
                )
                continue
            snapshots = _snapshots_from_payload(payload, captured_at)
            if snapshots:
                return snapshots
            failures.append(_status_snapshot("unavailable", captured_at, credential, {"error": "no_limits"}))
        except HTTPError as exc:
            status = "stale_token" if exc.code in {401, 403} else "fetch_error"
            failures.append(
                _status_snapshot(status, captured_at, credential, {"error": f"HTTP {exc.code}: {exc.reason}"})
            )
        except Exception as exc:
            failures.append(_status_snapshot("fetch_error", captured_at, credential, {"error": str(exc)}))
    return failures
