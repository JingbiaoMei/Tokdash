"""Tests for boundary-poll scheduling: sampling shortly before/after a fixed-reset window's
reset so `usage_store.quota_history`'s running-high consumption model gets a sample near the
true peak/baseline (see the module docstring on `_flush` in usage_store.py)."""
from __future__ import annotations

import tokdash.cli as cli
import tokdash.sources.quota as quota
from tokdash.sources.quota import config
from tokdash.sources.quota import next_boundary_poll_at, next_boundary_poll_target_with_kind, plan_boundary_poll

_CFG = config.BoundaryPollConfig(enabled=True, pre_seconds=120, post_reset_enabled=True, post_seconds=120)


def _row(provider: str, bucket: str, resets_at: int | None) -> dict:
    return {"provider": provider, "bucket": bucket, "resets_at": resets_at}


def test_next_boundary_poll_at_picks_nearest_future_pre_reset_target():
    now = 1_000
    rows = [
        _row("claude", "5h", now + 500),  # pre target: now + 380
        _row("claude", "weekly", now + 99_000),  # pre target: far away
    ]

    assert next_boundary_poll_at(now, rows, _CFG) == now + 380


def test_next_boundary_poll_at_excludes_codex_7d_and_reset_less_buckets():
    now = 1_000
    rows = [
        _row("codex", "7d", now + 500),  # rolling window: excluded
        _row("codex", "auto_7d", now + 400),  # suffixed rolling window: excluded
        _row("codex", "5h", None),  # no reset timestamp: excluded
        _row("claude", "5h", now + 10_000),  # only qualifying row
    ]

    assert next_boundary_poll_at(now, rows, _CFG) == now + 10_000 - _CFG.pre_seconds


def test_next_boundary_poll_at_includes_post_reset_target_when_enabled():
    now = 1_000
    # Reset already happened relative to the pre-reset lead time (pre target <= now, so it
    # is dropped), but the post-reset sample is still ahead of us.
    rows = [_row("claude", "5h", now)]

    assert next_boundary_poll_at(now, rows, _CFG) == now + _CFG.post_seconds


def test_next_boundary_poll_at_omits_post_reset_target_when_disabled():
    now = 1_000
    rows = [_row("claude", "5h", now)]
    cfg = config.BoundaryPollConfig(enabled=True, pre_seconds=120, post_reset_enabled=False, post_seconds=120)

    assert next_boundary_poll_at(now, rows, cfg) is None


def test_next_boundary_poll_at_none_when_nothing_qualifies():
    now = 1_000
    rows = [_row("codex", "7d", now + 500), _row("codex", "5h", None)]

    assert next_boundary_poll_at(now, rows, _CFG) is None


def test_next_boundary_poll_at_none_when_disabled():
    now = 1_000
    rows = [_row("claude", "5h", now + 100_000)]
    cfg = config.BoundaryPollConfig(enabled=False, pre_seconds=120, post_reset_enabled=True, post_seconds=120)

    assert next_boundary_poll_at(now, rows, cfg) is None


def test_next_boundary_poll_at_never_returns_candidate_at_or_before_now():
    now = 1_000
    # pre target lands exactly on `now`; with post disabled there is nothing left to fire.
    rows = [_row("claude", "5h", now + _CFG.pre_seconds)]
    cfg = config.BoundaryPollConfig(enabled=True, pre_seconds=_CFG.pre_seconds, post_reset_enabled=False, post_seconds=120)

    assert next_boundary_poll_at(now, rows, cfg) is None


def test_next_boundary_poll_at_ignores_jitter_rearmed_candidate():
    """The same physical boundary must fire once, despite +/-1s `resets_at` jitter.

    Regression: we just fired at `now` off a 13:39:59-style reading; the next poll reports
    the reset 1s later, putting that SAME boundary at now+1. A bare `> now` guard re-armed
    it and the daemon fired a duplicate poll one sleep-floor later (observed: 5 pre fires
    across 4 real resets).
    """
    now = 1_000
    cfg = config.BoundaryPollConfig(
        enabled=True, pre_seconds=_CFG.pre_seconds, post_reset_enabled=False, post_seconds=120
    )
    rows = [_row("claude", "5h", now + _CFG.pre_seconds + 1)]  # pre target: now + 1 (jitter)

    assert next_boundary_poll_at(now, rows, cfg) is None


def test_next_boundary_poll_at_still_returns_candidate_beyond_jitter():
    """The jitter horizon must not swallow a genuinely upcoming boundary."""
    now = 1_000
    cfg = config.BoundaryPollConfig(
        enabled=True, pre_seconds=_CFG.pre_seconds, post_reset_enabled=False, post_seconds=120
    )
    rows = [_row("claude", "5h", now + _CFG.pre_seconds + 60)]  # pre target: now + 60

    assert next_boundary_poll_at(now, rows, cfg) == now + 60


def test_next_boundary_poll_at_skips_malformed_reset_values():
    rows = [_row("claude", "5h", "not-a-timestamp")]

    assert next_boundary_poll_at(1_000, rows, _CFG) is None


def test_next_boundary_poll_target_with_kind_reports_pre_and_post():
    now = 1_000
    pre_only = [_row("claude", "5h", now + 500)]
    assert next_boundary_poll_target_with_kind(now, pre_only, _CFG) == (now + 380, "pre")

    post_only = [_row("claude", "5h", now)]
    assert next_boundary_poll_target_with_kind(now, post_only, _CFG) == (now + 120, "post")

    assert next_boundary_poll_target_with_kind(now, [], _CFG) is None


def test_next_poll_sleep_seconds_uses_regular_interval_when_no_boundary_near(monkeypatch):
    monkeypatch.setenv("TOKDASH_QUOTA_POLL_INTERVAL", "1000")
    monkeypatch.setattr(cli.random, "uniform", lambda a, b: 0)

    assert cli._next_poll_sleep_seconds(10_000, []) == 1000


def test_next_poll_sleep_seconds_delays_imminent_boundary_to_call_floor(monkeypatch):
    monkeypatch.setenv("TOKDASH_QUOTA_POLL_INTERVAL", "1000")
    monkeypatch.setattr(cli.random, "uniform", lambda a, b: 0)
    monkeypatch.setenv("TOKDASH_QUOTA_BOUNDARY_PRE_SECONDS", "50")
    now = 10_000
    rows = [{"provider": "claude", "bucket": "5h", "resets_at": now + 100}]  # pre target: now + 50

    assert cli._next_poll_sleep_seconds(now, rows) == 300


def test_next_poll_sleep_seconds_ignores_within_jitter_boundary(monkeypatch):
    """A boundary inside the jitter horizon is the one we just fired, not a new one, so the
    daemon falls back to the regular interval rather than firing a duplicate poll."""
    monkeypatch.setenv("TOKDASH_QUOTA_POLL_INTERVAL", "1000")
    monkeypatch.setattr(cli.random, "uniform", lambda a, b: 0)
    monkeypatch.setenv("TOKDASH_QUOTA_BOUNDARY_POST_SECONDS", "1")
    now = 10_000
    rows = [{"provider": "claude", "bucket": "5h", "resets_at": now}]  # post target: now + 1

    assert cli._next_poll_sleep_seconds(now, rows) == 1000


def test_next_poll_sleep_seconds_applies_call_floor_beyond_jitter(monkeypatch):
    monkeypatch.setenv("TOKDASH_QUOTA_POLL_INTERVAL", "1000")
    monkeypatch.setattr(cli.random, "uniform", lambda a, b: 0)
    monkeypatch.setenv("TOKDASH_QUOTA_BOUNDARY_POST_SECONDS", "6")
    now = 10_000
    rows = [{"provider": "claude", "bucket": "5h", "resets_at": now}]  # post target: now + 6

    assert cli._next_poll_sleep_seconds(now, rows) == cli._QUOTA_POLL_SLEEP_FLOOR_SECONDS


def test_next_poll_sleep_seconds_ignores_boundary_when_disabled(monkeypatch):
    monkeypatch.setenv("TOKDASH_QUOTA_POLL_INTERVAL", "1000")
    monkeypatch.setattr(cli.random, "uniform", lambda a, b: 0)
    monkeypatch.setenv("TOKDASH_QUOTA_BOUNDARY_POLL", "0")
    now = 10_000
    rows = [{"provider": "claude", "bucket": "5h", "resets_at": now + 1}]

    assert cli._next_poll_sleep_seconds(now, rows) == 1000


def test_boundary_plan_coalesces_providers_due_inside_call_floor():
    now = 10_000
    rows = [
        _row("claude", "5h", now + _CFG.pre_seconds + 50),
        _row("codex", "5h", now + _CFG.pre_seconds + 200),
    ]

    target = plan_boundary_poll(now, rows, _CFG, minimum_delay_seconds=300)

    assert target is not None
    assert target.at == now + 300
    assert target.kinds == frozenset({"pre", "post"})
    assert target.providers == frozenset({"claude", "codex"})


def test_boundary_plan_falls_back_to_regular_interval_on_planning_error(monkeypatch):
    monkeypatch.setenv("TOKDASH_QUOTA_POLL_INTERVAL", "1000")
    monkeypatch.setattr(cli.random, "uniform", lambda a, b: 0)
    monkeypatch.setattr(quota, "plan_boundary_poll", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError()))

    sleep_seconds, target = cli._plan_next_quota_poll(10_000, [])

    assert sleep_seconds == 1000
    assert target is None


def test_reset_advance_anchors_post_to_previous_epoch(monkeypatch):
    monkeypatch.setenv("TOKDASH_QUOTA_BOUNDARY_POST_SECONDS", "120")
    old_reset = 10_000
    before = [{"provider": "claude", "account": "a", "bucket": "5h", "resets_at": old_reset}]
    after = [{"provider": "claude", "account": "a", "bucket": "5h", "resets_at": old_reset + 18_000}]

    assert cli._advanced_reset_post_targets(before, after, old_reset + 30) == [(old_reset + 120, "claude")]


def test_overdue_anchor_remains_scheduled_until_provider_is_covered():
    now = 10_000

    target = plan_boundary_poll(
        now,
        [],
        _CFG,
        minimum_delay_seconds=300,
        anchored_post_targets=[(now - 60, "claude")],
    )

    assert target is not None
    assert target.at == now + 300
    assert target.kinds == frozenset({"post"})
    assert target.providers == frozenset({"claude"})


def test_boundary_network_collection_calls_only_triggering_provider(monkeypatch):
    calls = []
    monkeypatch.setattr(config, "enabled_network_sources", lambda: ["codex_api", "claude_api"])
    monkeypatch.setattr(quota, "collect_codex_api_snapshots", lambda: calls.append("codex") or [])
    monkeypatch.setattr(quota, "collect_claude_api_snapshots", lambda: calls.append("claude") or [])

    quota.collect_network_snapshots(["claude_api"])

    assert calls == ["claude"]


def test_network_collection_dispatches_new_provider_collectors(monkeypatch):
    calls = []
    enabled = ["minimax_api", "kimi_api", "grok_api"]
    monkeypatch.setattr(config, "enabled_network_sources", lambda: enabled)
    monkeypatch.setattr(quota, "collect_minimax_api_snapshots", lambda: calls.append("minimax") or [])
    monkeypatch.setattr(quota, "collect_kimi_api_snapshots", lambda: calls.append("kimi") or [])
    monkeypatch.setattr(quota, "collect_grok_api_snapshots", lambda: calls.append("grok") or [])

    quota.collect_network_snapshots()

    assert calls == ["minimax", "kimi", "grok"]


def test_record_boundary_poll_metric_increments_quota_meta():
    from tokdash.usage_store import UsageEntryStore

    cli._record_boundary_poll_metric("pre")
    cli._record_boundary_poll_metric("pre")
    cli._record_boundary_poll_metric("post")

    store = UsageEntryStore()
    assert store.quota_meta_get("quota_boundary_pre_polls") == "2"
    assert store.quota_meta_get("quota_boundary_post_polls") == "1"


def test_record_boundary_poll_metric_is_best_effort_when_persistence_disabled(monkeypatch):
    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")

    cli._record_boundary_poll_metric("pre")  # must not raise
