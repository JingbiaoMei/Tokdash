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
from stat import S_ISREG
from typing import Any
from urllib.error import HTTPError
import urllib.request
import time

from ... import __version__, clientpaths
from . import config as quota_config
from .codex import _normalize_percent, _parse_time
from .types import QuotaSnapshot

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# `cedar_ember=1` asks Anthropic to include Claude Code's limit-reset block (the vouchers
# `/limit-reset` spends) in the same usage response, so reading them costs no extra request.
# The CLI also sends `skip_spend=1`; we do not, because it nulls `spend`/`extra_usage` in
# the same body.
CLAUDE_USAGE_URL_WITH_RESETS = f"{CLAUDE_USAGE_URL}?cedar_ember=1"
# Anthropic decides which surface a reset grant is offered on from the User-Agent: with
# Python's default one the block answers `ineligible_reason: "surface"` and lists no grants,
# while the Claude Code CLI's own prefix gets the grants that CLI sign-in actually holds. The
# token polled here IS a Claude Code CLI sign-in, so the request carries the CLI's prefix and
# then names tokdash, which the server accepts (measured 2026-09-23). The version is the CLI
# release that shape was measured against; it gates nothing tokdash reads.
CLAUDE_CODE_UA_VERSION = "2.1.280"
CLAUDE_USAGE_USER_AGENT = f"claude-cli/{CLAUDE_CODE_UA_VERSION} (external, cli) tokdash/{__version__}"
# A proxy or server that does not know the reset flag answers from this family. The windows
# are still worth having, so those statuses get one retry at the plain URL. 429 is kept out
# on purpose: retrying a rate-limited endpoint doubles the traffic it just asked us to cut.
_RESET_FLAG_REJECTED = frozenset({400, 404, 405, 422})
RESET_CREDITS_BUCKET = "reset_credits"
CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
_KEYCHAIN_LABEL = f"macOS Keychain ({CLAUDE_KEYCHAIN_SERVICE})"
# Installs are polled concurrently because each request may hold its full timeout open:
# three installs behind a throttled endpoint would otherwise cost 3 x 15s of request time.
_MAX_CONCURRENT_FETCHES = 4
# Identity claims that survive a token refresh, in the order Claude Code has shipped them.
# All of them name the SIGN-IN, which is what a copied install shares. `organization_id` is
# deliberately absent: one Team or Enterprise organization has one organization id and one
# distinct seat per member, so keying on it would fold two people's subscriptions into one
# and silently drop the second install's numbers.
_IDENTITY_CLAIMS = ("account_id", "accountId", "sub", "email")


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

        One predicate serves every caller that needs it, so they cannot drift:
        ``discover_profiles`` admits an install with it, and the Quota tab refuses to add a
        Claude card for a directory that is only a leftover copy. The default install counts
        on a config directory alone -- reporting it signed out is that install's own
        ``unavailable`` state -- or on the environment override, since a headless sign-in has
        no config directory to be there or not. A sibling needs its own credential file,
        because a directory copied or restored into place is not a subscription -- and
        neither is one that cannot be stat'd, an ``EACCES`` out of a mode-000 install, which
        answers the same way: no credential to poll with. That is a different question from
        the file being GONE, which is what ``credential_observably_absent`` asks, and it is
        why this swallows the error rather than letting it answer that one:
        ``Path.is_file()`` re-raises whatever ``_ignore_error`` does not recognise,
        ``EACCES`` included, and it runs on every dashboard load.

        Note this is not the same question as "should this install be reported": the default
        install is reported even when this is false, so that a machine with no Claude Code at
        all still names the reason instead of showing an empty card. See ``scan_profiles``.
        """
        if self.is_default:
            return self.config_dir.is_dir() or bool(_env_token())
        try:
            return self.credential_path.is_file()
        except OSError:
            return False

    @property
    def credential_observably_absent(self) -> bool:
        """Whether there is observably no credential FILE at this path.

        Asked in ``configured``'s terms, or the two disagree again: that one demands
        ``is_file()``, so anything other than a regular file -- nothing at the path, or a
        directory a dotfile manager left in the file's place -- stops the polling just as
        surely and leaves the same permanently frozen card this predicate exists to end. So
        absence means ``ENOENT`` or "not a regular file", and nothing else.

        A file that will not OPEN is the opposite case, and the one to keep reporting for:
        an ``EACCES`` on the file, or on a directory that cannot be searched, fails the read
        while leaving ``stat()`` free to answer, and a regular file there gets polled again
        the moment it opens. Hence ``OSError`` is "cannot tell".

        What neither covers is a credential that goes missing and comes back -- an
        unlink-then-relink, a dangling symlink, a rename caught mid-flight. That reads as
        absence and blanks the install's bars for one poll cycle, which is the accepted cost
        and the asymmetric direction: retirement hides stored rows, so the next cycle puts
        them back, while quoting rows nothing can ever refresh has no such way back.
        """
        try:
            return not S_ISREG(self.credential_path.stat().st_mode)
        except FileNotFoundError:
            return True
        except OSError:
            return False


def _default_profile() -> ClaudeProfile:
    return ClaudeProfile(
        clientpaths.CLAUDE_DEFAULT_PROFILE, clientpaths.claude_config_dir(), True
    )


@dataclass(frozen=True)
class ProfileScan:
    """One enumeration of this machine's Claude installs, and how much to trust it.

    ``profiles`` is what to report quota for. ``known`` is every install the scan actually
    saw a directory for, or ``None`` when the enumeration was not trustworthy enough to
    conclude anything from a name's absence -- see ``_namespace_trusted``. ``signed_out`` is
    the installs inside that listing whose sign-in is observably gone -- see
    ``_signed_out_names``. All three come out of a single pass, because the home directory is
    enumerated once per dashboard load.
    """

    profiles: list[ClaudeProfile]
    known: frozenset[str] | None
    signed_out: frozenset[str] = frozenset()


def _namespace_trusted(profiles: list[ClaudeProfile]) -> bool:
    """Whether "this name was not found" may be read as "this install is gone".

    Retiring an install's stored windows on absence is only safe when absence was really
    observed. The oracle is therefore the *listing that names the installs*, never an
    individual install's files: a sibling that is present but unreadable -- a credential
    file mid-write, a mode-000 directory, a dotfile manager mid-relink -- stays in its
    parent's listing and so stays known, which is what keeps a transient read from looking
    like a deletion. An install that IS listed but has no sign-in left to poll is the one
    absence an individual install's own files do answer, and ``_signed_out_names`` settles
    it.

    With ``TOKDASH_CLAUDE_PROFILES`` the variable names the installs outright, so any
    listed path that is not a directory right now means the answer is unavailable (an
    unmounted volume), not that the install was removed. Otherwise the listing is the home
    directory, and failing to read it at all -- unmounted, not yet mounted, EPERM -- is the
    same unavailable answer.

    Reading the listing is necessary but not sufficient: at least one install directory has
    to be VISIBLE in it. A listing that names no install at all is not the news that every
    install was deleted, and the ways to get one are ordinary -- an autofs or NFS stub
    before the mount triggers, an fscrypt/ecryptfs home before unlock (entries are there,
    their names are ciphertext, so nothing matches ``.claude*``), a roaming profile
    mid-sync. Every one of those would otherwise retire every install on the machine at
    once, which is the transient-for-deleted confusion this whole predicate exists to stop.
    """
    # Every failure mode here means "untrusted", including the ones that are not OSError:
    # `Path.home()` raises RuntimeError with no resolvable home, and an unreadable listing
    # must never be able to 500 the dashboard on its way to being unanswerable.
    try:
        explicit = os.environ.get("TOKDASH_CLAUDE_PROFILES", "").strip()
        if explicit:
            if not all(
                Path(entry).expanduser().is_dir()
                for entry in explicit.split(os.pathsep)
                if entry.strip()
            ):
                return False
        else:
            with os.scandir(Path.home()) as entries:
                next(iter(entries), None)
        return any(profile.config_dir.is_dir() for profile in profiles)
    except Exception:
        return False


def scan_profiles() -> ProfileScan:
    """Claude installs worth reporting quota for, default profile first.

    A sibling is admitted on ``configured`` -- its own ``.credentials.json`` -- which keeps a
    leftover or unrelated ``.claude-*`` directory from opening an empty group on the card.
    The default install is admitted whether or not it is configured, because its row is what
    drives the consent prompt and the "not detected" card: a machine with no ``~/.claude`` at
    all has to name that reason rather than render an empty card, and the consent-declined
    path below returns that same row. The one case where it is dropped is a machine whose
    ``~/.claude`` is not there AND whose sign-in lives in a sibling: the subscription that IS
    installed then speaks for the card, with no ``unavailable`` notice over the top of it.

    Enumerating the home directory and opening the siblings' credential files is a
    credential access, so it is gated on ``quota.credential_scan`` like every other
    reader here, and the gate comes before the enumeration rather than after it: a
    machine whose user declined the consent must not have its home directory listed on
    every dashboard load. Without that consent this returns just the default dir, which is
    exactly the pre-profiles behavior, and ``known`` is ``None`` -- nothing was looked at, so
    nothing may be concluded gone.
    """
    if not quota_config.credential_scan_enabled():
        return ProfileScan([_default_profile()], None)
    profiles = [
        ClaudeProfile(name, path, name == clientpaths.CLAUDE_DEFAULT_PROFILE)
        for name, path in clientpaths.claude_profile_dirs()
    ]
    default = next((p for p in profiles if p.is_default), _default_profile())
    siblings = [p for p in profiles if not p.is_default and p.configured]
    reportable = siblings if siblings and not default.configured else [default, *siblings]
    trusted = _namespace_trusted(profiles)
    return ProfileScan(
        reportable,
        _known_names(profiles, default, trusted),
        _signed_out_names(profiles) if trusted else frozenset(),
    )


def _known_names(
    profiles: list[ClaudeProfile], default: ClaudeProfile, trusted: bool
) -> frozenset[str] | None:
    """Account names whose install this scan saw a directory for, or ``None`` for "cannot tell".

    Two things this is careful about, because a name that wrongly falls outside it retires a
    subscription's stored windows:

    ``claude_profile_dirs`` names the default install whether or not it is there -- it emits
    ``("default", claude_config_dir())`` before any existence check -- so the default's own
    presence has to be observed separately, or it could never be retired. It has to be
    retirable: migrating to ``~/.claude-work`` and deleting ``~/.claude`` is an ordinary
    thing to do, and it would otherwise leave a month-old bar and a permanent
    ``stale_token`` on the card for an install that is gone. ``configured`` is that
    observation (its directory, or the ``CLAUDE_CODE_OAUTH_TOKEN`` override that stands in
    for one), and a parent that cannot be listed means it was not observed at all, so it
    stays known.

    Both the ASSIGNED and the INTRINSIC slug of every directory count. Slug allocation is
    relative to ``CLAUDE_CONFIG_DIR``: point it at ``~/.claude-academic`` and that install
    is renamed ``default`` while ``~/.claude`` beside it is renamed ``claude``, so a stored
    ``academic`` account would fall outside a set of assigned names alone -- and be retired
    with its directory sitting right there in the listing. Membership answers "is this
    install's directory present", which must not depend on an environment variable, so a
    directory is known under every name it could have been stored under.
    """
    if not trusted:
        return None
    names: set[str] = set()
    for profile in profiles:
        if profile.is_default:
            continue
        names.add(profile.name)
        names.add(clientpaths.claude_profile_slug(profile.config_dir))
    if default.configured or not _listable(default.config_dir.parent):
        names.add(default.name)
        names.add(clientpaths.claude_profile_slug(default.config_dir))
    return frozenset(names)


def _signed_out_names(profiles: list[ClaudeProfile]) -> frozenset[str]:
    """Sibling installs that are still on disk with no sign-in left to poll.

    ``scan_profiles`` admits a sibling on ``configured``, so once ``claude logout`` removes
    its ``.credentials.json`` the install stops being polled for good. Nothing can refresh
    its window rows, and nothing can supersede the failure it last recorded -- typically
    ``stale_token``, whose whole message is "open Claude Code once to refresh it", advice
    for a sign-in that is not there to refresh. Its directory is still listed, so ``known``
    keeps the name and the card goes on quoting numbers that nothing will ever update.

    That is the one state where an install's own files may be the oracle, and the
    asymmetry that makes it safe is that retirement HIDES stored rows rather than deleting
    them: sign in again and the windows come back on the next poll. The alternative has no
    such correction -- an unpolled account keeps its frozen bars for as long as the
    directory is left in place.

    Deliberately not about whether the SUBSCRIPTION still works. A token Anthropic has
    stopped accepting, or one Claude Code lets lapse, is still a sign-in this machine holds:
    the install is polled, its `stale_token` row stays live, and its windows stay the last
    thing it measured -- the same deal every other provider's card makes. Only a sign-in
    that is off THIS machine ends the reporting, because that is the state that ends the
    polling and leaves the numbers with no way back.

    The default install is exempt. It is polled whether or not it is configured, so its own
    ``unavailable`` row is what names its state and keeps its error from going stale, and
    those rows also drive the consent and "not detected" card.

    Both the ASSIGNED and the INTRINSIC slug count, for the reason spelled out in
    ``_known_names``: which name a directory carries depends on ``CLAUDE_CONFIG_DIR``, so a
    signed-out install has to be retired under either name it could have stored rows under.

    But a name is only this install's to retire if no OTHER install answers to it, and that
    is where this set parts company with ``_known_names``. There, naming a directory twice
    can only ever KEEP rows, so a name that belongs to someone else costs nothing. Here it
    retires them, and the two names are allocated by different rules: the intrinsic slug is
    a property of the directory, while the assigned one is handed out relative to
    ``CLAUDE_CONFIG_DIR`` and made unique with a ``-2`` suffix. They collide exactly when an
    install is demoted out of a name another install now holds -- point the variable at
    ``~/.claude-academic`` and the plain ``~/.claude`` beside it is renamed ``claude`` while
    keeping the intrinsic slug ``default``, which is the live redirected install's account.
    Sign out of that ``~/.claude`` and retiring ``default`` blanks the working subscription.
    So a name any pollable install answers to is withheld, and only the remainder retires.
    """
    claimed: set[str] = set()
    candidates: set[str] = set()
    for profile in profiles:
        names = {profile.name, clientpaths.claude_profile_slug(profile.config_dir)}
        if (
            profile.is_default
            or profile.configured
            or not profile.credential_observably_absent
        ):
            claimed |= names
        else:
            candidates |= names
    return frozenset(candidates - claimed)


def _listable(path: Path) -> bool:
    """Whether ``path``'s entries can be read at all; false is "cannot tell", never "empty"."""
    try:
        with os.scandir(path) as entries:
            next(iter(entries), None)
    except Exception:
        return False
    return True


def discover_profiles() -> list[ClaudeProfile]:
    """The installs of ``scan_profiles``, for callers that do not need the namespace."""
    return scan_profiles().profiles


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


def _credential_rank(
    profile: ClaudeProfile, meta: dict[str, Any], captured_at: int
) -> tuple[int, int, int, int]:
    """Rank two credentials for ONE subscription; the highest is the one worth fetching with.

    ``cp -r`` leaves a copy behind that goes stale in whichever directory Claude Code is not
    run from, so "first directory found" is not the same as "the sign-in that still works".
    So, in order: not-expired beats expired; a known expiry beats no recorded expiry at all;
    then the one that expires latest; and the default install breaks a full tie so the
    reported account name does not wander between polls.

    A credential with no recorded expiry is treated as *not expired* but is outranked by any
    credential known to be live. Ranking it above one would hand the poll to whichever copy
    happens to have lost its ``expiresAt`` -- a ``cp -r`` of a partly-written file, or an
    install downgraded to a token pasted by hand -- and the real sign-in would stop being
    polled while its own numbers went stale. The case the tie-break was written for,
    ``CLAUDE_CODE_OAUTH_TOKEN``, does not need it: `_read_credentials` already ranks the
    environment token above every file source, so the two never meet here.

    "No recorded expiry" is a FALSY ``expires_at_ms`` -- absent, null, zero -- and nothing
    else. A recorded expiry that is negative or otherwise in the past is expired, which is
    what `_profile_snapshots` already concludes from the same field (``if expires_ms and
    ... <= captured_at``); classing it as unknown here would let it outrank a credential
    this function agrees is dead, and have the two readings of one field disagree.

    Milliseconds throughout, and never a float. `json.loads` will build an arbitrarily long
    integer literal, which survives ``int()`` and integer division intact and then raises
    ``OverflowError`` on the way into a float -- one odd credential file taking down the
    whole poll, which is the failure this guard exists to prevent, not to relocate.
    """
    raw = meta.get("expires_at_ms")
    try:
        expires_ms = int(raw) if raw else 0
    except (TypeError, ValueError, OverflowError):
        # OverflowError is not a ValueError: `json.loads` accepts the bare `Infinity`
        # literal, and `int(float("inf"))` raises it. An unparseable expiry is an unknown
        # one, which the ranking already has a place for.
        expires_ms = 0
    known = 1 if expires_ms else 0
    live = 1 if (not known or expires_ms > captured_at * 1000) else 0
    return (live, known, expires_ms, 1 if profile.is_default else 0)


def _drop_duplicate_subscriptions(
    jobs: list[tuple[ClaudeProfile, str | None, dict[str, Any]]], *, captured_at: int
) -> list[tuple[ClaudeProfile, str | None, dict[str, Any]]]:
    """One job per subscription, keeping the best credential within it.

    Installs without a token are untouched: they carry no identity to compare, and the
    default profile's "nothing to read" row is a state the card has to keep reporting.
    """
    best: dict[str, tuple[tuple[int, int, int, int], int]] = {}
    for index, (profile, token, meta) in enumerate(jobs):
        if not token:
            continue
        identity = _subscription_identity(token)
        challenger = (_credential_rank(profile, meta, captured_at), index)
        incumbent = best.get(identity)
        if incumbent is None or challenger[0] > incumbent[0]:
            best[identity] = challenger
    kept = {index for _, index in best.values()}
    return [job for index, job in enumerate(jobs) if not job[1] or index in kept]


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


def summarize_limit_resets(block: Any, *, now: int) -> dict[str, Any] | None:
    """Claude Code's limit-reset block, in the shape the Quota tab draws for Codex credits.

    ``block`` is ``usage.cedar_ember`` verbatim. Returns ``None`` when it is not a block at
    all, and otherwise ``{"available_count", "credits"}``, where ``credits`` lists only the
    grants that can still be spent -- not paused, not yet expired, already started, with a
    reset left -- and ``available_count`` is the resets those grants hold between them. An
    account outside the program gets a block with no grants, which comes back as a count of
    0 and an empty list.

    Shared by the poller, which stores the count, and ``quota_state``, which re-reads the
    stored block against the current time, so a grant that expired since the last poll stops
    being offered without waiting for the next one. Never raises: one odd payload must not
    cost the install its window rows.
    """
    if not isinstance(block, dict):
        return None
    grants = block.get("grants") if isinstance(block.get("grants"), list) else []
    credits: list[dict[str, Any]] = []
    available = 0
    for grant in grants:
        if not isinstance(grant, dict) or grant.get("paused") is True:
            continue
        try:
            left = int(grant.get("resets_left") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        starts_at = _parse_time(grant.get("starts_at"))
        expires_at = _parse_time(grant.get("ends_at"))
        if left <= 0 or (starts_at and starts_at > now) or (expires_at and expires_at <= now):
            continue
        available += left
        clears = grant.get("clears") if isinstance(grant.get("clears"), list) else []
        credits.append(
            {
                "id": str(grant.get("id") or "") or None,
                "title": str(grant.get("label") or "").strip() or None,
                "expires_at": expires_at,
                "resets_left": left,
                "clears": [str(item) for item in clears],
                "status": "available",
            }
        )
    credits.sort(key=lambda credit: credit["expires_at"] or float("inf"))
    return {"available_count": available, "credits": credits}


def _limit_reset_snapshots(
    block: Any, *, profile: ClaudeProfile, plan: Any, captured_at: int
) -> list[QuotaSnapshot]:
    """The install's reset inventory as one ``reset_credits`` row, or none without a block.

    Written even when the count is 0: that is how an exhausted or withdrawn batch stops
    looking current. The bucket id is the one Codex uses and stays unprefixed for every
    install, because ``quota_state`` and ``quota_history`` exclude it by that exact id, and
    rows are already unique per account. ``raw`` keeps the block verbatim so a field read
    wrongly today can be re-read from stored data rather than only from future polls.
    """
    summary = summarize_limit_resets(block, now=captured_at)
    if summary is None:
        return []
    return [
        QuotaSnapshot(
            "claude",
            profile.name,
            RESET_CREDITS_BUCKET,
            "Reset credits",
            float(summary["available_count"]),
            None,
            plan,
            captured_at,
            "claude_api",
            "ok",
            {"limit_resets": block},
        )
    ]


def _fetch_usage(token: str, *, opener, timeout: float) -> dict[str, Any]:
    """GET the usage body with the reset flag, falling back once to the plain URL.

    5xx keeps its retry-once-after-200ms rule at each URL; anything else propagates for
    the caller to map (``401``/``403`` to ``stale_token``, the rest to ``fetch_error``).
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
        "User-Agent": CLAUDE_USAGE_USER_AGENT,
    }

    def get(url: str) -> dict[str, Any]:
        req = urllib.request.Request(url, headers=headers)
        for attempt in range(2):
            try:
                with opener(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except HTTPError as exc:
                if exc.code not in {500, 502, 503, 504} or attempt == 1:
                    raise
                time.sleep(0.2)
        raise AssertionError("unreachable: the second attempt returns or raises")

    try:
        return get(CLAUDE_USAGE_URL_WITH_RESETS)
    except HTTPError as exc:
        if exc.code not in _RESET_FLAG_REJECTED:
            raise
    return get(CLAUDE_USAGE_URL)


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
    required its credential file, so an empty result here is not a card-wide absence of
    data. An install that has LOST that file is not polled at all, and `quota_state`
    retires the stored rows of an account nothing can refresh any more, so a signed-out
    install stops being quoted as though it were live. The default profile keeps
    reporting ``unavailable`` — that row is what drives the consent and
    "not detected" card, and `scan_profiles` reports the default install whether or not it
    is configured so that this stays true on a machine with no ``~/.claude`` at all.
    """
    if not token:
        return [] if not profile.is_default else [_status_snapshot("unavailable", captured_at, meta, profile)]
    expires_ms = meta.get("expires_at_ms")
    try:
        if expires_ms and int(expires_ms) // 1000 <= captured_at:
            return [_status_snapshot("stale_token", captured_at, meta, profile)]
    except Exception:
        pass
    try:
        payload = _fetch_usage(token, opener=opener, timeout=timeout)
    except HTTPError as exc:
        status = "stale_token" if exc.code in {401, 403} else "fetch_error"
        return [_status_snapshot(status, captured_at, {**meta, "error": f"HTTP {exc.code}: {exc.reason}"}, profile)]
    except Exception as exc:
        return [_status_snapshot("fetch_error", captured_at, {**meta, "error": str(exc)}, profile)]
    if not isinstance(payload, dict):
        return [_status_snapshot("fetch_error", captured_at, {**meta, "error": "empty_response"}, profile)]
    windows = _window_snapshots(payload, profile=profile, meta=meta, captured_at=captured_at)
    resets = _limit_reset_snapshots(
        payload.get("cedar_ember"), profile=profile, plan=meta.get("plan"), captured_at=captured_at
    )
    return windows + resets


def _window_snapshots(
    payload: dict[str, Any], *, profile: ClaudeProfile, meta: dict[str, Any], captured_at: int
) -> list[QuotaSnapshot]:
    """The usage windows of one response, or the install's ``no_limits`` status row."""
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
    one subscription, and reporting it twice would double its usage in the chart. The copy
    that still has a live credential is the one that reports, so a stale clone cannot evict
    the working sign-in and leave the subscription looking signed out.
    """
    captured_at = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    selected = profiles if profiles is not None else discover_profiles()
    # Credentials are read serially first: it is local file I/O, and it lets one
    # subscription be recognised as a duplicate before any request goes out for it.
    jobs: list[tuple[ClaudeProfile, str | None, dict[str, Any]]] = []
    for profile in selected:
        token, meta = _read_credentials(profile)
        jobs.append((profile, token, meta))
    jobs = _drop_duplicate_subscriptions(jobs, captured_at=captured_at)
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
