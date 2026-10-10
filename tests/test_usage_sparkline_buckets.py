"""Buckets must use headline counting/pricing and never add a source scan."""
from datetime import datetime, timedelta, timezone
import os
import time

import pytest

from tokdash.compute import parse_entries_json
from tokdash.dateutil import parse_date_range
from tokdash.usage_buckets import bucket_granularity, bucket_key, bucket_key_resolver, interval_buckets, local_hour_keys
from tokdash.usage_store import UsageEntryStore, build_source_signature
from tokdash.sources.openclaw import _openclaw_usage_from_store


def entries():
    start, _ = parse_date_range("2026-09-21", "2026-09-21")
    return [
        {"source": "codex", "provider": "openai", "model": "gpt-5.3-codex",
         "timestamp": int((start + timedelta(hours=hour)).timestamp() * 1000),
         "input": 20, "output": 10, "cacheRead": 80, "cacheWrite": 5,
         "reasoning": 7, "cost": cost, "costAuthoritative": authoritative, "messageCount": count,
         **({"_billing": {"kind": "fixed", "cost": 0.0}} if authoritative else {})}
        for hour, cost, authoritative, count in ((1, 0.2, False, 4), (2, 0, True, 7), (3, 0, False, 9))
    ]


@pytest.mark.parametrize("granularity", ["hour", "day", "month"])
def test_store_buckets_match_live_headlines_with_one_sql_scan(tmp_path, granularity):
    raw = entries()
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    store.sync_source("codex", build_source_signature(files=[["fixture", 1, 1]], parser={"v": 1}), lambda: raw)
    statements = []
    read = store._read_priced

    def traced(fn):
        def run(conn):
            conn.set_trace_callback(statements.append)
            return fn(conn)
        return read(run)

    store._read_priced = traced
    actual = store.aggregate_entries(sources=["codex"], bucket_granularity=granularity)
    live = parse_entries_json({"entries": raw}, granularity=granularity)
    assert actual["total_tokens"] == live["total_tokens"] == 366
    assert actual["total_messages"] == live["total_messages"] == 20
    assert actual["total_cost"] == pytest.approx(live["total_cost"])
    assert actual["all_models"][0]["tokens"] == 366
    assert len(actual["all_models"]) == 1
    assert sum(row["messages"] for row in actual["sparkline"]["buckets"]) == 20
    assert sum(row["cost"] for row in actual["sparkline"]["buckets"]) == pytest.approx(actual["total_cost"])
    assert actual["sparkline"] == live["sparkline"]
    queries = [sql for sql in statements if "FROM usage_entries" in sql and sql.lstrip().upper().startswith("SELECT")]
    assert len(queries) == 1


def test_bucket_granularity_is_bounded_and_uses_calendar_days():
    for end, expected in (("2026-09-01", "hour"), ("2026-09-07", "day"),
                          ("2026-10-01", "day"), ("2026-10-02", "month"),
                          ("2027-09-01", "month"), ("2027-09-02", None)):
        since, until = parse_date_range("2026-09-01", end)
        assert bucket_granularity(since, until) == expected
    assert bucket_granularity(None, None) is None


def test_wide_pre_epoch_windows_never_require_windows_local_time_conversion():
    class WindowsDateTime(datetime):
        def astimezone(self, tz=None):
            if self.year < 1970:
                raise OSError("Windows cannot convert a pre-epoch local time")
            return super().astimezone(tz)

    assert bucket_granularity(WindowsDateTime(1926, 1, 1, tzinfo=timezone.utc),
                              WindowsDateTime(2026, 1, 1, tzinfo=timezone.utc)) is None
    assert bucket_granularity(WindowsDateTime(1926, 1, 1, tzinfo=timezone.utc),
                              WindowsDateTime(1926, 1, 2, tzinfo=timezone.utc)) is None


def test_tokscale_backend_reuses_loaded_entries_for_hourly_buckets(monkeypatch):
    from tokdash import compute

    since, until = parse_date_range("2026-09-21", "2026-09-21")
    calls = []
    monkeypatch.setattr(compute, "USE_LOCAL_CODING_TOOLS_BACKEND", False)
    monkeypatch.setattr(compute, "run_tokscale_json", lambda args: calls.append(args) or {"entries": entries()})
    data = compute.get_tools_data_for_range(since, until)
    assert data["sparkline"]["granularity"] == "hour"
    assert sum(row["messages"] for row in data["sparkline"]["buckets"]) == data["total_messages"] == 20
    assert calls == [["--since", "2026-09-21", "--until", "2026-09-21"]]
    monkeypatch.setattr(compute, "period_to_range_args", lambda period: calls[0])
    assert compute.get_tools_data("today")["sparkline"] == data["sparkline"]
    assert len(calls) == 2


@pytest.mark.parametrize("raw", [[], entries()[:2]])
def test_empty_and_recorded_cost_usage_never_loads_pricing(monkeypatch, raw):
    from tokdash import compute

    def unexpected_pricing_load():
        raise AssertionError("recorded costs need no pricing file read")

    monkeypatch.setattr(compute, "PricingDatabase", unexpected_pricing_load)
    data = compute.parse_entries_json({"entries": raw}, granularity="hour")
    assert data["total_messages"] == (11 if raw else 0)
    assert data["total_cost"] == (.2 if raw else 0)
    assert sum(row["messages"] for row in data["sparkline"]["buckets"]) == data["total_messages"]


def test_native_bucket_optimization_preserves_model_order_and_independent_payloads():
    raw = [{**entries()[0], "source": source, "model": model}
           for source, model in (("codex", "model-z"), ("claude", "model-a"), ("codex", "model-a"))]
    control = parse_entries_json({"entries": raw})
    actual = parse_entries_json({"entries": raw}, granularity="month")
    assert {key: value for key, value in actual.items() if key != "sparkline"} == control
    assert [(row["source"], row["name"]) for row in actual["all_models"]] == [
        ("claude", "openai/model-a"), ("codex", "openai/model-a"), ("codex", "openai/model-z")]
    actual["apps"]["claude"]["models"][0]["tokens"] = 999
    assert actual["all_models"][0]["tokens"] == 122


@pytest.mark.parametrize("amount,expected", [(436.905, 436.90), (2188.175, 2188.18), (2.675, 2.68)])
def test_usage_cent_rounding_is_independent_of_binary_partition_noise(amount, expected):
    from tokdash.compute import _round_usage_cost
    for noise in (-1e-10, 0, 1e-10):
        assert _round_usage_cost(amount + noise) == expected


def test_openclaw_buckets_preserve_models_costs_and_message_counts(tmp_path):
    raw = [{**row, "source": "openclaw", "reasoning": 0, "cost": 0.01} for row in entries()]
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    store.sync_source("openclaw", build_source_signature(files=[["openclaw", 1, 1]], parser={"v": 1}), lambda: raw)
    since, until = parse_date_range("2026-09-21", "2026-09-21")
    data = _openclaw_usage_from_store(store, since, until)
    assert data["sparkline"]["granularity"] == "hour"
    assert sum(row["tokens"] for row in data["sparkline"]["buckets"]) == data["total_tokens"]
    assert sum(row["messages"] for row in data["sparkline"]["buckets"]) == data["total_messages"] == 20
    assert sum(row["cost"] for row in data["sparkline"]["buckets"]) == pytest.approx(data["total_cost"])
    assert data["models"]["gpt-5.3-codex"]["tokens"] == data["total_tokens"]


def test_agent_intervals_split_hours_and_keep_concurrent_agents_additive():
    since, _ = parse_date_range("2026-09-21", "2026-09-21")
    start = int((since + timedelta(hours=1, minutes=59)).timestamp() * 1000)
    split = interval_buckets([(start, start + 120_000)] * 2, "hour")
    assert split["buckets"] == [
        {"key": "2026-09-21T01", "agent_ms": 120_000},
        {"key": "2026-09-21T02", "agent_ms": 120_000},
    ]


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="tzset unavailable")
@pytest.mark.parametrize("zone,day", [("Europe/London", "2026-10-25"), ("Europe/London", "2026-03-29"), ("Asia/Kolkata", "2026-09-21")])
def test_interval_buckets_conserve_duration_across_dst_and_half_hour_zones(zone, day):
    prior = os.environ.get("TZ")
    try:
        os.environ["TZ"] = zone
        time.tzset()
        since, until = parse_date_range(day, day)
        bounds = (int(since.timestamp() * 1000), int(until.timestamp() * 1000))
        for granularity in ("hour", "day"):
            split = interval_buckets([bounds], granularity)
            assert sum(row["agent_ms"] for row in split["buckets"]) == bounds[1] - bounds[0]
            assert all(row["key"].startswith(day) for row in split["buckets"])
        assert bucket_granularity(since, until) == "hour"
        assert bucket_key(bounds[0], "hour") == f"{day}T00"
        keys = local_hour_keys(since)
        assert len(keys) == (23 if day == "2026-03-29" else 24)
        if day == "2026-03-29":
            assert f"{day}T01" not in keys
    finally:
        if prior is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prior
        time.tzset()


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="tzset unavailable")
@pytest.mark.parametrize("zone,first,last", [
    ("Europe/London", "2026-10-25", "2026-10-25"),
    ("Europe/London", "2026-03-27", "2026-04-02"),
    ("Europe/London", "2026-07-01", "2026-07-07"),
    ("Asia/Kolkata", "2026-09-21", "2026-09-21"),
    ("Europe/London", "2024-01-01", "2024-12-31"),
    ("Asia/Kolkata", "2026-01-15", "2026-12-15"),
    ("America/New_York", "2026-01-01", "2026-12-31"),
    ("Australia/Lord_Howe", "2026-10-04", "2026-10-04"),
])
def test_sql_buckets_match_local_event_buckets_across_timezones(tmp_path, zone, first, last):
    prior = os.environ.get("TZ")
    try:
        os.environ["TZ"] = zone
        time.tzset()
        since, until = parse_date_range(first, last)
        start = int(since.timestamp() * 1000)
        stop = int(until.timestamp() * 1000)
        raw = [{"source": "codex", "model": "model-a", "timestamp": stamp,
                "input": 10, "output": 5, "cacheRead": 20, "cost": .01, "messageCount": 3}
               for stamp in range(start + 1_800_000, stop, 3_600_000)]
        store = UsageEntryStore(tmp_path / "usage.sqlite3")
        store.sync_source("codex", build_source_signature(files=[["fixture", 1, 1]], parser={"v": 1}), lambda: raw)
        granularity = bucket_granularity(since, until)
        data = store.aggregate_entries(sources=["codex"], since=since, until=until, bucket_granularity=granularity)
        live = parse_entries_json({"entries": raw}, granularity=granularity)
        resolve = bucket_key_resolver(granularity)
        for event in raw[::2][::-1] + raw[1::2]:
            assert resolve(event["timestamp"]) == bucket_key(event["timestamp"], granularity)
        assert data["sparkline"]["granularity"] == live["sparkline"]["granularity"]
        assert len(data["sparkline"]["buckets"]) == len(live["sparkline"]["buckets"])
        for actual, expected in zip(data["sparkline"]["buckets"], live["sparkline"]["buckets"]):
            assert actual == {**expected, "cost": pytest.approx(expected["cost"])}
        assert data["total_tokens"] == live["total_tokens"]
        assert data["total_messages"] == live["total_messages"]
    finally:
        if prior is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prior
        time.tzset()


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="tzset unavailable")
def test_monthly_intervals_preserve_leap_days_dst_and_partial_months():
    prior = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "Europe/London"
        time.tzset()
        since, until = parse_date_range("2024-01-15", "2024-12-15")
        first, last = int(since.timestamp() * 1000), int(until.timestamp() * 1000)
        split = interval_buckets([(first, last)] * 2, "month")
        assert len(split["buckets"]) == 12
        assert sum(row["agent_ms"] for row in split["buckets"]) == 2 * (last - first)
        assert split["buckets"][1] == {"key": "2024-02", "agent_ms": 2 * 29 * 86_400_000}
        assert split["buckets"][2] == {"key": "2024-03", "agent_ms": 2 * (31 * 86_400_000 - 3_600_000)}
        assert split["buckets"][9] == {"key": "2024-10", "agent_ms": 2 * (31 * 86_400_000 + 3_600_000)}
    finally:
        if prior is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prior
        time.tzset()


def test_monthly_agent_endpoints_keep_unsorted_long_measured_intervals_additive():
    since, until = parse_date_range("2024-01-15", "2024-04-15")
    first, last = int(since.timestamp()*1000), int(until.timestamp()*1000)
    mid = first + (last-first)//2
    raw = [(mid, last), (first, last), (first, mid), (mid, mid), (last, first)]
    split = interval_buckets(raw, "month")
    assert len(split["buckets"]) == 4
    assert sum(row["agent_ms"] for row in split["buckets"]) == 2 * (last-first)
    ordered = sorted(raw[:3])
    assert interval_buckets(ordered, "month", (first,last), ordered_positive=True) == split


def test_bounded_monthly_read_uses_disjoint_indexed_ranges_and_iterable_sources(tmp_path):
    raw = []
    for month in range(1, 13):
        since, _ = parse_date_range(f"2024-{month:02d}-15", f"2024-{month:02d}-15")
        for source in ("codex", "claude"):
            raw.append({**entries()[0], "source":source, "timestamp":int(since.timestamp()*1000)})
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    for source in ("codex", "claude"):
        selected = [row for row in raw if row["source"] == source]
        store.sync_source(source, build_source_signature(files=[[source, 1, 1]], parser={"v":1}), lambda: selected)
    statements, plans = [], []
    read = store._read_priced

    def traced(fn):
        def run(conn):
            conn.set_trace_callback(statements.append)
            result = fn(conn)
            conn.set_trace_callback(None)
            for sql in statements:
                if sql.lstrip().startswith("SELECT") and "FROM usage_entries" in sql:
                    plans.extend(conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall())
            return result
        return read(run)

    store._read_priced = traced
    since, until = parse_date_range("2024-01-15", "2024-12-15")
    result = store.aggregate_entries(sources=iter(["codex"]), since=since, until=until, bucket_granularity="month")
    assert result["total_messages"] == 12 * 4
    assert len(result["sparkline"]["buckets"]) == 12
    queries = [sql for sql in statements if sql.lstrip().startswith("SELECT") and "FROM usage_entries" in sql]
    assert len(queries) == 1
    assert queries[0].count("UNION ALL") == 11
    searches = [str(row[3]) for row in plans if "SEARCH usage_entries" in str(row[3])]
    assert len(searches) == 12 and all("USING INDEX" in row for row in searches)


@pytest.mark.parametrize("granularity,first_day,last_day", [
    ("hour", "2026-03-29", "2026-03-29"),
    ("day", "2026-03-27", "2026-04-02"),
    ("month", "2024-01-15", "2024-12-15"),
])
@pytest.mark.parametrize("shape", ["sparse", "dense", "long"])
def test_shared_clock_and_bucket_pass_matches_independent_aggregations(granularity, first_day, last_day, shape):
    import random
    from tokdash.sessions import _merged_interval_ms
    from tokdash.usage_buckets import merged_interval_buckets
    since, until = parse_date_range(first_day, last_day)
    first, last = int(since.timestamp()*1000), int(until.timestamp()*1000)
    span = last-first
    rng = random.Random(17)
    if shape == "sparse":
        raw = [(first + i*span//37, min(last, first + i*span//37 + 120_000)) for i in range(37)]
    elif shape == "dense":
        raw = [(first + i*span//240, min(last, first + i*span//240 + span//2)) for i in range(120)]
    else:
        starts = [rng.randrange(first, last-1) for _ in range(37)]
        raw = [(start, min(last, start + rng.randrange(1, span//3))) for start in starts]
    ordered = sorted(raw)
    clock, buckets = merged_interval_buckets(ordered, granularity, (first,last))
    assert clock == _merged_interval_ms(raw)
    assert buckets == interval_buckets(raw, granularity, (first,last))
    assert sum(row["agent_ms"] for row in buckets["buckets"]) == sum(end-start for start,end in raw)
