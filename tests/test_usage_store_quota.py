from __future__ import annotations

import sqlite3

import pytest

from tokdash.sources.quota.types import QuotaSnapshot
from tokdash.usage_store import UsageEntryStore


BASE_TS = 1_782_907_200


def _snapshot(bucket: str, used: float, captured_at: int) -> QuotaSnapshot:
    return QuotaSnapshot(
        provider="codex",
        account="acct",
        bucket=bucket,
        bucket_label=bucket,
        used_percent=used,
        resets_at=captured_at + 3600,
        plan="pro",
        captured_at=captured_at,
        source="codex_session",
        status="ok",
        raw={"used": used},
    )


def test_quota_snapshots_are_idempotent_and_reported_in_status(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    rows = [_snapshot("5h", 10.0, BASE_TS), _snapshot("7d", 25.0, BASE_TS)]

    assert store.insert_quota_snapshots(rows) == 2
    assert store.insert_quota_snapshots(rows) == 0

    latest = store.latest_quota_snapshots()
    assert len(latest) == 2
    assert latest[0]["provider"] == "codex"
    assert latest[0]["raw"]["used"] in {10.0, 25.0}
    assert store.status()["quota_snapshots"] == 2


def test_quota_history_derives_consumption_and_reset_deltas(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    store.insert_quota_snapshots(
        [
            _snapshot("5h", 10.0, BASE_TS),
            _snapshot("5h", 22.5, BASE_TS + 3600),
            _snapshot("5h", 5.0, BASE_TS + 7200),
        ]
    )

    history = store.quota_history(granularity="hour")

    assert history["granularity"] == "hour"
    assert history["series"][0]["points"] == [
        {"captured_at": BASE_TS, "used_percent": 10.0},
        {"captured_at": BASE_TS + 3600, "used_percent": 22.5},
        {"captured_at": BASE_TS + 7200, "used_percent": 5.0},
    ]
    assert history["series"][0]["consumption"] == [
        {"period_start": BASE_TS + 3600, "consumed_percent": 12.5},
        {"period_start": BASE_TS + 7200, "consumed_percent": 5.0},
    ]


def test_quota_history_skips_status_and_reset_credit_rows(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    store.insert_quota_snapshots(
        [
            _snapshot("5h", 10.0, BASE_TS),
            QuotaSnapshot("codex", "acct", "reset_credits", "Reset credits", 3, None, "pro", BASE_TS, "codex_api", "ok", {}),
            QuotaSnapshot("codex", "acct", "api", "Codex API", None, None, None, BASE_TS, "codex_api", "stale_token", {}),
        ]
    )

    history = store.quota_history(granularity="hour")

    assert [(item["provider"], item["bucket"]) for item in history["series"]] == [("codex", "5h")]


def test_quota_retention_prunes_old_snapshots(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKDASH_QUOTA_RETENTION_DAYS", "365")
    store = UsageEntryStore(tmp_path / "usage.sqlite3")

    store.insert_quota_snapshots(
        [
            _snapshot("5h", 10.0, 1_600_000_000),
            _snapshot("5h", 20.0, BASE_TS),
        ]
    )

    rows = store.query_quota_snapshots()
    assert [row["captured_at"] for row in rows] == [BASE_TS]


def test_quota_retention_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("TOKDASH_QUOTA_RETENTION_DAYS", raising=False)
    store = UsageEntryStore(tmp_path / "usage.sqlite3")

    store.insert_quota_snapshots(
        [
            _snapshot("5h", 10.0, 1_600_000_000),
            _snapshot("5h", 20.0, BASE_TS),
        ]
    )

    rows = store.query_quota_snapshots()
    assert [row["captured_at"] for row in rows] == [1_600_000_000, BASE_TS]


def test_quota_schema_migrates_v4_database(tmp_path):
    db_path = tmp_path / "usage.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', '4')")

    status = UsageEntryStore(db_path).status()

    assert status["meta"]["schema_version"] == "5"
    assert status["quota_snapshots"] == 0
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='quota_snapshots'").fetchone()


def test_quota_history_merges_accounts_into_one_series_per_bucket(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    store.insert_quota_snapshots(
        [
            # Session row (account "default") and API row (real account) for the SAME bucket.
            QuotaSnapshot("codex", "default", "5h", "5-hour window", 10.0, None, "pro", BASE_TS, "codex_session", "ok", {}),
            QuotaSnapshot("codex", "acct_real", "5h", "5-hour window", 22.5, None, "pro", BASE_TS + 3600, "codex_api", "ok", {}),
        ]
    )

    history = store.quota_history(granularity="hour")

    # A single unified series, not one per account.
    assert [(s["provider"], s["bucket"]) for s in history["series"]] == [("codex", "5h")]
    assert history["series"][0]["points"] == [
        {"captured_at": BASE_TS, "used_percent": 10.0},
        {"captured_at": BASE_TS + 3600, "used_percent": 22.5},
    ]
    assert history["series"][0]["consumption"] == [
        {"period_start": BASE_TS + 3600, "consumed_percent": 12.5},
    ]


def test_quota_history_prefers_freshest_point_on_timestamp_collision(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    store.insert_quota_snapshots(
        [QuotaSnapshot("codex", "default", "5h", "5-hour window", 10.0, None, "pro", BASE_TS, "codex_session", "ok", {})]
    )
    # Same (provider, bucket, captured_at) from a different account/source -> later insert.
    store.insert_quota_snapshots(
        [QuotaSnapshot("codex", "acct_real", "5h", "5-hour window", 42.0, None, "pro", BASE_TS, "codex_api", "ok", {})]
    )

    history = store.quota_history(granularity="hour")

    points = history["series"][0]["points"]
    assert len(points) == 1
    assert points[0]["used_percent"] == 42.0  # freshest (highest id) wins the collision


def test_quota_history_downsamples_points_by_default_and_keeps_last_point(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    n = 500
    store.insert_quota_snapshots(
        [_snapshot("5h", float(i % 100), BASE_TS + i * 60) for i in range(n)]
    )
    last_captured_at = BASE_TS + (n - 1) * 60
    last_used_percent = float((n - 1) % 100)

    history = store.quota_history(granularity="hour")

    points = history["series"][0]["points"]
    assert len(points) <= 300
    assert points[-1] == {"captured_at": last_captured_at, "used_percent": last_used_percent}

    history_bounded = store.quota_history(granularity="hour", max_points=10)
    points_bounded = history_bounded["series"][0]["points"]
    assert len(points_bounded) <= 10
    assert points_bounded[-1] == {"captured_at": last_captured_at, "used_percent": last_used_percent}


def test_quota_history_max_points_zero_raises(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    store.insert_quota_snapshots([_snapshot("5h", 10.0, BASE_TS)])

    with pytest.raises(ValueError):
        store.quota_history(granularity="hour", max_points=0)
