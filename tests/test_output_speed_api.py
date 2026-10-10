"""The comparison route: GET /api/output-speed.

Two views, one route, one snapshot. These tests hold the parts that a reader of
the dashboard actually relies on:

* a rate is a ratio of sums, and an empty bucket is null rather than zero;
* hourly rows and six-hour rows agree because they come from the same read;
* nothing is prorated across midnight, and the local hour comes from the same
  clock the rest of the report uses;
* an incomplete comparison key is a 400, not an empty table;
* the cache never publishes a stale comparison, and never serves a comparison
  under someone else's key;
* ordinary report endpoints are untouched -- no speed columns, same payload.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException

import tokdash.api as api
from tokdash import speed_report, speed_cache
from tokdash.dateutil import parse_date_range
from tokdash.output_speed import (
    BASIS_EXCLUDING,
    BASIS_INCLUDING,
    BASIS_UNSPECIFIED,
    KIND_POST_FIRST_TOKEN,
    KIND_SERVER_DECODE,
    STATUS_AMBIGUOUS_PAIR,
    STATUS_INVALID_TIMING,
)
from tokdash.usage_store import UsageEntryStore


DAY_FROM = "2026-03-02"
DAY_TO = "2026-03-08"


def test_cache_only_flag_reaches_both_speed_readers(monkeypatch):
    from tokdash import session_speed
    calls=[]
    def reader(*args,**kwargs):
        calls.append(kwargs);return {'cache':{'state':'stale','job_id':None}}
    monkeypatch.setattr(speed_cache,'payload',reader)
    monkeypatch.setattr(session_speed,'timeline',reader)
    api.get_output_speed(date_from=DAY_FROM,date_to=DAY_TO,cache_only=True)
    api.get_session_speed('codex','one',cache_only=True)
    assert len(calls)==2 and all(c['cache_only'] is True for c in calls)


@pytest.mark.parametrize('endpoint', ['model', 'timeline', 'batch', 'ensure'])
def test_speed_publication_lock_is_retryable_and_recovers_after_release(tmp_path, monkeypatch, endpoint):
    from tokdash import session_speed
    db=tmp_path/'publication.sqlite3'
    with closing(sqlite3.connect(db)) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE staged(id INTEGER)');writer.commit()
        writer.execute('BEGIN IMMEDIATE');writer.execute('INSERT INTO staged VALUES (1)')
        def producer(*args,**kwargs):
            with closing(sqlite3.connect(db,timeout=0)) as reader:
                reader.execute('BEGIN IMMEDIATE');reader.rollback()
            return {'recovered':True}
        body={'sessions':[{'tool':'codex','session_id':'one'}],'date_from':DAY_FROM,'date_to':DAY_TO}
        if endpoint=='model':
            monkeypatch.setattr(speed_cache,'payload',producer)
            call=lambda:api.get_output_speed(date_from=DAY_FROM,date_to=DAY_TO)
        elif endpoint=='timeline':
            monkeypatch.setattr(session_speed,'timeline',producer)
            call=lambda:api.get_session_speed('codex','one')
        elif endpoint=='batch':
            monkeypatch.setattr(session_speed,'batch_summaries',producer)
            call=lambda:api.get_session_speeds(body)
        else:
            monkeypatch.setattr(session_speed,'ensure_sessions',producer)
            call=lambda:api.ensure_output_speed_sessions(body)
        with pytest.raises(HTTPException) as error:call()
        assert error.value.status_code==503
        assert 'publication is busy' in error.value.detail
        writer.rollback()
        assert call()=={'recovered':True}


def test_unrelated_speed_sql_failure_stays_a_failure(tmp_path,monkeypatch):
    def broken(*args,**kwargs):
        with closing(sqlite3.connect(tmp_path/'broken.sqlite3')) as conn:
            conn.execute('SELECT * FROM missing_table')
    monkeypatch.setattr(speed_cache,'payload',broken)
    with pytest.raises(HTTPException) as error:api.get_output_speed(date_from=DAY_FROM,date_to=DAY_TO)
    assert error.value.status_code==500
    assert 'missing_table' in error.value.detail


@pytest.mark.parametrize('message,status', [
    ('database is locked', 503),
    ('database table is locked: speed_responses', 503),
    ('database schema is locked: main', 503),
    ('database is busy', 503),
    ('no such table: database is locked', 500),
    ('disk I/O error', 500),
])
def test_speed_database_errors_without_python311_metadata(monkeypatch, message, status):
    # Python 3.10 provides neither these constants nor sqlite_errorcode.
    monkeypatch.delattr(sqlite3, 'SQLITE_BUSY', raising=False)
    monkeypatch.delattr(sqlite3, 'SQLITE_LOCKED', raising=False)
    error = sqlite3.OperationalError(message)
    assert not hasattr(error, 'sqlite_errorcode')
    with pytest.raises(HTTPException) as result:
        api._raise_speed_database_error(error)
    assert result.value.status_code == status
    assert result.value.__cause__ is error


@pytest.mark.parametrize('code,message,status', [
    (5, 'different localized message', 503),
    (5 | (2 << 8), 'different localized message', 503),
    (6 | (1 << 8), 'different localized message', 503),
    (1, 'database is locked', 500),
])
def test_speed_database_error_code_takes_precedence(code, message, status):
    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = code
    with pytest.raises(HTTPException) as result:
        api._raise_speed_database_error(error)
    assert result.value.status_code == status


def test_large_unmeasured_histories_keep_speed_and_availability_on_covering_indexes(tmp_path):
    with closing(speed_cache.connect(tmp_path/'speed.sqlite3')) as c:
        plan=' '.join(r[3] for r in c.execute('EXPLAIN QUERY PLAN SELECT timestamp FROM speed_responses WHERE speed_calls>0 ORDER BY timestamp LIMIT 1'))
        assert 'idx_speed_measured_time' in plan
        assert 'TEMP B-TREE' not in plan



def _ms(day: str, hour: int, minute: int = 0) -> int:
    """Epoch ms for a UTC wall-clock instant.

    The route bins on the *local* hour, resolved through the machine's own zone,
    so tests that assert a bin never hardcode an offset: they pass an explicit
    ``hour_offset`` or read the hour back. Everything here is built on UTC
    instants and compared against what the same clock reports.
    """
    dt = datetime.strptime(f"{day} {hour:02d}:{minute:02d}", "%Y-%m-%d %H:%M").replace(
        tzinfo=timezone.utc
    )
    return int(dt.timestamp() * 1000)


def _entry(*, source, model, ts, output=200, kind="", basis="", tokens=0, ms=0.0,
           calls=0, status="unknown", cost=0.002):
    return {
        "source": source,
        "model": model,
        "provider": "test",
        "timestamp": ts,
        "input": 50,
        "output": output,
        "cacheRead": 0,
        "cacheWrite": 0,
        "reasoning": 0,
        "cost": cost,
        "messageCount": 1,
        "speed_tokens": tokens,
        "speed_ms": ms,
        "speed_calls": calls,
        "speed_kind": kind,
        "speed_token_basis": basis,
        "speed_status": status,
    }


def _measured(entry, tokens, ms, *, kind=KIND_SERVER_DECODE, basis=BASIS_UNSPECIFIED):
    return dict(
        entry,
        speed_tokens=tokens,
        speed_ms=ms,
        speed_calls=1,
        speed_kind=kind,
        speed_token_basis=basis,
        speed_status="measured",
    )


def _seed(entries):
    """Write rows straight into the isolated usage database.

    The file signature is derived from the rows themselves, because a re-seed
    that changed the rows while leaving the signature alone is, correctly, not a
    change the store would reparse.
    """
    store = UsageEntryStore()
    by_file = {}
    for entry in entries:
        by_file.setdefault(f"/fixture/{entry['source']}.jsonl", []).append(entry)
    for path, rows in by_file.items():
        digest = hashlib.sha1(
            json.dumps(rows, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:8]
        store.sync_files(
            rows[0]["source"],
            [(path, int(digest, 16), 1000 + len(rows))],
            parser={"v": 1},
            parse_file_entries=lambda _sig, _rows=rows: list(_rows),
            durable=False,
        )
    _publish_fixture(entries,store)
    return store


def _publish_fixture(entries,store):
    from tokdash.usage_store import _entry_key, _speed_for_storage
    with closing(speed_cache.connect()) as c:
        c.execute('DELETE FROM speed_responses')
        for e in entries:
            t=_speed_for_storage(e)
            c.execute('INSERT INTO speed_responses VALUES ('+','.join('?' for _ in range(15))+')',
                (e['source'],_entry_key(e),'/fixture','','%s'%e['timestamp'],e['model'],e['output'],e.get('reasoning',0),int(e['output']>0 or e['source']=='qwen_code' and e.get('reasoning',0)>0),*[t[k] for k in speed_cache.FIELDS]))
        speed_cache.put_meta(c,published_generation=store.measurement_generation(),identity=speed_cache.identity(),updated_at='fixture')
        c.execute("INSERT OR REPLACE INTO speed_windows VALUES('','0001-01-01','9999-12-30',?)",(store.measurement_generation(),))
        c.commit()


class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body

    @property
    def text(self):
        return json.dumps(self._body, default=str)


class HandlerClient:
    """Call the route handler in process instead of over an ASGI transport.

    Starlette's TestClient deadlocks against this machine's anyio pairing, in and
    out of this repository, so the API suite's own alternative applies: exercise
    the handler, and read the HTTPException it raises as the status code the
    middleware would have turned it into. Validation, caching and payload
    assembly all still run.
    """

    def get(self, path, params=None):
        assert path == "/api/output-speed", path
        try:
            return _Response(200, api.get_output_speed(**dict(params or {})))
        except HTTPException as exc:
            return _Response(exc.status_code, exc.detail)


@pytest.fixture
def client(monkeypatch):
    """A fresh cache and a route-level client, per test.

    The cache is cleared on both sides so a comparison computed by one test can
    never answer another.
    """
    monkeypatch.setattr(speed_cache,"launch_worker",lambda c:None)
    api._clear_cache()
    with api._cache_guard:
        api._key_locks.clear()
    yield HandlerClient()
    api._clear_cache()
    with api._cache_guard:
        api._key_locks.clear()


def _get(client, **params):
    response = client.get("/api/output-speed", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _across(client, **extra):
    return _get(client, view="across-models", date_from=DAY_FROM, date_to=DAY_TO, **extra)


def _temporal(client, source, model, kind, basis, **extra):
    params = {
        "view": "time-of-day",
        "date_from": DAY_FROM,
        "date_to": DAY_TO,
        "source": source,
        "model": model,
        "measurement_kind": kind,
        "token_basis": basis,
    }
    params.update(extra)
    return _get(client, **params)


def _hour_of(ts_ms):
    """The local hour the route itself would assign, computed independently."""
    return datetime.fromtimestamp(ts_ms / 1000.0, timezone.utc).astimezone().hour


# --------------------------------------------------------------------------
# Across models
# --------------------------------------------------------------------------


def test_the_rate_is_a_ratio_of_sums_not_a_mean_of_call_rates(client):
    """One 40 ms call and one 4 s call weigh by the time they actually took."""
    _seed([
        _measured(_entry(source="kimi", model="k2", ts=_ms(DAY_FROM, 9), output=100),
                  tokens=100, ms=50.0),
        _measured(_entry(source="kimi", model="k2", ts=_ms(DAY_FROM, 10), output=4000),
                  tokens=4000, ms=4000.0),
    ])
    rows = _across(client)["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert row["speed_tokens"] == 4100 and row["speed_ms"] == 4050.0
    # 1000 * 4100 / 4050 = 1012.3, where the mean of the two call rates would be
    # (2000 + 1000) / 2 = 1500.
    assert row["output_tok_per_s"] == pytest.approx(1012.3, abs=0.1)
    assert row["speed_calls"] == 2


def test_incompatible_measurements_never_share_a_row(client):
    """A decode window and a whole-call window are different physical quantities."""
    _seed([
        _measured(_entry(source="kimi", model="shared", ts=_ms(DAY_FROM, 9)),
                  tokens=200, ms=2000.0, kind=KIND_SERVER_DECODE, basis=BASIS_UNSPECIFIED),
        _measured(_entry(source="kimi", model="shared", ts=_ms(DAY_FROM, 10), output=100),
                  tokens=100, ms=1000.0,
                  kind=KIND_POST_FIRST_TOKEN, basis=BASIS_INCLUDING),
    ])
    rows = _across(client)["rows"]
    assert len(rows) == 2, "two measurement variants must stay two rows"
    keys = {(r["measurement_kind"], r["token_basis"]) for r in rows}
    assert keys == {
        (KIND_SERVER_DECODE, BASIS_UNSPECIFIED),
        (KIND_POST_FIRST_TOKEN, BASIS_INCLUDING),
    }
    for row in rows:
        assert row["output_tok_per_s"] == 100.0


def test_two_tools_on_one_model_name_stay_two_rows(client):
    _seed([
        _measured(_entry(source="kimi", model="same", ts=_ms(DAY_FROM, 9)),
                  tokens=100, ms=1000.0),
        _measured(_entry(source="omp", model="same", ts=_ms(DAY_FROM, 9)),
                  tokens=300, ms=1000.0),
    ])
    rows = _across(client)["rows"]
    assert sorted(r["source"] for r in rows) == ["kimi", "omp"]
    assert {r["output_tok_per_s"] for r in rows} == {100.0, 300.0}


def test_a_model_with_no_measurements_is_reported_by_status_not_as_zero(client):
    _seed([
        _measured(_entry(source="kimi", model="measured", ts=_ms(DAY_FROM, 9)),
                  tokens=200, ms=2000.0),
        _entry(source="untracked", model="quiet", ts=_ms(DAY_FROM, 9)),
    ])
    payload = _across(client)
    assert [r["model"] for r in payload["rows"]] == ["measured"]
    assert "untracked" not in json.dumps(payload["rows"])
    status = {s["source"]: s for s in payload["source_status"]}
    assert status["untracked"]["status"] == "unsupported_reader"
    assert status["untracked"]["usage_rows"] == 1


def test_a_source_with_exclusions_only_is_reported_as_unavailable(client):
    _seed([
        _entry(source="broken", model="m", ts=_ms(DAY_FROM, 9), status=STATUS_INVALID_TIMING),
        _entry(source="broken", model="m", ts=_ms(DAY_FROM, 10), status=STATUS_AMBIGUOUS_PAIR),
    ])
    payload = _across(client)
    assert payload["rows"] == []
    assert payload["source_status"][0]["status"] == "timing_unavailable"


def test_empty_selected_dates_expose_recorded_dates_without_changing_the_window(client):
    _seed([
        _measured(_entry(source="kimi", model="m", ts=_ms("2026-07-16", 12)), 200, 2000),
        _measured(_entry(source="omp", model="m", ts=_ms("2026-09-15", 12)), 300, 3000),
        _entry(source="codex", model="other", ts=_ms("2026-10-08", 12)),
        _entry(source="kimi", model="m", ts=_ms("2026-01-01", 12), status=STATUS_INVALID_TIMING),
    ])
    payload = _get(client, view="across-models", date_from="2026-10-08", date_to="2026-10-08")
    assert payload["rows"] == []
    assert payload["range"] == {"from": "2026-10-08", "to": "2026-10-08"}
    assert payload["available_measurement_range"] == {"from": "2026-07-16", "to": "2026-09-15"}
    assert payload["schema_version"] == speed_report.SPEED_CACHE_CONTRACT_VERSION == 6


def test_recorded_dates_are_null_when_only_untimed_usage_exists(client):
    _seed([_entry(source="codex", model="m", ts=_ms(DAY_FROM, 12))])
    assert _across(client)["available_measurement_range"] is None


def test_recorded_date_bounds_use_the_compact_covering_timing_index(tmp_path):
    with closing(speed_cache.connect(tmp_path/'speed.sqlite3')) as c:
        statements=[];c.set_trace_callback(statements.append)
        assert speed_report.available_measurement_range(c) is None
        query=next(q for q in statements if 'SELECT timestamp' in q)
        plan=' '.join(r[3] for r in c.execute('EXPLAIN QUERY PLAN '+query))
        assert 'idx_speed_measured_time' in plan and 'TEMP B-TREE' not in plan



def test_coverage_counts_eligible_calls_inside_the_window(client):
    """Coverage is measured against the window the reader selected, not the corpus."""
    entries = [
        _measured(_entry(source="kimi", model="partial", ts=_ms(DAY_FROM, h)),
                  tokens=100, ms=1000.0)
        for h in (0, 1)
    ]
    # The unmeasured population sits on later days, so narrowing the range is
    # the act that removes them from the denominator.
    entries += [
        _entry(source="kimi", model="partial", ts=_ms("2026-03-0%d" % (3 + h % 5), 10 + h % 6))
        for h in range(8)
    ]
    _seed(entries)
    row = _across(client)["rows"][0]
    assert row["speed_calls"] == 2
    assert row["eligible_calls"] == 10
    assert row["coverage"] == pytest.approx(0.2, abs=1e-6)

    # Narrowing the range to the two measured hours must raise coverage to 100%,
    # which is what proves the denominator is windowed rather than lifetime.
    narrow = _get(client, view="across-models", date_from=DAY_FROM, date_to=DAY_FROM)
    narrow_row = narrow["rows"][0]
    assert narrow_row["eligible_calls"] == 2
    assert narrow_row["coverage"] == pytest.approx(1.0)


def test_absence_is_reported_as_absence_on_a_measured_row(client):
    _seed([
        _measured(_entry(source="kimi", model="mixed", ts=_ms(DAY_FROM, 1)),
                  tokens=200, ms=2000.0),
        _entry(source="kimi", model="mixed", ts=_ms(DAY_FROM, 2),
               status="missing_timing"),
        _entry(source="kimi", model="mixed", ts=_ms(DAY_FROM, 3),
               status=STATUS_INVALID_TIMING),
        _entry(source="kimi", model="mixed", ts=_ms(DAY_FROM, 4),
               status=STATUS_AMBIGUOUS_PAIR),
    ])
    row = _across(client)["rows"][0]
    absence = row["unmeasured"]
    assert absence["scope"] == "source_model"
    assert absence["missing_timing_calls"] == 1
    assert absence["excluded_calls_by_reason"] == {
        STATUS_INVALID_TIMING: 1, STATUS_AMBIGUOUS_PAIR: 1,
    }
    # The rate only ever saw the one matched call.
    assert row["speed_calls"] == 1 and row["output_tok_per_s"] == 100.0


def test_the_window_is_closed_and_never_prorates_across_midnight(client):
    """A call belongs to the day its usage row is stamped on, whole."""
    _seed([
        _measured(_entry(source="kimi", model="span", ts=_ms(DAY_FROM, 23, 59)),
                  tokens=600, ms=6000.0),
        _measured(_entry(source="kimi", model="span", ts=_ms(DAY_TO, 0, 0)),
                  tokens=100, ms=1000.0),
        _measured(_entry(source="kimi", model="span", ts=_ms(DAY_TO, 0, 1)),
                  tokens=1, ms=1000.0),
    ])
    payload = _across(client)
    assert payload["range"] == {"from": DAY_FROM, "to": DAY_TO}
    assert payload["selected_days"] == 7
    row = payload["rows"][0]
    assert (row["speed_tokens"], row["speed_ms"]) == (701, 8000.0)

    # Excluding the last day drops both of its rows entirely: no fractional token
    # is left behind from a call whose duration ran across the boundary.
    one_day = _get(client, view="across-models", date_from=DAY_FROM, date_to=DAY_FROM)
    assert one_day["rows"][0]["speed_tokens"] == 600


def test_days_with_measurements_counts_days_not_the_span(client):
    _seed([
        _measured(_entry(source="kimi", model="spiky", ts=_ms(DAY_FROM, 1)),
                  tokens=100, ms=1000.0),
        _measured(_entry(source="kimi", model="spiky", ts=_ms("2026-03-04", 1)),
                  tokens=100, ms=1000.0),
    ])
    row = _across(client)["rows"][0]
    assert row["days_with_measurements"] == 2
    assert row["selected_days"] == 7
    assert row["observed_range_status"] == "partial"


def test_measured_days_are_the_sources_own_union_across_models(client):
    """Model A on day one, model B on day two: the source was measured twice.

    Reported as a report that understated itself: the across-models note read the
    maximum of the per-row day counts, and a maximum cannot build a union -- two
    models measured on two different days still give one day each. A sum overcounts
    the other way, on the days the models share. So the count of distinct measured
    days is a per-source number and the query is the only place that can compute
    it, which is what the note reads.
    """
    _seed([
        _measured(_entry(source="kimi", model="early", ts=_ms(DAY_FROM, 1)),
                  tokens=100, ms=1000.0),
        _measured(_entry(source="kimi", model="late", ts=_ms("2026-03-03", 1)),
                  tokens=100, ms=1000.0),
        # Both models on a shared third day: two rows, one day.
        _measured(_entry(source="kimi", model="early", ts=_ms(DAY_TO, 1)),
                  tokens=100, ms=1000.0),
        _measured(_entry(source="kimi", model="late", ts=_ms(DAY_TO, 2)),
                  tokens=100, ms=1000.0),
        # Another source, another day. It must not reach kimi's count.
        _measured(_entry(source="omp", model="other", ts=_ms("2026-03-05", 1)),
                  tokens=100, ms=1000.0),
    ])

    rows = _across(client, source="kimi")["rows"]
    assert {row["model"] for row in rows} == {"early", "late"}
    assert {row["days_with_measurements"] for row in rows} == {2}, (
        "each model kept its own two days: the per-row count is unchanged")
    assert {row["source_days_with_measurements"] for row in rows} == {3}, (
        "the union, not the maximum (2) and not the sum (4)")

    # Filtered rows and filtered metadata describe the same population, so the
    # payload-wide count is kimi's three days rather than every source's four.
    assert _across(client, source="kimi")["days_with_measurements"] == 3
    assert _across(client)["days_with_measurements"] == 4


def test_source_filter_narrows_rows_without_narrowing_source_status(client):
    _seed([
        _measured(_entry(source="kimi", model="a", ts=_ms(DAY_FROM, 1)),
                  tokens=100, ms=1000.0),
        _measured(_entry(source="omp", model="b", ts=_ms(DAY_FROM, 1)),
                  tokens=100, ms=1000.0),
        _entry(source="quiet", model="c", ts=_ms(DAY_FROM, 1)),
    ])
    payload = _across(client, source="kimi")
    assert [r["source"] for r in payload["rows"]] == ["kimi"]
    assert {s["source"] for s in payload["source_status"]} == {"quiet"}


# --------------------------------------------------------------------------
# Time of day
# --------------------------------------------------------------------------


def test_hourly_and_six_hour_rows_come_from_one_snapshot(client):
    """The six-hour row is a sum of sums, never a mean of the hourly rates."""
    entries = []
    for i in range(5):
        # Five fast calls in one local hour.
        ts = _ms("2026-03-03", 3, i)
        entries.append(_measured(_entry(source="kimi", model="shape", ts=ts),
                                tokens=10, ms=100.0))
    for i in range(2):
        # Two slow calls in another, four hours later: 500 ms apart in UTC they
        # stay four local hours apart whatever the zone.
        ts = _ms("2026-03-03", 7, i)
        entries.append(_measured(_entry(source="kimi", model="shape", ts=ts),
                                tokens=1000, ms=5000.0))
    _seed(entries)

    payload = _temporal(client, "kimi", "shape", KIND_SERVER_DECODE, BASIS_UNSPECIFIED)
    hourly = payload["hourly"]
    assert len(hourly) == 24
    fast_hour = next(h for h in hourly if h["speed_calls"] == 5)
    slow_hour = next(h for h in hourly if h["speed_calls"] == 2)
    assert fast_hour["hour"] == _hour_of(_ms("2026-03-03", 3))
    assert slow_hour["hour"] == _hour_of(_ms("2026-03-03", 7))
    assert fast_hour["output_tok_per_s"] == pytest.approx(100.0)
    assert slow_hour["output_tok_per_s"] == pytest.approx(200.0)

    bin_a = payload["six_hour"][0]   # 00-06 holds the fast hour
    bin_b = payload["six_hour"][1]   # 06-12 holds the slow hour
    assert bin_a["speed_tokens"] == 50 and bin_a["speed_ms"] == 500.0
    assert bin_b["speed_tokens"] == 2000 and bin_b["speed_ms"] == 10000.0
    # A mean of the two hourly rates would say (100 + 200) / 2 = 150 for the
    # whole day. Weighting by the time each call actually ran says 195.2, because
    # the slow calls carry twenty times the duration.
    whole = 1000 * (bin_a["speed_tokens"] + bin_b["speed_tokens"]) / (
        bin_a["speed_ms"] + bin_b["speed_ms"])
    assert round(whole, 1) == pytest.approx(195.2, abs=0.1)
    assert payload["coverage"] == pytest.approx(1.0)


def test_an_empty_hour_is_null_and_renders_as_a_gap(client):
    _seed([
        _measured(_entry(source="kimi", model="sparse", ts=_ms("2026-03-03", 4)),
                  tokens=100, ms=1000.0),
    ])
    payload = _temporal(client, "kimi", "sparse", KIND_SERVER_DECODE, BASIS_UNSPECIFIED)
    hours_with_data = [h for h in payload["hourly"] if h["speed_calls"]]
    assert [h["hour"] for h in hours_with_data] == [_hour_of(_ms("2026-03-03", 4))]
    empty = [h for h in payload["hourly"] if not h["speed_calls"]]
    assert len(empty) == 23
    for hour in empty:
        assert hour["output_tok_per_s"] is None, "absence is not a rate of zero"
        assert hour["speed_tokens"] == 0 and hour["speed_ms"] == 0.0
    # A bin that contains only the one measured hour carries it; the other three
    # are empty rather than zero-filled.
    empty_bins = [b for b in payload["six_hour"] if not b["speed_calls"]]
    assert len(empty_bins) == 3
    assert all(b["output_tok_per_s"] is None for b in empty_bins)


def test_an_hour_of_rejected_calls_is_not_an_empty_hour(client):
    """'Nothing measured' and 'measured nothing usable' are different statements."""
    ts = _ms("2026-03-03", 5)
    _seed([
        _entry(source="kimi", model="noisy", ts=ts, status=STATUS_INVALID_TIMING),
        _entry(source="kimi", model="noisy", ts=ts + 1, status="missing_timing"),
    ])
    payload = _temporal(client, "kimi", "noisy", KIND_SERVER_DECODE, BASIS_UNSPECIFIED)
    hour = payload["hourly"][_hour_of(ts)]
    assert hour["speed_calls"] == 0
    assert hour["output_tok_per_s"] is None
    assert hour["excluded_calls_by_reason"] == {STATUS_INVALID_TIMING: 1}
    assert hour["missing_timing_calls"] == 1
    assert payload["excluded_calls_by_reason"] == {STATUS_INVALID_TIMING: 1}
    assert payload["missing_timing_calls"] == 1
    assert payload["observed_range_status"] == "no_measurements"


def test_the_temporal_view_bins_on_local_hours_not_utc(client):
    """One row per local hour of the machine's own clock, per DST-safe rule.

    The bins are local, so a fixed UTC hour maps to whatever hour this machine
    calls it. What has to hold is that the row lands in the same hour a call made
    at that local time always lands in, and that two UTC instants an hour apart
    do not share a bin unless the machine's clock says they do.
    """
    _seed([
        _measured(_entry(source="kimi", model="clock", ts=_ms("2026-03-03", 12)),
                  tokens=100, ms=1000.0),
        _measured(_entry(source="kimi", model="clock", ts=_ms("2026-03-03", 13)),
                  tokens=100, ms=1000.0),
    ])
    payload = _temporal(client, "kimi", "clock", KIND_SERVER_DECODE, BASIS_UNSPECIFIED)
    hours = sorted(h["hour"] for h in payload["hourly"] if h["speed_calls"])
    assert len(hours) == 2, "two UTC hours an hour apart are two local hours"
    assert hours == [
        _hour_of(_ms("2026-03-03", 12)),
        _hour_of(_ms("2026-03-03", 13)),
    ]
    assert hours == [hours[0], (hours[0] + 1) % 24]
    assert payload["timestamp_basis"] == "usage_timestamp"
    assert payload["bucket"] == "local_hour_of_day"


def test_a_selected_variant_cannot_borrow_another_variants_calls(client):
    _seed([
        _measured(_entry(source="kimi", model="two", ts=_ms("2026-03-03", 1)),
                  tokens=100, ms=1000.0, kind=KIND_SERVER_DECODE, basis=BASIS_UNSPECIFIED),
        _measured(_entry(source="kimi", model="two", ts=_ms("2026-03-03", 1), output=50),
                  tokens=50, ms=500.0, kind=KIND_POST_FIRST_TOKEN, basis=BASIS_INCLUDING),
    ])
    decode = _temporal(client, "kimi", "two", KIND_SERVER_DECODE, BASIS_UNSPECIFIED)
    post = _temporal(client, "kimi", "two", KIND_POST_FIRST_TOKEN, BASIS_INCLUDING)
    assert decode["hourly"][1 if _hour_of(_ms("2026-03-03", 1)) == 1 else 1] is not None
    assert sum(h["speed_calls"] for h in decode["hourly"]) == 1
    assert sum(h["speed_calls"] for h in post["hourly"]) == 1
    assert sum(h["speed_tokens"] for h in decode["hourly"]) == 100
    assert sum(h["speed_tokens"] for h in post["hourly"]) == 50
    # The unmeasured population is not variant-filtered: a row with no duration
    # has no kind either, so filtering it away would hide the gap.
    _seed([
        _measured(_entry(source="kimi", model="two", ts=_ms("2026-03-03", 1)),
                  tokens=100, ms=1000.0, kind=KIND_SERVER_DECODE, basis=BASIS_UNSPECIFIED),
        _entry(source="kimi", model="two", ts=_ms("2026-03-03", 2), status="missing_timing"),
    ])
    again = _temporal(client, "kimi", "two", KIND_POST_FIRST_TOKEN, BASIS_INCLUDING)
    assert again["missing_timing_calls"] == 1
    assert sum(h["speed_calls"] for h in again["hourly"]) == 0


def test_the_key_is_echoed_and_the_ranges_agree(client):
    _seed([
        _measured(_entry(source="kimi", model="echo", ts=_ms(DAY_FROM, 1)),
                  tokens=100, ms=1000.0),
    ])
    payload = _temporal(client, "kimi", "echo", KIND_SERVER_DECODE, BASIS_UNSPECIFIED)
    assert payload["selected_key"] == {
        "source": "kimi", "model": "echo",
        "measurement_kind": KIND_SERVER_DECODE, "token_basis": BASIS_UNSPECIFIED,
    }
    assert payload["range"] == {"from": DAY_FROM, "to": DAY_TO}
    assert payload["eligible_calls"] == 1
    assert payload["coverage"] == pytest.approx(1.0)
    assert payload["first_measured_at"] == _ms(DAY_FROM, 1)
    assert payload["last_measured_at"] == _ms(DAY_FROM, 1)


# --------------------------------------------------------------------------
# Keys, validation, and the cache
# --------------------------------------------------------------------------


def test_a_bad_or_incomplete_key_is_rejected_before_sql(client):
    missing = client.get(
        "/api/output-speed",
        params={"view": "time-of-day", "date_from": DAY_FROM, "date_to": DAY_TO},
    )
    assert missing.status_code == 400, missing.text
    bad_kind = client.get(
        "/api/output-speed",
        params={
            "view": "time-of-day", "date_from": DAY_FROM, "date_to": DAY_TO,
            "source": "kimi", "model": "m", "measurement_kind": "vibes",
            "token_basis": BASIS_UNSPECIFIED,
        },
    )
    assert bad_kind.status_code == 400
    bad_basis = client.get(
        "/api/output-speed",
        params={
            "view": "time-of-day", "date_from": DAY_FROM, "date_to": DAY_TO,
            "source": "kimi", "model": "m", "measurement_kind": KIND_SERVER_DECODE,
            "token_basis": "output_whatever",
        },
    )
    assert bad_basis.status_code == 400
    bad_view = client.get("/api/output-speed", params={"view": "everything"})
    assert bad_view.status_code == 400


def test_the_cache_key_tracks_the_generation_and_not_the_pricing(monkeypatch):
    before=(speed_cache.identity(),speed_cache.primary_generation())
    monkeypatch.setattr(api,'_baseline_pricing_signature',lambda:('pricing-before',1,1))
    ordinary=api._pricing_cache_key('usage')
    monkeypatch.setattr(api,'_baseline_pricing_signature',lambda:('pricing-after',2,2))
    assert api._pricing_cache_key('usage')!=ordinary
    assert (speed_cache.identity(),speed_cache.primary_generation())==before
    store=_seed([_measured(_entry(source='kimi',model='gen',ts=_ms(DAY_FROM,1)),tokens=100,ms=1000)])
    assert speed_cache.identity()==before[0]
    assert speed_cache.primary_generation()==store.measurement_generation()==1
    assert speed_cache.primary_generation()!=before[1]



def test_the_temporal_key_carries_the_clock_and_an_open_window_the_day(client,monkeypatch):
    """Uncached aggregation reads the current local clock on every request."""
    _seed([_measured(_entry(source='kimi',model='m',ts=_ms(DAY_FROM,1)),tokens=100,ms=1000)])
    monkeypatch.setattr(speed_report,'local_clock',lambda:(lambda ms:1,lambda ms:DAY_FROM))
    first=_temporal(client,'kimi','m',KIND_SERVER_DECODE,BASIS_UNSPECIFIED)
    monkeypatch.setattr(speed_report,'local_clock',lambda:(lambda ms:2,lambda ms:DAY_FROM))
    second=_temporal(client,'kimi','m',KIND_SERVER_DECODE,BASIS_UNSPECIFIED)
    assert first['hourly'][1]['speed_calls']==1 and first['hourly'][2]['speed_calls']==0
    assert second['hourly'][1]['speed_calls']==0 and second['hourly'][2]['speed_calls']==1
    assert first['measurement_generation']==second['measurement_generation']



def test_a_request_under_one_key_never_answers_a_different_key(client):
    _seed([
        _measured(_entry(source="kimi", model="a", ts=_ms(DAY_FROM, 1)),
                  tokens=100, ms=1000.0),
        _measured(_entry(source="omp", model="b", ts=_ms(DAY_FROM, 1), output=40),
                  tokens=40, ms=80.0, kind=KIND_POST_FIRST_TOKEN, basis=BASIS_INCLUDING),
    ])
    assert {r["source"] for r in _across(client)["rows"]} == {"kimi", "omp"}
    assert {r["source"] for r in _across(client, source="kimi")["rows"]} == {"kimi"}
    narrow_key = {r["source"] for r in _across(client, source="omp")["rows"]}
    assert narrow_key == {"omp"}
    # The unfiltered answer came back unmutated after the filtered ones were
    # cached, which is the failure mode a shared cache key would produce.
    assert {r["source"] for r in _across(client)["rows"]} == {"kimi", "omp"}


@pytest.mark.parametrize('bootstrap_busy', [False, True])
def test_a_cold_key_computes_once_and_a_second_reader_is_not_served_stale_numbers(client,monkeypatch,bootstrap_busy):
    """Concurrent cold reads join one durable job without sharing report slots."""
    UsageEntryStore()._connect().close()
    launches=[]
    monkeypatch.setattr(speed_cache,'launch_worker',lambda c:launches.append(True))
    from tokdash import speed_worker
    monkeypatch.setattr(speed_worker,'build',lambda *a,**kw:pytest.fail('synchronous extraction'))
    if bootstrap_busy:
        real_connect = speed_cache.connect
        gate = threading.Lock()
        first = [True]
        def connect(*args, **kwargs):
            with gate:
                if first[0]:
                    first[0] = False
                    raise sqlite3.OperationalError('database is locked')
            return real_connect(*args, **kwargs)
        monkeypatch.setattr(speed_cache, 'connect', connect)
    params=dict(date_from=DAY_FROM,date_to=DAY_TO)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses=list(pool.map(lambda _:client.get('/api/output-speed',params),range(4)))
    if bootstrap_busy:
        assert any(response.status_code == 503 for response in responses)
    answers = []
    for response in responses:
        # Cold WAL/schema initialization can briefly contend on Windows. The
        # browser retries this 503; after the concurrent reads finish, it must
        # join the same durable job instead of creating another or serving data.
        if response.status_code == 503:
            assert 'publication is busy' in response.text, response.text
            response = client.get('/api/output-speed', params)
        assert response.status_code == 200, response.text
        answers.append(response.json())
    assert len({p['cache']['job_id'] for p in answers})==1
    assert all(p['cache']['state']=='building' and p['rows']==[] for p in answers)
    with closing(speed_cache.connect()) as c:
        assert c.execute('SELECT count(*) FROM speed_jobs').fetchone()[0]==1
    assert not any(k.startswith('output_speed_') for k in api._cache)



# --------------------------------------------------------------------------
# One response, one snapshot, one schema
# --------------------------------------------------------------------------


def _commit_a_second_file(kept, added, new_path):
    """A second sync of the same source, committing while a reader is mid-flight.

    It enumerates both files, because that is what a sync of a source does: the
    file already stored keeps its rows, and the new file's rows land in a commit
    that started after the reader had begun.
    """
    groups = {f"/fixture/{kept[0]['source']}.jsonl": list(kept), new_path: list(added)}
    signatures = []
    for path, rows in groups.items():
        digest = hashlib.sha1(
            json.dumps(rows, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:8]
        signatures.append((path, int(digest, 16), 1000 + len(rows)))
    store = UsageEntryStore()
    store.sync_files(
        kept[0]["source"],
        signatures,
        parser={"v": 1},
        parse_file_entries=lambda sig: list(groups[sig[0]]),
        durable=False,
    )
    _publish_fixture(kept+added,store)
    return store


def test_a_commit_between_the_two_reads_cannot_join_the_response(client, monkeypatch):
    """One response is one snapshot, and one connection is not a snapshot.

    Reported as a response that counted 1 measured call where 2 were eligible:
    a sync committed after the generation was read and before the rows were, and
    the two SELECTs answered from different commits. Without an explicit read
    transaction each statement opens an implicit one of its own, so the pin has
    to be taken before the first read and held to the last.
    """
    first = [
        _measured(_entry(source="kimi", model="racy", ts=_ms(DAY_FROM, 9)),
                  tokens=100, ms=1000.0),
    ]
    _seed(first)
    generation_before = UsageEntryStore().measurement_generation()

    late = [
        _measured(_entry(source="kimi", model="racy", ts=_ms(DAY_FROM, 10)),
                  tokens=400, ms=2_000.0),
    ]
    real = speed_report.across_model_rows
    state = {"inserted": False}

    def racing(conn, day_from, day_to, *, source=""):
        if not state["inserted"]:
            state["inserted"] = True
            _commit_a_second_file(first, late, "/fixture/kimi-late.jsonl")
        return real(conn, day_from, day_to, source=source)

    monkeypatch.setattr(speed_report, "across_model_rows", racing)
    payload = _across(client)
    assert state["inserted"], "the commit never landed inside the read"

    row = payload["rows"][0]
    assert row["speed_calls"] == 1, "the row committed mid-flight is not in this snapshot"
    assert row["speed_tokens"] == 100 and row["speed_ms"] == pytest.approx(1_000.0)
    # The denominator is read by a second SELECT, so it is the other half of the
    # reported symptom: a measured count from one commit and an eligible count
    # from the next answers "1 of 2 eligible", which describes a call population
    # no commit ever contained.
    assert row["eligible_calls"] == 1, "the denominator came from the snapshot, not the next commit"
    assert row["coverage"] == pytest.approx(1.0), (
        "coverage compares counts from one snapshot; a mixed pair invents a gap")
    assert payload["measurement_generation"] == generation_before, (
        "the generation names the snapshot the rows came from, not a later commit"
    )

    # The row is really there: the next request, on a snapshot of its own, says 2.
    after = _get(client, view="across-models", date_from=DAY_FROM, date_to=DAY_TO,
                 refresh=True)["rows"][0]
    assert after["speed_calls"] == 2 and after["speed_tokens"] == 500
    assert after["eligible_calls"] == 2 and after["coverage"] == pytest.approx(1.0)


def test_a_database_from_an_older_build_answers_pending_not_broken(client):
    """Schema 9 is readable history, but it has none of the timing columns yet.

    Reported as ``OperationalError: no such column: speed_kind``. A read-only route
    is not allowed to run the migration -- that is the sync's job -- so the answer
    has to be a retryable one. 503 is what the dashboard already backs off on, and
    the database must still be schema 9 when the request is done.
    """
    db = Path(api.usage_db_path())
    db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            """
            CREATE TABLE usage_entries (
                source TEXT NOT NULL, file_path TEXT NOT NULL, entry_key TEXT NOT NULL,
                model TEXT NOT NULL, provider TEXT NOT NULL, timestamp INTEGER NOT NULL,
                input INTEGER NOT NULL DEFAULT 0, output INTEGER NOT NULL DEFAULT 0,
                cache_read INTEGER NOT NULL DEFAULT 0, cache_write INTEGER NOT NULL DEFAULT 0,
                reasoning INTEGER NOT NULL DEFAULT 0, cost REAL NOT NULL DEFAULT 0,
                message_count INTEGER NOT NULL DEFAULT 1, raw_json TEXT NOT NULL,
                billing_json TEXT NOT NULL DEFAULT '', cost_authoritative INTEGER NOT NULL DEFAULT 0
            )"""
        )
        conn.execute(
            "INSERT INTO usage_entries VALUES "
            "('kimi','/old.jsonl','old1','k2','p',?,10,55,0,0,0,0.5,1,'{}','',0)",
            (_ms(DAY_FROM, 9),),
        )
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '9')")
        conn.commit()

    response = client.get(
        "/api/output-speed", params={"date_from": DAY_FROM, "date_to": DAY_TO}
    )
    assert response.status_code == 200, response.text
    assert response.json()["cache"]["state"] == "building"

    with closing(sqlite3.connect(str(db))) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(usage_entries)")}
        assert "speed_kind" not in columns, "a GET must not be the thing that migrates"
        assert conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "9"


# The local zone can only be moved where time.tzset() exists; Windows reads it
# from the OS and a TZ override there would assert nothing.
needs_tzset = pytest.mark.skipif(
    not hasattr(time, "tzset"),
    reason="the OS local zone cannot be overridden without time.tzset()",
)


def test_the_day_window_is_the_same_window_every_other_report_uses():
    """A day means what ``parse_date_range`` says, on whatever zone this box runs.

    The report's own day and hour buckets are local; a window anchored on UTC
    midnight disagrees with them at both ends, which is how the first hour of a
    day went missing from the speed table while it stayed on the Overview.
    """
    for day_from, day_to in ((DAY_FROM, DAY_TO), ("2026-07-01", "2026-07-01")):
        since, until = parse_date_range(day_from, day_to)
        assert speed_report._day_bounds_ms(day_from, day_to) == (
            int(since.timestamp() * 1000),
            int(until.timestamp() * 1000),
        )


@needs_tzset
def test_london_owns_the_first_hour_of_the_first_of_july(client, monkeypatch):
    """The reported case, at the instant the report is asked about it.

    London is on BST in July, so a July 1 window runs 23:00Z on the 30th to
    23:00Z on the 1st. Anchored on UTC it ran 00:00Z to 00:00Z instead: the first
    local hour of the selected day fell outside it, and an hour of the following
    day was read as part of the selection.
    """
    monkeypatch.setenv("TZ", "Europe/London")
    time.tzset()
    try:
        assert speed_report._day_bounds_ms("2026-07-01", "2026-07-01") == (
            _ms("2026-06-30", 23),
            _ms("2026-07-01", 23),
        )

        # Half past midnight local on the 1st is twenty minutes before one in the
        # morning UTC: the row belongs to the 1st, and a request for the 30th must
        # not answer for it.
        _seed([
            _measured(_entry(source="kimi", model="bst", ts=_ms("2026-06-30", 23, 30)),
                      tokens=100, ms=1_000.0),
        ])
        api._clear_cache()
        on_the_first = _get(client, view="across-models",
                            date_from="2026-07-01", date_to="2026-07-01")
        assert [r["model"] for r in on_the_first["rows"]] == ["bst"]
        assert on_the_first["rows"][0]["speed_calls"] == 1

        on_the_thirty = _get(client, view="across-models",
                             date_from="2026-06-30", date_to="2026-06-30", refresh=True)
        assert on_the_thirty["rows"] == []
    finally:
        monkeypatch.undo()
        time.tzset()
