"""Multiple Claude Code subscriptions on one machine.

Each install (``~/.claude`` plus a ``~/.claude-<profile>`` sibling run through
``CLAUDE_CONFIG_DIR``) signs in to its own subscription and keeps its own
``.credentials.json``, so quota must report one set of windows per install and name each
by the directory it was set up in.
"""
from __future__ import annotations

import json
import base64
import threading
import os
from pathlib import Path

from tokdash import clientpaths
from tokdash.sources.quota import claude, config
from tokdash.sources.quota.types import QuotaSnapshot
from tokdash.usage_store import UsageEntryStore


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _home(monkeypatch, tmp_path) -> Path:
    """An empty home: no real ``~/.claude*`` install can leak into a test."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("TOKDASH_CLAUDE_PROFILES", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    return home


def _install(
    home: Path,
    name: str,
    *,
    token: str,
    subscription: str = "max",
    tier: str = "default_claude_max_5x",
) -> Path:
    directory = home / name
    directory.mkdir(parents=True, exist_ok=True)  # re-installing is signing in again
    (directory / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": token,
                    "expiresAt": 4_000_000_000_000,
                    "subscriptionType": subscription,
                    "rateLimitTier": tier,
                }
            }
        ),
        encoding="utf-8",
    )
    return directory


def _usage_payload(percent: float, resets_at: int) -> dict:
    return {"limits": [{"kind": "session", "percent": percent, "resets_at": resets_at, "is_active": True}]}


def _jwt_claims(claims: dict, nonce: str) -> str:
    """A JWT-shaped access token carrying ``claims``, as Claude Code ships them."""

    def part(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return ".".join(
        [part(b'{"alg":"none"}'), part(json.dumps({**claims, "iat": nonce}).encode()), "sig"]
    )


def _jwt(account_id: str, nonce: str) -> str:
    """A token whose `account_id` is the claim that survives a refresh.

    ``nonce`` stands for the token material a refresh replaces; the account id is the only
    stable thing two copies of one sign-in still share.
    """
    return _jwt_claims({"account_id": account_id}, nonce)


def _expire(directory: Path) -> None:
    """Make an install's stored credential expired, as an unused copy does within hours."""
    path = directory / ".credentials.json"
    blob = json.loads(path.read_text(encoding="utf-8"))
    blob["claudeAiOauth"]["expiresAt"] = 1_000  # long past
    path.write_text(json.dumps(blob), encoding="utf-8")


def _sign_out(directory: Path) -> None:
    """``claude logout``: the credential file is gone and the directory stays.

    Durable in a way a failed read is not -- nothing restores the file until the user signs
    in again, and an admitted-only-on-its-credential sibling is not polled in the meantime.
    """
    (directory / ".credentials.json").unlink()


def _replace_credential_with_a_directory(directory: Path) -> None:
    """Something stands where the credential was, and it is not a file.

    A dotfile manager's mishap, or an unpack that kept the wrong entry. ``configured``
    demands ``is_file()``, so the install is not polled -- and the path DOES exist, so only
    an absence test that means "nothing pollable here" can retire it. Answering only
    ``ENOENT`` here would leave exactly the permanently frozen card this rule exists to end.
    """
    path = directory / ".credentials.json"
    path.unlink()
    path.mkdir()


# --- discovery ----------------------------------------------------------------


def test_a_missing_or_non_file_credential_is_absent_and_an_unreadable_one_is_not(
    monkeypatch, tmp_path
):
    """What may read as "no sign-in here", and what may only read as "cannot tell".

    This predicate decides whether an install's stored rows are retired, so it has to be
    asked in `configured`'s terms: that one demands a regular file, so anything else at the
    path stops the polling just as surely as nothing being there. A failed OPEN is the
    opposite case -- the file is regular and gets polled again the moment it opens.
    """
    home = _home(monkeypatch, tmp_path)
    directory = _install(home, ".claude-lab", token="tok-lab")
    credential = directory / ".credentials.json"
    profile = claude.ClaudeProfile("lab", directory, False)

    assert profile.credential_observably_absent is False  # signed in

    credential.chmod(0o000)
    try:
        assert profile.credential_observably_absent is False  # unreadable, and still a file
    finally:
        credential.chmod(0o0600)

    directory.chmod(0o000)
    try:
        # An unsearchable directory cannot answer "where is the file" at all. A root test
        # user reads through both of these, which is the same verdict either way.
        assert profile.credential_observably_absent is False
    finally:
        directory.chmod(0o0755)

    _replace_credential_with_a_directory(directory)
    assert profile.credential_observably_absent is True  # there, but nothing to poll

    credential.rmdir()
    assert profile.credential_observably_absent is True  # observably gone

    credential.symlink_to(directory / "nowhere")  # a dangling link is no file either
    assert profile.credential_observably_absent is True

def test_profile_dirs_name_every_install_by_its_directory(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    _install(home, ".claude-academic", token="tok-academic")
    (home / ".claude.json").write_text("{}", encoding="utf-8")  # a file, not an install

    profiles = clientpaths.claude_profile_dirs()

    assert [name for name, _dir in profiles] == ["default", "academic"]
    assert profiles[0][1] == home / ".claude"
    assert profiles[1][1] == home / ".claude-academic"


def test_configured_default_dir_is_not_reported_twice(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    configured = _install(home, ".claude-academic", token="tok-academic")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(configured))

    # The env override owns the default slot; the plain `~/.claude` install beside it is
    # named after itself rather than becoming a second "default".
    assert [(name, str(path.relative_to(home))) for name, path in clientpaths.claude_profile_dirs()] == [
        ("default", ".claude-academic"),
        ("claude", ".claude"),
    ]


def test_explicit_profile_list_replaces_the_home_scan(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    outside = tmp_path / "srv" / "claude-academic"
    outside.mkdir(parents=True)
    (outside / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "t"}}), encoding="utf-8"
    )
    monkeypatch.setenv(
        "TOKDASH_CLAUDE_PROFILES", f"{outside}{os.pathsep}{home / '.claude'}{os.pathsep}{outside}"
    )

    profiles = clientpaths.claude_profile_dirs()

    # `~/.claude` is the default dir, so listing it again is a no-op, and a dir listed
    # twice is one install. The home scan is not consulted once the list is given.
    assert [(name, path) for name, path in profiles] == [
        ("default", home / ".claude"),
        ("claude-academic", outside),
    ]


def test_two_directories_never_share_one_account_id(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    lookalike = _install(home, ".claude-default", token="tok-lookalike")
    monkeypatch.setenv("TOKDASH_CLAUDE_PROFILES", f"{lookalike}{os.pathsep}{lookalike}")

    names = [name for name, _dir in clientpaths.claude_profile_dirs()]

    # `.claude-default` must not impersonate the built-in profile's account id.
    assert names == ["default", "claude-default"]


# --- collection ---------------------------------------------------------------


def test_siblings_are_scanned_only_with_credential_scan_consent(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    _install(home, ".claude-academic", token="tok-academic")

    assert [profile.name for profile in claude.discover_profiles()] == ["default"]

    config.set_quota_consent({"credential_scan": True})

    assert [profile.name for profile in claude.discover_profiles()] == ["default", "academic"]


def test_a_sibling_without_credentials_is_not_reported(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    (home / ".claude-backup").mkdir()  # an old copy, never signed in
    config.set_quota_consent({"credential_scan": True})

    assert [profile.name for profile in claude.discover_profiles()] == ["default"]


def test_collect_fetches_each_install_and_names_its_windows(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base", tier="default_claude_max_5x")
    _install(home, ".claude-academic", token="tok-academic", subscription="pro", tier="default_claude_pro")
    config.set_quota_consent({"credential_scan": True})
    tokens: list[str] = []
    per_token = {
        "tok-base": _usage_payload(75, 1_782_909_000),
        "tok-academic": _usage_payload(20, 1_782_907_200),
    }

    def opener(req, timeout=15):
        token = req.get_header("Authorization").split(" ", 1)[1]
        tokens.append(token)
        return FakeResponse(per_token[token])

    snapshots = claude.collect_claude_api_snapshots(opener=opener, now=1_782_907_200)

    assert sorted(tokens) == ["tok-academic", "tok-base"]
    # Default install first, each one's windows named after the directory it came from.
    assert [(s.account, s.bucket, s.used_percent, s.plan) for s in snapshots] == [
        ("default", "session", 75.0, "max/default_claude_max_5x"),
        ("academic", "academic_session", 20.0, "pro/default_claude_pro"),
    ]


def test_a_broken_sibling_does_not_blank_the_working_install(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    expired = _install(home, ".claude-expired", token="tok-expired")
    blob = json.loads((expired / ".credentials.json").read_text(encoding="utf-8"))
    blob["claudeAiOauth"]["expiresAt"] = 1_000  # long past
    (expired / ".credentials.json").write_text(json.dumps(blob), encoding="utf-8")
    config.set_quota_consent({"credential_scan": True})

    calls: list[str] = []

    def opener(req, timeout=15):
        calls.append(req.get_header("Authorization"))
        return FakeResponse(_usage_payload(75, 1_782_909_000))

    # A sibling with nothing to show reports nothing, rather than a card-wide error row.
    assert claude.collect_claude_api_snapshots(
        opener=opener,
        now=1_782_907_200,
        profiles=[claude.ClaudeProfile("academic", home / ".claude-academic", False)],
    ) == []

    snapshots = claude.collect_claude_api_snapshots(opener=opener, now=1_782_907_200)

    assert calls == ["Bearer tok-base"]  # the expired sibling's token was never sent out
    assert [(s.account, s.bucket, s.status) for s in snapshots] == [
        ("default", "session", "ok"),
        ("expired", "api", "stale_token"),
    ]


def test_a_copied_install_reports_one_subscription_once(monkeypatch, tmp_path):
    # `cp -r ~/.claude ~/.claude-copy` copies the sign-in too; both dirs then hold the
    # same access token, and counting it twice would double the subscription's usage.
    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-shared")
    _install(home, ".claude-copy", token="tok-shared")
    config.set_quota_consent({"credential_scan": True})
    calls: list[str] = []

    def opener(req, timeout=15):
        calls.append(req.get_header("Authorization"))
        return FakeResponse(_usage_payload(40, 1_782_909_000))

    snapshots = claude.collect_claude_api_snapshots(opener=opener, now=1_782_907_200)

    assert len(calls) == 1
    assert [(s.account, s.bucket) for s in snapshots] == [("default", "session")]


def test_a_copied_sign_in_is_still_one_subscription_after_a_token_refresh(
    monkeypatch, tmp_path
):
    """The copy's token has now rotated away, so a token comparison alone would miss it.

    Only the account inside the token says these are the same subscription; comparing the
    bearer strings here would fetch both and add their usage together in the chart.
    """
    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token=_jwt("acct-9", "issued-1"))
    _install(home, ".claude-copy", token=_jwt("acct-9", "issued-2"))
    config.set_quota_consent({"credential_scan": True})
    calls: list[str] = []

    def opener(req, timeout=15):
        calls.append(req.get_header("Authorization"))
        return FakeResponse(_usage_payload(40, 1_782_909_000))

    snapshots = claude.collect_claude_api_snapshots(opener=opener, now=1_782_907_200)

    assert calls == ["Bearer " + _jwt("acct-9", "issued-1")]  # the default install wins
    assert [(s.account, s.bucket) for s in snapshots] == [("default", "session")]


def test_two_different_sign_ins_are_both_fetched_concurrently(monkeypatch, tmp_path):
    """Per-install requests overlap, so a slow endpoint costs one timeout, not N.

    Each fake response blocks; serial collection would take their sum and push the poll
    cycle past the window the boundary scheduler planned for.
    """
    home = _home(monkeypatch, tmp_path)
    for name, token in ((".claude", "tok-a"), (".claude-b", "tok-b"), (".claude-c", "tok-c")):
        _install(home, name, token=token)
    config.set_quota_consent({"credential_scan": True})
    barrier = threading.Barrier(3, timeout=5)
    timeouts: list[str] = []

    def opener(req, timeout=15):
        # Blocks until all three requests are in flight at once; times out if serial. The
        # error is recorded rather than left to propagate, because collection swallows
        # every exception a fetch raises into a `fetch_error` row -- without this the test
        # would fail on a bucket-list diff that says nothing about the serialisation.
        try:
            barrier.wait()
        except threading.BrokenBarrierError as exc:
            timeouts.append(f"{req.get_header('Authorization')}: {exc}")
        return FakeResponse(_usage_payload(30, 1_782_909_000))

    snapshots = claude.collect_claude_api_snapshots(opener=opener, now=1_782_907_200)

    assert timeouts == []  # every install had a request open at the same moment
    assert [(s.account, s.bucket) for s in snapshots] == [
        ("default", "session"),
        ("b", "b_session"),
        ("c", "c_session"),
    ]


def test_a_stale_copy_does_not_evict_the_sign_in_that_still_works(monkeypatch, tmp_path):
    """One subscription reports once, and reports with the credential that still works.

    ``cp -r ~/.claude ~/.claude-work`` and then always ``CLAUDE_CONFIG_DIR=~/.claude-work
    claude`` leaves the ORIGINAL stale within hours, because that is not the directory
    Claude Code refreshes. Deduping on the subscription identity and keeping the first
    directory found made that stale copy the reporter, so the card read "sign-in expired"
    with no windows at all for a subscription that was signed in and working.
    """
    home = _home(monkeypatch, tmp_path)
    stale = _install(home, ".claude", token=_jwt("acct-9", "issued-1"))
    _expire(stale)
    _install(home, ".claude-work", token=_jwt("acct-9", "issued-2"))
    config.set_quota_consent({"credential_scan": True})
    calls: list[str] = []

    def opener(req, timeout=15):
        calls.append(req.get_header("Authorization"))
        return FakeResponse(_usage_payload(40, 1_782_909_000))

    snapshots = claude.collect_claude_api_snapshots(opener=opener, now=1_782_907_200)

    assert calls == ["Bearer " + _jwt("acct-9", "issued-2")]  # the live credential, not the stale one
    assert [(s.account, s.bucket, s.status) for s in snapshots] == [("work", "work_session", "ok")]


def test_a_copy_with_no_recorded_expiry_does_not_outrank_the_live_sign_in(
    monkeypatch, tmp_path
):
    """A missing `expiresAt` must lose to a credential known to be live, not beat it.

    Ranking "no recorded expiry" as the latest expiry hands the poll to whichever copy
    happens to be missing the field -- a `cp -r` of a file caught mid-write, or an install
    downgraded to a hand-pasted token. The real sign-in then stops being polled, the
    reported account and history bucket prefix move to the copy, and if the copy's token is
    dead the card reports `stale_token` with no windows: the exact failure the ranking was
    added to prevent, with the inputs the other way round.
    """
    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token=_jwt("acct-9", "issued-1"))
    copy = _install(home, ".claude-copy", token=_jwt("acct-9", "issued-2"))
    blob = json.loads((copy / ".credentials.json").read_text(encoding="utf-8"))
    del blob["claudeAiOauth"]["expiresAt"]
    (copy / ".credentials.json").write_text(json.dumps(blob), encoding="utf-8")
    config.set_quota_consent({"credential_scan": True})
    calls: list[str] = []

    def opener(req, timeout=15):
        calls.append(req.get_header("Authorization"))
        return FakeResponse(_usage_payload(40, 1_782_909_000))

    snapshots = claude.collect_claude_api_snapshots(opener=opener, now=1_782_907_200)

    assert calls == ["Bearer " + _jwt("acct-9", "issued-1")]  # the install with a known expiry
    assert [(s.account, s.bucket) for s in snapshots] == [("default", "session")]


def test_a_credential_with_no_expiry_still_beats_an_expired_one(monkeypatch, tmp_path):
    """Not-expired outranks expired first, before any of the tie-breaks below it.

    Demoting a missing expiry must not demote it past a credential that is definitely
    dead -- a hand-pasted token would then lose to the expired file it was pasted to
    replace, and the install would report `stale_token` with a working token on disk.
    """
    home = _home(monkeypatch, tmp_path)
    stale = _install(home, ".claude", token=_jwt("acct-9", "issued-1"))
    _expire(stale)
    fresh = _install(home, ".claude-work", token=_jwt("acct-9", "issued-2"))
    blob = json.loads((fresh / ".credentials.json").read_text(encoding="utf-8"))
    del blob["claudeAiOauth"]["expiresAt"]
    (fresh / ".credentials.json").write_text(json.dumps(blob), encoding="utf-8")
    config.set_quota_consent({"credential_scan": True})
    calls: list[str] = []

    def opener(req, timeout=15):
        calls.append(req.get_header("Authorization"))
        return FakeResponse(_usage_payload(40, 1_782_909_000))

    snapshots = claude.collect_claude_api_snapshots(opener=opener, now=1_782_907_200)

    assert calls == ["Bearer " + _jwt("acct-9", "issued-2")]
    assert [(s.account, s.bucket, s.status) for s in snapshots] == [("work", "work_session", "ok")]


def test_a_non_integral_expiry_does_not_kill_the_whole_claude_poll(monkeypatch, tmp_path):
    """`{"expiresAt": Infinity}` must degrade to "no recorded expiry", not raise.

    `json.loads` accepts the bare `Infinity` literal, and `int(float("inf"))` raises
    `OverflowError`, which is not a `ValueError`. Uncaught in the ranking it propagates out
    of `collect_claude_api_snapshots` through `poll_quota`, so ONE install's odd credential
    file turns `/api/quota/refresh` into a 500 for every provider on the machine.
    """
    home = _home(monkeypatch, tmp_path)
    install = _install(home, ".claude", token=_jwt("acct-9", "issued-1"))
    path = install / ".credentials.json"
    path.write_text(
        json.dumps(json.loads(path.read_text(encoding="utf-8"))).replace(
            '"expiresAt": 4000000000000', '"expiresAt": Infinity'
        ),
        encoding="utf-8",
    )
    config.set_quota_consent({"credential_scan": True})

    def opener(req, timeout=15):
        return FakeResponse(_usage_payload(40, 1_782_909_000))

    snapshots = claude.collect_claude_api_snapshots(opener=opener, now=1_782_907_200)

    assert [(s.account, s.bucket, s.status) for s in snapshots] == [("default", "session", "ok")]


def test_two_seats_on_one_organization_are_two_subscriptions(monkeypatch, tmp_path):
    """A Team or Enterprise organization has one org id and one seat per member.

    Treating `organization_id` as the identity claim folds a colleague's sign-in into
    this one, so the second install reports no windows and no failure: it simply vanishes
    from the card. The identity has to name the seat, not the company.
    """
    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token=_jwt_claims({"organization_id": "org-1", "sub": "user-a"}, "i1"))
    _install(home, ".claude-coworker", token=_jwt_claims({"organization_id": "org-1", "sub": "user-b"}, "i2"))
    config.set_quota_consent({"credential_scan": True})
    calls: list[str] = []

    def opener(req, timeout=15):
        calls.append(req.get_header("Authorization"))
        return FakeResponse(_usage_payload(40, 1_782_909_000))

    snapshots = claude.collect_claude_api_snapshots(opener=opener, now=1_782_907_200)

    assert len(calls) == 2
    assert [(s.account, s.bucket) for s in snapshots] == [
        ("default", "session"),
        ("coworker", "coworker_session"),
    ]


def test_declining_credential_scan_never_lists_the_home_directory(monkeypatch, tmp_path):
    """Consent is a decision about the filesystem, not just about opening files.

    Without it the card must not learn which `~/.claude*` directories exist, so the scan
    has to be skipped rather than run and then filtered.
    """
    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    _install(home, ".claude-academic", token="tok-academic")
    listed: list[str] = []
    real_glob = Path.glob

    def spy(self, pattern):
        listed.append(str(pattern))
        return real_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", spy)

    assert [p.name for p in claude.discover_profiles()] == ["default"]

    from tokdash.sources.quota import quota_state

    provider = quota_state()["providers"]["claude"]

    assert [p for p in listed if str(p).startswith(".claude")] == []
    assert "accounts" not in provider  # siblings are not even known to exist


def test_read_claude_profiles_reports_a_plan_per_install(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base", tier="default_claude_max_5x")
    _install(home, ".claude-academic", token="tok-academic", subscription="pro", tier="default_claude_pro")
    config.set_quota_consent({"credential_scan": True})

    profiles = claude.read_claude_profiles()

    assert [(p["account"], p["plan"], p["status"]) for p in profiles] == [
        ("default", "Max 5x", "ok"),
        ("academic", "Pro", "ok"),
    ]
    assert str(home / ".claude-academic") in profiles[1]["credential_path"]


# --- dashboard payload --------------------------------------------------------


def _snapshot(
    account: str,
    bucket: str,
    label: str,
    used,
    captured: int,
    status: str = "ok",
    plan: str | None = None,
) -> QuotaSnapshot:
    return QuotaSnapshot(
        "claude", account, bucket, label, used, 1_782_909_000, plan, captured, "claude_api", status, {}
    )


def test_quota_state_lists_each_install_with_its_own_plan(monkeypatch, tmp_path):
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base", tier="default_claude_max_5x")
    _install(home, ".claude-academic", token="tok-academic", subscription="pro", tier="default_claude_pro")
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _snapshot("default", "session", "Session", 75.0, 1_782_907_200, plan="max"),
            _snapshot("academic", "academic_session", "Session", 20.0, 1_782_907_200, plan="pro"),
        ]
    )

    provider = quota_state()["providers"]["claude"]

    assert [(b["account"], b["bucket"], b["used_percent"]) for b in provider["buckets"]] == [
        ("academic", "academic_session", 20.0),
        ("default", "session", 75.0),
    ]
    # `accounts` is the generic per-account list every multi-account card reads: the
    # default install first, each install's own plan, and its own failure if it has one.
    assert [(a["account"], a["plan"]) for a in provider["accounts"]] == [
        ("default", "Max 5x"),
        ("academic", "Pro"),
    ]
    assert provider["plan"] == "Max 5x"  # the card line still describes the default install
    assert provider["detected"] is True


def test_a_broken_sibling_warns_the_card_and_names_the_install_that_broke(monkeypatch, tmp_path):
    """The card keeps the warning, and the account list says which install it is about.

    Two things have to be true at once. A consumer that only reads the provider's own
    status must still learn that something behind the card is broken -- the companion
    contract says one broken credential warns about the provider, or it refreshes a stale
    meter as if it were current. And the healthy install must not be blamed for it, which
    is what the per-account list is for: only ``academic`` carries the error.

    Seeded the way a poll actually writes rows: a SUCCESSFUL fetch writes no ``api`` row at
    all, so the healthy default install is represented by its window row alone and the
    failure exists only for ``academic``. Card status therefore cannot be derived from
    "which accounts have an api row", the guard that made this fail before.
    """
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    _install(home, ".claude-academic", token="tok-academic")
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _snapshot("default", "session", "Session", 10.0, 1_782_907_200),
            _snapshot("academic", "academic_session", "Session", 30.0, 1_782_907_200),
            _snapshot("academic", "api", "Claude API", None, 1_782_907_200, status="stale_token"),
        ]
    )

    provider = quota_state()["providers"]["claude"]

    assert provider["status"] == "stale_token"
    assert provider["status_detail"] == "stale_token"
    assert provider["status_at"] == 1_782_907_200
    by_account = {a["account"]: a for a in provider["accounts"]}
    assert by_account["academic"]["status_detail"] == "stale_token"
    assert by_account["academic"]["status_at"] == 1_782_907_200
    # The working install: no error of its own, and its windows are untouched.
    assert by_account["default"]["status_detail"] is None
    assert by_account["default"]["status"] == "ok"
    assert [(b["account"], b["used_percent"]) for b in provider["buckets"]] == [
        ("academic", 30.0),
        ("default", 10.0),
    ]


def test_a_recovered_install_stops_reporting_its_old_failure(monkeypatch, tmp_path):
    """Re-signing ``academic`` must clear its notice, with no success row to clear it.

    The stale ``api`` row stays the newest ``api`` row for that account forever, because
    successes are not written as ``api`` rows. Recovery has to come from the account's own
    later window rows, which is what ``ok_at`` tracks.
    """
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    _install(home, ".claude-academic", token="tok-academic")
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _snapshot("academic", "api", "Claude API", None, 1_782_900_000, status="stale_token"),
            _snapshot("academic", "academic_session", "Session", 12.0, 1_782_907_200),
            _snapshot("default", "session", "Session", 40.0, 1_782_907_200),
        ]
    )

    provider = quota_state()["providers"]["claude"]

    by_account = {a["account"]: a for a in provider["accounts"]}
    assert by_account["academic"]["status_detail"] is None
    assert by_account["academic"]["status"] == "ok"
    assert provider["status_detail"] is None


def test_still_failing_install_keeps_reporting_its_error(monkeypatch, tmp_path):
    """The other half: a newer success elsewhere must not silence this account's failure.

    Recovery is per account, so ``academic`` is still broken and the card still says so;
    what it does NOT do is claim the whole provider went stale at the default install's
    fresher timestamp.
    """
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    _install(home, ".claude-academic", token="tok-academic")
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _snapshot("academic", "api", "Claude API", None, 1_782_907_200, status="stale_token"),
            _snapshot("default", "session", "Session", 40.0, 1_782_910_800),
        ]
    )

    provider = quota_state()["providers"]["claude"]

    assert provider["status_detail"] == "stale_token"
    assert provider["status_at"] == 1_782_907_200  # academic's failure, not the default's poll
    by_account = {a["account"]: a for a in provider["accounts"]}
    assert by_account["academic"]["status_detail"] == "stale_token"
    assert by_account["default"]["status_detail"] is None


def test_windows_of_a_removed_install_are_not_shown_as_current(monkeypatch, tmp_path):
    """A deleted `~/.claude-lab` stops owning a card group: its directory is observably gone.

    The rows are as fresh as the surviving install's, so nothing about their age says the
    install went away -- only the home directory listing does. That listing is the oracle
    precisely because it cannot be confused by a file that will not open right now (see the
    tests below).
    """
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    _install(home, ".claude-academic", token="tok-academic")
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _snapshot("default", "session", "Session", 40.0, 1_782_907_200),
            _snapshot("lab", "lab_session", "Session", 90.0, 1_782_907_200),
        ]
    )

    provider = quota_state()["providers"]["claude"]

    assert [b["account"] for b in provider["buckets"]] == ["default"]
    assert [a["account"] for a in provider["accounts"]] == ["default", "academic"]


def test_a_removed_installs_expired_sign_in_stops_warning_the_card(monkeypatch, tmp_path):
    """Retiring the account has to retire its error too, or the card warns about it forever.

    `_provider_status` reports the newest live error of any account behind the card, so a
    deleted install whose last act was to record `stale_token` would leave a permanent
    "couldn't refresh -- showing last known" on a card whose remaining install is fine. The
    retired rows are dropped before any view reads them, so the card, its `accounts` list
    and its `updated_at` all answer from the installs that are actually there.
    """
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _snapshot("default", "session", "Session", 40.0, 1_782_907_200),
            _snapshot("lab", "api", "Claude API", None, 1_782_907_300, status="stale_token"),
        ]
    )

    provider = quota_state()["providers"]["claude"]

    assert provider["status_detail"] is None
    assert provider["status_at"] is None
    assert provider["updated_at"] == 1_782_907_200  # not the deleted install's newer row


def test_a_signed_out_install_stops_quoting_its_last_known_windows(monkeypatch, tmp_path):
    """`claude logout` leaves the directory in the listing, so directory presence is not enough.

    The install stops being polled the moment its credential goes, because a sibling is
    admitted on that file. Nothing can then refresh its window rows, and nothing can
    supersede the failure it last recorded -- whose whole message is "open Claude Code once
    to refresh it", advice for a sign-in that is no longer there to refresh. A card that
    keeps quoting both is showing numbers that can never be corrected, which is what this
    retires. It hides the stored rows rather than deleting them, so signing in again puts
    them back (see the test below).
    """
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    academic = _install(home, ".claude-academic", token="tok-academic")
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _snapshot("default", "session", "Session", 40.0, 1_782_907_200),
            _snapshot("academic", "academic_session", "Session", 20.0, 1_782_907_200),
            _snapshot(
                "academic", "api", "Claude API", None, 1_782_907_300, status="stale_token"
            ),
        ]
    )
    _sign_out(academic)

    provider = quota_state()["providers"]["claude"]

    assert [b["account"] for b in provider["buckets"]] == ["default"]
    # One install left, so the payload stays as a single-install card always had it -- with
    # the signed-out install's stale advice gone with it.
    assert "accounts" not in provider
    assert provider["status_detail"] is None
    assert provider["updated_at"] == 1_782_907_200


def test_a_signed_out_install_that_signs_in_again_comes_back_with_its_windows(
    monkeypatch, tmp_path
):
    """The asymmetry that makes early retirement safe: it is a display decision, not a delete.

    Nothing is written to `quota_snapshots` by retiring an account, so the same rows that
    vanished from the card are still there when the credential returns. The reverse
    decision -- keep the rows of an install nothing polls -- has no equivalent correction.
    """
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    academic = _install(home, ".claude-academic", token="tok-academic")
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _snapshot("default", "session", "Session", 40.0, 1_782_907_200),
            _snapshot("academic", "academic_session", "Session", 20.0, 1_782_907_200),
        ]
    )

    _sign_out(academic)
    assert [b["account"] for b in quota_state()["providers"]["claude"]["buckets"]] == [
        "default"
    ]

    _install(home, ".claude-academic", token="tok-academic")  # signed in again
    provider = quota_state()["providers"]["claude"]

    assert [b["account"] for b in provider["buckets"]] == ["academic", "default"]
    assert [a["account"] for a in provider["accounts"]] == ["default", "academic"]


def test_an_install_whose_credential_will_not_open_keeps_its_windows(monkeypatch, tmp_path):
    """A credential that cannot be OPENED is not an install that has been signed out.

    An `EACCES` on the file and an install directory that cannot be searched both fail the
    read while leaving the predicate an answer to work with, and either can clear on its own
    -- so the install keeps what it already reported. Its rows are a month behind the default
    install's here on purpose: age is not part of the decision, so no amount of it retires an
    install that could still be polled again.
    """
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    academic = _install(home, ".claude-academic", token="tok-academic")
    blocked = _install(home, ".claude-lab", token="tok-lab")
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _snapshot("default", "session", "Session", 40.0, 1_782_907_200),
            # A month behind the default install, and still not retired.
            _snapshot("academic", "academic_session", "Session", 20.0, 1_780_315_200),
            _snapshot("lab", "lab_session", "Session", 60.0, 1_780_315_200),
        ]
    )
    (academic / ".credentials.json").chmod(0o000)  # a file that will not open
    blocked.chmod(0o000)  # cannot even be searched
    try:
        provider = quota_state()["providers"]["claude"]
    finally:
        blocked.chmod(0o0755)

    assert [b["account"] for b in provider["buckets"]] == ["academic", "default", "lab"]


def test_a_revoked_token_stays_and_a_removed_sign_in_does_not(monkeypatch, tmp_path):
    """Where a Claude install's quota stops being reported: this machine, not the server.

    Three installs, three states, one card. A sign-in we hold is a sign-in we report, even
    when Anthropic has stopped accepting it -- the install is still polled, its error is
    live, and its windows are the last thing it measured, which is the deal every other
    provider's card already makes. Only a sign-in that is gone from THIS machine stops
    being reported, because that is the one state that ends the polling and leaves the
    numbers with no way back.
    """
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    expired = _install(home, ".claude-expired", token="tok-expired")
    gone = _install(home, ".claude-gone", token="tok-gone")
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _snapshot("default", "session", "Session", 10.0, 1_782_907_200),
            _snapshot("expired", "expired_session", "Session", 30.0, 1_782_907_200),
            _snapshot("expired", "api", "Claude API", None, 1_782_907_200, status="stale_token"),
            _snapshot("gone", "gone_session", "Session", 50.0, 1_782_907_200),
            _snapshot("gone", "api", "Claude API", None, 1_782_907_200, status="stale_token"),
        ]
    )

    _expire(expired)  # expired locally / revoked upstream: the file is still here
    _sign_out(gone)  # `claude logout`: the sign-in itself is off this machine

    provider = quota_state()["providers"]["claude"]

    assert [b["account"] for b in provider["buckets"]] == ["default", "expired"]
    assert [a["account"] for a in provider["accounts"]] == ["default", "expired"]
    by_account = {a["account"]: a for a in provider["accounts"]}
    assert by_account["expired"]["status_detail"] == "stale_token"  # the card still says so
    assert provider["status_account"] == "expired"  # and blames the install that owns it


def test_an_install_with_no_credential_file_in_its_place_is_retired(monkeypatch, tmp_path):
    """A directory where the credential was is as unpolllable as no credential at all.

    `configured` demands a regular file, so the install stopped being polled the moment
    something else took the path; an absence test answering only `ENOENT` would call this
    "present" and leave the frozen bars on the card for good, which is the exact state the
    sign-out rule exists to end.
    """
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    academic = _install(home, ".claude-academic", token="tok-academic")
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _snapshot("default", "session", "Session", 40.0, 1_782_907_200),
            _snapshot("academic", "academic_session", "Session", 20.0, 1_782_907_200),
        ]
    )

    _replace_credential_with_a_directory(academic)

    provider = quota_state()["providers"]["claude"]

    assert [b["account"] for b in provider["buckets"]] == ["default"]


def test_a_signed_out_default_install_keeps_its_rows(monkeypatch, tmp_path):
    """The default install is exempt from the sign-out rule, on purpose.

    It is polled whether or not it is configured, so its own `unavailable` row is what names
    its state and keeps its error from going stale, and those rows are also what drive the
    consent and "not detected" card. Retiring the account would take that explanation with
    it and leave a blank card on the machine that most needs the reason. What the default
    install does still keep, and the sibling no longer does, is its last-known windows:
    fixing that needs a state the card can print in their place, which no surface speaks
    yet.
    """
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    default = _install(home, ".claude", token="tok-base")
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [_snapshot("default", "session", "Session", 40.0, 1_782_907_200)]
    )

    _sign_out(default)

    provider = quota_state()["providers"]["claude"]

    assert [b["account"] for b in provider["buckets"]] == ["default"]


def test_a_home_that_cannot_be_listed_retires_nothing(monkeypatch, tmp_path):
    """An unavailable home is an unavailable answer, not the news that every install is gone.

    A networked or encrypted home that is not up yet lists nothing at all, which by the
    membership test alone would retire every install on the machine at once. Absence has to
    be OBSERVED, so a listing that could not be read means nothing may be concluded from it.
    """
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _snapshot("default", "session", "Session", 40.0, 1_782_907_200),
            _snapshot("academic", "academic_session", "Session", 20.0, 1_782_907_200),
        ]
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "not-mounted")

    provider = quota_state()["providers"]["claude"]

    assert [b["account"] for b in provider["buckets"]] == ["academic", "default"]


def test_an_unmounted_explicit_profile_volume_retires_nothing(monkeypatch, tmp_path):
    """`TOKDASH_CLAUDE_PROFILES` names the installs, so a missing path is an unmounted one.

    The variable is how installs outside the home directory are declared. One of them not
    being a directory right now is the unmounted-volume case, not a deletion -- the user did
    not stop declaring it -- so the whole answer is untrusted rather than that install being
    retired.
    """
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    lab = _install(home, "lab", token="tok-lab")
    monkeypatch.setenv("TOKDASH_CLAUDE_PROFILES", f"{lab}{os.pathsep}{tmp_path / 'volume' / 'work'}")
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _snapshot("default", "session", "Session", 40.0, 1_782_907_200),
            _snapshot("work", "work_session", "Session", 20.0, 1_782_907_200),
        ]
    )

    provider = quota_state()["providers"]["claude"]

    assert [b["account"] for b in provider["buckets"]] == ["default", "work"]


def test_no_credential_scan_consent_retires_nothing(monkeypatch, tmp_path):
    """Without consent nothing was looked at, so nothing may be concluded gone."""
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _snapshot("default", "session", "Session", 40.0, 1_782_907_200),
            _snapshot("lab", "lab_session", "Session", 90.0, 1_782_907_200),
        ]
    )
    config.set_quota_consent({"credential_scan": False, "claude_api": True})

    provider = quota_state()["providers"]["claude"]

    assert [b["account"] for b in provider["buckets"]] == ["default", "lab"]


def test_a_row_stamped_in_the_future_drops_nothing(monkeypatch, tmp_path):
    """A poll that ran on a wrong clock must not take the card down with it.

    A VM resumed before NTP, a dual-boot clock skew or a container with a bad RTC writes a
    `captured_at` in the future, and that row stays the newest one for its bucket for as
    long as the skew lasts. Nothing here compares one row's age against another's, so the
    skewed row is just a row.
    """
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    _install(home, ".claude-academic", token="tok-academic")
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _snapshot("default", "session", "Session", 40.0, 1_782_907_200 + 86_400),
            _snapshot("academic", "academic_session", "Session", 20.0, 1_782_907_200),
        ]
    )

    provider = quota_state()["providers"]["claude"]

    assert [b["account"] for b in provider["buckets"]] == ["academic", "default"]
    assert [a["account"] for a in provider["accounts"]] == ["default", "academic"]


def test_a_card_with_only_a_sibling_speaks_for_that_sibling(monkeypatch, tmp_path):
    """No `~/.claude` at all, with the only sign-in in `~/.claude-academic`.

    A default install that is simply not there used to answer `unavailable` for the whole
    provider, which marked the card failed however well the real subscription polled. An
    install is admitted on the same test for every install, so a phantom directory has no
    opinion to impose.
    """
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude-academic", token="tok-academic")
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [_snapshot("academic", "academic_session", "Session", 20.0, 1_782_907_200)]
    )

    provider = quota_state()["providers"]["claude"]

    assert provider["detected"] is True
    assert provider["status"] == "ok"
    assert provider["status_detail"] is None
    assert provider["plan"] == "Max 5x"
    # One install on the card, so the payload stays as a single-install card always had it.
    assert "accounts" not in provider


def test_an_install_with_no_rows_yet_reports_its_local_status(monkeypatch, tmp_path):
    """Signed in but never polled: `accounts` names it and says what its own file says.

    This is the documented answer for an install with no snapshot rows yet, and the reason
    a freshly set-up second install is not rendered as a subscription in trouble.
    """
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base", tier="default_claude_max_5x")
    _install(home, ".claude-academic", token="tok-academic", subscription="pro", tier="default_claude_pro")
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [_snapshot("default", "session", "Session", 40.0, 1_782_907_200)]
    )

    by_account = {a["account"]: a for a in quota_state()["providers"]["claude"]["accounts"]}

    assert by_account["academic"]["status"] == "ok"
    assert by_account["academic"]["plan"] == "Pro"
    assert by_account["academic"]["updated_at"] is None


def test_revoking_credential_scan_stops_naming_the_installs(monkeypatch, tmp_path):
    """Which installs exist, and each one's plan, come off the filesystem.

    Consent withdrawn after the fact means the card stops shipping what a scan produced,
    stored rows included. The rows are Tokdash's own data and keep rendering -- just
    without naming a second install the card is no longer allowed to have looked for.
    """
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    _install(home, ".claude-academic", token="tok-academic")
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _snapshot("default", "session", "Session", 40.0, 1_782_907_200),
            _snapshot("academic", "academic_session", "Session", 20.0, 1_782_907_200),
        ]
    )
    config.set_quota_consent({"credential_scan": False, "claude_api": True})

    provider = quota_state()["providers"]["claude"]

    assert "accounts" not in provider
    assert [b["account"] for b in provider["buckets"]] == ["academic", "default"]


def test_a_sibling_without_credentials_does_not_open_a_card(monkeypatch, tmp_path):
    """Detection and discovery must agree, or a dotfile backup grows a permanent card."""
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    (home / ".claude-backup").mkdir()  # restored dotfiles, never signed in
    config.set_quota_consent({"credential_scan": True, "claude_api": True})

    provider = quota_state()["providers"]["claude"]

    assert provider["detected"] is False
    assert "accounts" not in provider


def test_history_keeps_two_subscriptions_on_separate_series():
    store = UsageEntryStore()
    store.insert_quota_snapshots(
        [
            _snapshot("default", "session", "Session", 10.0, 1_782_907_200),
            _snapshot("default", "session", "Session", 30.0, 1_782_909_000),
            _snapshot("academic", "academic_session", "Session", 5.0, 1_782_907_200),
        ]
    )

    series = store.quota_history(providers=["claude"])["series"]

    assert [(s["account"], s["bucket"], len(s["points"])) for s in series] == [
        ("academic", "academic_session", 1),
        ("default", "session", 2),
    ]


# --- the same rule for the other multi-account card ---------------------------


def test_minimax_region_failure_warns_the_card_and_names_the_region(monkeypatch, tmp_path):
    """MiniMax has two accounts on one card for the same reason Claude does.

    Same split as Claude's: the card still warns -- `COMPANION_API.md` says a single broken
    credential must warn about the provider, and a consumer that drops the warning would
    read the stale region's bar as current -- while the per-account list attributes it, so
    the card can print it under China and leave the global plan's numbers alone.
    """
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    config.set_quota_consent({"credential_scan": True, "minimax_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            QuotaSnapshot(
                "minimax", "global", "session", "Session", 30.0, 1_782_909_000, None,
                1_782_907_200, "minimax_api", "ok", {},
            ),
            QuotaSnapshot(
                "minimax", "cn", "api", "MiniMax API", None, None, None,
                1_782_907_200, "minimax_api", "fetch_error", {},
            ),
        ]
    )

    provider = quota_state()["providers"]["minimax"]

    assert provider["status_detail"] == "fetch_error"
    assert provider["status_at"] == 1_782_907_200
    by_account = {a["account"]: a for a in provider["accounts"]}
    assert by_account["cn"]["status_detail"] == "fetch_error"
    assert by_account["global"]["status_detail"] is None
    # The healthy region's meter is still there, and still its own.
    assert [(b["account"], b["used_percent"]) for b in provider["buckets"]] == [("global", 30.0)]


# --- retirement must not reach the providers whose accounts are not directories ----------
# `minimax`, `antigravity` and `grok` all write their FAILURE row under a synthetic
# `default` account when there was no credential to name -- `region if credential else
# "default"`, `raw.get("email") or "default"`, `meta.get("user_id") or "default"` -- while
# their successful rows go under the region, email or user id. A retirement rule that ran
# per provider would therefore let that synthetic row stand in for the whole provider and
# evict the real accounts' stored bars, which is the opposite of what a failed provider is
# required to do: `COMPANION_API.md` says its buckets are last-known and stay visible.


def _row(provider: str, account: str, bucket: str, used, captured: int, status: str = "ok"):
    return QuotaSnapshot(
        provider, account, bucket, bucket, used, 1_782_909_000, None,
        captured, f"{provider}_api", status, {},
    )


def test_a_minimax_credential_failure_keeps_the_regions_last_known_bars(monkeypatch, tmp_path):
    from tokdash.sources.quota import quota_state

    _home(monkeypatch, tmp_path)
    config.set_quota_consent({"credential_scan": True, "minimax_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _row("minimax", "global", "global_5h", 62.0, 1_782_900_000),
            _row("minimax", "cn", "cn_5h", 44.0, 1_782_900_000),
            # `credentials_not_found`: no credential to name, so the row lands on `default`
            # and is the freshest row the provider has.
            _row("minimax", "default", "api", None, 1_782_907_200, status="unavailable"),
        ]
    )

    provider = quota_state()["providers"]["minimax"]

    assert [(b["account"], b["used_percent"]) for b in provider["buckets"]] == [
        ("cn", 44.0),
        ("global", 62.0),
    ]
    assert provider["status_detail"] == "unavailable"  # still warns about the provider


def test_an_antigravity_credential_failure_keeps_the_last_known_bars(monkeypatch, tmp_path):
    from tokdash.sources.quota import quota_state

    _home(monkeypatch, tmp_path)
    config.set_quota_consent({"credential_scan": True, "antigravity_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _row("antigravity", "user@example.com", "pool_a", 80.0, 1_782_900_000),
            _row("antigravity", "default", "api", None, 1_782_907_200, status="fetch_error"),
        ]
    )

    provider = quota_state()["providers"]["antigravity"]

    assert [(b["bucket"], b["used_percent"]) for b in provider["buckets"]] == [("pool_a", 80.0)]
    assert provider["status_detail"] == "fetch_error"


def test_a_grok_credential_failure_keeps_the_last_known_bars(monkeypatch, tmp_path):
    from tokdash.sources.quota import quota_state

    _home(monkeypatch, tmp_path)
    config.set_quota_consent({"credential_scan": True, "grok_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _row("grok", "user-77", "credits", 15.0, 1_782_900_000),
            _row("grok", "default", "api", None, 1_782_907_200, status="stale_token"),
        ]
    )

    provider = quota_state()["providers"]["grok"]

    assert [(b["bucket"], b["used_percent"]) for b in provider["buckets"]] == [("credits", 15.0)]
    assert provider["status_detail"] == "stale_token"


# --- what the card says about itself -----------------------------------------------------


def test_a_consented_machine_with_no_claude_install_names_the_reason(monkeypatch, tmp_path):
    """An empty home has to produce the `unavailable` row, not an unexplained empty card.

    Admitting the default install only when it is `configured` made `discover_profiles`
    return nothing at all here, so `collect_claude_api_snapshots` returned nothing, and the
    card fell back to "No quota snapshots yet." while `network_enabled` kept it on screen --
    with `status_detail` null, so no surface could say why. Declining the scan produced the
    row that granting it did not.
    """
    _home(monkeypatch, tmp_path)  # no `~/.claude`, no sibling, no env token
    config.set_quota_consent({"credential_scan": True, "claude_api": True})

    snapshots = claude.collect_claude_api_snapshots(
        opener=lambda req, timeout=15: FakeResponse({}), now=1_782_907_200
    )

    assert [(s.account, s.bucket, s.status) for s in snapshots] == [
        ("default", "api", "unavailable")
    ]


def test_the_card_plan_is_the_default_installs_not_the_first_alphabetically(
    monkeypatch, tmp_path
):
    """`providers.claude.plan` describes the default install, as `API.md` promises.

    Reading it off the provider-wide view took the first plan any row carried, and rows
    arrive ordered by `(provider, account, bucket)` -- so `academic` came before `default`
    and a Max 20x install reported `pro`. The local read at the end of `quota_state` used to
    paper over this, which is why it only showed with `credential_scan` revoked after
    polling: the one state where nothing overwrites it.
    """
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    _install(home, ".claude-academic", token="tok-academic")
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _snapshot("academic", "academic_session", "Session", 20.0, 1_782_907_200, plan="pro"),
            _snapshot("default", "session", "Session", 75.0, 1_782_907_200, plan="max_20x"),
        ]
    )
    config.set_quota_consent({"credential_scan": False, "claude_api": True})

    assert quota_state()["providers"]["claude"]["plan"] == "max_20x"


def test_a_sibling_only_card_may_still_name_that_siblings_plan(monkeypatch, tmp_path):
    """No `~/.claude` behind the card at all: one account's plan may speak for it."""
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude-academic", token="tok-academic")
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [_snapshot("academic", "academic_session", "Session", 20.0, 1_782_907_200, plan="pro")]
    )
    config.set_quota_consent({"credential_scan": False, "claude_api": True})

    assert quota_state()["providers"]["claude"]["plan"] == "pro"


# --- what the retirement predicate must and must not conclude ----------------------------


def test_a_deleted_default_install_is_retired_too(monkeypatch, tmp_path):
    """Migrating to `~/.claude-work` and deleting `~/.claude` is an ordinary thing to do.

    `claude_profile_dirs` emits `("default", claude_config_dir())` before any existence
    check, so a set built from the names it returns always contains `default` and could
    never retire it -- leaving the deleted install's month-old bar on the card and its
    `stale_token` warning there for good, which is the very thing retirement exists for.
    Presence has to be observed for the default install like any other.
    """
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude-work", token="tok-work")
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _snapshot("default", "session", "Session", 91.0, 1_780_315_200),
            _snapshot("default", "api", "Claude API", None, 1_780_315_300, status="stale_token"),
            _snapshot("work", "work_session", "Session", 12.0, 1_782_907_200),
        ]
    )

    provider = quota_state()["providers"]["claude"]

    assert [b["account"] for b in provider["buckets"]] == ["work"]
    assert provider["status_detail"] is None
    assert provider["updated_at"] == 1_782_907_200


def test_a_default_install_behind_an_unlistable_parent_is_kept(monkeypatch, tmp_path):
    """`CLAUDE_CONFIG_DIR` on an unmounted volume is unobserved, not observably gone."""
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude-work", token="tok-work")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "volume" / "claude"))
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _snapshot("default", "session", "Session", 91.0, 1_780_315_200),
            _snapshot("work", "work_session", "Session", 12.0, 1_782_907_200),
        ]
    )

    provider = quota_state()["providers"]["claude"]

    assert [b["account"] for b in provider["buckets"]] == ["default", "work"]


def test_redirecting_the_default_slot_does_not_retire_the_redirected_install(
    monkeypatch, tmp_path
):
    """Slug allocation is relative to `CLAUDE_CONFIG_DIR`; directory presence is not.

    Pointing the env var at `~/.claude-academic` renames that install `default` and renames
    the plain `~/.claude` beside it `claude`, so a set of assigned names alone does not
    contain `academic` -- and the stored `academic` account would be retired with its
    directory sitting right there in the listing. A directory is known under every name it
    could have been stored under.
    """
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    academic = _install(home, ".claude-academic", token="tok-academic")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(academic))
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _snapshot("default", "session", "Session", 40.0, 1_782_907_200),
            _snapshot("academic", "academic_session", "Session", 20.0, 1_782_907_200),
        ]
    )

    provider = quota_state()["providers"]["claude"]

    assert [b["account"] for b in provider["buckets"]] == ["academic", "default"]


def test_signing_out_of_a_demoted_default_install_keeps_the_redirected_one(
    monkeypatch, tmp_path
):
    """The sign-out rule retires under both of an install's names, and they are not its own.

    A directory's intrinsic slug is a property of the directory; its assigned name is handed
    out relative to `CLAUDE_CONFIG_DIR`. Point the variable at `~/.claude-academic` and the
    plain `~/.claude` beside it is renamed `claude` while keeping the intrinsic slug
    `default` -- which is now the REDIRECTED install's account. Sign out of `~/.claude` and
    retiring everything it answers to blanks the subscription that is working, on the card
    of the machine that still has it. Naming a directory twice is safe in `_known_names`,
    where it can only keep rows; here it deletes them from the card, so a name another
    pollable install answers to is not this one's to retire.
    """
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    (home / ".claude").mkdir()  # present, and signed out: no `.credentials.json`
    academic = _install(home, ".claude-academic", token="tok-academic")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(academic))
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            # `~/.claude-academic` holds the default slot, so its rows are the `default` ones.
            _snapshot("default", "session", "Session", 40.0, 1_782_907_200),
            _snapshot("claude", "claude_session", "Session", 99.0, 1_780_315_200),
        ]
    )

    assert claude.scan_profiles().signed_out == frozenset({"claude"})

    provider = quota_state()["providers"]["claude"]

    assert [b["account"] for b in provider["buckets"]] == ["default"]


def test_signing_out_of_one_of_two_installs_sharing_a_slug_keeps_the_other(
    monkeypatch, tmp_path
):
    """Two directories can want one slug, and the loser keeps the winner's name intrinsically.

    `claude_profile_dirs` makes assigned names unique with a `-2` suffix, so a second
    `.claude-academic` from somewhere else is assigned `claude-academic` while its intrinsic
    slug stays `academic` -- the first one's account. Signing out of the copy must not retire
    the original's windows.
    """
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    live = _install(home, ".claude-academic", token="tok-academic")
    elsewhere = tmp_path / "elsewhere" / ".claude-academic"
    elsewhere.mkdir(parents=True)  # same intrinsic slug, and signed out
    monkeypatch.setenv(
        "TOKDASH_CLAUDE_PROFILES", os.pathsep.join([str(live), str(elsewhere)])
    )
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _snapshot("default", "session", "Session", 40.0, 1_782_907_200),
            _snapshot("academic", "academic_session", "Session", 20.0, 1_782_907_200),
        ]
    )

    assert claude.scan_profiles().signed_out == frozenset({"claude-academic"})

    provider = quota_state()["providers"]["claude"]

    assert [b["account"] for b in provider["buckets"]] == ["academic", "default"]


def test_a_home_naming_no_install_at_all_retires_nothing(monkeypatch, tmp_path):
    """An empty or opaque listing is not the news that every install was deleted.

    An autofs stub before the mount triggers, an fscrypt home before unlock (entries are
    there, their names are ciphertext, so nothing matches `.claude*`) and a roaming profile
    mid-sync all read as "no install exists". Trusting that retires every install on the
    machine at once, which is the transient-for-deleted confusion the redesign exists to
    stop; the listing being readable is necessary but not sufficient.
    """
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    (home / "Documents").mkdir()  # readable, non-empty, and names no Claude install
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _snapshot("default", "session", "Session", 40.0, 1_782_907_200),
            _snapshot("academic", "academic_session", "Session", 20.0, 1_782_907_200),
        ]
    )

    provider = quota_state()["providers"]["claude"]

    assert [b["account"] for b in provider["buckets"]] == ["academic", "default"]


# --- the synthetic account a credential-less failure writes under ------------------------


def test_a_credential_less_minimax_failure_names_no_account(monkeypatch, tmp_path):
    """`credentials_not_found` has no credential to name, so it must name no account.

    MiniMax writes that row under `region if credential else "default"`. Emitting it as an
    `accounts` entry gives the card a third, untranslated Token Plan group headed `default`
    that no region corresponds to, and hands a consumer counting healthy accounts one that
    does not exist. The failure still has to reach the card, so it stays in the
    provider-wide status -- as an error no account claims, which is the honest shape.
    """
    from tokdash.sources.quota import quota_state

    _home(monkeypatch, tmp_path)
    config.set_quota_consent({"credential_scan": True, "minimax_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _row("minimax", "global", "global_5h", 62.0, 1_782_900_000),
            _row("minimax", "cn", "cn_5h", 44.0, 1_782_900_000),
            _row("minimax", "default", "api", None, 1_782_907_200, status="unavailable"),
        ]
    )

    provider = quota_state()["providers"]["minimax"]

    assert [a["account"] for a in provider["accounts"]] == ["global", "cn"]
    assert provider["status_detail"] == "unavailable"  # still warns, still unattributed


def test_a_region_that_never_reported_a_window_keeps_its_account(monkeypatch, tmp_path):
    """`cn` is a real credential that failed early, not the credential-less fallback.

    It measures nothing either, so the rule that drops the synthetic account has to be
    narrow enough to keep this one -- it is what lets the card put CN's error under CN.
    """
    from tokdash.sources.quota import quota_state

    _home(monkeypatch, tmp_path)
    config.set_quota_consent({"credential_scan": True, "minimax_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _row("minimax", "global", "global_5h", 62.0, 1_782_900_000),
            _row("minimax", "cn", "api", None, 1_782_907_200, status="stale_token"),
        ]
    )

    by_account = {a["account"]: a for a in quota_state()["providers"]["minimax"]["accounts"]}

    assert set(by_account) == {"global", "cn"}
    assert by_account["cn"]["status_detail"] == "stale_token"


def test_the_claude_default_install_keeps_its_account_with_nothing_measured(
    monkeypatch, tmp_path
):
    """Claude's `default` IS an install and is that card's primary account.

    Its `unavailable` row with no windows behind it is what drives the consent and
    "not detected" card, so the synthetic-account rule must not reach it.
    """
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    _install(home, ".claude-academic", token="tok-academic")
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _snapshot("default", "api", "Claude API", None, 1_782_907_200, status="unavailable"),
            _snapshot("academic", "academic_session", "Session", 20.0, 1_782_907_200),
        ]
    )

    by_account = {a["account"]: a for a in quota_state()["providers"]["claude"]["accounts"]}

    assert set(by_account) == {"default", "academic"}
    assert by_account["default"]["status_detail"] == "unavailable"


def test_a_negative_expiry_is_expired_rather_than_unknown(monkeypatch, tmp_path):
    """One field must not be read two ways by two functions.

    `_profile_snapshots` calls a truthy-but-past `expiresAt` expired and reports
    `stale_token` for it. Classing the same value as "no recorded expiry" in the ranking
    made it rank as not-expired, so it outranked a credential both functions agree is dead
    and the poll went to the copy that cannot work.
    """
    home = _home(monkeypatch, tmp_path)
    negative = _install(home, ".claude", token=_jwt("acct-9", "issued-1"))
    blob = json.loads((negative / ".credentials.json").read_text(encoding="utf-8"))
    blob["claudeAiOauth"]["expiresAt"] = -1_000
    (negative / ".credentials.json").write_text(json.dumps(blob), encoding="utf-8")
    expired = _install(home, ".claude-work", token=_jwt("acct-9", "issued-2"))
    _expire(expired)  # merely expired, and still the better of the two
    config.set_quota_consent({"credential_scan": True})
    calls: list[str] = []

    def opener(req, timeout=15):
        calls.append(req.get_header("Authorization"))
        return FakeResponse(_usage_payload(40, 1_782_909_000))

    snapshots = claude.collect_claude_api_snapshots(opener=opener, now=1_782_907_200)

    # Both directories hold the same sign-in, so one of them reports -- and it is the
    # merely-expired credential, not the negative one, which is what says the negative one
    # was ranked as expired rather than as an unknown expiry that beats everything expired.
    # (As an unknown it also carried the default install's tie-break, so it won twice over.)
    assert calls == []  # neither is live, so no token is sent out
    assert claude._credential_rank(
        claude.ClaudeProfile("x", home / ".claude", False),
        {"expires_at_ms": -1_000},
        1_782_907_200,
    )[:2] == (0, 1)  # (not live, expiry recorded)
    assert [(s.account, s.status) for s in snapshots] == [("work", "stale_token")]


def test_an_absurdly_long_expiry_does_not_kill_the_whole_claude_poll(monkeypatch, tmp_path):
    """A 400-digit `expiresAt` survives `int()` and then overflows on the way into a float.

    `json.loads` builds an arbitrarily long integer literal, so the `OverflowError` guard on
    the parse does not help: the conversion that raises is the one building the rank tuple.
    `poll_quota` wraps no collector, so one odd credential file 500s `/api/quota/refresh`
    for every provider on the machine.
    """
    home = _home(monkeypatch, tmp_path)
    install = _install(home, ".claude", token=_jwt("acct-9", "issued-1"))
    path = install / ".credentials.json"
    path.write_text(
        path.read_text(encoding="utf-8").replace("4000000000000", "9" * 400),
        encoding="utf-8",
    )
    config.set_quota_consent({"credential_scan": True})

    snapshots = claude.collect_claude_api_snapshots(
        opener=lambda req, timeout=15: FakeResponse(_usage_payload(40, 1_782_909_000)),
        now=1_782_907_200,
    )

    assert [(s.account, s.bucket, s.status) for s in snapshots] == [
        ("default", "session", "ok")
    ]


def test_an_unreadable_credential_is_not_attributed_to_an_older_failure(
    monkeypatch, tmp_path
):
    """`status_account` says whose error the card reports, which is not "someone also failed".

    The sequence is ordinary: CN's key expires, so an older poll left `cn/stale_token`; then
    the credential file is removed, so the newest poll writes the credential-less failure
    under the synthetic `default` account. `_measured_accounts` drops that account, as it
    must -- it is not a Token Plan -- and the card is then left reporting an error that
    belongs to nothing it lists. A consumer asking only "does any account carry an error"
    finds CN's and reads the card as attributed, so a provider whose credentials cannot be
    read at all counts as working. The owner is a fact only the server has.
    """
    from tokdash.sources.quota import quota_state

    _home(monkeypatch, tmp_path)
    config.set_quota_consent({"credential_scan": True, "minimax_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _row("minimax", "global", "global_5h", 62.0, 1_782_900_000),
            _row("minimax", "cn", "cn_5h", 44.0, 1_782_900_000),
            _row("minimax", "cn", "api", None, 1_782_900_100, status="stale_token"),
            # The newest poll: no credential file to read at all.
            _row("minimax", "default", "api", None, 1_782_907_200, status="unavailable"),
        ]
    )

    provider = quota_state()["providers"]["minimax"]

    assert [a["account"] for a in provider["accounts"]] == ["global", "cn"]
    # CN really is still broken, and says so under its own name.
    by_account = {a["account"]: a for a in provider["accounts"]}
    assert by_account["cn"]["status_detail"] == "stale_token"
    # But the error the CARD reports is the newer, credential-less one, which belongs to no
    # account listed -- so nothing here may be mistaken for its owner.
    assert provider["status_detail"] == "unavailable"
    assert provider["status_account"] is None


def test_a_cards_error_names_the_account_it_belongs_to(monkeypatch, tmp_path):
    """The attributed case: one broken sibling beside a working install."""
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    _install(home, ".claude-academic", token="tok-academic")
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _snapshot("default", "session", "Session", 40.0, 1_782_907_200),
            _snapshot("academic", "academic_session", "Session", 20.0, 1_782_900_000),
            _snapshot("academic", "api", "Claude API", None, 1_782_907_200, status="stale_token"),
        ]
    )

    provider = quota_state()["providers"]["claude"]

    assert provider["status_detail"] == "stale_token"
    assert provider["status_account"] == "academic"


def test_a_single_account_card_carries_no_status_account(monkeypatch, tmp_path):
    """`status_account` is only meaningful beside `accounts`, and ships with it."""
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [_snapshot("default", "session", "Session", 40.0, 1_782_907_200)]
    )

    provider = quota_state()["providers"]["claude"]

    assert "accounts" not in provider
    assert "status_account" not in provider
