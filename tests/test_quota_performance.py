from __future__ import annotations

import time

import tokdash.api as api
from tokdash.sources.quota import quota_state
from tokdash.sources.quota.types import QuotaSnapshot
from tokdash.usage_store import UsageEntryStore

N_ROWS = 100_000


def _bulk_snapshots(n: int):
    providers = ("codex", "claude", "antigravity")
    buckets = ("5h", "7d")
    base_ts = int(time.time()) - 60 * n
    for i in range(n):
        provider = providers[i % len(providers)]
        bucket = buckets[i % len(buckets)]
        yield QuotaSnapshot(
            provider=provider,
            account="default",
            bucket=bucket,
            bucket_label=bucket,
            used_percent=float(i % 100),
            resets_at=None,
            plan="pro",
            captured_at=base_ts + i * 60,
            source=f"{provider}_session",
            status="ok",
            raw={},
        )


def test_quota_history_route_bounded_and_fast_on_100k_rows():
    store = UsageEntryStore()
    inserted = store.insert_quota_snapshots(list(_bulk_snapshots(N_ROWS)))
    assert inserted == N_ROWS

    start = time.perf_counter()
    history = api.get_quota_history(granularity="day")
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"/api/quota/history took {elapsed:.3f}s on {N_ROWS} rows"
    assert history["series"]
    for series in history["series"]:
        assert len(series["points"]) <= 300  # default max_points

    start2 = time.perf_counter()
    history2 = api.get_quota_history(granularity="day", max_points=50)
    elapsed2 = time.perf_counter() - start2
    assert elapsed2 < 1.0
    for series in history2["series"]:
        assert len(series["points"]) <= 50


def test_quota_state_is_fast_on_100k_rows():
    store = UsageEntryStore()
    store.insert_quota_snapshots(list(_bulk_snapshots(N_ROWS)))

    start = time.perf_counter()
    quota_state(store)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.1, f"quota_state() took {elapsed:.3f}s on {N_ROWS} rows"
