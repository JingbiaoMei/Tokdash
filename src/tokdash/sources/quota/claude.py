from __future__ import annotations

import json
import os
import subprocess
import sys
from base64 import urlsafe_b64decode
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
import urllib.request
import time

from ... import clientpaths
from . import config as quota_config
from .codex import _normalize_percent, _parse_time
from .types import QuotaSnapshot

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
_KEYCHAIN_LABEL = f"macOS Keychain ({CLAUDE_KEYCHAIN_SERVICE})"
# Installs are polled concurrently because each request may hold its full timeout open:
# three installs behind a throttled endpoint would otherwise cost 3 x 15s of request time.
_MAX_CONCURRENT_FETCHES = 4
# Identity claims that survive a token refresh, in the order Claude Code has shipped them.
_IDENTITY_CLAIMS = ("account_id", "accountId", "organization_id", "sub", "email")


def _is_macos() -> bool:
    return sys.platform == "darwin"


@dataclass(frozen=True)
class ClaudeProfile:
    """One Claude Code install: ``~/.claude`` or a ``CLAUDE_CONFIG_DIR`` sibling.

    ``name`` is both the quota account id and the label the dashboard shows
    (``default``, ``academic``). A non-default profile reads only its own
    ``.credentials.json``: ``CLAUDE_CODE_OAUTH_TOKEN`` and the macOS Keychain item are
    per-user, so applying them to a sibling would report one subscription twice under
    two names.
    """

    name: str
    config_dir: Path
    is_default: bool = False

    @property
    def credential_path(self) -> Path:
        return self.config_dir / ".credentials.json"

    @property
    def bucket_prefix(self) -> str:
        """Bucket-id prefix, empty for the default profile.

        Windows are keyed per account (``session`` vs ``academic_session``) because
        ``quota_history`` unifies a series by ``(provider, bucket)`` alone: two
        subscriptions on one bucket id would interleave into one zigzag series. Keeping
        the default profile unprefixed leaves every row stored before this feature
        reading as the same series it was.
        """
        return "" if self.is_default else f"{self.name}_"

    @property
    def configured(self) -> bool:
        """Whether this install is really there, to the level each kind can be checked.

        One predicate serves both callers that need it, so they cannot drift:
        ``discover_profiles`` refuses to open a group for a sibling that was never signed
        in, and the Quota tab refuses to add a Claude card for a directory that is only a
        leftover copy. The default install counts on a config directory alone -- reporting
        it signed out is that install's own ``unavailable`` state -- or on the environment
        override, since a headless sign-in has no config directory to be there or not.
        """
        if self.is_default:
            return self.config_dir.is_dir() or bool(_env_token())
        return self.credential_path.is_file()


def _default_profile() -> ClaudeProfile:
    return ClaudeProfile(
        clientpaths.CLAUDE_DEFAULT_PROFILE, clientpaths.claude_config_dir(), True
    )


def discover_profiles() -> list[ClaudeProfile]:
    """Claude installs worth reporting quota for, default profile first.

    The default profile is always included — a missing ``.credentials.json`` there is
    itself the state the card reports. A ``~/.claude-*`` sibling is included only once
    it has its own ``.credentials.json``, so a leftover or unrelated ``.claude-*``
    directory cannot open an empty group on the card.

    Enumerating the home directory and opening the siblings' credential files is a
    credential access, so it is gated on ``quota.credential_scan`` like every other
    reader here, and the gate comes before the enumeration rather than after it: a
    machine whose user declined the consent must not have its home directory listed on
    every dashboard load. Without that consent this returns just the configured default
    dir, which is exactly the pre-profiles behavior.
    """
    if not quota_config.credential_scan_enabled():
        return [_default_profile()]
    profiles = [
        ClaudeProfile(name, path, name == clientpaths.CLAUDE_DEFAULT_PROFILE)
        for name, path in clientpaths.claude_profile_dirs()
    ]
    # The default profile stays whatever is on disk: a missing ``.credentials.json`` there
    # is the state the card reports, and ``CLAUDE_CODE_OAUTH_TOKEN`` headless users have no
    # config directory at all.
    return [p for p in profiles if p.is_default or p.configured]


def _read_keychain_credentials(keychain: str | None = None) -> dict[str, Any] | None:
    """Read the Claude Code credential blob from the macOS Keychain.

    On macOS, Claude Code stores the same JSON that Linux/Windows keep in
    ``.credentials.json`` as a login-Keychain generic password (service
    ``Claude Code-credentials``) — the same source ccstatusline and CodexBar read.
    Read-only, via the ``security`` CLI with an argument list (never a shell). Returns
    ``None`` off-macOS, when the item is missing, when the keychain is locked or access
    is denied, or when the payload is not a JSON object — callers degrade to
    ``unavailable`` plus the ``CLAUDE_CODE_OAUTH_TOKEN`` hint. The first read from a new
    binary may show a one-time Keychain permission prompt; the timeout keeps an
    unanswered prompt from wedging a poll cycle. ``keychain`` narrows the lookup to one
    keychain file (used by the CI integration test); production searches the default
    keychain list.
    """
    if not _is_macos():
        return None
    cmd = ["security", "find-generic-password", "-s", CLAUDE_KEYCHAIN_SERVICE, "-w"]
    if keychain:
        cmd.append(keychain)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    text = (result.stdout or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _env_token() -> str:
    return os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()


def _load_credential_data(profile: ClaudeProfile | None = None) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
    """Shared credential-source resolution: ``.credentials.json``, then the macOS Keychain.

    Callers check ``CLAUDE_CODE_OAUTH_TOKEN`` BEFORE calling this — the explicit override
    must short-circuit both sources, notably the Keychain subprocess and its potential
    permission prompt (it is the documented headless/locked-Keychain escape hatch).
    Only the default profile may fall back to the Keychain: the item is per-user and holds
    whichever install signed in last, so reading it for a sibling would attribute one
    subscription's token to another profile.
    Returns ``(data, source_label, error_meta)``: ``data`` is the parsed blob or ``None``
    on failure, with ``error_meta`` carrying the error fields.
    """
    profile = profile if profile is not None else _default_profile()
    path = profile.credential_path
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw, str(path), {}
        return None, str(path), {"error": "credentials_invalid", "message": "not a JSON object"}
    except FileNotFoundError:
        if profile.is_default:
            keychain_data = _read_keychain_credentials()
            if keychain_data is not None:
                return keychain_data, _KEYCHAIN_LABEL, {}
        return None, str(path), {"error": "credentials_not_found"}
    except Exception as exc:
        return None, str(path), {"error": "credentials_invalid", "message": str(exc)}


def read_claude_plan(profile: ClaudeProfile | None = None) -> dict[str, Any]:
    # Same source precedence as _read_credentials (env var > file > Keychain): the usage
    # data is fetched with the env token's account when the override is set, so plan/tier
    # must not be read from another source's (possibly different) account — and the
    # Keychain subprocess must not run at all. The env var carries no plan metadata.
    profile = profile if profile is not None else _default_profile()
    if profile.is_default and _env_token():
        return {"status": "ok", "plan": None, "tier": None, "credential_path": "CLAUDE_CODE_OAUTH_TOKEN"}
    data, source, _error = _load_credential_data(profile)
    if data is None:
        return {"status": "unavailable", "plan": None, "tier": None, "credential_path": source}

    oauth = data.get("claudeAiOauth") if isinstance(data.get("claudeAiOauth"), dict) else {}
    plan = oauth.get("subscriptionType") or data.get("subscriptionType")
    tier = oauth.get("rateLimitTier") or data.get("rateLimitTier")
    return {"status": "ok", "plan": _plan_label(plan, tier), "tier": tier, "credential_path": source}


def read_claude_profiles(
    profiles: list[ClaudeProfile] | None = None,
) -> list[dict[str, Any]]:
    """Local plan/tier state for every Claude install, default profile first.

    Drives the per-profile headings on the Claude quota card, so a second subscription
    gets its own name and plan line instead of hiding behind the default install's.

    ``profiles`` takes the caller's already-discovered list. ``quota_state`` needs these
    facts before it picks which stored rows are still current, so it enumerates once and
    passes the result down rather than triggering a second home-directory scan.
    """
    out: list[dict[str, Any]] = []
    for profile in discover_profiles() if profiles is None else profiles:
        state = read_claude_plan(profile)
        out.append(
            {
                "account": profile.name,
                "status": state.get("status"),
                "plan": state.get("plan"),
                "tier": state.get("tier"),
                "credential_path": state.get("credential_path"),
            }
        )
    return out


def _plan_label(plan: Any, tier: Any) -> str | None:
    """Human plan label for the card header: "Max 5x" / "Max 20x" / "Pro".

    Display-only — snapshot rows keep the raw subscription/tier strings.
    """
    tier_text = str(tier or "").lower()
    if "max_20x" in tier_text:
        return "Max 20x"
    if "max_5x" in tier_text:
        return "Max 5x"
    if plan:
        return str(plan).replace("_", " ").strip().title() or None
    return None


def _read_credentials(profile: ClaudeProfile | None = None) -> tuple[str | None, dict[str, Any]]:
    profile = profile if profile is not None else _default_profile()
    # ``account`` rides along in every status row's raw payload, so a failure can always
    # be attributed to the install that produced it.
    meta: dict[str, Any] = {"account": profile.name}
    if profile.is_default:
        env_token = _env_token()
        if env_token:
            return env_token, {
                **meta,
                "plan": None,
                "tier": None,
                "credential_path": "CLAUDE_CODE_OAUTH_TOKEN",
            }
    data, source, error_meta = _load_credential_data(profile)
    if data is None:
        return None, {**meta, **error_meta, "credential_path": source}
    oauth = data.get("claudeAiOauth") if isinstance(data.get("claudeAiOauth"), dict) else {}
    token = oauth.get("accessToken")
    plan = oauth.get("subscriptionType") or data.get("subscriptionType")
    tier = oauth.get("rateLimitTier") or data.get("rateLimitTier")
    return str(token) if token else None, {
        **meta,
        "expires_at_ms": oauth.get("expiresAt") or data.get("expiresAt"),
        "plan": "/".join(str(v) for v in (plan, tier) if v) or None,
        "tier": tier,
        "credential_path": source,
    }


def _subscription_identity(token: str) -> str:
    """Key for the subscription a Claude access token belongs to, used to spot one
    sign-in living in two directories.

    ``cp -r ~/.claude ~/.claude-copy`` copies the sign-in too, and both directories then
    report the same subscription's windows twice, which the consumption chart adds
    together. The token string cannot be the key: Claude Code refreshes it in whichever
    directory is actually used, so the copy keeps a different string for the same
    subscription and a token comparison silently stops matching. Claude Code's access
    token is a JWT whose payload names the account, and that survives the refresh, so the
    identity claim is the key and the raw token is only the fallback for a token that is
    not a JWT.

    The signature is deliberately not verified. This is a local dedupe key, not an
    authentication decision; the token goes to Anthropic, which is where that is settled.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return token
    try:
        payload = json.loads(urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except Exception:
        return token
    if not isinstance(payload, dict):
        return token
    for claim in _IDENTITY_CLAIMS:
        value = payload.get(claim)
        if value:
            return f"{claim}:{value}"
    return token


def _status_snapshot(
    status: str, captured_at: int, raw: dict[str, Any], profile: ClaudeProfile | None = None
) -> QuotaSnapshot:
    # The bucket stays "api" for every profile: both `quota_state` and `quota_history`
    # special-case that id, and `quota_snapshots` is already unique per account, so two
    # installs' failures cannot collide.
    account = (profile if profile is not None else _default_profile()).name
    return QuotaSnapshot("claude", account, "api", "Claude API", None, None, raw.get("plan"), captured_at, "claude_api", status, raw)


def _label_for_limit(limit: dict[str, Any]) -> tuple[str, str]:
    kind = str(limit.get("kind") or "usage")
    # Defensive: the API could return scope/model as something other than a dict (schema
    # drift). isinstance guards keep a string scope from raising AttributeError and 500ing
    # GET /api/quota/refresh — we simply fall back to the kind label.
    scope = limit.get("scope")
    scope = scope if isinstance(scope, dict) else {}
    model_obj = scope.get("model")
    model_obj = model_obj if isinstance(model_obj, dict) else {}
    model = str(model_obj.get("display_name") or "").strip()
    if model:
        slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in model).strip("_")
        return f"{kind}_{slug}", model
    return kind, kind.replace("_", " ").title()


def _profile_snapshots(
    profile: ClaudeProfile,
    token: str | None,
    meta: dict[str, Any],
    *,
    opener=urllib.request.urlopen,
    captured_at: int,
    timeout: float = 15.0,
) -> list[QuotaSnapshot]:
    """One install's usage windows, or a status snapshot when there is nothing to fetch.

    A sibling profile that yields nothing reports nothing: `discover_profiles` already
    required its credential file, so an empty result here is a state the group heading
    explains through its own status, not a card-wide absence of data. The default
    profile keeps reporting ``unavailable`` — that row is what drives the consent and
    "not detected" card.
    """
    if not token:
        return [] if not profile.is_default else [_status_snapshot("unavailable", captured_at, meta, profile)]
    expires_ms = meta.get("expires_at_ms")
    try:
        if expires_ms and int(expires_ms) // 1000 <= captured_at:
            return [_status_snapshot("stale_token", captured_at, meta, profile)]
    except Exception:
        pass
    req = urllib.request.Request(
        CLAUDE_USAGE_URL,
        headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20", "Accept": "application/json"},
    )
    payload: dict[str, Any] | None = None
    try:
        for attempt in range(2):
            try:
                with opener(req, timeout=timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                break
            except HTTPError as exc:
                if exc.code not in {500, 502, 503, 504} or attempt == 1:
                    raise
                time.sleep(0.2)
    except HTTPError as exc:
        status = "stale_token" if exc.code in {401, 403} else "fetch_error"
        return [_status_snapshot(status, captured_at, {**meta, "error": f"HTTP {exc.code}: {exc.reason}"}, profile)]
    except Exception as exc:
        return [_status_snapshot("fetch_error", captured_at, {**meta, "error": str(exc)}, profile)]
    if payload is None:
        return [_status_snapshot("fetch_error", captured_at, {**meta, "error": "empty_response"}, profile)]
    limits = payload.get("limits") if isinstance(payload.get("limits"), list) else []
    out: list[QuotaSnapshot] = []
    for limit in limits:
        if not isinstance(limit, dict):
            continue
        # A single malformed entry should be skipped, never abort the whole fetch (which
        # would surface as a raw 500 on /api/quota/refresh instead of a fetch_error).
        try:
            used = _normalize_percent(limit.get("percent", limit.get("utilization")))
            if used is None:
                continue
            bucket, label = _label_for_limit(limit)
        except Exception:
            continue
        out.append(
            QuotaSnapshot(
                "claude",
                profile.name,
                f"{profile.bucket_prefix}{bucket}",
                label,
                used,
                _parse_time(limit.get("resets_at")),
                meta.get("plan"),
                captured_at,
                "claude_api",
                "ok",
                {"limit": limit},
            )
        )
    if out:
        return out
    for key, label in (("five_hour", "5-hour window"), ("seven_day", "7-day window")):
        obj = payload.get(key) if isinstance(payload.get(key), dict) else {}
        used = _normalize_percent(obj.get("utilization"))
        if used is not None:
            out.append(QuotaSnapshot("claude", profile.name, f"{profile.bucket_prefix}{key}", label, used, _parse_time(obj.get("resets_at")), meta.get("plan"), captured_at, "claude_api", "ok", {"limit": obj}))
    return out or [_status_snapshot("unavailable", captured_at, {**meta, "error": "no_limits"}, profile)]


def collect_claude_api_snapshots(
    *,
    opener=urllib.request.urlopen,
    now: int | None = None,
    timeout: float = 15.0,
    profiles: list[ClaudeProfile] | None = None,
) -> list[QuotaSnapshot]:
    """Usage windows for every Claude Code install on this machine, one fetch each.

    Each install is fetched separately and a failure lands on its own status row, so an
    expired token in one directory cannot blank another subscription's windows. Two
    directories holding the same sign-in report once (see `_subscription_identity`): it is
    one subscription, and reporting it twice would double its usage in the chart.
    """
    captured_at = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    selected = profiles if profiles is not None else discover_profiles()
    # Credentials are read serially first: it is local file I/O, and it lets one
    # subscription be recognised as a duplicate before any request goes out for it.
    jobs: list[tuple[ClaudeProfile, str | None, dict[str, Any]]] = []
    seen_accounts: set[str] = set()
    for profile in selected:
        token, meta = _read_credentials(profile)
        if token:
            identity = _subscription_identity(token)
            if identity in seen_accounts:
                continue
            seen_accounts.add(identity)
        jobs.append((profile, token, meta))
    if len(jobs) == 1:
        # The common case, and it stays a direct call: no pool, no thread, in-process.
        profile, token, meta = jobs[0]
        return _profile_snapshots(
            profile, token, meta, opener=opener, captured_at=captured_at, timeout=timeout
        )
    if not jobs:
        return []
    # Concurrent because every install may hold its full timeout open, and a slow
    # api.anthropic.com would otherwise add 15s per install to the poll cycle and to
    # GET /api/quota/refresh. Results are collected in profile order, so a card's
    # install sequence never depends on which socket answered first.
    with ThreadPoolExecutor(
        max_workers=min(len(jobs), _MAX_CONCURRENT_FETCHES),
        thread_name_prefix="claude-quota",
    ) as pool:
        futures = [
            pool.submit(
                _profile_snapshots,
                profile,
                token,
                meta,
                opener=opener,
                captured_at=captured_at,
                timeout=timeout,
            )
            for profile, token, meta in jobs
        ]
        return [snapshot for future in futures for snapshot in future.result()]
