from __future__ import annotations

import json
import base64
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
import urllib.request
import time

from ... import clientpaths
from ...codex_quota_windows import classify_codex_api_windows
from .types import QuotaSnapshot

CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_RESET_CREDITS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"


def _parse_time(value: Any) -> int | None:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            number = float(value)
            return int(number / 1000) if number > 10_000_000_000 else int(number)
        text = str(value).strip()
        if not text:
            return None
        if text.isdigit():
            return _parse_time(int(text))
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp())
    except Exception:
        return None


def _normalize_percent(value: Any, *, unit_interval_as_fraction: bool = True) -> float | None:
    try:
        if value is None:
            return None
        pct = float(value)
    except Exception:
        return None
    if pct < 0:
        return None
    if unit_interval_as_fraction and 0.0 <= pct <= 1.0:
        return round(pct * 100.0, 4)
    return round(pct, 4)


def _first_present(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data:
            return data.get(key)
    return None


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        part = str(token).split(".")[1]
        padded = part + "=" * (-len(part) % 4)
        data = base64.urlsafe_b64decode(padded.encode("ascii"))
        obj = json.loads(data.decode("utf-8"))
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _status_snapshot(status: str, captured_at: int, raw: dict[str, Any], account: str = "default") -> QuotaSnapshot:
    return QuotaSnapshot(
        provider="codex",
        account=account,
        bucket="api",
        bucket_label="Codex API",
        used_percent=None,
        resets_at=None,
        plan=None,
        captured_at=captured_at,
        source="codex_api",
        status=status,
        raw=raw,
    )


def _bucket_snapshot(
    *,
    rate_limits: dict[str, Any],
    bucket: str,
    bucket_label: str,
    bucket_payload: dict[str, Any],
    captured_at: int,
) -> QuotaSnapshot | None:
    used_percent = _normalize_percent(
        _first_present(bucket_payload, "used_percent", "usage_percent", "usedPercent"),
        unit_interval_as_fraction=False,
    )
    if used_percent is None:
        return None
    account = str(rate_limits.get("account_id") or "default")
    resets_at = _parse_time(_first_present(bucket_payload, "resets_at", "reset_at", "resetAt"))
    if not used_percent:
        # Codex's rolling-window API returns resets_at ~= captured_at + window_length even
        # for an idle window (0% used, timer hasn't actually started on first use yet). That
        # is a phantom reset, not a real one -- null it out so idle buckets render "reset --",
        # mirroring how Claude already treats its null buckets.
        resets_at = None
    return QuotaSnapshot(
        provider="codex",
        account=account,
        bucket=bucket,
        bucket_label=bucket_label,
        used_percent=used_percent,
        resets_at=resets_at,
        plan=str(rate_limits.get("plan_type") or "") or None,
        captured_at=captured_at,
        source="codex_session",
        status="ok",
        raw={"rate_limits": rate_limits},
    )


def snapshots_from_token_count_event(obj: dict[str, Any]) -> list[QuotaSnapshot]:
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    if obj.get("type") != "event_msg" or payload.get("type") != "token_count":
        return []
    captured_at = _parse_time(obj.get("timestamp"))
    if captured_at is None:
        return []
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    rate_limits = payload.get("rate_limits") if isinstance(payload.get("rate_limits"), dict) else {}
    if not rate_limits:
        rate_limits = info.get("rate_limits") if isinstance(info.get("rate_limits"), dict) else {}
    if not rate_limits:
        return []

    out: list[QuotaSnapshot] = []
    primary = rate_limits.get("primary") if isinstance(rate_limits.get("primary"), dict) else {}
    secondary = rate_limits.get("secondary") if isinstance(rate_limits.get("secondary"), dict) else {}
    if primary:
        snap = _bucket_snapshot(
            rate_limits=rate_limits,
            bucket="5h",
            bucket_label="5-hour window",
            bucket_payload=primary,
            captured_at=captured_at,
        )
        if snap:
            out.append(snap)
    if secondary:
        snap = _bucket_snapshot(
            rate_limits=rate_limits,
            bucket="7d",
            bucket_label="7-day window",
            bucket_payload=secondary,
            captured_at=captured_at,
        )
        if snap:
            out.append(snap)
    return out


QUOTA_SESSION_SOURCE = "codex_session"
_BACKFILL_META_KEY = "quota_codex_session_backfill_done"


def _downsample_snapshots(snapshots: list[QuotaSnapshot]) -> list[QuotaSnapshot]:
    """Keep the first observation per (provider, account, bucket, hour).

    Bounds row growth for both the one-time backfill (huge history) and per-cycle tail
    reads. Live polling is already coarse; the `INSERT OR IGNORE` UNIQUE key is the final
    dedup net, so this is purely a volume guard.
    """
    kept: dict[tuple[str, str, str, int], QuotaSnapshot] = {}
    for snapshot in sorted(snapshots, key=lambda item: item.captured_at):
        hour = snapshot.captured_at - (snapshot.captured_at % 3600)
        key = (snapshot.provider, snapshot.account, snapshot.bucket, hour)
        kept.setdefault(key, snapshot)
    return list(kept.values())


def _snapshots_from_bytes(data: bytes) -> tuple[list[QuotaSnapshot], int]:
    """Parse complete newline-terminated JSON lines out of ``data``.

    Returns ``(snapshots, consumed)`` where ``consumed`` is the number of bytes up to and
    including the last newline. A partial trailing line (Codex still mid-write) is left
    UNconsumed so it is re-read and parsed on a later cycle once it is complete.
    """
    last_nl = data.rfind(b"\n")
    if last_nl < 0:
        return [], 0
    out: list[QuotaSnapshot] = []
    for raw_line in data[: last_nl + 1].split(b"\n"):
        if not raw_line.strip():
            continue
        try:
            obj = json.loads(raw_line)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.extend(snapshots_from_token_count_event(obj))
    return out, last_nl + 1


def _read_session_bytes(path: Path, offset: int) -> bytes:
    """Read from ``offset`` to EOF. Isolated so tests can count/observe file reads."""
    with open(path, "rb") as handle:
        if offset:
            handle.seek(offset)
        return handle.read()


def collect_codex_session_snapshots(sessions_dir: Path | None = None) -> list[QuotaSnapshot]:
    """Full rescan of every rollout file (no watermarks).

    Used for the DB-disabled fallback path where watermarks cannot be persisted. The
    incremental poll path uses :func:`collect_codex_session_snapshots_incremental`.
    """
    root = sessions_dir or clientpaths.codex_sessions_dir()
    if not root.exists():
        return []
    snapshots: list[QuotaSnapshot] = []
    for path in sorted(root.rglob("rollout-*.jsonl")):
        try:
            data = _read_session_bytes(path, 0)
        except OSError:
            continue
        found, _consumed = _snapshots_from_bytes(data)
        snapshots.extend(found)
    return _downsample_snapshots(snapshots)


def collect_codex_session_snapshots_incremental(
    store, sessions_dir: Path | None = None
) -> list[QuotaSnapshot]:
    """Watermark-based incremental session ingestion (mirrors ``file_state``).

    Each cycle stats every rollout file (no reads). Unchanged files (same mtime_ns + size)
    read ZERO bytes. Grown files seek to the stored offset and read only appended bytes,
    advancing the offset past the last complete line. Shrunken/rewritten files (size below
    the stored offset) drop the watermark and re-read whole; brand-new files read whole. The
    one-time full backfill (first run, no watermarks yet) reads everything once and records
    completion in the meta table so it never re-runs.
    """
    root = sessions_dir or clientpaths.codex_sessions_dir()
    if not root.exists():
        return []
    source = QUOTA_SESSION_SOURCE
    backfilled = store.quota_meta_get(_BACKFILL_META_KEY) == "1"
    watermarks = store.quota_file_watermarks(source)

    updates: list[tuple[str, int, int, int]] = []
    fresh: list[QuotaSnapshot] = []
    for path in sorted(root.rglob("rollout-*.jsonl")):
        try:
            stat = path.stat()
        except OSError:
            continue
        key = str(path)
        watermark = watermarks.get(key)
        if (
            watermark is not None
            and watermark["mtime_ns"] == stat.st_mtime_ns
            and watermark["size"] == stat.st_size
        ):
            continue  # unchanged: skipped with zero bytes read
        if watermark is None or stat.st_size < watermark["safe_offset"]:
            base = 0  # new file, or shrunk/rewritten -> re-read whole
        else:
            base = watermark["safe_offset"]  # grown/changed -> tail read
        try:
            data = _read_session_bytes(path, base)
        except OSError:
            continue
        found, consumed = _snapshots_from_bytes(data)
        fresh.extend(found)
        updates.append((key, stat.st_mtime_ns, stat.st_size, base + consumed))

    snapshots = _downsample_snapshots(fresh)
    # Snapshots and the watermarks that cover them commit in ONE transaction: if the
    # insert fails, the watermarks (and the backfill-done flag) roll back too, so the
    # next cycle re-reads the same bytes instead of skipping them forever.
    store.commit_quota_session_batch(
        snapshots,
        source,
        updates,
        backfill_meta_key=None if backfilled else _BACKFILL_META_KEY,
    )
    return snapshots


def _account_from_claims(claims: dict[str, Any], tokens: dict[str, Any]) -> str | None:
    token_account = tokens.get("account_id") or tokens.get("chatgpt_account_id")
    if token_account:
        return str(token_account)
    auth_claim = claims.get("https://api.openai.com/auth")
    if isinstance(auth_claim, dict):
        nested = auth_claim.get("chatgpt_account_id") or auth_claim.get("account_id")
        if nested:
            return str(nested)
    flat = claims.get("https://api.openai.com/auth.chatgpt_account_id") or claims.get("account_id")
    return str(flat) if flat else None


def _read_auth() -> tuple[str | None, str | None, dict[str, Any]]:
    path = clientpaths.codex_home() / "auth.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "default", {"error": "auth_not_found", "path": str(path)}
    except Exception as exc:
        return None, "default", {"error": "auth_invalid", "message": str(exc), "path": str(path)}
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    access_token = tokens.get("access_token")
    id_token = tokens.get("id_token")
    claims = _decode_jwt_payload(str(id_token or access_token or ""))
    account = _account_from_claims(claims, tokens)
    return str(access_token) if access_token else None, account, claims


def _get_json(url: str, token: str, account: str | None, opener, timeout: float) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        "originator": "Codex Desktop",
        "OAI-Product-Sku": "CODEX",
        "Accept": "application/json",
    }
    if account:
        headers["ChatGPT-Account-Id"] = account
    req = urllib.request.Request(
        url,
        headers=headers,
    )
    last_error: HTTPError | None = None
    for attempt in range(2):
        try:
            with opener(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            last_error = exc
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 1:
                raise
            time.sleep(0.2)
    assert last_error is not None
    raise last_error


def collect_codex_api_snapshots(
    *,
    opener=urllib.request.urlopen,
    now: int | None = None,
    timeout: float = 15.0,
) -> list[QuotaSnapshot]:
    captured_at = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    token, account, claims = _read_auth()
    snapshot_account = account or "default"
    if not token:
        return [_status_snapshot("unavailable", captured_at, claims, snapshot_account)]

    # Usage (the rate-limit windows) is the primary data — a failure here is fatal for the
    # cycle and surfaces as one error snapshot.
    try:
        usage = _get_json(CODEX_USAGE_URL, token, account, opener, timeout)
    except HTTPError as exc:
        status = "stale_token" if exc.code in {401, 403} else "fetch_error"
        return [_status_snapshot(status, captured_at, {"error": f"HTTP {exc.code}: {exc.reason}"}, snapshot_account)]
    except Exception as exc:
        return [_status_snapshot("fetch_error", captured_at, {"error": str(exc)}, snapshot_account)]

    # Reset credits are a SEPARATE, best-effort call. A failure here must NOT discard the
    # window data we just fetched — degrade to "no reset-credit snapshot this cycle".
    credits: dict[str, Any] | None
    try:
        fetched = _get_json(CODEX_RESET_CREDITS_URL, token, account, opener, timeout)
        credits = fetched if isinstance(fetched, dict) else None
    except Exception:
        credits = None

    rate_limits = _usage_rate_limits(usage)
    if isinstance(rate_limits, dict) and usage.get("plan_type") and not rate_limits.get("plan_type"):
        rate_limits = dict(rate_limits)
        rate_limits["plan_type"] = usage.get("plan_type")

    out: list[QuotaSnapshot] = []
    primary = rate_limits.get("primary") if isinstance(rate_limits.get("primary"), dict) else {}
    secondary = rate_limits.get("secondary") if isinstance(rate_limits.get("secondary"), dict) else {}
    for bucket, label, payload in (("5h", "5-hour window", primary), ("7d", "7-day window", secondary)):
        snap = _bucket_snapshot(
            rate_limits={**rate_limits, "account_id": snapshot_account},
            bucket=bucket,
            bucket_label=label,
            bucket_payload=payload,
            captured_at=captured_at,
        )
        if snap:
            out.append(
                QuotaSnapshot(
                    **{**snap.as_dict(), "source": "codex_api", "raw": {"usage": usage}},
                )
            )

    additional = usage.get("additional_rate_limits")
    additional_items: list[dict[str, Any]] = []
    if isinstance(additional, list):
        additional_items = [item for item in additional if isinstance(item, dict)]
    elif isinstance(additional, dict):
        additional_items = [item for item in additional.values() if isinstance(item, dict)]
    for item in additional_items:
        metered_feature = item.get("metered_feature")
        if not metered_feature:
            continue  # older synthetic shape without metered_feature; already folded into the main 7d bucket above
        nested = item.get("rate_limit") if isinstance(item.get("rate_limit"), dict) else item
        limit_name = str(item.get("limit_name") or metered_feature)
        has_nested_windows = "primary_window" in nested or "secondary_window" in nested
        windows: list[tuple[str, str, dict[str, Any]]]
        if has_nested_windows:
            canonical_windows = classify_codex_api_windows(
                nested.get("primary_window") if isinstance(nested.get("primary_window"), dict) else None,
                nested.get("secondary_window") if isinstance(nested.get("secondary_window"), dict) else None,
            )
            windows = []
            if "5h" in canonical_windows:
                windows.append((f"{metered_feature}_5h", f"{limit_name} · 5-hour", canonical_windows["5h"]))
            if "7d" in canonical_windows:
                windows.append((f"{metered_feature}_7d", f"{limit_name} · 7-day", canonical_windows["7d"]))
        else:
            # Older/synthetic shape: "rate_limit" (or the item itself) IS the single window
            # payload. Keep the legacy unsuffixed bucket id for backward compatibility.
            windows = [(str(metered_feature), limit_name, nested)]
        for bucket_id, bucket_label, window_payload in windows:
            snap = _bucket_snapshot(
                rate_limits={**rate_limits, "account_id": snapshot_account},
                bucket=bucket_id,
                bucket_label=bucket_label,
                bucket_payload=window_payload,
                captured_at=captured_at,
            )
            if snap:
                out.append(
                    QuotaSnapshot(**{**snap.as_dict(), "source": "codex_api", "raw": {"usage": usage, "additional_rate_limit": item}})
                )

    if isinstance(credits, dict):
        available = credits.get("available_count")
        try:
            available_percent = float(available)
        except Exception:
            available_percent = None
        out.append(
            QuotaSnapshot(
                provider="codex",
                account=snapshot_account,
                bucket="reset_credits",
                bucket_label="Reset credits",
                used_percent=available_percent,
                resets_at=None,
                plan=str(rate_limits.get("plan_type") or "") or None,
                captured_at=captured_at,
                source="codex_api",
                status="ok",
                raw={"reset_credits": credits},
            )
        )
    return out


def _usage_rate_limits(usage: dict[str, Any]) -> dict[str, Any]:
    raw = usage.get("rate_limits") if isinstance(usage.get("rate_limits"), dict) else {}
    if "primary" in raw or "secondary" in raw:
        rate_limits = {key: value for key, value in raw.items() if key not in {"primary", "secondary"}}
        canonical_windows = classify_codex_api_windows(
            raw.get("primary") if isinstance(raw.get("primary"), dict) else None,
            raw.get("secondary") if isinstance(raw.get("secondary"), dict) else None,
        )
        if "5h" in canonical_windows:
            rate_limits["primary"] = canonical_windows["5h"]
        if "7d" in canonical_windows:
            rate_limits["secondary"] = canonical_windows["7d"]
    else:
        rate_limits = dict(raw)

    single = usage.get("rate_limit") if isinstance(usage.get("rate_limit"), dict) else None
    if single is not None:
        has_nested_windows = "primary_window" in single or "secondary_window" in single
        if has_nested_windows:
            canonical_windows = classify_codex_api_windows(
                single.get("primary_window") if isinstance(single.get("primary_window"), dict) else None,
                single.get("secondary_window") if isinstance(single.get("secondary_window"), dict) else None,
            )
            if "5h" in canonical_windows and not isinstance(rate_limits.get("primary"), dict):
                rate_limits["primary"] = canonical_windows["5h"]
            if "7d" in canonical_windows and not isinstance(rate_limits.get("secondary"), dict):
                rate_limits["secondary"] = canonical_windows["7d"]
        elif not isinstance(rate_limits.get("primary"), dict):
            # Older synthetic shape: "rate_limit" IS the primary window payload.
            rate_limits["primary"] = single

    additional = usage.get("additional_rate_limits")
    items: list[dict[str, Any]] = []
    if isinstance(additional, list):
        items = [_unwrap_rate_limit_item(item) for item in additional if isinstance(item, dict)]
    elif isinstance(additional, dict):
        items = [_unwrap_rate_limit_item(item) for item in additional.values() if isinstance(item, dict)]
        if not items and any(key in additional for key in ("used_percent", "usage_percent", "usedPercent")):
            items = [additional]
    if items and not isinstance(rate_limits.get("secondary"), dict):
        weekly = next((item for item in items if _window_minutes(item) >= 10_080), None)
        rate_limits["secondary"] = weekly or items[0]

    if not rate_limits:
        return dict(usage)
    return rate_limits


def _unwrap_rate_limit_item(item: dict[str, Any]) -> dict[str, Any]:
    nested = item.get("rate_limit") if isinstance(item.get("rate_limit"), dict) else None
    if not nested:
        return item
    merged = {key: value for key, value in item.items() if key != "rate_limit"}
    merged.update(nested)
    return merged


def _window_minutes(item: dict[str, Any]) -> int:
    try:
        return int(float(item.get("window_minutes") or 0))
    except Exception:
        return 0
