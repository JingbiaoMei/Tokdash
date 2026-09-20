from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
import urllib.request

from ... import clientpaths
from .codex import _parse_time
from .types import QuotaSnapshot

PROVIDER = "commandcode"
SOURCE = "commandcode_api"

# Single fixed endpoint (ported from the vendor's own shipped CLI, npm
# command-code@1.54.2, dist/cli.mjs): no per-user base URL, no staging/local
# override, so no allowlist check is needed before attaching the bearer token.
_API_BASE = "https://api.commandcode.ai"
_WHOAMI_PATH = "/alpha/whoami"
_CREDITS_PATH = "/alpha/billing/credits"
_SUBSCRIPTIONS_PATH = "/alpha/billing/subscriptions"

_TIMEOUT_SECONDS = 15.0
_MAX_BODY_BYTES = 1024 * 1024

# Both ecosystem spellings exist: the vendor CLI reads COMMAND_CODE_API_KEY,
# while the opencode-cmd-provider plugin documents COMMANDCODE_API_KEY.
_ENV_VARS = ("COMMAND_CODE_API_KEY", "COMMANDCODE_API_KEY")

_AUTH_KEYS = ("commandcode",)

# The vendor CLI's own catalog, keyed by its real plan ids. Display names follow the
# plan-doc wording (Max 10x / Max 20x) rather than the CLI's short "Max"/"Ultra".
_PLAN_CATALOG: dict[str, tuple[str, int]] = {
    "individual-go": ("Go", 10),
    "individual-goat": ("GOAT", 70),
    "individual-pro": ("Pro", 30),
    "individual-pro-v1": ("Pro", 80),
    "individual-provider": ("Provider", 15),
    "individual-max": ("Max 10x", 150),
    "individual-ultra": ("Max 20x", 300),
    "teams-pro": ("Team Pro", 40),
}
# Longest alias first, so `individual-pro-v1` cannot be shadowed by `individual-pro`.
_PLAN_ALIASES = tuple(sorted(_PLAN_CATALOG, key=len, reverse=True))

# The vendor CLI holds a subscription's plan only in these states (`Kr` in dist/cli.mjs).
_PLAN_STATUS_GATE = frozenset({"active", "trialing", "past_due"})

# Known catalog disagreement, recorded here so stored rows can be read against it: the
# vendor's pricing page advertises Pro at $80 of monthly usage, while its shipping CLI
# carries both `individual-pro: 30` and `individual-pro-v1: 80` — and the reported Pro
# caps (16/40) imply 80. We mirror the CLI, which understates the pool for an account
# whose live planId is the older `individual-pro`: the bar floors at 0% used rather than
# going negative. GOAT is unaffected — pricing page, CLI table and the reported 14/35
# caps all agree at 70.
#
# History note (no `usage_store` change, by design): Command Code's 5-hour, Weekly and
# Monthly windows are FIRST-USE-ANCHORED FIXED-EPOCH windows. `used` falls only at a
# rollover, and a rollover also advances `resets_at`, so `quota_history`'s running-high
# path is already the correct consumption model and
# `_quota_history_uses_adjacent_deltas` needs no commandcode branch. The same
# fixed-epoch property makes each window a legitimate boundary-poll target in
# `_boundary_candidate_details` — intended, not a bug.

_WINDOWS = (("fiveHour", "5h", "5-hour"), ("weekly", "7d", "Weekly"))
_MONTHLY_BUCKET = ("monthly", "Monthly")


@dataclass(frozen=True)
class _Credential:
    token: str
    source: str


def _entry_token(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ""
    for key in ("apiKey", "key", "api_key", "token"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _env_key() -> _Credential | None:
    for name in _ENV_VARS:
        token = os.environ.get(name, "").strip()
        if token:
            return _Credential(token, name)
    return None


def _native_file_key() -> _Credential | None:
    # The vendor CLI writes a ROOT-level `{apiKey, userName}`. Only the allowlisted
    # token field names are read, so an unrelated file dropped in this directory can
    # never have some nested value mistaken for the key.
    try:
        data = json.loads(clientpaths.commandcode_auth_path().read_text(encoding="utf-8"))
    except Exception:
        return None
    token = _entry_token(data)
    return _Credential(token, "~/.commandcode/auth.json") if token else None


def _opencode_auth_key() -> _Credential | None:
    try:
        data = json.loads(clientpaths.opencode_auth_path().read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    for auth_key in _AUTH_KEYS:
        entry = data.get(auth_key)
        if isinstance(entry, dict) and str(entry.get("type") or "").strip().lower() == "oauth":
            continue
        token = entry.strip() if isinstance(entry, str) else _entry_token(entry)
        if token:
            return _Credential(token, f"opencode auth.json:{auth_key}")
    return None


def read_commandcode_key() -> _Credential | None:
    """Read COMMAND_CODE_API_KEY/COMMANDCODE_API_KEY first, then the two auth files.

    Read-only: the vendor CLI's `~/.commandcode/auth.json` is never refreshed or
    rewritten, and OpenCode's auth.json entry is only read.
    """
    return _env_key() or _native_file_key() or _opencode_auth_key()


def has_credentials() -> bool:
    return read_commandcode_key() is not None


def _status_snapshot(status: str, captured_at: int, credential: _Credential | None, raw: dict[str, Any]) -> QuotaSnapshot:
    return QuotaSnapshot(
        PROVIDER, "default", "api", "Command Code API", None, None, None,
        captured_at, SOURCE, status,
        {"credential_source": credential.source if credential else None, **raw},
    )


def _read_capped(resp: Any) -> bytes:
    data = resp.read(_MAX_BODY_BYTES + 1)
    if len(data) > _MAX_BODY_BYTES:
        raise ValueError(f"response exceeds {_MAX_BODY_BYTES} byte cap")
    return data


def _url(path: str, org_id: str | None) -> str:
    # Mirrors the CLI's buildUsageEndpoint: a null param is omitted, not sent empty.
    query = urlencode({"orgId": org_id}) if org_id else ""
    return f"{_API_BASE}{path}?{query}" if query else f"{_API_BASE}{path}"


def _error_detail(payload: Any, body: str) -> str:
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            detail = str(err.get("message") or "").strip()
            if detail:
                return detail
        elif err is not None:
            detail = str(err).strip()
            if detail:
                return detail
    return body.strip()[:200]


def _get_json(
    opener: Any, url: str, credential: _Credential, captured_at: int, timeout: float
) -> tuple[dict[str, Any] | None, QuotaSnapshot | None]:
    """GET one JSON object, returning ``(payload, None)`` or ``(None, failure row)``.

    Every failure path returns a single ``api`` row carrying THIS cycle's
    ``captured_at``, so a same-cycle success can never retire a same-cycle error.
    """
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {credential.token}",
            "Accept": "application/json",
            "User-Agent": "tokdash/commandcode-quota",
        },
    )
    try:
        with opener(req, timeout=timeout) as resp:
            payload = json.loads(_read_capped(resp).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("response is not a JSON object")
        if payload.get("success") is False:
            detail = _error_detail(payload, "") or "request failed"
            return None, _status_snapshot("fetch_error", captured_at, credential, {"error": detail[:220]})
        return payload, None
    except HTTPError as exc:
        try:
            body = exc.read(_MAX_BODY_BYTES + 1)[:_MAX_BODY_BYTES].decode("utf-8", "replace")
        except Exception:
            body = ""
        try:
            error_payload = json.loads(body) if body else None
        except ValueError:
            error_payload = None
        detail = _error_detail(error_payload, body)
        if exc.code in (401, 403):
            return None, _status_snapshot("stale_token", captured_at, credential, {
                "error": f"HTTP {exc.code}: {detail}"[:220],
                "hint": "Run 'cmd login' in Command Code to refresh the sign-in, or check COMMAND_CODE_API_KEY / COMMANDCODE_API_KEY.",
            })
        return None, _status_snapshot("fetch_error", captured_at, credential, {"error": f"HTTP {exc.code}: {detail}"[:220]})
    except Exception as exc:
        return None, _status_snapshot("fetch_error", captured_at, credential, {"error": str(exc)[:220]})


def _normalize_plan_id(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def resolve_plan(plan_id: Any) -> tuple[str, int] | None:
    """``(display name, catalog credits)`` for a plan id, or None when unknown.

    Mirrors the vendor CLI's ``getPlanInfo``: lowercase, underscores -> hyphens, then
    the LONGEST alias that the normalized id starts with.
    """
    normalized = _normalize_plan_id(plan_id)
    if not normalized:
        return None
    for alias in _PLAN_ALIASES:
        if normalized.startswith(alias):
            return _PLAN_CATALOG[alias]
    return None


def _org_id(payload: dict[str, Any] | None) -> str | None:
    org = payload.get("org") if isinstance(payload, dict) else None
    org_id = org.get("id") if isinstance(org, dict) else None
    return str(org_id) if org_id else None


def _positive_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, number)


def _clamped_percent(used: float, cap: float) -> float:
    if cap <= 0:
        return 0.0
    return round(max(0.0, min(100.0, used / cap * 100.0)), 4)


def _window_rows(
    limits: Any, captured_at: int, credential: _Credential, plan: str | None
) -> list[QuotaSnapshot]:
    rows: list[QuotaSnapshot] = []
    for key, bucket, label in _WINDOWS:
        window = limits.get(key) if isinstance(limits, dict) and limits.get("limited") is True else None
        cap = _positive_float(window.get("cap")) if isinstance(window, dict) else 0.0
        if cap <= 0:
            # Stored buckets survive omission: explicitly retire a removed limit and
            # its old plan, just as we do for an unresolved Monthly window.
            rows.append(_suppression_row(captured_at, credential, "no_usage_limit", (bucket, label)))
            continue
        used = _positive_float(window.get("used"))
        rows.append(
            QuotaSnapshot(
                PROVIDER, "default", bucket, label, _clamped_percent(used, cap),
                _parse_time(window.get("resetAt")), plan, captured_at, SOURCE, "ok",
                {"credential_source": credential.source, "window": window},
            )
        )
    return rows


def _suppression_row(
    captured_at: int, credential: _Credential, reason: str,
    bucket: tuple[str, str] = _MONTHLY_BUCKET,
) -> QuotaSnapshot:
    """A removed or unresolved bucket's withdrawal row.

    Omitting the bucket would leave its last stored row as the freshest one for
    ``_freshest_usage_rows``, so an unsubscribed account would keep rendering a stale bar
    and keep naming its old plan. ``status`` stays ``ok`` (this is not an error) and
    ``used_percent``/``plan`` are null, so the bar and the plan label both disappear.

    This row shares its cycle's ``captured_at`` with the reading it withdraws, so it must
    be written to the same observation slot. The store upserts a taken slot (latest write
    wins) rather than ignoring it, which is what lets the withdrawal land inside the same
    wall-clock second; under an ignore-on-conflict insert it was discarded and the stale
    bar lingered until a later cycle happened to land in a different second.
    """
    return QuotaSnapshot(
        PROVIDER, "default", bucket[0], bucket[1], None, None, None,
        captured_at, SOURCE, "ok",
        {"credential_source": credential.source, "suppressed": reason},
    )


def _monthly_row(
    credits: dict[str, Any],
    subscription: dict[str, Any] | None,
    plan_id: Any,
    resolved: tuple[str, int],
    captured_at: int,
    credential: _Credential,
) -> QuotaSnapshot:
    """Derive Monthly exactly as the vendor CLI does (``projectUsageView``).

    pool = max(catalog credits, monthlyCredits remaining) + purchased + free;
    used = pool - total remaining; percent clamped; resets_at = currentPeriodEnd.
    The credits block, the plan id/status, the resolved catalog credits, the derived
    pool and the reported windowLimits all ride along in raw_json so catalog drift
    against a live account is visible in stored rows.
    """
    block = credits.get("credits") if isinstance(credits.get("credits"), dict) else {}
    balance = block.get("monthlyCredits")
    try:
        monthly = float(balance)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("invalid_monthly_credits") from None
    if isinstance(balance, bool) or not math.isfinite(monthly) or monthly < 0:
        raise ValueError("invalid_monthly_credits")
    purchased = _positive_float(block.get("purchasedCredits"))
    free = _positive_float(block.get("freeCredits"))

    plan_label, plan_credits = resolved
    # An understated pool (see the catalog note above) floors the bar at 0% used rather
    # than going negative, because the percent is clamped.
    pool = max(float(plan_credits), monthly) + purchased + free
    used = pool - (monthly + purchased + free)
    percent = _clamped_percent(used, pool)

    return QuotaSnapshot(
        PROVIDER, "default", _MONTHLY_BUCKET[0], _MONTHLY_BUCKET[1], percent,
        _parse_time((subscription or {}).get("currentPeriodEnd")), plan_label,
        captured_at, SOURCE, "ok",
        {
            "credential_source": credential.source,
            "plan_id": _normalize_plan_id(plan_id),
            "plan_credits": plan_credits,
            "pool": pool,
            "monthly_credits": monthly,
            "purchased_credits": purchased,
            "free_credits": free,
            "credits": credits.get("credits"),
            "window_limits": credits.get("windowLimits"),
            "subscription_status": (subscription or {}).get("status"),
        },
    )


def collect_commandcode_api_snapshots(
    *, opener=urllib.request.urlopen, now: int | None = None, timeout: float = _TIMEOUT_SECONDS
) -> list[QuotaSnapshot]:
    """Collect Command Code quota rows for one cycle, sharing ONE ``captured_at``.

    whoami is best-effort (its only job is an orgId to scope the billing calls, and
    org limits are out of scope); credits and subscriptions each fail independently so
    a failed leg never hides the other leg's windows.
    """
    captured_at = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    credential = read_commandcode_key()
    if credential is None:
        return [_status_snapshot("unavailable", captured_at, None, {"error": "credentials_not_found"})]

    # whoami is best-effort: its only job is an orgId to scope the billing requests
    # (org limits themselves are out of scope), so its own failure is not recorded.
    whoami, _whoami_error = _get_json(opener, _url(_WHOAMI_PATH, None), credential, captured_at, timeout)
    org_id = _org_id(whoami)

    snapshots: list[QuotaSnapshot] = []
    failures: list[QuotaSnapshot] = []

    credits, credits_error = _get_json(
        opener, _url(_CREDITS_PATH, org_id), credential, captured_at, timeout
    )
    if credits is None:
        failures.append(credits_error or _status_snapshot(
            "fetch_error", captured_at, credential, {"error": "credits unavailable"}
        ))
    else:
        credits_plan_id = None
        if isinstance(credits.get("credits"), dict):
            credits_plan_id = credits["credits"].get("planId")

        subscription: dict[str, Any] | None = None
        payload, subscription_error = _get_json(
            opener, _url(_SUBSCRIPTIONS_PATH, org_id), credential, captured_at, timeout
        )
        if payload is not None:
            data = payload.get("data")
            subscription = data if isinstance(data, dict) else None
        else:
            failures.append(subscription_error or _status_snapshot(
                "fetch_error", captured_at, credential, {"error": "subscriptions unavailable"}
            ))

        status = str((subscription or {}).get("status") or "").strip().lower()
        plan_id = (subscription or {}).get("planId")
        # Resolve to the id that ACTUALLY resolved, or the raw evidence would name a leg the
        # label did not come from: the subscription leg takes precedence over the credits
        # fallback, but an unresolvable subscription id must fall through rather than be
        # passed only because it was truthy.
        resolved = resolve_plan(plan_id)
        plan_id_used = plan_id
        if resolved is None:
            resolved = resolve_plan(credits_plan_id)
            plan_id_used = credits_plan_id
        if subscription is None or status not in _PLAN_STATUS_GATE or resolved is None:
            # Never guess: no bar and no plan label, and the bucket is withdrawn rather
            # than merely skipped (see `_suppression_row`).
            snapshots.append(_suppression_row(captured_at, credential, "plan_unresolved"))
        else:
            try:
                monthly_row = _monthly_row(
                    credits, subscription, plan_id_used, resolved, captured_at, credential,
                )
            except ValueError as exc:
                # A malformed balance is a failed observation, not an exhausted pool.
                # Keep the last valid Monthly reading under the error notice.
                failures.append(_status_snapshot(
                    "fetch_error", captured_at, credential, {"error": str(exc)},
                ))
            else:
                snapshots.append(monthly_row)

        # The plan label is carried only when the window rows' plan actually resolved;
        # the suppression case above leaves them plan-less along with the Monthly bar.
        window_plan = resolved[0] if resolved is not None and subscription is not None and status in _PLAN_STATUS_GATE else None
        window_rows = _window_rows(credits.get("windowLimits"), captured_at, credential, window_plan)
        snapshots.extend(window_rows)
        if not any(row.used_percent is not None for row in window_rows):
            failures.append(_status_snapshot("unavailable", captured_at, credential, {"error": "no_usage_limits"}))

    # Failure rows precede snapshot rows in this list, but the order does NOT decide the
    # account's status: all rows share one captured_at, and `_account_status`/`_resolved_status`
    # (quota/__init__.py) let a same-cycle live `api` error win regardless of bucket order.
    return [*failures, *snapshots]
