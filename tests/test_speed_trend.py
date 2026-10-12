"""Combined throughput, the per-model merge and the trend view.

What a reader of the speed page relies on, in order:

* combined is tokens over the union of busy time, so two responses running at
  once count once, and combined = per stream x parallelism exactly;
* every supported reader has a decided timestamp anchor, and the OpenCode family
  really does store the start of its window;
* the same model reached through two harnesses is one combined workload, while
  its per-harness rows stay apart and it carries no pooled per-stream rate;
* the grouping ladder offers the sizes the README's table says, Monday weeks and
  calendar months, clipped to the window;
* the trend view's buckets and hours come from the same read, with absence null.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

pytest.importorskip("fastapi")

from tokdash import speed_report, speed_trend
from tokdash.output_speed import (
    BASIS_INCLUDING,
    KIND_REQUEST_WINDOW,
    KIND_RESPONSE_WINDOW,
)
from test_output_speed_api import _entry, _get, _measured, _ms, _seed, client  # noqa: F401  (client is a fixture)

DAY_FROM = "2026-03-02"
DAY_TO = "2026-03-08"


# --------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------

@pytest.mark.parametrize("intervals,busy,peak", [
    ([(0, 10), (5, 15)], 15, 2),             # overlap counts once
    ([(0, 10), (20, 30)], 20, 1),            # disjoint adds up
    ([(0, 30), (10, 20)], 30, 2),            # nested adds nothing
    ([(0, 10), (10, 20)], 20, 1),            # touching neither overlaps nor gaps
    ([(20, 30), (0, 10), (5, 15)], 25, 2),   # order of arrival does not matter
    ([], 0, 0),
])
def test_sweep_is_the_union_and_the_peak(intervals, busy, peak):
    assert speed_trend.sweep(intervals) == (busy, peak)


def test_two_fully_overlapping_responses_double_the_rate():
    tokens, ms = 200, 20_000.0
    busy, _peak = speed_trend.sweep([(0, 10_000), (0, 10_000)])
    combined = speed_trend._combined(tokens, ms, busy)
    assert combined["combined_tok_per_s"] == pytest.approx(2 * 1000 * tokens / ms)
    assert combined["parallelism"] == pytest.approx(2.0)


# --------------------------------------------------------------------------
# Anchors
# --------------------------------------------------------------------------

#: Traced through the parsers: the file readers store the record that settles a
#: response, a few milliseconds after its window ends.
END_ANCHORED = frozenset({"kimi", "omp", "codex", "dsh", "qwen_code", "claude"})


def test_every_supported_reader_has_a_decided_anchor():
    # A new reader has to be placed in one group or the other on purpose.
    assert speed_trend.START_ANCHORED_SOURCES | END_ANCHORED == speed_report.SUPPORTED_READERS
    assert not speed_trend.START_ANCHORED_SOURCES & END_ANCHORED


@pytest.mark.parametrize("source", sorted(speed_trend.START_ANCHORED_SOURCES))
def test_start_anchored_interval_runs_forward(source):
    assert speed_trend.busy_interval(source, 1000.0, 300.0) == (1000.0, 1300.0)


@pytest.mark.parametrize("source", sorted(END_ANCHORED))
def test_end_anchored_interval_runs_back(source):
    assert speed_trend.busy_interval(source, 1000.0, 300.0) == (700.0, 1000.0)


def test_opencode_rows_store_the_start_of_their_window(tmp_path, monkeypatch):
    """The stored timestamp is ``time_created``, and the window starts there."""
    from tokdash import speed_native, speed_worker
    db = tmp_path / "opencode.db"
    created, completed = 1_770_000_000_000, 1_770_000_004_500
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE message(id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
        data = {"role": "assistant", "modelID": "m", "tokens": {"input": 10, "output": 90, "reasoning": 0,
                "cache": {"read": 0, "write": 0}}, "time": {"created": created, "completed": completed},
                "finish": "stop"}
        conn.execute("INSERT INTO message VALUES ('a', 's', ?, ?)", (created, json.dumps(data)))
        conn.commit()
    monkeypatch.setattr(speed_native, "paths", lambda: {"opencode": (db,)})
    (row, _member), = list(speed_worker.native_rows("opencode"))
    fields = dict(zip(("source", "entry_key", "owner", "session", "timestamp"), row[:5]))
    duration = row[10]
    assert fields["timestamp"] == created
    assert speed_trend.busy_interval("opencode", fields["timestamp"], duration) == (created, completed)


# --------------------------------------------------------------------------
# The ladder
# --------------------------------------------------------------------------

@pytest.mark.parametrize("day_from,day_to,offered,auto", [
    ("2026-10-11", "2026-10-11", ["hour"], "hour"),
    ("2026-10-05", "2026-10-11", ["day"], "day"),
    ("2026-09-28", "2026-10-11", ["day", "week"], "day"),
    ("2026-08-28", "2026-10-11", ["day", "week"], "day"),      # 45 days: daily still fits
    ("2026-08-27", "2026-10-11", ["week"], "week"),            # 46 days: daily is over its cap
    ("2026-07-13", "2026-10-11", ["week", "month"], "week"),   # 91 days
    ("2025-10-12", "2026-10-11", ["month", "quarter"], "month"),
    ("2024-11-11", "2026-10-11", ["month", "quarter"], "month"),
])
def test_the_ladder_offers_what_the_readme_table_says(day_from, day_to, offered, auto):
    offer = speed_trend.offered_buckets(day_from, day_to)
    assert [e["bucket"] for e in offer if e["offered"]] == offered
    assert speed_trend.auto_bucket(offer) == auto


def test_weeks_start_on_monday_and_are_clipped_to_the_window():
    edges = speed_trend.bucket_edges("week", "2026-10-01", "2026-10-11")
    assert edges == [
        {"key": "2026-09-28", "from": "2026-10-01", "to": "2026-10-04"},
        {"key": "2026-10-05", "from": "2026-10-05", "to": "2026-10-11"},
    ]


def test_month_and_quarter_keys_follow_the_calendar():
    assert [e["key"] for e in speed_trend.bucket_edges("month", "2026-01-20", "2026-03-02")] == [
        "2026-01", "2026-02", "2026-03"]
    assert [e["key"] for e in speed_trend.bucket_edges("quarter", "2025-11-01", "2026-04-01")] == [
        "2025-Q4", "2026-Q1", "2026-Q2"]
    assert speed_trend.bucket_key("2026-12-31", "week") == "2026-12-28"
    assert speed_trend.bucket_key("2026-10-11", "hour", 7) == "2026-10-11T07"


# --------------------------------------------------------------------------
# Through the route
# --------------------------------------------------------------------------

def _overlapping_corpus():
    """Three responses of one model, two harnesses, one overlap.

    codex (end-anchored):    A = [T, T+10s], C = [T+30s, T+40s]
    opencode (start-anchor): B = [T+5s, T+15s]
    Each carries 100 tokens over 10 s. Union over both harnesses = 25 s.
    """
    t = _ms("2026-03-03", 12)
    return [
        _measured(_entry(source="codex", model="qwen-test", ts=t + 10_000), 100, 10_000.0,
                  kind=KIND_RESPONSE_WINDOW, basis=BASIS_INCLUDING),
        _measured(_entry(source="opencode", model="qwen-test", ts=t + 5_000), 100, 10_000.0,
                  kind=KIND_REQUEST_WINDOW, basis=BASIS_INCLUDING),
        _measured(_entry(source="codex", model="qwen-test", ts=t + 40_000), 100, 10_000.0,
                  kind=KIND_RESPONSE_WINDOW, basis=BASIS_INCLUDING),
    ]


def test_rows_gain_combined_and_models_merge_harnesses(client):
    _seed(_overlapping_corpus())
    payload = _get(client, view="across-models", date_from=DAY_FROM, date_to=DAY_TO)
    rows = {r["source"]: r for r in payload["rows"]}
    # Within one harness nothing overlaps: combined equals per stream.
    assert rows["codex"]["output_tok_per_s"] == pytest.approx(10.0)
    assert rows["codex"]["combined_tok_per_s"] == pytest.approx(10.0)
    assert rows["codex"]["parallelism"] == pytest.approx(1.0)
    assert rows["codex"]["busy_ms"] == pytest.approx(20_000.0)
    assert rows["opencode"]["combined_tok_per_s"] == pytest.approx(10.0)

    (model,) = payload["models"]
    assert model["model"] == "qwen-test"
    assert model["sources"] == ["codex", "opencode"]
    assert model["measurement_kinds"] == [KIND_REQUEST_WINDOW, KIND_RESPONSE_WINDOW]
    assert model["speed_calls"] == 3
    assert model["busy_ms"] == pytest.approx(25_000.0)
    assert model["combined_tok_per_s"] == pytest.approx(12.0)
    assert model["parallelism"] == pytest.approx(1.2)
    assert model["peak_in_flight"] == 2
    # Two timing methods: a pooled per-stream rate would measure nothing.
    assert "output_tok_per_s" not in model


def test_trend_buckets_and_hours_come_from_one_read(client):
    _seed(_overlapping_corpus())
    payload = _get(client, view="trend", date_from=DAY_FROM, date_to=DAY_TO)
    assert payload["granularity"] == "day" and payload["auto_bucket"] == "day"
    assert [b["key"] for b in payload["buckets"]][:2] == ["2026-03-02", "2026-03-03"]
    assert len(payload["buckets"]) == 7

    codex = next(s for s in payload["series"] if s["source"] == "codex")
    assert codex["calls"][:3] == [0, 2, 0]
    assert codex["per_stream"][:3] == [None, 10.0, None]   # absence is null, not zero
    assert codex["combined"][1] == 10.0

    (merged,) = payload["model_series"]
    assert merged["sources"] == ["codex", "opencode"]
    assert "per_stream" not in merged
    assert merged["combined"][1] == pytest.approx(12.0)
    assert merged["parallelism"][1] == pytest.approx(1.2)

    hour_of_local, _ = speed_report.local_clock()
    hour = hour_of_local(_ms("2026-03-03", 12))
    assert merged["hours"]["calls"][hour] == 3
    assert merged["hours"]["combined"][hour] == pytest.approx(12.0)
    assert sum(merged["hours"]["calls"]) == 3


def test_a_size_the_window_does_not_offer_falls_back_to_auto(client):
    _seed(_overlapping_corpus())
    # Seven days cannot hold two weeks' worth of reading: weekly is not offered.
    payload = _get(client, view="trend", date_from=DAY_FROM, date_to=DAY_TO, bucket="week")
    assert payload["requested_bucket"] == "week"
    assert payload["granularity"] == "day"
    two_weeks = _get(client, view="trend", date_from=DAY_FROM, date_to="2026-03-15", bucket="week")
    assert two_weeks["granularity"] == "week"
    assert [b["key"] for b in two_weeks["buckets"]] == ["2026-03-02", "2026-03-09"]
    assert {"bucket": "week", "count": 2} in two_weeks["offered_buckets"]


def test_an_unknown_bucket_is_a_400(client):
    response = client.get("/api/output-speed", params={
        "view": "trend", "date_from": DAY_FROM, "date_to": DAY_TO, "bucket": "fortnight"})
    assert response.status_code == 400


def test_a_source_filter_scopes_the_combined_pass_too(client):
    # Regression: the filter must reach the interval pass, not the name of the
    # last row the grouped pass happened to visit.
    _seed(_overlapping_corpus())
    payload = _get(client, view="across-models", date_from=DAY_FROM, date_to=DAY_TO, source="codex")
    (row,) = payload["rows"]
    assert row["busy_ms"] == pytest.approx(20_000.0)
    (model,) = payload["models"]
    assert model["sources"] == ["codex"]
    assert model["busy_ms"] == pytest.approx(20_000.0)
