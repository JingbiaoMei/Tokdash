from __future__ import annotations

import json
import platform
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


_TOKEN_KEYS = {"access_token", "refresh_token", "id_token", "token"}


def _safe_token_meta(data: dict[str, Any], path: str) -> dict[str, Any]:
    meta: dict[str, Any] = {"path": path}
    for key, value in data.items():
        # _TOKEN_KEYS already covers "token" (and access/refresh/id_token); only copy
        # non-token scalar fields through.
        if key in _TOKEN_KEYS:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            meta[key] = value
    token_obj = data.get("token") if isinstance(data.get("token"), dict) else {}
    for key in ("expiry", "expires_at", "expiry_date"):
        if key in token_obj:
            meta[key] = token_obj.get(key)
    return meta


def _read_token() -> tuple[str | None, dict[str, Any]]:
    path = clientpaths.antigravity_cli_dir() / "antigravity-oauth-token"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None, {"error": "token_not_found", "path": str(path)}
    except Exception as exc:
        return None, {"error": "token_invalid", "message": str(exc), "path": str(path)}
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        return text or None, {"path": str(path)}
    token_obj = data.get("token") if isinstance(data.get("token"), dict) else {}
    token = data.get("access_token") or token_obj.get("access_token")
    return str(token) if token else None, _safe_token_meta(data, str(path))


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
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 1:
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
        quota = item.get("quotaInfo") if isinstance(item.get("quotaInfo"), dict) else item.get("quota_info", {})
        try:
            remaining = float(quota.get("remainingFraction"))
        except Exception:
            continue
        out.append(
            QuotaSnapshot(
                "antigravity",
                account,
                str(item.get("name") or item.get("model") or "model"),
                str(item.get("displayName") or item.get("display_name") or item.get("name") or "Model"),
                round((1.0 - remaining) * 100.0, 4),
                _parse_time(quota.get("resetTime") or quota.get("reset_time")),
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
