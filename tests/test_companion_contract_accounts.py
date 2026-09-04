"""The per-account row-failed rule, pinned across contract, fixture and server payload.

`COMPANION_API.md` documents how a companion decides that one quota row is last-known, and
both companions implement it in their own language. Three rounds of review moved that
document without anything checking it against either the payload the server actually sends
or the behavior the clients actually have, so a rule could be (and was) documented and
unimplemented at the same time.

This file closes the half that runs in CI here: the shared fixture is regenerated from
`quota_state()` and compared field by field, and the documented rule is executed against it
to pin the verdicts. The companions' own tests load the same fixture and must reach the same
verdicts, which is what makes the fixture the single place the three agree.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from tokdash.sources.quota import config, quota_state
from tokdash.sources.quota.types import QuotaSnapshot
from tokdash.usage_store import UsageEntryStore

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "companion"
    / "contract"
    / "fixtures"
    / "quota-multi-account.json"
)

# The moment the fixture's newest poll cycle ran, and one two hours earlier.
NOW = 1_785_080_061
EARLIER = NOW - 7_200


def _install(home: Path, name: str, *, tier: str, subscription: str) -> None:
    directory = home / name
    directory.mkdir(parents=True)
    (directory / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "tok",
                    "expiresAt": 4_000_000_000_000,
                    "subscriptionType": subscription,
                    "rateLimitTier": tier,
                }
            }
        ),
        encoding="utf-8",
    )


def _scenario(monkeypatch, tmp_path) -> list[QuotaSnapshot]:
    """A healthy `~/.claude` beside a permanently broken `~/.claude-academic`.

    The shape that makes the two failure levels come apart: the default install's Opus
    window is only reported once Opus has been used, so it carries an OLDER `captured_at`
    than the cycle the sibling's failure was recorded in -- while being perfectly current
    data belonging to a credential that works.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    for name in ("CLAUDE_CONFIG_DIR", "TOKDASH_CLAUDE_PROFILES", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    _install(home, ".claude", tier="default_claude_max_20x", subscription="max")
    _install(home, ".claude-academic", tier="default_claude_pro", subscription="pro")
    config.set_quota_consent({"credential_scan": True, "claude_api": True})

    def snap(account, bucket, label, used, captured, status="ok", plan=None):
        return QuotaSnapshot(
            "claude", account, bucket, label, used, 1_785_700_000, plan,
            captured, "claude_api", status, {},
        )

    return [
        snap("default", "session", "5-hour window", 29.0, NOW, plan="max"),
        snap("default", "weekly_all", "weekly window", 62.0, NOW, plan="max"),
        snap("default", "weekly_scoped_opus", "Opus", 88.0, EARLIER, plan="max"),
        snap("academic", "academic_session", "5-hour window", 91.0, EARLIER, plan="pro"),
        snap("academic", "api", "Claude API", None, NOW, status="stale_token", plan="pro"),
    ]


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# --- the fixture is the payload -----------------------------------------------


def test_the_fixture_is_what_the_server_actually_sends(monkeypatch, tmp_path):
    """Regenerate the fixture's provider from `quota_state()` and compare it.

    A fixture the companions decode against is worthless if the server has since stopped
    producing that shape, and nothing else in the tree checks one. `credential_path` is
    dropped from both sides because it is an absolute path on the machine that generated
    it; every other field, including the whole `accounts` list, has to match exactly.

    Set ``TOKDASH_REWRITE_FIXTURES=1`` to write the regenerated payload back instead of
    asserting, which is how the fixture is refreshed when the payload gains a field on
    purpose. Review that diff -- it is the contract changing.
    """
    UsageEntryStore().insert_quota_snapshots(_scenario(monkeypatch, tmp_path))
    state = quota_state()
    provider = state["providers"]["claude"]

    provider.pop("credential_path", None)
    for entry in provider.get("accounts", []):
        entry.pop("credential_path", None)

    if os.environ.get("TOKDASH_REWRITE_FIXTURES") == "1":
        FIXTURE.write_text(
            json.dumps(
                {
                    "providers": {"claude": provider},
                    "consent": state["consent"],
                    "enabled": True,
                    "poll": state["poll"],
                    "timestamp": NOW,
                },
                indent=1,
            )
            + "\n",
            encoding="utf-8",
        )

    assert provider == _fixture()["providers"]["claude"]


def test_the_fixture_carries_the_fields_the_rule_needs():
    """`accounts` exists, is attributed, and dates each account's own failure."""
    provider = _fixture()["providers"]["claude"]
    accounts = {entry["account"]: entry for entry in provider["accounts"]}

    assert set(accounts) == {"default", "academic"}
    # Every bucket names an account that has an entry, or the rule cannot attribute it.
    assert {bucket["account"] for bucket in provider["buckets"]} <= set(accounts)
    # The healthy account carries no failure timestamp at all -- the case the rule has to
    # short-circuit before it reaches for one.
    assert accounts["default"]["status_detail"] is None
    assert accounts["default"]["status_at"] is None
    # And the broken one dates its own error, newer than the data it did not refresh.
    assert accounts["academic"]["status_detail"] == "stale_token"
    assert accounts["academic"]["status_at"] == NOW
    # `status` alone is not a verdict: the default install reports "ok" and so does the
    # provider-wide view of a card that is warning. This is why the rule reads the detail.
    assert accounts["default"]["status"] == "ok"
    assert provider["status_detail"] == "stale_token"
    assert provider["status_at"] == NOW


# --- the documented rule, executed --------------------------------------------


def _account_failed(entry: dict) -> bool:
    """`COMPANION_API.md` "Group failed", applied to one account entry."""
    detail = entry.get("status_detail")
    if detail and detail != "ok":
        return True
    status = entry.get("status")
    return bool(status) and status not in {"ok", "local_plan"}


def _row_failed(bucket: dict, provider: dict) -> bool:
    """`COMPANION_API.md` "Row failed", transcribed from the documented pseudocode."""
    if not _account_failed(provider):
        return False
    entry = next(
        (e for e in provider.get("accounts") or [] if e.get("account") == bucket.get("account")),
        None,
    )
    if entry is not None:
        if not _account_failed(entry):
            return False
        status_at = entry.get("status_at") or provider.get("status_at")
    else:
        status_at = provider.get("status_at")
    if bucket.get("captured_at") is None or status_at is None:
        return True
    return bucket["captured_at"] < status_at


def test_the_documented_rule_spares_the_working_installs_stale_window():
    """The finding this rule exists for, as a verdict per row.

    `weekly_scoped_opus` is two hours older than the sibling's failure and belongs to the
    install that works. Judged against `providers.claude.status_at` -- the newest error of
    ANY account -- it is marked last-known and drops out of low-quota notification, and
    stays that way for as long as the sibling is broken. Judged against its own account, it
    is what it is: current data for a healthy credential.
    """
    provider = _fixture()["providers"]["claude"]
    verdicts = {
        f"{bucket['account']}/{bucket['bucket']}": _row_failed(bucket, provider)
        for bucket in provider["buckets"]
    }

    assert verdicts == {
        "academic/academic_session": True,  # not refreshed since its own sign-in expired
        "default/session": False,
        "default/weekly_all": False,
        "default/weekly_scoped_opus": False,  # older than the SIBLING's failure, not its own
    }


def test_the_provider_level_rule_is_what_got_the_working_install_wrong():
    """Pins the defect itself, so the rule cannot quietly regress to it.

    If this ever starts agreeing with the per-account rule above, one of the two stopped
    saying what it says here and the fixture no longer exercises the distinction.
    """
    provider = _fixture()["providers"]["claude"]

    def provider_level(bucket):
        if not _account_failed(provider):
            return False
        captured, status_at = bucket.get("captured_at"), provider.get("status_at")
        return True if captured is None or status_at is None else captured < status_at

    opus = next(b for b in provider["buckets"] if b["bucket"] == "weekly_scoped_opus")

    assert provider_level(opus) is True
    assert _row_failed(opus, provider) is False


def test_a_payload_without_accounts_falls_back_to_the_provider():
    """Every pre-`accounts` server, and every single-credential provider today."""
    provider = _fixture()["providers"]["claude"]
    legacy = {**provider}
    legacy.pop("accounts")

    verdicts = {b["bucket"]: _row_failed(b, legacy) for b in legacy["buckets"]}

    # Nothing to attribute, so freshness against the provider is all there is -- and the
    # rows that did not refresh in the failing cycle are last-known, as they should be.
    assert verdicts == {
        "academic_session": True,
        "session": False,
        "weekly_all": False,
        "weekly_scoped_opus": True,
    }


def test_a_bucket_naming_an_unlisted_account_falls_back_to_the_provider():
    """A row the payload cannot attribute must not be un-suppressed by that failure."""
    provider = _fixture()["providers"]["claude"]
    orphan = {"account": "retired", "bucket": "session", "captured_at": EARLIER}

    assert _row_failed(orphan, provider) is True

