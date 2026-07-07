from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ...usage_store import UsageEntryStore, persistent_usage_db_enabled
from . import config
from .antigravity import collect_antigravity_api_snapshots
from .claude import read_claude_plan
from .claude import collect_claude_api_snapshots
from .codex import collect_codex_session_snapshots
from .codex import collect_codex_session_snapshots_incremental
from .codex import collect_codex_api_snapshots
from .types import QuotaSnapshot

_CURRENT_SNAPSHOTS: list[QuotaSnapshot] = []
_LAST_POLL_AT: int | None = None
_LAST_POLL_META_KEY = "quota_last_poll_at"


def quota_network_consent() -> dict[str, bool]:
    return config.read_quota_config()


def collect_local_snapshots(store: UsageEntryStore | None = None) -> list[QuotaSnapshot]:
    """Collect Codex session-file snapshots.

    With the persistent usage DB enabled (default) this uses byte-offset watermarks so a
    steady-state poll only tail-reads the active session file; the collector persists the
    snapshots and their watermarks atomically itself (re-inserting the returned snapshots
    is a harmless no-op under the UNIQUE key). When persistence is off there is nowhere to
    store watermarks, so it falls back to a full rescan and persists nothing.
    """
    if not persistent_usage_db_enabled():
        return collect_codex_session_snapshots()
    return collect_codex_session_snapshots_incremental(store or UsageEntryStore())


def collect_network_snapshots() -> list[QuotaSnapshot]:
    snapshots: list[QuotaSnapshot] = []
    for key in config.enabled_network_sources():
        if key == "codex_api":
            snapshots.extend(collect_codex_api_snapshots())
        elif key == "claude_api":
            snapshots.extend(collect_claude_api_snapshots())
        elif key == "antigravity_api":
            snapshots.extend(collect_antigravity_api_snapshots())
    return snapshots


def collect_enabled_snapshots(
    *, include_network: bool = True, store: UsageEntryStore | None = None
) -> list[QuotaSnapshot]:
    snapshots = collect_local_snapshots(store)
    if include_network:
        snapshots.extend(collect_network_snapshots())
    return snapshots


def remember_current_snapshots(snapshots: list[QuotaSnapshot]) -> None:
    global _CURRENT_SNAPSHOTS
    if snapshots:
        _CURRENT_SNAPSHOTS = list(snapshots)


def sync_local_snapshots(store: UsageEntryStore | None = None) -> int:
    """Collect + persist Codex session snapshots (the incremental collector commits the
    snapshots and their watermarks itself). Returns the number of snapshots collected."""
    if not persistent_usage_db_enabled():
        return 0
    return len(collect_local_snapshots(store or UsageEntryStore()))


def poll_quota(store: UsageEntryStore | None = None, *, include_network: bool = True) -> dict[str, Any]:
    """Run one collect+store cycle. Idles entirely when quota tracking is disabled."""
    global _LAST_POLL_AT
    if not config.quota_tracking_enabled():
        return {"snapshots": 0, "inserted": 0, "network_sources": [], "disabled": True}
    store = store or UsageEntryStore() if persistent_usage_db_enabled() else None
    snapshots = collect_enabled_snapshots(include_network=include_network, store=store)
    remember_current_snapshots(snapshots)
    now = int(datetime.now(timezone.utc).timestamp())
    _LAST_POLL_AT = now
    inserted = 0
    if store is not None:
        if snapshots:
            # Session snapshots were already committed (atomically with their watermarks)
            # by the incremental collector, so the UNIQUE key ignores them here and
            # ``inserted`` counts the network rows this cycle added.
            inserted = store.insert_quota_snapshots(snapshots)
        store.quota_meta_set(_LAST_POLL_META_KEY, str(now))
    return {"snapshots": len(snapshots), "inserted": inserted, "network_sources": config.enabled_network_sources()}


def last_poll_at(store: UsageEntryStore | None = None) -> int | None:
    """Best-effort last-poll wall time: in-memory value, else the persisted meta key."""
    if _LAST_POLL_AT is not None:
        return _LAST_POLL_AT
    if not persistent_usage_db_enabled():
        return None
    try:
        value = (store or UsageEntryStore()).quota_meta_get(_LAST_POLL_META_KEY)
        return int(value) if value else None
    except Exception:
        return None


_CODEX_PLAN_LABELS = {
    "prolite": "Pro Lite",
    "pro_lite": "Pro Lite",
    "plus": "Plus",
    "pro": "Pro",
    "free": "Free",
    "team": "Team",
    "business": "Business",
    "enterprise": "Enterprise",
}


def _codex_plan_label(plan: Any) -> str | None:
    """Human plan label for the card header ("prolite" -> "Pro Lite").

    Display-only — snapshot rows keep the raw ``plan_type`` string.
    """
    if not plan:
        return None
    key = str(plan).strip().lower()
    return _CODEX_PLAN_LABELS.get(key) or key.replace("_", " ").title()


def _network_key_for_provider(name: str) -> str:
    return {
        "codex": "codex_api",
        "claude": "claude_api",
        "antigravity": "antigravity_api",
    }.get(name, f"{name}_api")


def _provider_shell(name: str, consent: dict[str, bool]) -> dict[str, Any]:
    return {
        "provider": name,
        "network_enabled": bool(consent.get(_network_key_for_provider(name), False)),
        "plan": None,
        "buckets": [],
        "status": "unavailable",
        "status_detail": None,
        "status_at": None,
        "updated_at": None,
        "sources": [],
        "estimated": False,
    }


def _freshest_usage_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if row.get("bucket") in {"api", "reset_credits"}:
            continue
        provider = str(row.get("provider") or "")
        bucket = str(row.get("bucket") or "")
        key = (provider, bucket)
        current = selected.get(key)
        if current is None or int(row.get("captured_at") or 0) > int(current.get("captured_at") or 0):
            selected[key] = row
    return sorted(selected.values(), key=lambda item: (str(item.get("provider") or ""), str(item.get("bucket") or "")))


def quota_state(store: UsageEntryStore | None = None) -> dict[str, Any]:
    tracking_enabled = config.quota_tracking_enabled()
    if persistent_usage_db_enabled():
        latest = (store or UsageEntryStore()).latest_quota_snapshots()
    else:
        # Persistence opted out: never construct the store — its __init__ mkdirs the DB
        # parent directory, which a read-only GET must not do in TOKDASH_USAGE_DB=0 mode.
        latest = [s.as_dict() for s in _CURRENT_SNAPSHOTS]

    consent = quota_network_consent()
    providers = {name: _provider_shell(name, consent) for name in ("codex", "claude", "antigravity")}
    last_network_run: int | None = _LAST_POLL_AT
    # When Codex API polling is enabled, the API is the sole oracle for the current-quota
    # cards: codex_session rows are excluded from bucket selection below so a newer cached
    # session row can never override a fresher API observation. Prefer
    # `config.network_enabled` (not raw `consent`) so the `TOKDASH_QUOTA_POLL` kill switch
    # is honored consistently with `quota_history`'s `network_only_providers` gate.
    network_only = {"codex"} if config.network_enabled("codex_api") else set()
    for row in latest:
        provider = str(row.get("provider") or "")
        if provider not in providers:
            providers[provider] = _provider_shell(provider, consent)
        ref = providers[provider]
        source = str(row.get("source") or "")
        if source.endswith("_api"):
            ref["network_enabled"] = True
            captured = int(row.get("captured_at") or 0)
            if captured:
                last_network_run = max(last_network_run or 0, captured)
                if row.get("status") == "ok":
                    ref["_ok_api_at"] = max(int(ref.get("_ok_api_at") or 0), captured)
        ref["status"] = "ok" if row.get("status") == "ok" else str(row.get("status") or ref["status"])
        if row.get("bucket") == "api":
            captured = int(row.get("captured_at") or 0)
            if captured >= int(ref.get("status_at") or 0):
                ref["status_detail"] = str(row.get("status") or "unavailable")
                ref["status_at"] = captured or None
        ref["plan"] = ref["plan"] or row.get("plan")
        ref["updated_at"] = max(int(ref["updated_at"] or 0), int(row.get("captured_at") or 0)) or None
        if row.get("source") and row.get("source") not in ref["sources"]:
            ref["sources"].append(row.get("source"))
        if provider == "codex" and row.get("bucket") == "reset_credits":
            reset_payload = row.get("raw", {}).get("reset_credits") if isinstance(row.get("raw"), dict) else {}
            if isinstance(reset_payload, dict):
                ref["reset_credits"] = {
                    "available_count": reset_payload.get("available_count", row.get("used_percent")),
                    "credits": reset_payload.get("credits") if isinstance(reset_payload.get("credits"), list) else [],
                }

    for ref in providers.values():
        # Failure status rows (bucket == "api") are only written when a fetch FAILS, so
        # after recovery the newest "api" row is a stale artifact. Suppress the error
        # detail (and the banner it drives) once a newer successful API observation exists.
        ok_at = int(ref.pop("_ok_api_at", 0) or 0)
        if ref.get("status_detail") and ok_at > int(ref.get("status_at") or 0):
            ref["status_detail"] = None
            ref["status_at"] = None
            ref["status"] = "ok"

    # Apply source authority ONLY to bucket selection (the status/reset_credits/
    # network_enabled loop above must keep reading the full `latest`). Dropping
    # codex_session rows here means: if codex is API-only and only session rows exist for a
    # bucket, that bucket is simply omitted rather than falling back to stale session data.
    bucket_rows = [
        r
        for r in latest
        if not (
            "codex" in network_only
            and str(r.get("provider")) == "codex"
            and str(r.get("source")) == "codex_session"
        )
    ]
    for row in _freshest_usage_rows(bucket_rows):
        provider = str(row.get("provider") or "")
        if provider not in providers:
            providers[provider] = _provider_shell(provider, consent)
        bucket_row = {
            key: row.get(key)
            for key in (
                "account",
                "bucket",
                "bucket_label",
                "used_percent",
                "resets_at",
                "captured_at",
                "source",
                "status",
            )
        }
        used_percent = bucket_row.get("used_percent")
        # Additive: the UI displays remaining quota (TASK 1), but storage/other API
        # consumers keep reading used_percent unchanged.
        bucket_row["remaining_percent"] = None if used_percent is None else round(100.0 - float(used_percent), 4)
        providers[provider]["buckets"].append(bucket_row)

    providers["codex"]["plan"] = _codex_plan_label(providers["codex"]["plan"])
    # Codex cards are estimated (may include session-source data) exactly when codex_api
    # polling is off; claude/antigravity have no session source and are never estimated.
    providers["codex"]["estimated"] = "codex" not in network_only

    claude_plan = read_claude_plan()
    providers["claude"]["plan"] = claude_plan.get("plan")
    if claude_plan.get("status") == "ok" and providers["claude"]["status"] == "unavailable":
        providers["claude"]["status"] = "local_plan"
    providers["claude"]["credential_path"] = claude_plan.get("credential_path")
    providers["claude"]["tier"] = claude_plan.get("tier")

    interval_seconds, interval_source = config.effective_poll_interval()
    now = int(datetime.now(timezone.utc).timestamp())
    return {
        "providers": providers,
        "consent": consent,
        "enabled": tracking_enabled,
        "poll": {
            "enabled": tracking_enabled,
            "network_enabled": bool(config.enabled_network_sources()),
            "interval": interval_seconds,
            "interval_source": interval_source,
            "interval_minutes": config.read_poll_interval_minutes() or config.DEFAULT_POLL_INTERVAL_MINUTES,
            "interval_choices": list(config.POLL_INTERVAL_CHOICES),
            "last_run": last_network_run,
            "kill_switch": config.quota_poll_killed(),
        },
        "timestamp": now,
    }
