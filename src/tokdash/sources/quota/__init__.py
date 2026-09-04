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
from .claude import ClaudeProfile
from .claude import discover_profiles
from .claude import read_claude_profiles
from .claude import collect_claude_api_snapshots
from .codex import collect_codex_session_snapshots
from .codex import collect_codex_session_snapshots_incremental
from .codex import collect_codex_api_snapshots
from .grok import collect_grok_api_snapshots
from .kimi import collect_kimi_api_snapshots
from .minimax import collect_minimax_api_snapshots
from .zai import collect_zai_api_snapshots
from .credential_sources import zai_coding_base_url_allowed
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
        elif key == "zai_api":
            snapshots.extend(collect_zai_api_snapshots())
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
        "zai": "zai_api",
    }.get(name, f"{name}_api")


# The cards that really measure more than one account at once, and the account each card is
# named after. Claude Code keeps one sign-in per config directory and MiniMax one per region;
# both are configured deliberately, so an expired sibling sign-in or a failing China Token
# Plan is another account's state rather than a problem with the default install or the global
# plan. Membership here is the single place that decides per-account handling: whether a
# stored account stays separate when the freshest row per bucket is chosen, and whether a card
# gets an ``accounts`` list at all. The value only orders that list, so the account the card
# has always spoken for still comes first.
_MULTI_ACCOUNT_CARDS = {"claude": "default", "minimax": "global"}


def _new_account_view() -> dict[str, Any]:
    return {
        "plan": None,
        "updated_at": 0,
        "status": None,
        "status_detail": None,
        "status_at": None,
        "ok_at": 0,
    }


def _record_row(view: dict[str, Any], row: dict[str, Any]) -> None:
    """Fold one stored row into what one account of one provider knows about itself.

    The same view backs the card's status line and the per-account list, so the recovery
    rule is written once and cannot differ between them. ``api`` rows exist only for
    FAILURES, so a success never lands in ``status_detail`` and ``ok_at`` cannot come from
    those rows either: it tracks the newest successful ``*_api`` row of ANY bucket, window
    rows included, because that observation is what turns an older error row into history.
    Keyed per account, so one subscription's recovery does not silence another's error.
    """
    captured = int(row.get("captured_at") or 0)
    if captured:
        view["updated_at"] = max(int(view["updated_at"] or 0), captured)
    if not view["plan"] and row.get("plan"):
        view["plan"] = row.get("plan")
    status = str(row.get("status") or "")
    if status:
        view["status"] = "ok" if status == "ok" else status
    if captured and status == "ok" and str(row.get("source") or "").endswith("_api"):
        view["ok_at"] = max(int(view["ok_at"] or 0), captured)
    if row.get("bucket") == "api" and captured >= int(view["status_at"] or 0):
        view["status_detail"] = status or "unavailable"
        view["status_at"] = captured or None


def _resolved_status(view: dict[str, Any]) -> tuple[str | None, int | None]:
    """The account's live error: a failure with no successful observation after it."""
    detail = view.get("status_detail")
    if (
        not detail
        or detail == "ok"  # an `api` row that recorded success is not an error
        or int(view.get("ok_at") or 0) > int(view.get("status_at") or 0)
    ):
        return None, None
    return str(detail), view.get("status_at")


def _account_status(view: dict[str, Any]) -> tuple[str | None, str | None, int | None]:
    """One account's (status, live error, when it happened), read the same way everywhere.

    `status` alone would repeat the text of the last row seen, and because a success writes
    no ``api`` row, that row can be a failure the account has long since recovered from. So
    a retired error promotes the status: card and account list answer through this one
    function and cannot drift apart.
    """
    detail, status_at = _resolved_status(view)
    status = view.get("status")
    if detail is None and view.get("status_detail") and status and status != "ok":
        status = "ok"
    return status, detail, status_at


def _provider_status(
    aggregate: dict[str, Any], views: dict[str, dict[str, Any]]
) -> tuple[str | None, str | None, int | None]:
    """The card's own (status, live error, when it happened), from the accounts behind it.

    A card speaks for every credential it measures, so its error is the NEWEST error any of
    its accounts is still carrying -- not the error of whichever account the card happens to
    be named after. The companion contract depends on this breadth: one broken credential has
    to keep warning about the provider, or a consumer reads a stale meter as a current one.

    Resolving it per account rather than from the provider-wide row is what keeps the two
    failure modes apart: a healthy account's newer success does not silence this account's
    error, and this account's own recovery does not leave its old error row warning the card
    forever. Attribution survives because the same resolution runs per account in ``accounts``
    and each card prints the notice under the account that owns it.
    """
    newest: tuple[int, str | None, str, int | None] | None = None
    for view in views.values():
        status, detail, status_at = _account_status(view)
        if detail is None:
            continue
        stamp = int(status_at or 0)
        if newest is None or stamp > newest[0]:
            newest = (stamp, status, detail, status_at)
    if newest is not None:
        return newest[1], newest[2], newest[3]
    # Nothing is live: the provider-wide answer, with an error every account has since
    # recovered from promoted away, exactly as it is in the per-account list.
    return _account_status(aggregate)[0], None, None


def _stale_accounts(
    views: dict[str, dict[str, Any]], *, newest: int, interval_seconds: int
) -> set[str]:
    """Accounts whose stored rows stopped keeping up with the rest of their provider.

    Three poll intervals, and never less than an hour, so an account is never dropped for
    being one late cycle behind; the floor also means a machine polled on an odd interval
    cannot retire everything at once. `newest` is the freshest row of any account of that
    provider, so an account ages out only relative to data this machine still reads. The
    providers that fold their accounts into one always have `newest` as that one account's
    timestamp, so they can never retire anything here.
    """
    if newest <= 0:
        return set()
    cutoff = newest - max(3 * int(interval_seconds or 0), 3600)
    return {
        account
        for account, view in views.items()
        if int(view.get("updated_at") or 0) < cutoff
    }


def _account_entries(
    provider: str,
    views: dict[str, dict[str, Any]],
    extra: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """One row per account of one provider, the card's own account first.

    A provider whose card carries several accounts (Claude installs, MiniMax regions) has
    to say which account a failure belongs to, or one broken secondary account reads as a
    problem with the healthy primary one. Membership of `_MULTI_ACCOUNT_CARDS` decides which
    cards get here, and the card-level status reads these same views (see
    ``_provider_status``). ``extra`` adds facts the stored rows do not carry: for Claude, the
    plan, tier and credential path read from each install's own file, which also brings in an
    install whose poll has not run yet.
    """
    accounts = dict(views)
    for name in extra or {}:
        accounts.setdefault(name, _new_account_view())
    primary = _MULTI_ACCOUNT_CARDS.get(provider, "default")

    def entry(name: str) -> dict[str, Any]:
        view = accounts[name]
        facts = (extra or {}).get(name, {})
        status, detail, status_at = _account_status(view)
        row: dict[str, Any] = {
            "account": name,
            # A locally-readable plan wins: it is the human label ("Max 5x") rather than
            # whatever raw subscription string a stored row happened to carry.
            "plan": facts.get("plan") or view.get("plan"),
            "status": status or facts.get("status"),
            "status_detail": detail,
            "status_at": status_at,
            "updated_at": int(view.get("updated_at") or 0) or None,
        }
        for key in ("tier", "credential_path"):
            if key in facts:
                row[key] = facts.get(key)
        return row

    return [entry(name) for name in sorted(accounts, key=lambda name: (name != primary, name))]


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


def _detected_local_providers(claude_profiles: list[ClaudeProfile]) -> set[str]:
    """Providers with a local CLI directory or explicit credential override.

    This is intentionally read-only and shallow: directory existence and env-var
    presence are enough to drive dashboard visibility. It never opens a provider
    connection or refreshes credentials.

    ``claude_profiles`` is the caller's already-discovered Claude install list (see
    ``quota_state``), so the home directory is enumerated once per dashboard load rather
    than once per consumer of that list.
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
        "zai": (clientpaths.zcode_home() / "v2" / "config.json", ("ZAI_API_KEY", "Z_AI_API_KEY")),
    }
    for provider, (path, env_names) in checks.items():
        if path.exists() or any(os.environ.get(name, "").strip() for name in env_names):
            detected.add(provider)
    if os.environ.get("KIMI_API_KEY", "").strip() or any(root.exists() for root in clientpaths.kimi_roots()):
        detected.add("kimi")
    if os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip() and zai_coding_base_url_allowed(
        os.environ.get("ANTHROPIC_BASE_URL", "")
    ):
        detected.add("zai")
    if config.credential_scan_enabled():
        # A second sign-in in `~/.claude-<profile>` means the provider is configured even
        # with no `~/.claude` at all. `configured` is the same test `discover_profiles`
        # uses to admit an install, so a leftover directory that was never signed in can
        # neither open a card here nor open a group on it.
        if any(profile.configured for profile in claude_profiles if not profile.is_default):
            detected.add("claude")
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
        # the freshest real account. `_MULTI_ACCOUNT_CARDS` is the exception, and is the only
        # list of them: MiniMax's global and mainland-China Token Plans, and Claude's
        # `~/.claude` beside a `~/.claude-<profile>` sibling, are separate subscriptions whose
        # newest rows must not evict each other.
        account = (
            str(row.get("account") or "") if provider in _MULTI_ACCOUNT_CARDS else ""
        )
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
    # Claude's per-install facts are needed three times below: to decide whether the
    # provider is configured at all, to drop the stored windows of installs that no longer
    # exist, and to name each install on the card. So the home directory is enumerated once
    # here and each install's credential file opened once here, rather than once per
    # consumer. Opening a plan reads `.credentials.json` and can raise a macOS Keychain
    # prompt, so without `credential_scan` consent none of it happens.
    claude_scan = config.credential_scan_enabled()
    claude_profiles = discover_profiles()
    claude_installs = read_claude_profiles(claude_profiles) if claude_scan else []
    # A renamed or deleted `~/.claude-<profile>` leaves its window rows behind forever --
    # nothing expires a stored (account, bucket) row -- so a subscription the user removed
    # would hold a card group open on a month-old reading. Which accounts have fallen behind
    # is decided from the stored rows below, once their timestamps are known, rather than
    # from whether a credential file happens to be openable right now.
    providers = {
        name: _provider_shell(name, consent)
        for name in ("codex", "claude", "antigravity", "minimax", "kimi", "grok", "zai")
    }
    for name in _detected_local_providers(claude_profiles):
        providers[name]["detected"] = True
    last_network_run: int | None = _LAST_POLL_AT
    # When Codex API polling is enabled, the API is the sole oracle for the current-quota
    # cards: codex_session rows are excluded from bucket selection below so a newer cached
    # session row can never override a fresher API observation. Prefer
    # `config.network_enabled` (not raw `consent`) so the `TOKDASH_QUOTA_POLL` kill switch
    # is honored consistently with `quota_history`'s `network_only_providers` gate.
    network_only = {"codex"} if config.network_enabled("codex_api") else set()
    # One status view per (provider, account), plus a provider-wide view. Both are fed by
    # the same `_record_row`, so a card and its account list cannot disagree about which
    # error is still live, and neither needs to know that Claude has several installs.
    account_views: dict[str, dict[str, dict[str, Any]]] = {}
    provider_views: dict[str, dict[str, Any]] = {}
    for row in latest:
        provider = str(row.get("provider") or "")
        if provider not in providers:
            providers[provider] = _provider_shell(provider, consent)
        ref = providers[provider]
        account = str(row.get("account") or "default")
        # Stored quota data is evidence that the provider was configured even if its CLI
        # directory is temporarily unavailable (mounted home, migrated install, etc.).
        ref["detected"] = True
        _record_row(provider_views.setdefault(provider, _new_account_view()), row)
        _record_row(
            account_views.setdefault(provider, {}).setdefault(account, _new_account_view()), row
        )
        source = str(row.get("source") or "")
        if source.endswith("_api"):
            ref["network_enabled"] = True
            captured = int(row.get("captured_at") or 0)
            if captured:
                last_network_run = max(last_network_run or 0, captured)
        if row.get("source") and row.get("source") not in ref["sources"]:
            ref["sources"].append(row.get("source"))
        if provider == "codex" and row.get("bucket") == "reset_credits":
            reset_payload = row.get("raw", {}).get("reset_credits") if isinstance(row.get("raw"), dict) else {}
            if isinstance(reset_payload, dict):
                ref["reset_credits"] = {
                    "available_count": reset_payload.get("available_count", row.get("used_percent")),
                    "credits": reset_payload.get("credits") if isinstance(reset_payload.get("credits"), list) else [],
                }

    interval_seconds, interval_source = config.effective_poll_interval()
    # Age, not existence, retires an account's rows. An account whose own newest row has
    # fallen well behind the freshest row of its provider is one nothing polls any more,
    # which for a Claude install is also the only reading available once the directory is
    # gone. Whether a credential file opens right now says nothing about whether stored rows
    # are current: an unmounted or networked home that is not up yet, a permissions error, a
    # dotfile manager mid-relink or a logout all read as "deleted", and would hide a
    # subscription while its card still claimed to be freshly updated. Accounts polled
    # together also age together, so a home that is entirely unavailable loses none of them.
    # Providers that fold their accounts into one (`_MULTI_ACCOUNT_CARDS` again) age as one
    # account and so never drop anything here.
    stale_accounts: dict[str, set[str]] = {}
    for name, views in account_views.items():
        stale_accounts[name] = _stale_accounts(
            views,
            newest=int(provider_views.get(name, {}).get("updated_at") or 0),
            interval_seconds=interval_seconds,
        )
        for account in stale_accounts[name]:
            del views[account]

    for name, ref in providers.items():
        aggregate = provider_views.get(name)
        if aggregate is None:
            continue
        # `status` and `status_detail` are the newest live error of any account behind the
        # card (see `_provider_status`); `plan` and `updated_at` stay provider-wide, because
        # they answer what this provider reported and when it was last seen by anything.
        status, detail, status_at = _provider_status(aggregate, account_views.get(name, {}))
        ref["status_detail"] = detail
        ref["status_at"] = status_at
        ref["status"] = str(status or ref["status"])
        ref["plan"] = aggregate.get("plan")
        ref["updated_at"] = int(aggregate.get("updated_at") or 0) or None

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
    bucket_rows = [
        row
        for row in bucket_rows
        if str(row.get("account") or "default")
        not in stale_accounts.get(str(row.get("provider")), ())
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
        raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
        bucket_row["unlimited"] = raw.get("unlimited") is True
        # Additive: the UI displays remaining quota (TASK 1), but storage/other API
        # consumers keep reading used_percent unchanged.
        bucket_row["remaining_percent"] = None if used_percent is None else round(100.0 - float(used_percent), 4)
        providers[provider]["buckets"].append(bucket_row)

    providers["codex"]["plan"] = _codex_plan_label(providers["codex"]["plan"])
    # Codex cards are estimated (may include session-source data) exactly when codex_api
    # polling is off; claude/antigravity have no session source and are never estimated.
    providers["codex"]["estimated"] = "codex" not in network_only

    if claude_scan:
        # `plan`, `tier` and `credential_path` describe the DEFAULT install, unchanged from
        # before profiles existed, so the dashboard and both companion apps keep reading
        # the same thing. `accounts` below carries every install's own plan and failure,
        # which is what lets the card name a second subscription. The reads themselves
        # happened above, once, under the same credential-scan consent.
        primary = next(
            (p for p in claude_installs if p.get("account") == clientpaths.CLAUDE_DEFAULT_PROFILE),
            None,
        )
        if primary is None and claude_installs:
            primary = claude_installs[0]
        plan = (primary or {}).get("plan")
        if not plan:
            # Only one install reported a plan (e.g. `~/.claude` is signed out and all the
            # usage is in `~/.claude-academic`): the card can still name it.
            reported = {p.get("plan") for p in claude_installs if p.get("plan")}
            plan = next(iter(reported)) if len(reported) == 1 else None
        providers["claude"]["plan"] = plan
        if (primary or {}).get("status") == "ok":
            if providers["claude"]["status"] == "unavailable":
                providers["claude"]["status"] = "local_plan"
            providers["claude"]["detected"] = True
        providers["claude"]["credential_path"] = (primary or {}).get("credential_path")
        providers["claude"]["tier"] = (primary or {}).get("tier")

    # Per-account rows, for the cards that carry more than one account: `~/.claude` beside a
    # `~/.claude-<profile>` sibling, or a MiniMax global and China Token Plan. A card needs
    # this to attribute a failure to the account that owns it instead of blaming the card;
    # a card with one account does not, and its payload stays as it was.
    for name, ref in providers.items():
        if name not in _MULTI_ACCOUNT_CARDS:
            # Every other provider has its account folded into one by
            # `_freshest_usage_rows`, so a per-account list would name accounts the card
            # does not measure separately -- and for antigravity that name is an email.
            continue
        if name == "claude" and not claude_scan:
            # Which installs exist, and each one's plan, come from the filesystem. Without
            # `credential_scan` consent the card may not learn any of it, stored rows
            # included; those still render, just unattributed.
            continue
        # Aged-out accounts are already gone from these views.
        views = account_views.get(name, {})
        extra: dict[str, dict[str, Any]] = {}
        if name == "claude":
            # An install readable locally with nothing polled yet -- signed in, poll not run
            # -- joins the list from the credential side, so it is named before it has data.
            for install in claude_installs:
                account = str(install.get("account") or "")
                views.setdefault(account, _new_account_view())
                extra[account] = {
                    "plan": install.get("plan"),
                    "tier": install.get("tier"),
                    "credential_path": install.get("credential_path"),
                    # `read_claude_profiles` knows the install is signed in (or not) from
                    # its own file, which is the documented answer for an install with no
                    # snapshot rows yet.
                    "status": install.get("status"),
                }
        entries = _account_entries(name, views, extra)
        if len(entries) > 1:
            ref["accounts"] = entries

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
