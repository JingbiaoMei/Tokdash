from __future__ import annotations

import json
import sqlite3

import pytest

from tokdash.sources.quota.types import QuotaSnapshot
from tokdash.usage_store import (
    _CODEX_PERCENT_SCALE_REPAIR_META_KEY,
    UsageEntryStore,
    _repair_codex_api_percent_scale_rows,
)


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


def _codex_api_raw(primary_used: float, secondary_used: float) -> dict:
    return {
        "usage": {
            "plan_type": "prolite",
            "rate_limit": {
                "primary_window": {
                    "limit_window_seconds": 18000,
                    "reset_after_seconds": 13975,
                    "reset_at": BASE_TS + 18_000,
                    "used_percent": primary_used,
                },
                "secondary_window": {
                    "limit_window_seconds": 604800,
                    "reset_after_seconds": 600775,
                    "reset_at": BASE_TS + 604_800,
                    "used_percent": secondary_used,
                },
            },
        }
    }


_QUOTA_SNAPSHOTS_DDL = """
    CREATE TABLE quota_snapshots (
        id INTEGER PRIMARY KEY,
        provider TEXT NOT NULL,
        account TEXT NOT NULL DEFAULT 'default',
        bucket TEXT NOT NULL,
        bucket_label TEXT,
        used_percent REAL,
        resets_at INTEGER,
        plan TEXT,
        captured_at INTEGER NOT NULL,
        source TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'ok',
        raw_json TEXT,
        UNIQUE(provider, account, bucket, source, captured_at)
    )
"""


def _insert_codex_api_row(conn, bucket: str, used: float, captured_at: int, raw: dict) -> None:
    conn.execute(
        """
        INSERT INTO quota_snapshots(
            provider, account, bucket, bucket_label, used_percent,
            resets_at, plan, captured_at, source, status, raw_json
        ) VALUES ('codex', 'acct', ?, ?, ?, ?, 'prolite', ?, 'codex_api', 'ok', ?)
        """,
        (bucket, bucket, used, captured_at + 18_000, captured_at, json.dumps(raw)),
    )


def _create_legacy_quota_db(db_path, rows) -> None:
    """A pre-upgrade DB: old schema_version plus rows the old fraction-scaling parser wrote."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', '5')")
        conn.execute(_QUOTA_SNAPSHOTS_DDL)
        for bucket, used, captured_at, raw in rows:
            _insert_codex_api_row(conn, bucket, used, captured_at, raw)


def test_usage_store_repairs_legacy_codex_api_one_percent_rows_on_open(tmp_path):
    db_path = tmp_path / "usage.sqlite3"
    _create_legacy_quota_db(db_path, [("5h", 100.0, BASE_TS, _codex_api_raw(1, 0))])

    store = UsageEntryStore(db_path)

    row = store.latest_quota_snapshots()[0]
    assert row["used_percent"] == 1.0
    assert store.quota_history(granularity="hour")["series"][0]["points"] == [
        {"captured_at": BASE_TS, "used_percent": 1.0}
    ]


def test_usage_store_repair_generalizes_beyond_the_one_percent_case(tmp_path):
    db_path = tmp_path / "usage.sqlite3"
    _create_legacy_quota_db(
        db_path,
        [
            ("5h", 50.0, BASE_TS, _codex_api_raw(0.5, 40)),  # scaled fraction: really 0.5%
            ("7d", 40.0, BASE_TS, _codex_api_raw(0.5, 40)),  # genuine 40%
            ("5h", 100.0, BASE_TS + 3600, _codex_api_raw(100, 40)),  # genuine 100%
        ],
    )

    store = UsageEntryStore(db_path)
    store.status()  # opening the store runs the repair sweep

    with sqlite3.connect(db_path) as conn:
        values = {
            (bucket, captured_at): used
            for bucket, captured_at, used in conn.execute(
                "SELECT bucket, captured_at, used_percent FROM quota_snapshots"
            )
        }
    assert values == {
        ("5h", BASE_TS): 0.5,
        ("7d", BASE_TS): 40.0,
        ("5h", BASE_TS + 3600): 100.0,
    }


def test_usage_store_repair_leaves_unprovable_flat_shape_7d_rows_alone(tmp_path):
    # Flat legacy shape: "rate_limit" IS the 5h window, so a 7d row's original value
    # cannot be re-derived from this payload and must not be guessed at.
    flat_raw = {"usage": {"rate_limit": {"used_percent": 1, "resets_at": BASE_TS + 18_000}}}
    db_path = tmp_path / "usage.sqlite3"
    _create_legacy_quota_db(
        db_path,
        [("5h", 100.0, BASE_TS, flat_raw), ("7d", 100.0, BASE_TS, flat_raw)],
    )

    store = UsageEntryStore(db_path)
    store.status()

    with sqlite3.connect(db_path) as conn:
        values = dict(conn.execute("SELECT bucket, used_percent FROM quota_snapshots"))
    assert values == {"5h": 1.0, "7d": 100.0}


def test_usage_store_repair_watches_for_stale_writers_then_stops(tmp_path):
    db_path = tmp_path / "usage.sqlite3"
    store = UsageEntryStore(db_path)
    store.status()  # schema init; the empty scan proves nothing, watcher stays armed

    def _state(conn) -> str:
        return conn.execute(
            "SELECT value FROM meta WHERE key = ?", (_CODEX_PERCENT_SCALE_REPAIR_META_KEY,)
        ).fetchone()[0]

    def _used_at(conn, captured_at: int) -> float:
        return conn.execute(
            "SELECT used_percent FROM quota_snapshots WHERE captured_at = ?", (captured_at,)
        ).fetchone()[0]

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # A stale process still running the old parser writes another scaled row.
        _insert_codex_api_row(conn, "5h", 100.0, BASE_TS, _codex_api_raw(1, 0))
        assert _repair_codex_api_percent_scale_rows(conn) == 1
        assert _used_at(conn, BASE_TS) == 1.0

        # A fixed-parser row scans clean -> the watcher declares itself done.
        _insert_codex_api_row(conn, "5h", 6.0, BASE_TS + 3600, _codex_api_raw(6, 0))
        assert _repair_codex_api_percent_scale_rows(conn) == 0
        assert _state(conn) == "done"

        # After "done" the scan is permanently off, even for a would-match row.
        _insert_codex_api_row(conn, "5h", 100.0, BASE_TS + 7200, _codex_api_raw(1, 0))
        assert _repair_codex_api_percent_scale_rows(conn) == 0
        assert _used_at(conn, BASE_TS + 7200) == 100.0


def _snap_reset(bucket: str, used: float, captured_at: int, resets_at: int) -> QuotaSnapshot:
    # Like _snapshot but with an explicit resets_at, so a window's identity (its reset time)
    # is independent of when it was sampled — which is what consumption keys on.
    return QuotaSnapshot(
        "codex", "acct", bucket, bucket, used, resets_at, "pro", captured_at, "codex_session", "ok", {"used": used}
    )


def _provider_snap_reset(provider: str, bucket: str, used: float, captured_at: int, resets_at: int | None) -> QuotaSnapshot:
    return QuotaSnapshot(
        provider,
        "acct",
        bucket,
        bucket,
        used,
        resets_at,
        "pro",
        captured_at,
        f"{provider}_api",
        "ok",
        {"used": used},
    )


def test_quota_history_derives_consumption_and_reset_deltas(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    r1, r2 = BASE_TS + 18_000, BASE_TS + 36_000  # two window epochs (distinct reset times)
    store.insert_quota_snapshots(
        [
            _snap_reset("5h", 10.0, BASE_TS, r1),            # epoch A baseline
            _snap_reset("5h", 22.5, BASE_TS + 3600, r1),     # +12.5
            _snap_reset("5h", 20.0, BASE_TS + 5400, r1),     # dip within epoch A -> ignored
            _snap_reset("5h", 5.0, BASE_TS + 7200, r2),      # reset: epoch B baseline (drop not counted)
            _snap_reset("5h", 18.0, BASE_TS + 10800, r2),    # +13.0 post-reset
        ]
    )

    history = store.quota_history(granularity="hour")

    assert history["granularity"] == "hour"
    # Points keep every raw reading (line chart), including the dip.
    assert [p["used_percent"] for p in history["series"][0]["points"]] == [10.0, 22.5, 20.0, 5.0, 18.0]
    # Consumption counts only increases above each epoch's running high: the dip and the
    # reset drop contribute nothing; the post-reset climb counts fresh.
    assert history["series"][0]["consumption"] == [
        {"period_start": BASE_TS + 3600, "consumed_percent": 12.5},
        {"period_start": BASE_TS + 10800, "consumed_percent": 13.0},
    ]


@pytest.mark.parametrize(
    ("provider", "bucket"),
    [
        ("codex", "5h"),
        ("codex", "7d"),
        ("codex", "codex_bengalfox_5h"),
        ("codex", "codex_bengalfox_7d"),
        ("claude", "session"),
        ("claude", "weekly_all"),
        ("claude", "weekly_scoped_fable"),
        ("antigravity", "gemini-3-flash"),
        ("antigravity", "claude-sonnet-4-6"),
    ],
)
def test_quota_history_counts_usage_across_reset_timestamp_change_for_subscription_buckets(
    tmp_path, provider, bucket
):
    # UI remaining 40->10, reset 10->100, then 100->90 maps to stored used_percent
    # 60->90, reset 90->0, then 0->10. Total daily consumption is 30+10 = 40.
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    r1, r2 = BASE_TS + 18_000, BASE_TS + 36_000
    store.insert_quota_snapshots(
        [
            _provider_snap_reset(provider, bucket, 60.0, BASE_TS, r1),
            _provider_snap_reset(provider, bucket, 90.0, BASE_TS + 1800, r1),
            _provider_snap_reset(provider, bucket, 0.0, BASE_TS + 3600, r2),
            _provider_snap_reset(provider, bucket, 10.0, BASE_TS + 5400, r2),
        ]
    )

    history = store.quota_history(granularity="day")

    assert history["series"][0]["consumption"] == [
        {"period_start": BASE_TS - (BASE_TS % 86400), "consumed_percent": 40.0},
    ]


def test_quota_history_treats_same_reset_drop_as_transient_for_claude_weekly(tmp_path):
    # Claude weekly rows are treated as fixed-window limits: with a stable reset timestamp,
    # a dip below the prior high is noise/aging inside the same epoch, not a reset signal.
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    resets_at = BASE_TS + 7 * 86400
    store.insert_quota_snapshots(
        [
            _provider_snap_reset("claude", "weekly_all", 80.0, BASE_TS, resets_at),
            _provider_snap_reset("claude", "weekly_all", 70.0, BASE_TS + 1800, resets_at),
            _provider_snap_reset("claude", "weekly_all", 75.0, BASE_TS + 3600, resets_at),
        ]
    )

    history = store.quota_history(granularity="day")

    assert history["series"][0]["consumption"] == []


def test_quota_history_counts_post_reset_usage_when_reset_time_is_missing(tmp_path):
    # Some live subscription rows, especially a few Antigravity pools and older Claude
    # session rows, have no reset timestamp. A clear drop to zero still represents a reset,
    # and the post-reset climb should count as new usage.
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    store.insert_quota_snapshots(
        [
            _provider_snap_reset("antigravity", "chat_20706", 60.0, BASE_TS, None),
            _provider_snap_reset("antigravity", "chat_20706", 90.0, BASE_TS + 1800, None),
            _provider_snap_reset("antigravity", "chat_20706", 0.0, BASE_TS + 3600, None),
            _provider_snap_reset("antigravity", "chat_20706", 10.0, BASE_TS + 5400, None),
        ]
    )

    history = store.quota_history(granularity="day")

    assert history["series"][0]["consumption"] == [
        {"period_start": BASE_TS - (BASE_TS % 86400), "consumed_percent": 40.0},
    ]


def test_quota_history_interleaved_windows_do_not_inflate_consumption(tmp_path):
    # Regression: two distinct windows (different reset times) merged into one bucket — e.g.
    # two Codex accounts' 7-day windows — must NOT read as reset+refill on every switch. The
    # old delta model turned this into ~92%; keyed on resets_at it is the real 10%.
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    rx, ry = BASE_TS + 600_000, BASE_TS + 800_000
    store.insert_quota_snapshots(
        [
            _snap_reset("7d", 40.0, BASE_TS + 0, rx),
            _snap_reset("7d", 6.0, BASE_TS + 600, ry),
            _snap_reset("7d", 44.0, BASE_TS + 1200, rx),
            _snap_reset("7d", 8.0, BASE_TS + 1800, ry),
            _snap_reset("7d", 48.0, BASE_TS + 2400, rx),
        ]
    )

    history = store.quota_history(granularity="hour")

    # window X climbs 40->48 (+8), window Y 6->8 (+2); total 10, all within the first hour.
    assert history["series"][0]["consumption"] == [
        {"period_start": BASE_TS, "consumed_percent": 10.0},
    ]


def test_quota_history_absorbs_reset_time_jitter(tmp_path):
    # Regression: providers report the SAME physical window's reset time with ±1s jitter
    # poll-to-poll (e.g. Claude alternates 13:39:59 / 13:40:00). Keyed on the exact reset
    # time that splits one window into two epochs and counts the climb in both (~2x). Reset
    # times within RESET_JITTER_SECONDS must chain into one window: the climb counts once.
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    r, rj = BASE_TS + 18_000, BASE_TS + 18_001  # same window, 1s jitter
    store.insert_quota_snapshots(
        [
            _snap_reset("session", 10.0, BASE_TS + 0, r),
            _snap_reset("session", 20.0, BASE_TS + 600, rj),
            _snap_reset("session", 30.0, BASE_TS + 1200, r),
            _snap_reset("session", 40.0, BASE_TS + 1800, rj),
        ]
    )

    history = store.quota_history(granularity="hour")

    # One window climbing 10->40 = 30 consumed (NOT 40, which exact-reset keying would give).
    assert history["series"][0]["consumption"] == [
        {"period_start": BASE_TS, "consumed_percent": 30.0},
    ]


def test_quota_history_codex_weekly_counts_climbs_after_rolling_drop(tmp_path):
    # Codex's 7-day window is rolling: older usage can age out before the reset timestamp
    # changes. A later climb below the earlier high is still new consumption and must show
    # up in the daily chart.
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    resets_at = BASE_TS + 7 * 86400
    day_2 = BASE_TS + 86400
    store.insert_quota_snapshots(
        [
            _snap_reset("7d", 80.0, BASE_TS, resets_at),
            _snap_reset("7d", 70.0, day_2, resets_at),
            _snap_reset("7d", 75.0, day_2 + 3600, resets_at),
        ]
    )

    history = store.quota_history(granularity="day")

    assert history["series"][0]["consumption"] == [
        {"period_start": day_2 - (day_2 % 86400), "consumed_percent": 5.0},
    ]


def test_quota_history_codex_weekly_ignores_recovery_to_prior_high_after_transient_drop(tmp_path):
    # Adjacent-delta windows must not turn a transient low reading followed by recovery
    # to the previous high into fake consumption.
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    resets_at = BASE_TS + 7 * 86400
    day_2 = BASE_TS + 86400
    store.insert_quota_snapshots(
        [
            _snap_reset("7d", 80.0, BASE_TS, resets_at),
            _snap_reset("7d", 70.0, day_2, resets_at),
            _snap_reset("7d", 80.0, day_2 + 3600, resets_at),
        ]
    )

    history = store.quota_history(granularity="day")

    assert history["series"][0]["consumption"] == []


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


# --- Torn-read guard regression tests -----------------------------------------------------
# At a window rollover the API can return the OLD window's near-max used_percent stamped
# with the NEW window's resets_at for one sample, then revert. Both shapes are physically
# impossible within one window (used_percent only rises until reset), so the guard drops
# them before EITHER points or consumption are derived. See `_drop_torn_reads` and
# QUOTA_TORN_READ_MIN_PERCENT in usage_store.py.


def test_quota_history_drops_interior_torn_read_for_weekly_over_count(tmp_path):
    # Interior lone peak (7d over-count shape): within one epoch, 0 -> 100 -> 2 -> 3. The
    # isolated 100 is a torn read; consumption must count only the real climb (0->3 = 3),
    # not the +100 spike.
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    resets_at = BASE_TS + 7 * 86400
    store.insert_quota_snapshots(
        [
            _snap_reset("7d", 0.0, BASE_TS, resets_at),
            _snap_reset("7d", 100.0, BASE_TS + 900, resets_at),
            _snap_reset("7d", 2.0, BASE_TS + 1800, resets_at),
            _snap_reset("7d", 3.0, BASE_TS + 2700, resets_at),
        ]
    )

    history = store.quota_history(granularity="hour")

    series = history["series"][0]
    assert [p["used_percent"] for p in series["points"]] == [0.0, 2.0, 3.0]
    assert sum(c["consumed_percent"] for c in series["consumption"]) == 3.0


def test_quota_history_drops_leading_torn_read_for_five_hour_under_count(tmp_path):
    # Leading carry-over (5h under-count shape): the reset-boundary torn read lands as the
    # FIRST reading of the new epoch (100), then reverts (2 -> 40). Left in place it becomes
    # the running-high baseline and masks the whole window (0 counted instead of 38).
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    resets_at = BASE_TS + 3600
    store.insert_quota_snapshots(
        [
            _snap_reset("5h", 100.0, BASE_TS, resets_at),
            _snap_reset("5h", 2.0, BASE_TS + 900, resets_at),
            _snap_reset("5h", 40.0, BASE_TS + 1800, resets_at),
        ]
    )

    history = store.quota_history(granularity="hour")

    series = history["series"][0]
    assert [p["used_percent"] for p in series["points"]] == [2.0, 40.0]
    assert sum(c["consumed_percent"] for c in series["consumption"]) == 38.0


def test_quota_history_torn_read_guard_leaves_real_fast_fill_alone(tmp_path):
    # The guard only fires on a same-epoch REVERSION (a drop back down within one window).
    # A real fast fill that climbs and then plateaus at the top (0 -> 100 -> 100) must
    # survive untouched: nothing dropped, full consumption counted.
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    resets_at = BASE_TS + 3600
    store.insert_quota_snapshots(
        [
            _snap_reset("5h", 0.0, BASE_TS, resets_at),
            _snap_reset("5h", 100.0, BASE_TS + 900, resets_at),
            _snap_reset("5h", 100.0, BASE_TS + 1800, resets_at),
        ]
    )

    history = store.quota_history(granularity="hour")

    series = history["series"][0]
    assert [p["used_percent"] for p in series["points"]] == [0.0, 100.0, 100.0]
    assert sum(c["consumed_percent"] for c in series["consumption"]) == 100.0


def test_quota_history_torn_read_guard_ignores_genuine_reset_across_epochs(tmp_path):
    # A genuine reset (100 at the end of epoch A, 0 as the start of epoch B) must be left
    # alone: the drop crosses an epoch boundary, so the guard can't confirm a same-window
    # reversion and conservatively does not fire.
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    r1, r2 = BASE_TS + 3600, BASE_TS + 7200
    store.insert_quota_snapshots(
        [
            _snap_reset("5h", 95.0, BASE_TS, r1),
            _snap_reset("5h", 100.0, BASE_TS + 900, r1),  # epoch A ends near-full
            _snap_reset("5h", 0.0, BASE_TS + 1800, r2),   # genuine reset: epoch B baseline
            _snap_reset("5h", 10.0, BASE_TS + 2700, r2),
        ]
    )

    history = store.quota_history(granularity="hour")

    series = history["series"][0]
    assert [p["used_percent"] for p in series["points"]] == [95.0, 100.0, 0.0, 10.0]
    assert sum(c["consumed_percent"] for c in series["consumption"]) == 15.0  # +5 (A) + 10 (B)


def test_quota_history_torn_read_guard_threshold_boundary(tmp_path):
    # QUOTA_TORN_READ_MIN_PERCENT (40.0) is inclusive: a same-epoch reversion of exactly 39
    # points is real noise and is kept; a reversion of 41 points is dropped.
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    resets_at = BASE_TS + 3600
    store.insert_quota_snapshots(
        [
            # Kept: reversion of 39 (< 40) is not a torn read.
            _snap_reset("5h", 50.0, BASE_TS, resets_at),
            _snap_reset("5h", 11.0, BASE_TS + 900, resets_at),
            # Dropped: isolated (leading) reversion of 41 (>= 40).
            _snap_reset("codex_boundary_5h", 50.0, BASE_TS, resets_at),
            _snap_reset("codex_boundary_5h", 9.0, BASE_TS + 900, resets_at),
        ]
    )

    history = store.quota_history(granularity="hour")
    by_bucket = {s["bucket"]: s for s in history["series"]}

    assert [p["used_percent"] for p in by_bucket["5h"]["points"]] == [50.0, 11.0]
    assert [p["used_percent"] for p in by_bucket["codex_boundary_5h"]["points"]] == [9.0]


def test_quota_history_network_only_providers_excludes_codex_session_rows(tmp_path):
    # Source authority: when the caller opts codex into API-only mode (Codex API polling
    # enabled), stale codex_session rows are excluded entirely and only codex_api rows drive
    # the series; it is not marked estimated.
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    resets_at = BASE_TS + 3600
    store.insert_quota_snapshots(
        [
            _snap_reset("5h", 90.0, BASE_TS, resets_at),  # stale codex_session snapshot
            _provider_snap_reset("codex", "5h", 10.0, BASE_TS + 60, resets_at),  # live codex_api
        ]
    )

    history = store.quota_history(granularity="hour", network_only_providers={"codex"})

    series = history["series"][0]
    assert [p["used_percent"] for p in series["points"]] == [10.0]
    assert series["estimated"] is False
    assert history["any_estimated"] is False


def test_quota_history_marks_codex_session_only_series_estimated_by_default(tmp_path):
    # Without network_only_providers (Codex API polling off / not opted in), a codex series
    # built from session rows is a local-log estimate and must be labelled as such.
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    store.insert_quota_snapshots([_snap_reset("5h", 10.0, BASE_TS, BASE_TS + 3600)])

    history = store.quota_history(granularity="hour")

    series = history["series"][0]
    assert series["estimated"] is True
    assert history["any_estimated"] is True


def test_quota_history_counts_post_reset_refill_as_new_consumption(tmp_path):
    # Regression: post-reset refills stay COUNTED (current, intended behaviour) -- the
    # torn-read guard must not suppress a genuine reset+refill. Two sequential windows, each
    # climbing, sum their climbs.
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    r1, r2 = BASE_TS + 3600, BASE_TS + 7200
    store.insert_quota_snapshots(
        [
            _snap_reset("5h", 0.0, BASE_TS, r1),
            _snap_reset("5h", 20.0, BASE_TS + 900, r1),   # window A climbs +20
            _snap_reset("5h", 0.0, BASE_TS + 1800, r2),   # genuine reset: window B baseline
            _snap_reset("5h", 15.0, BASE_TS + 2700, r2),  # window B climbs +15
        ]
    )

    history = store.quota_history(granularity="hour")

    series = history["series"][0]
    assert sum(c["consumed_percent"] for c in series["consumption"]) == 35.0


def test_quota_history_codex_idle_to_active_5h_has_no_phantom_delta(tmp_path):
    # Regression: `_bucket_snapshot` (codex.py) nulls `resets_at` for an idle Codex window
    # (used_percent == 0) because Codex's rolling-window API stamps a phantom
    # resets_at ~= captured_at + window_length even before the window has actually started.
    # A `resets_at is None` row lands in the adjacent-delta "None" epoch
    # (`_quota_history_uses_adjacent_deltas` returns True when resets_at is None). The
    # concern raised in review: once the window goes active, does its first real reading get
    # chained onto that idle 0% row as a phantom 0->30 delta? It must not -- the active
    # reading carries its own genuine (non-None) resets_at, so it lives in its OWN real epoch,
    # keyed separately from the None epoch, and is treated as that epoch's first
    # sighting/baseline (fixed-window running-high path), never differenced against the idle
    # row.
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    active_resets_at = BASE_TS + 3600 + 18_000  # a real 5h window, first seen on first use
    store.insert_quota_snapshots(
        [
            _snap_reset("5h", 0.0, BASE_TS, None),  # idle: phantom resets_at nulled by the fix
            _snap_reset("5h", 30.0, BASE_TS + 3600, active_resets_at),  # first use: real window
        ]
    )

    history = store.quota_history(granularity="hour")

    series = history["series"][0]
    # Both raw readings still appear on the line chart.
    assert [p["used_percent"] for p in series["points"]] == [0.0, 30.0]
    # No consumption at all is derived from this pair -- NOT the phantom 30.0 a naive
    # 0->30 adjacent delta would produce. The idle row (None epoch) only records itself as
    # that epoch's baseline (nothing to delta against yet). The active row is the FIRST
    # sighting of its own real epoch (active_resets_at), so the fixed-window path also just
    # records it as that epoch's baseline -- the two rows never share an epoch, so they are
    # never differenced against each other.
    assert series["consumption"] == []
