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
    directory.mkdir(parents=True)
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


def _jwt(account_id: str, nonce: str) -> str:
    """A JWT-shaped access token, as Claude Code ships them.

    ``nonce`` stands for the token material a refresh replaces; ``account_id`` is the claim
    that survives it, and the only stable thing two copies of one sign-in still share.
    """
    def part(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return ".".join(
        [part(b'{"alg":"none"}'), part(json.dumps({"account_id": account_id, "iat": nonce}).encode()), "sig"]
    )


# --- discovery ----------------------------------------------------------------


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

    def opener(req, timeout=15):
        # Blocks until all three requests are in flight at once; times out if serial.
        barrier.wait()
        return FakeResponse(_usage_payload(30, 1_782_909_000))

    snapshots = claude.collect_claude_api_snapshots(opener=opener, now=1_782_907_200)

    assert [(s.account, s.bucket) for s in snapshots] == [
        ("default", "session"),
        ("b", "b_session"),
        ("c", "c_session"),
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


def test_card_status_follows_the_default_install_not_a_broken_sibling(monkeypatch, tmp_path):
    """One expired sibling must not put a working subscription under an error banner.

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

    assert provider["status"] == "ok"
    assert provider["status_detail"] is None
    assert provider["status_at"] is None
    by_account = {a["account"]: a for a in provider["accounts"]}
    assert by_account["academic"]["status_detail"] == "stale_token"
    assert by_account["academic"]["status_at"] == 1_782_907_200
    assert by_account["default"]["status_detail"] is None


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
    """The other half: a newer success elsewhere must not silence this account's failure."""
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

    assert provider["status_detail"] is None
    by_account = {a["account"]: a for a in provider["accounts"]}
    assert by_account["academic"]["status_detail"] == "stale_token"


def test_windows_of_a_removed_install_are_not_shown_as_current(monkeypatch, tmp_path):
    """A deleted `~/.claude-lab` stops owning a card group; its rows have no expiry."""
    from tokdash.sources.quota import quota_state

    home = _home(monkeypatch, tmp_path)
    _install(home, ".claude", token="tok-base")
    _install(home, ".claude-academic", token="tok-academic")
    config.set_quota_consent({"credential_scan": True, "claude_api": True})
    UsageEntryStore().insert_quota_snapshots(
        [
            _snapshot("default", "session", "Session", 40.0, 1_782_907_200),
            _snapshot("lab", "lab_session", "Session", 90.0, 1_782_800_000),
        ]
    )

    provider = quota_state()["providers"]["claude"]

    assert [b["account"] for b in provider["buckets"]] == ["default"]
    assert [a["account"] for a in provider["accounts"]] == ["default", "academic"]


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


def test_minimax_region_failure_does_not_blackout_the_global_plan(monkeypatch, tmp_path):
    """MiniMax has two accounts on one card for the same reason Claude does.

    The per-account status view is generic, so a failing mainland-China Token Plan is
    attributed to that region instead of warning about the healthy global plan.
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

    assert provider["status"] == "ok"
    assert provider["status_detail"] is None
    by_account = {a["account"]: a for a in provider["accounts"]}
    assert by_account["cn"]["status_detail"] == "fetch_error"
    assert by_account["global"]["status_detail"] is None
