from __future__ import annotations

import json
from datetime import datetime
from urllib.error import HTTPError

from tokdash.sources.quota import commandcode

API = "https://api.commandcode.ai"
WHOAMI = f"{API}/alpha/whoami"
CREDITS = f"{API}/alpha/billing/credits"
SUBSCRIPTIONS = f"{API}/alpha/billing/subscriptions"

# Live shapes captured 2026-09-16 from the real GOAT account (/tmp/cc-c3.json,
# /tmp/cc-s3.json): monthlyCredits is REMAINING, resetAt is epoch MILLISECONDS.
CREDITS_GOAT = {
    "credits": {
        "belowThreshold": False,
        "creditThreshold": 0,
        "monthlyCredits": 69.133417185,
        "purchasedCredits": 0,
        "freeCredits": 0,
    },
    "windowLimits": {
        "limited": True,
        "exceeded": None,
        "fiveHour": {"used": 0.866582815, "cap": 14, "exceeded": False, "resetAt": 1789601836103},
        "weekly": {"used": 0.866582815, "cap": 35, "exceeded": False, "resetAt": 1790188636103},
    },
}
SUBSCRIPTION_GOAT = {
    "success": True,
    "data": {
        # Shape only: the live capture's subscription id is redacted.
        "id": "sub_000000000000000000000000",
        "status": "active",
        "planId": "individual-goat",
        "currentPeriodStart": "2026-09-16T18:18:12.000Z",
        "currentPeriodEnd": "2026-10-16T18:18:12.000Z",
    },
}
WHOAMI_PAYLOAD = {"success": True, "user": {"id": "u1"}, "org": None}


class FakeResponse:
    def __init__(self, raw: bytes):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, n: int = -1) -> bytes:
        return self.raw if n is None or n < 0 else self.raw[:n]


class FakeFP:
    def __init__(self, raw: bytes):
        self.raw = raw

    def read(self, n: int = -1) -> bytes:
        return self.raw

    def close(self) -> None:
        pass


def _opener(routes: dict[str, object], seen: list | None = None):
    """Route by URL; a value is either a payload or an HTTPError status/body pair."""

    def opener(request, timeout=0):
        url = request.full_url
        if seen is not None:
            seen.append((url, request.get_header("Authorization")))
        assert request.get_header("Authorization").startswith("Bearer ")
        assert request.get_header("Accept") == "application/json"
        route = routes.get(url.split("?")[0])
        if route is None:
            raise AssertionError(f"unexpected URL {url}")
        if isinstance(route, tuple) and route and route[0] == "error":
            _kind, code, body = route
            raise HTTPError(url, code, "Error", {}, FakeFP(body if isinstance(body, bytes) else json.dumps(body).encode()))
        return FakeResponse(json.dumps(route).encode("utf-8"))

    return opener


def _live_routes(credits=CREDITS_GOAT, subscription=SUBSCRIPTION_GOAT, whoami=WHOAMI_PAYLOAD):
    return {WHOAMI: whoami, CREDITS: credits, SUBSCRIPTIONS: subscription}


def _auth_files(tmp_path, monkeypatch, native=None, opencode=None):
    commandcode_home = tmp_path / ".commandcode"
    commandcode_home.mkdir(parents=True, exist_ok=True)
    if native is not None:
        (commandcode_home / "auth.json").write_text(json.dumps(native), encoding="utf-8")
    monkeypatch.setattr(commandcode.clientpaths, "commandcode_home", lambda: commandcode_home)
    opencode_dir = tmp_path / "opencode-data"
    opencode_dir.mkdir(parents=True, exist_ok=True)
    if opencode is not None:
        (opencode_dir / "auth.json").write_text(json.dumps({"commandcode": opencode}), encoding="utf-8")
    monkeypatch.setattr(commandcode.clientpaths, "opencode_data_dir", lambda: opencode_dir)
    for name in ("COMMAND_CODE_API_KEY", "COMMANDCODE_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _by_bucket(snapshots):
    return {s.bucket: s for s in snapshots}


def test_collects_three_goat_windows_with_catalog_monthly_derivation(monkeypatch, tmp_path):
    _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_live", "userName": "me"})

    snapshots = commandcode.collect_commandcode_api_snapshots(
        opener=_opener(_live_routes()), now=1_789_000_000
    )

    buckets = _by_bucket(snapshots)
    assert sorted(buckets) == ["5h", "7d", "monthly"]
    assert all(
        s.provider == "commandcode" and s.source == "commandcode_api" and s.status == "ok"
        for s in snapshots
    )
    assert buckets["5h"].used_percent == round(0.866582815 / 14 * 100, 4)
    assert buckets["7d"].used_percent == round(0.866582815 / 35 * 100, 4)
    # 70 - 69.133417185 = 0.866582815 used out of a 70-credit pool.
    assert buckets["monthly"].used_percent == round(0.866582815 / 70 * 100, 4)
    assert buckets["monthly"].plan == "GOAT"
    assert all(s.plan == "GOAT" for s in snapshots)
    # resetAt is epoch milliseconds; rows store seconds.
    assert buckets["5h"].resets_at == 1_789_601_836
    assert buckets["7d"].resets_at == 1_790_188_636
    assert buckets["monthly"].resets_at == int(
        datetime.fromisoformat("2026-10-16T18:18:12+00:00").timestamp()
    )


def test_goat_arithmetic_is_self_verifying_and_pins_caps_and_ratios(monkeypatch, tmp_path):
    """Pins the live account's invariant: the monthly remainder and each window's reported
    `used` agree (70 - monthlyCredits == reported used), and 14/35 are GOAT's documented
    20%/50% ratios of the 70-credit pool.

    The ``round(70 - remaining, 9) == reported_used`` assert below guards only the FIXTURE's
    internal consistency — both sides are hand-written inputs. What actually pins
    ``_monthly_row`` is the derivation asserted against the collector's output in the loop:
    pool, the clamped monthly percent, and the 5h/7d per-cap percents. Both live samples
    captured 2026-09-16 are pinned: /tmp/cc-c3.json (0.866582815 used) and
    /tmp/cc-credits.json (0.241822 used).
    """
    samples = [
        (69.133417185, 0.866582815),
        (69.758178, 0.241822),
    ]
    for remaining, reported_used in samples:
        credits = json.loads(json.dumps(CREDITS_GOAT))
        credits["credits"]["monthlyCredits"] = remaining
        credits["windowLimits"]["fiveHour"]["used"] = reported_used
        credits["windowLimits"]["weekly"]["used"] = reported_used
        _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_live"})

        buckets = _by_bucket(
            commandcode.collect_commandcode_api_snapshots(
                opener=_opener(_live_routes(credits=credits)), now=1_789_000_000
            )
        )

        assert round(70 - remaining, 9) == reported_used
        assert credits["windowLimits"]["fiveHour"]["cap"] / 70 == 0.20
        assert credits["windowLimits"]["weekly"]["cap"] / 70 == 0.50
        assert buckets["5h"].used_percent == round(reported_used / 14 * 100, 4)
        assert buckets["7d"].used_percent == round(reported_used / 35 * 100, 4)
        assert buckets["monthly"].raw["pool"] == 70.0
        assert buckets["monthly"].raw["window_limits"] == credits["windowLimits"]
        assert buckets["monthly"].used_percent == round(reported_used / 70 * 100, 4)


def test_monthly_raw_json_carries_reporting_evidence(monkeypatch, tmp_path):
    _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_live"})

    snapshots = commandcode.collect_commandcode_api_snapshots(
        opener=_opener(_live_routes()), now=1_789_000_000
    )
    raw = _by_bucket(snapshots)["monthly"].raw

    assert raw["credits"] == CREDITS_GOAT["credits"]
    assert raw["window_limits"] == CREDITS_GOAT["windowLimits"]
    assert raw["plan_id"] == "individual-goat"
    assert raw["plan_credits"] == 70
    assert raw["pool"] == 70.0
    assert raw["monthly_credits"] == 69.133417185
    assert raw["subscription_status"] == "active"


def test_purchased_and_free_topups_grow_the_pool(monkeypatch, tmp_path):
    credits = json.loads(json.dumps(CREDITS_GOAT))
    credits["credits"]["purchasedCredits"] = 10.0
    credits["credits"]["freeCredits"] = 5.0
    _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_live"})

    snapshots = commandcode.collect_commandcode_api_snapshots(
        opener=_opener(_live_routes(credits=credits)), now=1_789_000_000
    )
    monthly = _by_bucket(snapshots)["monthly"]

    # pool = max(70, 69.133417185) + 10 + 5 = 85; used = 85 - (69.133417185 + 10 + 5)
    assert monthly.raw["pool"] == 85.0
    assert monthly.used_percent == round(0.866582815 / 85 * 100, 4)


def test_topup_above_catalog_floor_raises_the_pool(monkeypatch, tmp_path):
    # monthlyCredits is REMAINING, so a pool can never fall below it: the vendor CLI's
    # max(catalog, monthlyCredits) is what keeps a big remaining balance from producing
    # a negative used value.
    credits = json.loads(json.dumps(CREDITS_GOAT))
    credits["credits"]["monthlyCredits"] = 90.0
    _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_live"})

    monthly = _by_bucket(
        commandcode.collect_commandcode_api_snapshots(
            opener=_opener(_live_routes(credits=credits)), now=1_789_000_000
        )
    )["monthly"]

    assert monthly.raw["pool"] == 90.0
    assert monthly.used_percent == 0.0


def test_unknown_plan_withdraws_monthly_instead_of_omitting_it(monkeypatch, tmp_path):
    subscription = json.loads(json.dumps(SUBSCRIPTION_GOAT))
    subscription["data"]["planId"] = "individual-mystery"
    _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_live"})

    snapshots = commandcode.collect_commandcode_api_snapshots(
        opener=_opener(_live_routes(subscription=subscription)), now=1_789_000_000
    )
    buckets = _by_bucket(snapshots)

    assert buckets["monthly"].used_percent is None
    assert buckets["monthly"].plan is None
    assert buckets["monthly"].status == "ok"
    assert buckets["monthly"].raw["suppressed"] == "plan_unresolved"
    # The 5h/7d windows are still real quota; only the plan label is withheld.
    assert buckets["5h"].used_percent is not None
    assert buckets["5h"].plan is None


def test_canceled_subscription_withdraws_monthly(monkeypatch, tmp_path):
    subscription = json.loads(json.dumps(SUBSCRIPTION_GOAT))
    subscription["data"]["status"] = "canceled"
    _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_live"})

    snapshots = commandcode.collect_commandcode_api_snapshots(
        opener=_opener(_live_routes(subscription=subscription)), now=1_789_000_000
    )

    assert _by_bucket(snapshots)["monthly"].raw["suppressed"] == "plan_unresolved"


def test_credits_plan_id_fallback_when_subscription_has_none(monkeypatch, tmp_path):
    subscription = json.loads(json.dumps(SUBSCRIPTION_GOAT))
    subscription["data"]["planId"] = None
    credits = json.loads(json.dumps(CREDITS_GOAT))
    credits["credits"]["planId"] = "individual_goat"  # underscore spelling normalizes
    _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_live"})

    snapshots = commandcode.collect_commandcode_api_snapshots(
        opener=_opener(_live_routes(credits=credits, subscription=subscription)), now=1_789_000_000
    )

    assert _by_bucket(snapshots)["monthly"].plan == "GOAT"


def test_unresolved_subscription_plan_falls_through_to_credits_id(monkeypatch, tmp_path):
    # An unknown subscription planId is truthy but must not be recorded as the monthly row's
    # plan_id when the CREDITS leg is the one that actually resolved: label and raw evidence
    # both have to name the credits id.
    subscription = json.loads(json.dumps(SUBSCRIPTION_GOAT))
    subscription["data"]["planId"] = "individual-mystery"
    credits = json.loads(json.dumps(CREDITS_GOAT))
    credits["credits"]["planId"] = "individual_goat"
    _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_live"})

    monthly = _by_bucket(
        commandcode.collect_commandcode_api_snapshots(
            opener=_opener(_live_routes(credits=credits, subscription=subscription)), now=1_789_000_000
        )
    )["monthly"]

    assert monthly.plan == "GOAT"
    assert monthly.raw["plan_id"] == "individual-goat"
    assert monthly.raw["plan_credits"] == 70


def test_longest_alias_wins_over_shorter_prefix(monkeypatch):
    # individual-pro-v1 must not be shadowed by individual-pro.
    assert commandcode.resolve_plan("individual-pro-v1") == ("Pro", 80)
    assert commandcode.resolve_plan("individual-pro") == ("Pro", 30)
    assert commandcode.resolve_plan("individual-max") == ("Max 10x", 150)
    assert commandcode.resolve_plan("individual-ultra") == ("Max 20x", 300)
    assert commandcode.resolve_plan("teams-pro") == ("Team Pro", 40)
    assert commandcode.resolve_plan("") is None
    assert commandcode.resolve_plan("nope-plan") is None


def test_env_key_short_circuits_file_reads(monkeypatch, tmp_path):
    _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_file"}, opencode={"type": "api", "key": "oc_file"})
    monkeypatch.setenv("COMMAND_CODE_API_KEY", "user_env")
    before_native = (tmp_path / ".commandcode" / "auth.json").read_bytes()

    def unexpected(*_args, **_kwargs):
        raise AssertionError("credential file read despite explicit environment key")

    monkeypatch.setattr(commandcode, "_native_file_key", unexpected)
    monkeypatch.setattr(commandcode, "_opencode_auth_key", unexpected)
    seen: list = []

    commandcode.collect_commandcode_api_snapshots(
        opener=_opener(_live_routes(), seen=seen), now=1_789_000_000
    )

    assert seen[0][1] == "Bearer user_env"
    assert (tmp_path / ".commandcode" / "auth.json").read_bytes() == before_native


def test_second_env_spelling_accepted_when_first_unset(monkeypatch, tmp_path):
    _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_file"})
    monkeypatch.setenv("COMMANDCODE_API_KEY", "user_alt")

    credential = commandcode.read_commandcode_key()

    assert credential is not None and credential.token == "user_alt"
    assert credential.source == "COMMANDCODE_API_KEY"


def test_blank_env_falls_back_to_native_auth_file(monkeypatch, tmp_path):
    _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_file", "userName": "me"})
    monkeypatch.setenv("COMMAND_CODE_API_KEY", "   ")

    credential = commandcode.read_commandcode_key()

    assert credential is not None and credential.token == "user_file"
    assert credential.source == "~/.commandcode/auth.json"


def test_opencode_auth_entry_used_when_native_files_absent(monkeypatch, tmp_path):
    _auth_files(tmp_path, monkeypatch, opencode={"type": "api", "key": "user_oc"})

    credential = commandcode.read_commandcode_key()

    assert credential is not None and credential.token == "user_oc"
    assert credential.source == "opencode auth.json:commandcode"


def test_opencode_oauth_entry_is_not_used_as_an_api_key(monkeypatch, tmp_path):
    _auth_files(tmp_path, monkeypatch, opencode={"type": "oauth", "key": "oauth-token"})

    assert commandcode.read_commandcode_key() is None


def test_missing_credentials_reports_unavailable_without_network(monkeypatch, tmp_path):
    _auth_files(tmp_path, monkeypatch)

    snapshots = commandcode.collect_commandcode_api_snapshots(
        opener=lambda request, timeout=0: (_ for _ in ()).throw(AssertionError("network called")),
        now=1,
    )

    assert len(snapshots) == 1
    assert snapshots[0].bucket == "api"
    assert snapshots[0].status == "unavailable"
    assert snapshots[0].raw["error"] == "credentials_not_found"


def test_401_marks_key_stale_with_refresh_hint(monkeypatch, tmp_path):
    _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_live"})
    body = {"success": False, "error": {"code": "UNAUTHORIZED", "status": 401, "message": "Invalid 'Authorization' header or token."}}
    routes = _live_routes()
    routes[CREDITS] = ("error", 401, body)

    snapshots = commandcode.collect_commandcode_api_snapshots(opener=_opener(routes), now=7)

    assert len(snapshots) == 1
    assert snapshots[0].status == "stale_token"
    assert "HTTP 401" in snapshots[0].raw["error"]
    assert "cmd login" in snapshots[0].raw["hint"]
    assert snapshots[0].captured_at == 7


def test_403_marks_key_stale(monkeypatch, tmp_path):
    _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_live"})
    routes = _live_routes()
    routes[CREDITS] = ("error", 403, {"success": False, "error": {"code": "FORBIDDEN", "message": "Nope."}})

    snapshots = commandcode.collect_commandcode_api_snapshots(opener=_opener(routes), now=7)

    assert len(snapshots) == 1
    assert snapshots[0].status == "stale_token"


def test_429_is_fetch_error_not_stale_token(monkeypatch, tmp_path):
    # The vendor documents a `rate_limit` error code: throttling says nothing about the key,
    # so it must not raise the stale-token notice.
    _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_live"})
    routes = _live_routes()
    routes[CREDITS] = ("error", 429, {"success": False, "error": {"code": "rate_limit", "message": "Slow down."}})

    snapshots = commandcode.collect_commandcode_api_snapshots(opener=_opener(routes), now=7)

    assert len(snapshots) == 1
    assert snapshots[0].status == "fetch_error"


def test_5xx_body_truncated_to_fetch_error(monkeypatch, tmp_path):
    _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_live"})
    routes = _live_routes()
    routes[CREDITS] = ("error", 502, b"<html>" + b"x" * 2000)

    snapshots = commandcode.collect_commandcode_api_snapshots(opener=_opener(routes), now=7)

    assert len(snapshots) == 1
    assert snapshots[0].status == "fetch_error"
    assert len(snapshots[0].raw["error"]) <= 230


def test_malformed_json_is_fetch_error(monkeypatch, tmp_path):
    _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_live"})

    def opener(request, timeout=0):
        return FakeResponse(b"not json{")

    snapshots = commandcode.collect_commandcode_api_snapshots(opener=opener, now=7)

    assert snapshots[0].status == "fetch_error"


def test_failed_credits_still_records_error_and_omits_bars(monkeypatch, tmp_path):
    _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_live"})
    routes = _live_routes()
    routes[CREDITS] = ("error", 500, b"boom")

    snapshots = commandcode.collect_commandcode_api_snapshots(opener=_opener(routes), now=7)

    assert len(snapshots) == 1
    assert snapshots[0].bucket == "api"
    assert snapshots[0].status == "fetch_error"


def test_failed_subscriptions_keeps_windows_and_records_error(monkeypatch, tmp_path):
    _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_live"})
    routes = _live_routes()
    routes[SUBSCRIPTIONS] = ("error", 503, b"down")

    snapshots = commandcode.collect_commandcode_api_snapshots(opener=_opener(routes), now=7)

    buckets = _by_bucket(snapshots)
    assert sorted(buckets) == ["5h", "7d", "api", "monthly"]
    assert buckets["5h"].used_percent is not None
    assert buckets["api"].status == "fetch_error"
    # Without a subscription there is no plan to derive Monthly from, so the bucket is
    # withdrawn rather than left showing the previous cycle's bar.
    assert buckets["monthly"].used_percent is None
    assert buckets["monthly"].raw["suppressed"] == "plan_unresolved"
    # One cycle, one captured_at — failure rows included.
    assert {s.captured_at for s in snapshots} == {7}


def test_limited_false_reports_unavailable_no_usage_limits(monkeypatch, tmp_path):
    credits = json.loads(json.dumps(CREDITS_GOAT))
    credits["windowLimits"] = {"limited": False, "exceeded": None, "fiveHour": None, "weekly": None}
    _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_live"})

    snapshots = commandcode.collect_commandcode_api_snapshots(
        opener=_opener(_live_routes(credits=credits)), now=7
    )

    api_rows = [s for s in snapshots if s.bucket == "api"]
    assert [s.raw["error"] for s in api_rows] == ["no_usage_limits"]
    # No fabricated 0% window bars: 5h/7d are absent, leaving the subscription's
    # Monthly row and the cycle's status row.
    assert sorted(_by_bucket(snapshots)) == ["api", "monthly"]
    assert _by_bucket(snapshots)["monthly"].used_percent is not None
    assert {s.captured_at for s in snapshots} == {7}


def test_percent_clamped_and_caps_without_numbers_ignored(monkeypatch, tmp_path):
    credits = json.loads(json.dumps(CREDITS_GOAT))
    credits["windowLimits"]["fiveHour"] = {"used": 140.5, "cap": 14, "resetAt": 1_789_601_836_103}
    credits["windowLimits"]["weekly"] = {"used": 1.0, "cap": 0}
    _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_live"})

    buckets = _by_bucket(
        commandcode.collect_commandcode_api_snapshots(
            opener=_opener(_live_routes(credits=credits)), now=7
        )
    )

    assert buckets["5h"].used_percent == 100.0
    assert "7d" not in buckets


def test_whoami_org_scopes_the_billing_requests(monkeypatch, tmp_path):
    _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_live"})
    whoami = {"success": True, "user": {"id": "u1"}, "org": {"id": "org_42"}}
    seen: list = []
    routes = {WHOAMI: whoami, CREDITS: CREDITS_GOAT, SUBSCRIPTIONS: SUBSCRIPTION_GOAT}

    commandcode.collect_commandcode_api_snapshots(
        opener=_opener(routes, seen=seen), now=1_789_000_000
    )

    urls = [url for url, _auth in seen]
    assert f"{WHOAMI}" in urls
    assert f"{CREDITS}?orgId=org_42" in urls
    assert f"{SUBSCRIPTIONS}?orgId=org_42" in urls


def test_null_org_omits_the_org_id_param(monkeypatch, tmp_path):
    # With no org, the billing URLs must carry no `orgId` param at all (the live API 400s on
    # a malformed/empty orgId), so this pins the exact URL for a null-org whoami payload.
    _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_live"})
    whoami = {"success": True, "user": {"id": "u1"}, "org": None}
    seen: list = []
    routes = {WHOAMI: whoami, CREDITS: CREDITS_GOAT, SUBSCRIPTIONS: SUBSCRIPTION_GOAT}

    commandcode.collect_commandcode_api_snapshots(opener=_opener(routes, seen=seen), now=1_789_000_000)

    urls = [url for url, _auth in seen]
    assert urls == [WHOAMI, CREDITS, SUBSCRIPTIONS]
    assert not any("orgId" in url for url in urls)


def test_opencode_auth_plain_string_entry_is_a_credential(monkeypatch, tmp_path):
    # The mirror entry can be a plain token string, not just an {apiKey: ...} object.
    _auth_files(tmp_path, monkeypatch, opencode="user_oc_string")

    credential = commandcode.read_commandcode_key()

    assert credential is not None and credential.token == "user_oc_string"
    assert credential.source == "opencode auth.json:commandcode"


def test_whoami_failure_does_not_block_the_billing_calls(monkeypatch, tmp_path):
    _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_live"})
    routes = _live_routes()
    routes[WHOAMI] = ("error", 500, b"boom")

    buckets = _by_bucket(
        commandcode.collect_commandcode_api_snapshots(opener=_opener(routes), now=1_789_000_000)
    )

    # orgId is best-effort: org limits are out of scope, so an unscoped read is fine.
    assert sorted(buckets) == ["5h", "7d", "monthly"]


def test_collector_never_rewrites_the_auth_files(monkeypatch, tmp_path):
    _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_live", "userName": "me"})
    native_path = tmp_path / ".commandcode" / "auth.json"
    before = native_path.read_bytes()

    commandcode.collect_commandcode_api_snapshots(opener=_opener(_live_routes()), now=1_789_000_000)

    assert native_path.read_bytes() == before


def test_detection_reads_key_content_only_with_credential_scan(monkeypatch, tmp_path):
    from tokdash.sources import quota

    _auth_files(tmp_path, monkeypatch, opencode={"type": "api", "key": "user_oc"})

    # Consented: the OpenCode-only key path surfaces the card.
    monkeypatch.setattr(quota.config, "credential_scan_enabled", lambda: True)
    assert "commandcode" in quota._detected_local_providers([])

    # Pre-consent detection is shallow: no ~/.commandcode/auth.json and no env var, so
    # an OpenCode-only key cannot surface the card before consent.
    monkeypatch.setattr(quota.config, "credential_scan_enabled", lambda: False)
    assert "commandcode" not in quota._detected_local_providers([])

    (tmp_path / ".commandcode" / "auth.json").write_text(json.dumps({"apiKey": "user_native"}), encoding="utf-8")
    assert "commandcode" in quota._detected_local_providers([])

    (tmp_path / ".commandcode" / "auth.json").unlink()
    monkeypatch.setenv("COMMANDCODE_API_KEY", "user_env")
    assert "commandcode" in quota._detected_local_providers([])


def test_monthly_suppression_withdraws_a_stored_bar_through_api(monkeypatch, tmp_path):
    from tokdash import api
    from tokdash.sources import quota
    from tokdash.sources.quota import config
    from tokdash.usage_store import UsageEntryStore

    _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_live"})
    config.set_quota_consent({"credential_scan": True, "commandcode_api": True})
    monkeypatch.setattr(quota, "collect_local_snapshots", lambda store=None: [])
    monkeypatch.setattr(quota, "_LAST_POLL_AT", None)
    store = UsageEntryStore()

    def poll(routes, now):
        opener = _opener(routes)
        monkeypatch.setattr(
            quota,
            "collect_commandcode_api_snapshots",
            lambda: commandcode.collect_commandcode_api_snapshots(opener=opener, now=now),
        )
        quota.poll_quota(store)
        api._clear_cache()
        return api.get_quota()["providers"]["commandcode"]

    provider = poll(_live_routes(), 1_789_000_000)
    assert provider["plan"] == "GOAT"
    buckets = {b["bucket"]: b for b in provider["buckets"]}
    assert sorted(buckets) == ["5h", "7d", "monthly"]
    assert buckets["monthly"]["used_percent"] is not None

    subscription = json.loads(json.dumps(SUBSCRIPTION_GOAT))
    subscription["data"]["status"] = "canceled"
    provider = poll(_live_routes(subscription=subscription), 1_789_000_000)

    assert provider["plan"] is None
    buckets = {b["bucket"]: b for b in provider["buckets"]}
    assert buckets["monthly"]["used_percent"] is None
    assert "monthly" in buckets  # withdrawn, not merely omitted


def test_status_recovery_retires_the_cycle_error(monkeypatch, tmp_path):
    from tokdash import api
    from tokdash.sources import quota
    from tokdash.sources.quota import config
    from tokdash.usage_store import UsageEntryStore

    _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_live"})
    config.set_quota_consent({"credential_scan": True, "commandcode_api": True})
    monkeypatch.setattr(quota, "collect_local_snapshots", lambda store=None: [])
    monkeypatch.setattr(quota, "_LAST_POLL_AT", None)
    store = UsageEntryStore()

    def poll(routes, now):
        opener = _opener(routes)
        monkeypatch.setattr(
            quota,
            "collect_commandcode_api_snapshots",
            lambda: commandcode.collect_commandcode_api_snapshots(opener=opener, now=now),
        )
        quota.poll_quota(store)
        api._clear_cache()
        return api.get_quota()["providers"]["commandcode"]

    provider = poll(_live_routes(), 1_789_000_000)
    assert provider["status"] == "ok"

    stale_routes = _live_routes()
    stale_routes[CREDITS] = ("error", 401, {"success": False, "error": {"message": "Invalid token."}})
    provider = poll(stale_routes, 1_789_000_100)
    assert provider["status"] == "stale_token"
    assert provider["status_detail"] == "stale_token"
    assert provider["status_at"] == 1_789_000_100
    # The stored bars survive as history under the notice.
    assert {b["bucket"] for b in provider["buckets"]} == {"5h", "7d", "monthly"}

    provider = poll(_live_routes(), 1_789_000_200)
    assert provider["status"] == "ok"
    assert provider["status_detail"] is None
    assert provider["status_at"] is None


def test_same_cycle_success_cannot_retire_a_same_cycle_error(monkeypatch, tmp_path):
    # A failure and window rows written under ONE captured_at: ok_at can never be
    # strictly greater than status_at, so the error stays live.
    from tokdash import api
    from tokdash.usage_store import UsageEntryStore

    _auth_files(tmp_path, monkeypatch, native={"apiKey": "user_live"})
    api._clear_cache()
    store = UsageEntryStore()
    store.insert_quota_snapshots(
        commandcode.collect_commandcode_api_snapshots(
            opener=_opener({**_live_routes(), SUBSCRIPTIONS: ("error", 503, b"down")}), now=5
        )
    )

    provider = api.get_quota()["providers"]["commandcode"]

    assert provider["status_detail"] == "fetch_error"
    assert provider["status_at"] == 5


def test_history_running_high_needs_no_code_change_for_fixed_epoch_windows(monkeypatch, tmp_path):
    """Command Code's windows are first-use-anchored fixed epochs: `used` rises inside
    one epoch (dips and all) and only falls at a rollover, which advances `resets_at`.
    The running-high path already measures that correctly, so
    `_quota_history_uses_adjacent_deltas` is pinned to stay as-is."""
    from tokdash.sources.quota.types import QuotaSnapshot
    from tokdash.usage_store import UsageEntryStore, _quota_history_uses_adjacent_deltas

    assert _quota_history_uses_adjacent_deltas("commandcode", "5h", 1_789_601_836) is False
    assert _quota_history_uses_adjacent_deltas("commandcode", "7d", 1_790_188_636) is False
    # Monthly is the same fixed-epoch shape: a rollover advances resets_at, so it stays on
    # the running-high path too.
    assert _quota_history_uses_adjacent_deltas("commandcode", "monthly", 1_792_174_692) is False

    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    epoch_a, epoch_b = 1_789_601_836, 1_789_619_636  # a rollover advances resets_at

    def row(percent, captured_at, resets_at):
        return QuotaSnapshot(
            "commandcode", "default", "5h", "5-hour", percent, resets_at, "GOAT",
            captured_at, "commandcode_api", "ok", {},
        )

    store.insert_quota_snapshots(
        [
            row(1.0, 1_000, epoch_a),   # epoch A baseline
            row(3.0, 1_600, epoch_a),   # +2 inside the epoch
            row(2.5, 2_200, epoch_a),   # dip: not consumption
            row(0.2, 3_000, epoch_b),   # rollover: new baseline, drop not counted
            row(1.7, 3_600, epoch_b),   # +1.5 after the rollover
        ]
    )

    history = store.quota_history(providers=["commandcode"], granularity="hour")

    consumption = history["series"][0]["consumption"]
    assert [(p["consumed_percent"]) for p in consumption] == [2.0, 1.5]
    assert history["series"][0]["estimated"] is False
