from __future__ import annotations

import ast
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

# Kimi Code coding-plan usage lives only here. Exact host + HTTPS + /coding path, checked
# before the bearer token is attached (a substring check would pass evil.example/api.kimi.com).
_ALLOWED_HOSTS = frozenset({"api.kimi.com"})


@dataclass(frozen=True)
class _Credential:
    token: str
    base_url: str
    source: str
    expires_at: int | None = None


def _unquote_toml(value: str) -> str:
    value = value.strip()
    try:
        parsed = ast.literal_eval(value)
    except Exception:
        return value.strip('"\'')
    return str(parsed) if isinstance(parsed, str) else ""


def _static_config_credentials(path: Path) -> list[_Credential]:
    """Read only Kimi provider blocks from config.toml without adding a TOML dependency."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    blocks: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = {} if line[1:-1].strip().startswith("providers.") else None
            if current is not None:
                blocks.append(current)
            continue
        if current is None or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in {"type", "base_url", "api_key"}:
            current[key] = _unquote_toml(value.split("#", 1)[0])
    out: list[_Credential] = []
    for block in blocks:
        base_url = block.get("base_url", "").rstrip("/")
        token = block.get("api_key", "").strip()
        if token and (block.get("type") == "kimi" or "api.kimi.com/coding" in base_url):
            out.append(_Credential(token, base_url or "https://api.kimi.com/coding/v1", str(path)))
    return out


def _oauth_credential(root: Path) -> _Credential | None:
    path = root / "credentials" / "kimi-code.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    token = str(data.get("access_token") or "").strip()
    if not token:
        return None
    return _Credential(token, "https://api.kimi.com/coding/v1", str(path), _parse_time(data.get("expires_at")))


def _credentials() -> list[_Credential]:
    out: list[_Credential] = []
    env_token = os.environ.get("KIMI_API_KEY", "").strip()
    if env_token:
        base_url = os.environ.get("KIMI_BASE_URL", "https://api.kimi.com/coding/v1").rstrip("/")
        out.append(_Credential(env_token, base_url, "KIMI_API_KEY"))
    for root in clientpaths.kimi_roots():
        out.extend(_static_config_credentials(root / "config.toml"))
        credential = _oauth_credential(root)
        if credential is not None:
            out.append(credential)
    if quota_config.credential_scan_enabled():
        for candidate in discover_external_credentials("kimi"):
            out.append(_Credential(candidate.token, candidate.base_url, candidate.source_ref))

    deduped: list[_Credential] = []
    seen: set[tuple[str, str]] = set()
    for credential in out:
        key = (credential.token, credential.base_url)
        if key not in seen:
            seen.add(key)
            deduped.append(credential)
    return deduped


def _status_snapshot(status: str, captured_at: int, credential: _Credential | None, raw: dict[str, Any]) -> QuotaSnapshot:
    return QuotaSnapshot(
        "kimi", "default", "api", "Kimi Code API", None, None, None,
        captured_at, "kimi_api", status,
        {"credential_source": credential.source if credential else None, **raw},
    )


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _usage_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    return f"{base}/v1/usages" if base.endswith("/coding") else f"{base}/usages"


def _window_bucket(item: dict[str, Any], index: int) -> tuple[str, str]:
    window = item.get("window") if isinstance(item.get("window"), dict) else {}
    duration = _number(window.get("duration") or item.get("duration"))
    unit = str(window.get("timeUnit") or window.get("time_unit") or item.get("timeUnit") or "").upper()
    seconds: float | None = None
    if duration is not None:
        if "MINUTE" in unit:
            seconds = duration * 60
        elif "HOUR" in unit:
            seconds = duration * 3600
        elif "DAY" in unit:
            seconds = duration * 86400
        elif "SECOND" in unit or not unit:
            seconds = duration
    if seconds is not None and 4 * 3600 <= seconds <= 6 * 3600:
        return "5h", "5-hour window"
    if seconds is not None and 6 * 86400 <= seconds <= 8 * 86400:
        return "7d", "Weekly"
    return f"limit_{index + 1}", str(item.get("name") or item.get("title") or f"Limit {index + 1}")


def _usage_snapshot(detail: dict[str, Any], bucket: str, label: str, captured_at: int, plan: str | None) -> QuotaSnapshot | None:
    limit = _number(detail.get("limit"))
    if limit is None or limit <= 0:
        return None
    used = _number(detail.get("used"))
    if used is None:
        remaining = _number(detail.get("remaining"))
        if remaining is None:
            return None
        used = limit - remaining
    used_percent = round(max(0.0, min(100.0, used / limit * 100.0)), 4)
    reset = next((detail.get(key) for key in ("reset_at", "resetAt", "reset_time", "resetTime") if detail.get(key)), None)
    return QuotaSnapshot(
        "kimi", "default", bucket, label, used_percent, _parse_time(reset), plan,
        captured_at, "kimi_api", "ok", {"detail": detail},
    )


def _snapshots_from_payload(payload: dict[str, Any], captured_at: int) -> list[QuotaSnapshot]:
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    membership = user.get("membership") if isinstance(user.get("membership"), dict) else {}
    plan_value = membership.get("level") or membership.get("name")
    plan = str(plan_value).removeprefix("LEVEL_").replace("_", " ").title() if plan_value else None

    out: list[QuotaSnapshot] = []
    seen: set[tuple[str, float, int | None]] = set()
    limits = payload.get("limits") if isinstance(payload.get("limits"), list) else []
    for index, item in enumerate(limits):
        if not isinstance(item, dict):
            continue
        detail = item.get("detail") if isinstance(item.get("detail"), dict) else item
        bucket, label = _window_bucket(item, index)
        snapshot = _usage_snapshot(detail, bucket, label, captured_at, plan)
        if snapshot is not None:
            key = (snapshot.bucket, float(snapshot.used_percent or 0), snapshot.resets_at)
            if key not in seen:
                seen.add(key)
                out.append(snapshot)
    # The top-level `usage` object carries no window/duration field, so its period
    # is unknown (observed live resetting the same day, i.e. NOT weekly). Label it
    # neutrally rather than asserting a period the payload does not state. Some plans
    # echo one of the `limits` windows here; dedup on (used%, reset) across buckets so
    # that echo collapses, while a genuinely distinct usage window still surfaces.
    seen_window = {(float(s.used_percent or 0), s.resets_at) for s in out}
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    snapshot = _usage_snapshot(usage, "plan", "Plan usage", captured_at, plan) if usage else None
    if snapshot is not None and (float(snapshot.used_percent or 0), snapshot.resets_at) not in seen_window:
        out.append(snapshot)
    return out


def collect_kimi_api_snapshots(*, opener=urllib.request.urlopen, now: int | None = None, timeout: float = 15.0) -> list[QuotaSnapshot]:
    captured_at = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    credentials = _credentials()
    if not credentials:
        return [_status_snapshot("unavailable", captured_at, None, {"error": "credentials_not_found"})]

    failures: list[QuotaSnapshot] = []
    for credential in credentials:
        if credential.expires_at is not None and credential.expires_at <= captured_at:
            failures.append(_status_snapshot("stale_token", captured_at, credential, {"error": "oauth_token_expired"}))
            continue
        usage_url = _usage_url(credential.base_url)
        if not endpoint_host_allowed(usage_url, _ALLOWED_HOSTS, path_prefix="/coding"):
            failures.append(_status_snapshot("unavailable", captured_at, credential, {"error": "not_kimi_code_endpoint"}))
            continue
        req = urllib.request.Request(
            usage_url,
            headers={"Authorization": f"Bearer {credential.token}", "Accept": "application/json", "User-Agent": "tokdash/kimi-quota"},
        )
        try:
            with opener(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("response is not a JSON object")
            snapshots = _snapshots_from_payload(payload, captured_at)
            if snapshots:
                return snapshots
            failures.append(_status_snapshot("unavailable", captured_at, credential, {"error": "no_limits"}))
        except HTTPError as exc:
            status = "stale_token" if exc.code in {401, 403} else "fetch_error"
            failures.append(_status_snapshot(status, captured_at, credential, {"error": f"HTTP {exc.code}: {exc.reason}"}))
        except Exception as exc:
            failures.append(_status_snapshot("fetch_error", captured_at, credential, {"error": str(exc)}))
    return failures
