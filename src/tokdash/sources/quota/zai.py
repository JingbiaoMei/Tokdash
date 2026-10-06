"""Z.ai Coding Plan quota tracking provider.

Extracts Z.ai (GLM / BigModel) credentials from environment variables,
ZCode CLI configurations, Anthropic proxy environment settings, and
credential discovery sources. Queries the Z.ai quota limit monitor API
to track 5-hour, weekly, and monthly MCP quota limits.
"""

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
from .credential_sources import discover_external_credentials, endpoint_host_allowed, zai_coding_base_url_allowed
from .types import QuotaSnapshot

_QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
_ALLOWED_HOSTS = frozenset({"api.z.ai"})


@dataclass(frozen=True)
class _Credential:
    """Credential container for Z.ai API authentication.

    Attributes:
        token: API key token used for authorization.
        source: Origin or configuration file reference for the token.
    """

    token: str
    source: str


def _read_json(path: Path) -> dict[str, Any]:
    """Read and parse a JSON file from disk into a dictionary.

    Args:
        path: Path to the JSON file to read.

    Returns:
        Parsed JSON dictionary, or an empty dictionary on read or decode errors.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _zcode_credentials() -> list[_Credential]:
    """Extract Z.ai credentials from the local ZCode configuration file.

    Reads ``~/.zcode/v2/config.json`` (via ``clientpaths.zcode_home()``)
    and inspects enabled providers targeting Z.ai coding endpoints
    (``https://api.z.ai/api/anthropic`` or ``https://api.z.ai/api/coding/``)
    or provider IDs matching ``zai-coding-plan``.

    Returns:
        List of discovered ``_Credential`` objects.
    """
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
    """Discover and deduplicate all available Z.ai credentials.

    Checks environment variables (``ZAI_API_KEY``, ``Z_AI_API_KEY``, and
    ``ANTHROPIC_AUTH_TOKEN`` if ``ANTHROPIC_BASE_URL`` points to Z.ai),
    local ZCode configuration files, and external credential scan paths.

    Returns:
        List of unique ``_Credential`` instances deduplicated by token string.
    """
    out: list[_Credential] = []
    for name in ("ZAI_API_KEY", "Z_AI_API_KEY"):
        token = os.environ.get(name, "").strip()
        if token:
            out.append(_Credential(token, name))

    anthropic_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    anthropic_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    if anthropic_token and zai_coding_base_url_allowed(anthropic_url):
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
    """Create a fallback or error QuotaSnapshot for Z.ai.

    Args:
        status: Status code indicating the state (e.g., 'unavailable',
            'stale_token', 'fetch_error').
        captured_at: Unix epoch timestamp in seconds when the snapshot was recorded.
        credential: The credential instance that produced this status, if any.
        raw: Extra metadata and error details to record in the snapshot.

    Returns:
        A QuotaSnapshot initialized with provider 'zai', label 'Z.ai Coding Plan',
        source 'zai_api', and the given status and metadata.
    """
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
    """Safely convert a value to a float.

    Args:
        value: Any input value to parse as float.

    Returns:
        Parsed float value, or None if value cannot be converted.
    """
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _used_percent(item: dict[str, Any]) -> float | None:
    """Calculate the percentage of quota consumed from a limit item.

    Reads explicit 'percentage' field or computes ``(usage - remaining) / usage * 100``
    or ``currentValue / usage * 100``. Clamps the result between 0.0 and 100.0%.

    Args:
        item: Dictionary containing usage and limit values for a quota window.

    Returns:
        Used quota percentage rounded to 4 decimal places, or None if undetermined.
    """
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
    """Determine the bucket ID and display label for a limit item.

    Maps limit types and units to standard Tokdash bucket identifiers:
    unit 3 or TOKENS_LIMIT maps to ('5h', '5-hour window'), unit 6 maps to
    ('7d', 'Weekly'), and TIME_LIMIT maps to ('mcp_monthly', 'MCP monthly').

    Args:
        item: Dictionary representing a limit item in the response payload.
        index: Zero-based positional index of the item within the limits list.

    Returns:
        Tuple of ``(bucket_id, bucket_label)``.
    """
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
    """Parse a Z.ai quota limit response payload into a list of QuotaSnapshot objects.

    Extracts limits array, subscription plan level, used percentages, and reset
    timestamps (``nextResetTime`` / ``resetAt``) from the API response payload.

    Args:
        payload: Decoded JSON response dictionary from the Z.ai quota API.
        captured_at: Unix epoch timestamp in seconds when the snapshot was captured.

    Returns:
        List of QuotaSnapshot objects representing each active quota limit window.
    """
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
    """Collect quota snapshots from the Z.ai monitor API.

    Queries ``_QUOTA_URL`` (``https://api.z.ai/api/monitor/usage/quota/limit``)
    using discovered credentials. Validates the endpoint host against allowed
    hosts, attaches the API token via the Authorization header, and iterates
    through available credentials until active limits are retrieved.

    Args:
        opener: Callable used to issue the HTTP request (defaults to ``urllib.request.urlopen``).
        now: Optional Unix epoch timestamp in seconds overriding current time.
        timeout: Request timeout in seconds (defaults to 15.0).

    Returns:
        List of QuotaSnapshot objects for active limits, or a list with a single
        error/fallback snapshot if queries fail or credentials are unavailable.
    """
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
