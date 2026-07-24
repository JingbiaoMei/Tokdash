from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from ... import clientpaths
from ...usage_store import (
    RESET_JITTER_SECONDS,
    UsageEntryStore,
    _quota_history_uses_adjacent_deltas,
    persistent_usage_db_enabled,
)
from . import config
from .antigravity import collect_antigravity_api_snapshots
from .claude import read_claude_plan
from .claude import collect_claude_api_snapshots
from .codex import collect_codex_session_snapshots
from .codex import collect_codex_session_snapshots_incremental
from .codex import collect_codex_api_snapshots
from .grok import collect_grok_api_snapshots
from .kimi import collect_kimi_api_snapshots
from .minimax import collect_minimax_api_snapshots
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


def collect_network_snapshots(sources: Iterable[str] | None = None) -> list[QuotaSnapshot]:
    enabled = config.enabled_network_sources()
    if sources is not None:
        requested = {str(source) for source in sources}
        enabled = [source for source in enabled if source in requested]
    snapshots: list[QuotaSnapshot] = []
    for key in enabled:
        if key == "codex_api":
            snapshots.extend(collect_codex_api_snapshots())
        elif key == "claude_api":
            snapshots.extend(collect_claude_api_snapshots())
        elif key == "antigravity_api":
            snapshots.extend(collect_antigravity_api_snapshots())
        elif key == "minimax_api":
            snapshots.extend(collect_minimax_api_snapshots())
        elif key == "kimi_api":
            snapshots.extend(collect_kimi_api_snapshots())
        elif key == "grok_api":
            snapshots.extend(collect_grok_api_snapshots())
    return snapshots


def collect_enabled_snapshots(
    *,
    include_network: bool = True,
    store: UsageEntryStore | None = None,
    network_sources: Iterable[str] | None = None,
) -> list[QuotaSnapshot]:
    snapshots = collect_local_snapshots(store)
    if include_network:
        if network_sources is None:
            snapshots.extend(collect_network_snapshots())
        else:
            snapshots.extend(collect_network_snapshots(network_sources))
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


def poll_quota(
    store: UsageEntryStore | None = None,
    *,
    include_network: bool = True,
    network_sources: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Run one collect+store cycle. Idles entirely when quota tracking is disabled."""
    global _LAST_POLL_AT
    if not config.quota_tracking_enabled():
        return {"snapshots": 0, "inserted": 0, "network_sources": [], "disabled": True}
    store = store or UsageEntryStore() if persistent_usage_db_enabled() else None
    requested_sources = None if network_sources is None else tuple(str(source) for source in network_sources)
    if requested_sources is None:
        snapshots = collect_enabled_snapshots(include_network=include_network, store=store)
    else:
        snapshots = collect_enabled_snapshots(
            include_network=include_network,
            store=store,
            network_sources=requested_sources,
        )
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
    enabled_sources = config.enabled_network_sources() if include_network else []
    if requested_sources is not None:
        requested = set(requested_sources)
        enabled_sources = [source for source in enabled_sources if source in requested]
    return {"snapshots": len(snapshots), "inserted": inserted, "network_sources": enabled_sources}


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


def _boundary_candidate_details(
    now: int, latest_snapshots: Iterable[dict[str, Any]], cfg: config.BoundaryPollConfig
) -> list[tuple[int, str, str]]:
    """Future pre-reset and post-reset candidate fire times for qualifying fixed windows.

    Only fixed-reset windows qualify: `_quota_history_uses_adjacent_deltas` is reused
    (not reimplemented) so this scheduler and `quota_history`'s consumption math can never
    disagree about which (provider, bucket, resets_at) rows are fixed-reset vs
    rolling/reset-less.

    Candidates within RESET_JITTER_SECONDS of `now` are dropped, not just those at or before
    it. `resets_at` jitters +/-1s poll-to-poll (providers round the wall clock differently
    each poll — the same reason `quota_history` chains reset times into one epoch), so a
    bare ``> now`` guard re-arms the boundary we just fired: firing at ``resets_at - lead``
    off a 13:39:59 reading, the next poll reports 13:40:00, putting that same physical
    boundary 1s in the future and triggering a duplicate poll one sleep-floor later
    (measured: 5 pre fires across 4 real resets). One physical boundary must fire once, so
    a candidate that close to `now` is treated as the one already handled.
    """
    candidates: list[tuple[int, str, str]] = []
    horizon = now + RESET_JITTER_SECONDS
    for row in latest_snapshots:
        resets_at = row.get("resets_at")
        if resets_at is None:
            continue
        provider = str(row.get("provider") or "")
        bucket = str(row.get("bucket") or "")
        if _quota_history_uses_adjacent_deltas(provider, bucket, resets_at):
            continue
        try:
            resets_at = int(resets_at)
        except (TypeError, ValueError, OverflowError):
            continue
        pre_candidate = resets_at - cfg.pre_seconds
        if pre_candidate > horizon:
            candidates.append((pre_candidate, "pre", provider))
        if cfg.post_reset_enabled:
            post_candidate = resets_at + cfg.post_seconds
            if post_candidate > horizon:
                candidates.append((post_candidate, "post", provider))
    return candidates


def _boundary_candidates(
    now: int, latest_snapshots: Iterable[dict[str, Any]], cfg: config.BoundaryPollConfig
) -> tuple[list[int], list[int]]:
    details = _boundary_candidate_details(now, latest_snapshots, cfg)
    return (
        [target for target, kind, _provider in details if kind == "pre"],
        [target for target, kind, _provider in details if kind == "post"],
    )


@dataclass(frozen=True)
class BoundaryPollTarget:
    at: int
    kinds: frozenset[str]
    providers: frozenset[str]


def plan_boundary_poll(
    now: int,
    latest_snapshots: Iterable[dict[str, Any]],
    cfg: config.BoundaryPollConfig,
    *,
    minimum_delay_seconds: int = 0,
    anchored_post_targets: Iterable[tuple[int, str]] = (),
) -> BoundaryPollTarget | None:
    """Return one coalesced boundary plan, optionally delayed by a call-spacing floor.

    When the floor delays the earliest candidate, every other candidate due by that
    delayed time is folded into the same provider-scoped poll. Anchored post targets are
    reset epochs observed before a poll that rolled the provider into its next window.
    """
    if not cfg.enabled:
        return None
    candidates = _boundary_candidate_details(now, latest_snapshots, cfg)
    horizon = now + RESET_JITTER_SECONDS
    if cfg.post_reset_enabled:
        for target, provider in anchored_post_targets:
            try:
                target = int(target)
            except (TypeError, ValueError, OverflowError):
                continue
            # An anchor remains owed until its provider is actually sampled. If another
            # provider's scoped boundary poll let it become overdue, schedule it at the
            # next call floor instead of silently discarding the old reset epoch.
            candidates.append((max(target, horizon + 1), "post", str(provider or "")))
    if not candidates:
        return None

    earliest = min(target for target, _kind, _provider in candidates)
    scheduled_at = max(earliest, now + max(0, int(minimum_delay_seconds)))
    coalesce_until = scheduled_at + RESET_JITTER_SECONDS
    due = [candidate for candidate in candidates if candidate[0] <= coalesce_until]
    return BoundaryPollTarget(
        at=scheduled_at,
        kinds=frozenset(kind for _target, kind, _provider in due),
        providers=frozenset(provider for _target, _kind, provider in due if provider),
    )


def next_boundary_poll_at(
    now: int, latest_snapshots: Iterable[dict[str, Any]], cfg: config.BoundaryPollConfig
) -> int | None:
    """Earliest future boundary-poll fire time across all qualifying fixed-reset windows.

    Pure and side-effect free (no clock/DB access of its own) so it is unit-testable in
    isolation: `now` and `latest_snapshots` (the shape returned by
    `UsageEntryStore.latest_quota_snapshots()`) are both passed in explicitly. Returns
    ``None`` when boundary polling is disabled, or no qualifying window has a future
    pre/post-reset candidate.
    """
    if not cfg.enabled:
        return None
    pre, post = _boundary_candidates(now, latest_snapshots, cfg)
    candidates = pre + post
    return min(candidates) if candidates else None


def next_boundary_poll_target_with_kind(
    now: int, latest_snapshots: Iterable[dict[str, Any]], cfg: config.BoundaryPollConfig
) -> tuple[int, str] | None:
    """Like `next_boundary_poll_at`, but also names which kind of boundary won: ``"pre"``
    or ``"post"``.

    Kept as a small compatibility helper for callers that need the winning kind without
    provider coalescing. The daemon uses :func:`plan_boundary_poll`.
    """
    if not cfg.enabled:
        return None
    pre, post = _boundary_candidates(now, latest_snapshots, cfg)
    best_pre = min(pre) if pre else None
    best_post = min(post) if post else None
    if best_pre is None and best_post is None:
        return None
    if best_post is None or (best_pre is not None and best_pre <= best_post):
        return best_pre, "pre"
    return best_post, "post"


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
        "minimax": "minimax_api",
        "kimi": "kimi_api",
        "grok": "grok_api",
    }.get(name, f"{name}_api")


def _provider_shell(name: str, consent: dict[str, bool]) -> dict[str, Any]:
    network_key = _network_key_for_provider(name)
    return {
        "provider": name,
        "network_enabled": config.network_enabled(network_key),
        "plan": None,
        "buckets": [],
        "status": "unavailable",
        "status_detail": None,
        "status_at": None,
        "updated_at": None,
        "sources": [],
        "estimated": False,
        "detected": False,
    }


def _detected_local_providers() -> set[str]:
    """Providers with a local CLI directory or explicit credential override.

    This is intentionally read-only and shallow: directory existence and env-var
    presence are enough to drive dashboard visibility. It never opens a provider
    connection or refreshes credentials.
    """
    detected: set[str] = set()
    checks = {
        "codex": (clientpaths.codex_home(), ()),
        "claude": (clientpaths.claude_config_dir(), ("CLAUDE_CODE_OAUTH_TOKEN",)),
        "antigravity": (clientpaths.antigravity_cli_dir(), ()),
        "minimax": (
            clientpaths.minimax_cli_root(),
            (
                "MINIMAX_API_KEY",
                "MINIMAX_TOKEN_PLAN_GLOBAL_KEY",
                "MINIMAX_TOKEN_PLAN_CN_KEY",
            ),
        ),
        "grok": (clientpaths.grok_home(), ()),
    }
    for provider, (path, env_names) in checks.items():
        if path.exists() or any(os.environ.get(name, "").strip() for name in env_names):
            detected.add(provider)
    if os.environ.get("KIMI_API_KEY", "").strip() or any(root.exists() for root in clientpaths.kimi_roots()):
        detected.add("kimi")
    if config.credential_scan_enabled():
        try:
            from .credential_sources import discover_provider_sources

            detected.update(discover_provider_sources())
        except Exception:
            pass
    return detected


def _freshest_usage_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        if row.get("bucket") in {"api", "reset_credits"}:
            continue
        provider = str(row.get("provider") or "")
        # Existing providers deliberately collapse stale placeholder/default accounts into
        # the freshest real account. MiniMax is the exception: global and mainland-China
        # Token Plans are separate and may both be configured intentionally.
        account = str(row.get("account") or "") if provider == "minimax" else ""
        bucket = str(row.get("bucket") or "")
        key = (provider, account, bucket)
        current = selected.get(key)
        if current is None or int(row.get("captured_at") or 0) > int(current.get("captured_at") or 0):
            selected[key] = row
    return sorted(
        selected.values(),
        key=lambda item: (
            str(item.get("provider") or ""),
            str(item.get("account") or ""),
            str(item.get("bucket") or ""),
        ),
    )


def quota_state(store: UsageEntryStore | None = None) -> dict[str, Any]:
    tracking_enabled = config.quota_tracking_enabled()
    if persistent_usage_db_enabled():
        latest = (store or UsageEntryStore()).latest_quota_snapshots()
    else:
        # Persistence opted out: never construct the store — its __init__ mkdirs the DB
        # parent directory, which a read-only GET must not do in TOKDASH_USAGE_DB=0 mode.
        latest = [s.as_dict() for s in _CURRENT_SNAPSHOTS]

    consent = quota_network_consent()
    providers = {
        name: _provider_shell(name, consent)
        for name in ("codex", "claude", "antigravity", "minimax", "kimi", "grok")
    }
    for name in _detected_local_providers():
        providers[name]["detected"] = True
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
        # Stored quota data is evidence that the provider was configured even if its CLI
        # directory is temporarily unavailable (mounted home, migrated install, etc.).
        ref["detected"] = True
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
    # The Codex endpoint can temporarily return only the weekly window. Current cards
    # must reflect that payload exactly; older per-bucket rows remain available to history.
    if "codex" in network_only:
        codex_api_usage_times = [
            int(row.get("captured_at") or 0)
            for row in bucket_rows
            if str(row.get("provider")) == "codex"
            and str(row.get("source")) == "codex_api"
            and row.get("bucket") not in {"api", "reset_credits"}
        ]
        if codex_api_usage_times:
            current_codex_api_at = max(codex_api_usage_times)
            bucket_rows = [
                row
                for row in bucket_rows
                if not (
                    str(row.get("provider")) == "codex"
                    and str(row.get("source")) == "codex_api"
                    and row.get("bucket") not in {"api", "reset_credits"}
                    and int(row.get("captured_at") or 0) != current_codex_api_at
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

    # Reading the Claude local plan opens .credentials.json / the macOS Keychain — that is a
    # credential access, so gate it on credential-scan consent. Without it, a dashboard load
    # must never touch those stores or trigger a Keychain permission prompt.
    if config.credential_scan_enabled():
        claude_plan = read_claude_plan()
        providers["claude"]["plan"] = claude_plan.get("plan")
        if claude_plan.get("status") == "ok" and providers["claude"]["status"] == "unavailable":
            providers["claude"]["status"] = "local_plan"
        if claude_plan.get("status") == "ok":
            providers["claude"]["detected"] = True
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
