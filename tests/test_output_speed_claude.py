"""Claude Code request timing: one window per request, from its own transcript.

What the reader relies on, in order:

* a request is every assistant entry of one message id, timed once, on the row
  the usage parser bills;
* a leading thinking block with thinkingDurationMs opens the window at the first
  token; otherwise the window opens at the sending user entry (prompt or
  tool_result), never at an attachment written beside the response;
* retries, interruptions, error placeholders and chains with no sender of their
  own are excluded by name, and older entries without block indexes or thinking
  durations still time as whole requests;
* ordinary accounting carries the session id and nothing timed, and the session
  timeline gets main and subagent streams from the same rows.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from tokdash import clientpaths, compute, sessions, session_speed, speed_cache, speed_trend, speed_worker
from tokdash.compute import _collect_parser_file
from tokdash.output_speed_readers import ClaudeRequestAssociation
from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import ClaudeParser, CodingToolsUsageTracker
from tokdash.speed_mode import collect_timings

T0 = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
T0_MS = int(T0.timestamp() * 1000)
DAY = '2026-10-01'


def iso(ms):
    return (T0 + timedelta(milliseconds=ms)).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def user(uuid, parent, ms, *, tool_result=False, sid='s'):
    content = [{'type': 'tool_result', 'tool_use_id': 'tool', 'content': 'ok'}] if tool_result else 'go'
    rec = {'type': 'user', 'uuid': uuid, 'parentUuid': parent, 'timestamp': iso(ms), 'sessionId': sid,
           'message': {'role': 'user', 'content': content}}
    if tool_result:
        rec['toolUseResult'] = {'stdout': 'ok'}
    return rec


def block(uuid, parent, ms, mid, kind, *, index=None, think=None, out=400, stop='tool_use',
          model='claude-opus-5-5', sid='s', **extra):
    rec = {'type': 'assistant', 'uuid': uuid, 'parentUuid': parent, 'timestamp': iso(ms), 'sessionId': sid,
           'requestId': 'req_' + mid,
           'message': {'id': mid, 'role': 'assistant', 'model': model, 'content': [{'type': kind}],
                       'stop_reason': stop, 'usage': {'input_tokens': 10, 'output_tokens': out,
                                                      'cache_read_input_tokens': 0,
                                                      'cache_creation_input_tokens': 0}}}
    if index is not None:
        rec['apiBlockIndex'] = index
    if think is not None:
        rec['thinkingDurationMs'] = think
    rec.update(extra)
    return rec


def attachment(uuid, parent, ms):
    return {'type': 'attachment', 'uuid': uuid, 'parentUuid': parent, 'timestamp': iso(ms),
            'attachment': {'type': 'hook_success'}}


def system(uuid, parent, ms, subtype):
    return {'type': 'system', 'subtype': subtype, 'uuid': uuid, 'parentUuid': parent, 'timestamp': iso(ms),
            'retryInMs': 500, 'retryAttempt': 1, 'maxRetries': 10}


def write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in records), encoding='utf-8')
    return path


def parse(path, *, timing=True):
    st = path.stat()
    with collect_timings(timing):
        entries = _collect_parser_file(ClaudeParser(PricingDatabase()), (str(path), st.st_mtime_ns, st.st_size))
    return {e['entry_id']: e for e in entries}


def speed(tmp_path, records, mid='m1'):
    return parse(write(tmp_path / 'p' / 's.jsonl', records))['claude:' + mid]['_speed']


THINKING_FIRST = [
    user('u1', None, 0),
    block('a0', 'u1', 3000, 'm1', 'thinking', index=0, think=1500),
    block('a1', 'a0', 3600, 'm1', 'text', index=1),
    block('a2', 'a1', 5000, 'm1', 'tool_use', index=2),
]


def test_a_thinking_first_request_is_timed_from_its_first_token_once(tmp_path):
    rows = parse(write(tmp_path / 'p' / 's.jsonl', THINKING_FIRST))
    assert list(rows) == ['claude:m1']
    row = rows['claude:m1']
    assert row['_speed']['speed_kind'] == 'post_first_token'
    assert row['_speed']['speed_token_basis'] == 'output_including_reasoning'
    # Last block minus (first block minus its own thinking time).
    assert (row['_speed']['speed_ms'], row['_speed']['speed_tokens'], row['_speed']['speed_calls']) == (3500, 400, 1)
    # Every block carries the final usage, so the billed row is the first write.
    assert row['timestamp'] == T0_MS + 3000 and row['output'] == 400


def test_the_first_block_is_the_lowest_block_index_not_the_first_line(tmp_path):
    records = [THINKING_FIRST[0], THINKING_FIRST[2], THINKING_FIRST[1], THINKING_FIRST[3]]
    assert speed(tmp_path, records)['speed_ms'] == 3500


def test_a_request_window_opens_at_the_sender_not_its_attachments(tmp_path):
    s = speed(tmp_path, [
        user('u1', None, 0),
        attachment('x1', 'u1', 2999),
        attachment('x2', 'x1', 3000),
        block('a0', 'x2', 3000, 'm1', 'text', index=0),
        block('a1', 'a0', 4200, 'm1', 'tool_use', index=1),
    ])
    assert (s['speed_kind'], s['speed_ms'], s['speed_tokens']) == ('request_window', 4200, 400)


def test_a_tool_result_opens_the_next_request(tmp_path):
    rows = parse(write(tmp_path / 'p' / 's.jsonl', THINKING_FIRST + [
        user('r1', 'a2', 6000, tool_result=True),
        block('b0', 'r1', 7500, 'm2', 'tool_use', index=0, out=90),
    ]))
    assert rows['claude:m2']['_speed']['speed_kind'] == 'request_window'
    assert rows['claude:m2']['_speed']['speed_ms'] == 1500
    assert rows['claude:m1']['_speed']['speed_ms'] == 3500


@pytest.mark.parametrize('between', [
    [system('e1', 'u1', 500, 'api_error'), system('e2', 'e1', 1500, 'api_error')],
    [block('e1', 'u1', 900, 'synthetic', 'text', stop='stop_sequence', model='<synthetic>', out=0,
           isApiErrorMessage=True)],
    [system('e1', 'u1', 900, 'model_refusal_fallback'), system('e2', 'e1', 901, 'informational')],
])
def test_a_request_window_holding_a_retry_is_excluded(tmp_path, between):
    last = between[-1]['uuid']
    s = speed(tmp_path, [user('u1', None, 0), *between, block('a0', last, 4000, 'm1', 'text', index=0)])
    assert s['speed_status'] == 'retry_contaminated' and s['speed_calls'] == 0


def test_a_thinking_window_after_a_retry_starts_after_the_backoff(tmp_path):
    s = speed(tmp_path, [user('u1', None, 0), system('e1', 'u1', 500, 'api_error'),
                         block('a0', 'e1', 6000, 'm1', 'thinking', index=0, think=1000),
                         block('a1', 'a0', 7000, 'm1', 'text', index=1)])
    assert (s['speed_status'], s['speed_ms']) == ('measured', 2000)


def test_an_interrupted_request_is_incomplete(tmp_path):
    s = speed(tmp_path, [user('u1', None, 0), block('a0', 'u1', 3000, 'm1', 'thinking', index=0, think=900, stop=None),
                         block('a1', 'a0', 3500, 'm1', 'text', index=1, stop=None)])
    assert s['speed_status'] == 'incomplete' and s['speed_calls'] == 0


def test_a_synthetic_or_error_placeholder_is_never_timed():
    a = ClaudeRequestAssociation()
    a.observe(user('u1', None, 0))
    a.observe(block('a0', 'u1', 900, 'm1', 'text', model='<synthetic>'))
    a.observe(block('b0', 'u1', 900, 'm2', 'text', isApiErrorMessage=True))
    assert a.resolve('m1', 400)['speed_status'] == 'incomplete'
    assert a.resolve('m2', 400)['speed_status'] == 'incomplete'


def test_older_entries_without_block_index_or_thinking_time_are_whole_requests(tmp_path):
    rows = parse(write(tmp_path / 'p' / 's.jsonl', [
        user('u1', None, 0),
        block('a0', 'u1', 2000, 'm1', 'thinking', out=1, stop=None),
        block('a1', 'a0', 2500, 'm1', 'text', out=1, stop=None),
        block('a2', 'a1', 4000, 'm1', 'tool_use', out=300),
    ]))
    row = rows['claude:m1']
    assert (row['_speed']['speed_kind'], row['_speed']['speed_ms'], row['_speed']['speed_tokens']) == (
        'request_window', 4000, 300)
    # Cumulative usage: the fullest, last write is billed.
    assert row['timestamp'] == T0_MS + 4000 and row['output'] == 300


def test_a_chain_through_another_response_has_no_sender_of_its_own(tmp_path):
    s = speed(tmp_path, THINKING_FIRST + [attachment('x1', 'a2', 6000),
                                          block('b0', 'x1', 7000, 'm2', 'text', index=0)], mid='m2')
    assert s['speed_status'] == 'ambiguous_pair'


def test_a_sender_outside_this_transcript_is_missing_timing(tmp_path):
    assert speed(tmp_path, [block('a0', 'elsewhere', 3000, 'm1', 'text', index=0)])['speed_status'] == 'missing_timing'


def test_malformed_bookkeeping_cannot_abort_the_transcript(tmp_path):
    odd = [{'type': 'system', 'subtype': ['x'], 'uuid': {'x': 1}, 'parentUuid': ['y']},
           {'type': 'attachment', 'uuid': 'x1', 'parentUuid': {'z': 1}, 'timestamp': 7}]
    rows = parse(write(tmp_path / 'p' / 's.jsonl', odd + THINKING_FIRST))
    assert rows['claude:m1']['_speed']['speed_ms'] == 3500


def test_zero_or_negative_windows_are_invalid(tmp_path):
    s = speed(tmp_path, [user('u1', None, 5000), block('a0', 'u1', 5000, 'm1', 'text', index=0)])
    assert s['speed_status'] == 'invalid_timing'


def test_ordinary_accounting_reads_no_timing_and_bills_the_same_rows(tmp_path):
    path = write(tmp_path / 'p' / 's.jsonl', THINKING_FIRST)
    plain, timed = parse(path, timing=False), parse(path)
    assert set(plain) == set(timed)
    row = plain['claude:m1']
    assert not {'_speed', '_speed_start', '_stream_id'} & set(row) and row['_session_id'] == 's'
    strip = lambda e: {k: v for k, v in e.items() if k not in ('_speed', '_speed_start', '_stream_id')}
    assert strip(timed['claude:m1']) == row


def test_session_and_stream_follow_the_session_reader(tmp_path):
    main = write(tmp_path / 'p' / 'parent.jsonl', [dict(r, sessionId='parent') for r in THINKING_FIRST])
    sub = write(tmp_path / 'p' / 'parent' / 'subagents' / 'agent-x.jsonl', [
        dict(user('v1', None, 100), sessionId='parent', agentId='x', isSidechain=True),
        dict(block('c0', 'v1', 900, 'm9', 'text', index=0), sessionId='parent', agentId='x', isSidechain=True),
    ])
    for path in (main, sub):
        st = path.stat()
        raw = sessions._parse_claude_session_file(str(path), st.st_mtime_ns, st.st_size)
        rows = parse(path)
        assert {r['_session_id'] for r in rows.values()} == {raw['session_id']} == {'parent'}
        assert sorted(r['_stream_id'] for r in rows.values()) == sorted(t['_stream_id'] for t in raw['turns'])
        mapping = speed_worker.session_mapping('claude', None, list(rows.values()))
        assert {k: v[:3] for k, v in mapping.items()} == {
            k: ('parent', k, r['_stream_id']) for k, r in rows.items()}


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    root = tmp_path / 'claude' / 'projects'
    monkeypatch.setattr(clientpaths, 'claude_project_dirs', lambda: [root])
    original = CodingToolsUsageTracker.__init__

    def init(self):
        original(self)
        self.parsers = {k: v for k, v in self.parsers.items() if k == 'claude'}
    monkeypatch.setattr(CodingToolsUsageTracker, '__init__', init)
    monkeypatch.setattr(speed_cache, 'launch_worker', lambda conn: None)
    return root


def _run(payload):
    with closing(speed_cache.connect()) as c:
        row = c.execute('SELECT * FROM speed_jobs WHERE id=?', (payload['job_id'],)).fetchone()
        assert speed_worker.run_job(c, row, sync=False)


def test_model_rows_and_the_session_timeline_come_from_one_build(claude_home):
    write(claude_home / 'proj' / 'parent.jsonl', [dict(r, sessionId='parent') for r in THINKING_FIRST] + [
        dict(user('r1', 'a2', 6000, tool_result=True), sessionId='parent'),
        dict(block('b0', 'r1', 7500, 'm2', 'tool_use', index=0, out=90), sessionId='parent'),
    ])
    write(claude_home / 'proj' / 'parent' / 'subagents' / 'agent-x.jsonl', [
        dict(user('v1', None, 100), sessionId='parent', agentId='x'),
        dict(block('c0', 'v1', 900, 'm9', 'text', index=0, stop=None, out=4), sessionId='parent', agentId='x'),
    ])
    compute._sync_usage_store(CodingToolsUsageTracker())
    _run(speed_cache.ensure(DAY, DAY))
    report = speed_cache.payload(DAY, DAY, cache_only=True)
    assert report['cache']['state'] == 'ready'
    rows = {(r['source'], r['measurement_kind']): r for r in report['rows']}
    assert rows[('claude', 'post_first_token')]['speed_ms'] == 3500
    assert rows[('claude', 'request_window')]['speed_ms'] == 1500
    assert rows[('claude', 'post_first_token')]['unmeasured']['excluded_calls_by_reason'] == {'incomplete': 1}
    assert not [s for s in report['source_status'] if s['source'] == 'claude']

    timeline = session_speed.timeline('claude', 'parent', cache_only=True)
    assert timeline['cache']['state'] == 'ready'
    assert [(p['stream_id'], p['measurement_status']) for p in timeline['series']] == [
        ('main', 'measured'), ('main', 'measured'), ('x', 'incomplete')]
    assert session_speed.batch_summaries([{'tool': 'claude', 'session_id': 'parent'}], DAY, DAY)['sessions'][0][
        'cache_state'] == 'ready'


def test_claude_is_end_anchored_and_single_block_requests_are_exact():
    # Single-block (and pre-2.1.x cumulative) requests bill the write that closes
    # the window; a recorded start covers multi-block ones.
    assert 'claude' not in speed_trend.START_ANCHORED_SOURCES
    assert speed_trend.busy_interval('claude', T0_MS + 4200, 4200) == (T0_MS, T0_MS + 4200)
    assert speed_trend.busy_interval('claude', T0_MS + 3000, 3500, T0_MS + 1500) == (T0_MS + 1500, T0_MS + 5000)


def test_each_timed_request_records_where_its_window_opened(tmp_path):
    rows = parse(write(tmp_path / 'p' / 's.jsonl', THINKING_FIRST + [
        user('r1', 'a2', 6000, tool_result=True),
        block('b0', 'r1', 7500, 'm2', 'tool_use', index=0, out=90),
        user('r2', 'b0', 8000, tool_result=True),
        block('c0', 'r2', 9000, 'm3', 'text', index=0, stop=None),
    ]))
    assert rows['claude:m1']['_speed_start'] == T0_MS + 1500  # first block minus its thinking
    assert rows['claude:m2']['_speed_start'] == T0_MS + 6000  # the tool result that sent it
    assert '_speed_start' not in rows['claude:m3']


def test_sequential_requests_never_overlap_in_combined_throughput(claude_home):
    # The billed write is each request's first block. m3's tail after it is long,
    # so an end anchor would slide m3 back over m1 and claim parallel work.
    write(claude_home / 'proj' / 'parent.jsonl', [dict(r, sessionId='parent') for r in THINKING_FIRST + [
        user('r1', 'a2', 5100, tool_result=True),
        block('b0', 'r1', 5200, 'm3', 'thinking', index=0, think=100),
        block('b1', 'b0', 9000, 'm3', 'tool_use', index=1),
    ]])
    compute._sync_usage_store(CodingToolsUsageTracker())
    _run(speed_cache.ensure(DAY, DAY))
    row, = speed_cache.payload(DAY, DAY, source='claude', cache_only=True)['rows']
    assert (row['speed_ms'], row['busy_ms'], row['parallelism'], row['peak_in_flight']) == (7400, 7400, 1.0, 1)
    trend = speed_cache.payload(DAY, DAY, view='trend', source='claude', cache_only=True)
    assert [p for p in trend['series'][0]['parallelism'] if p is not None] == [1.0]
    # A rebuild that reuses the published rows keeps their starts.
    with closing(speed_cache.connect()) as c:
        c.execute("UPDATE speed_meta SET value='0' WHERE key='published_generation'"); c.commit()
    _run(speed_cache.ensure(DAY, DAY))
    with closing(speed_cache.connect(readonly=True)) as c:
        assert json.loads(speed_cache.meta(c)['last_build_counts'])['reused_files'] == 1
        assert sorted(r[0] for r in c.execute("SELECT speed_start FROM speed_responses")) == [T0_MS + 1500, T0_MS + 5100]
    assert speed_cache.payload(DAY, DAY, source='claude', cache_only=True)['rows'][0]['busy_ms'] == 7400


def test_a_schema3_cache_gains_speed_start_in_place(tmp_path):
    path = tmp_path / 'output_speed.sqlite3'
    columns = speed_cache.COLUMNS.split(',')
    with closing(sqlite3.connect(path)) as c:
        c.executescript('''
          CREATE TABLE speed_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE speed_sessions(source TEXT,session_id TEXT,usage_generation INTEGER,state TEXT,PRIMARY KEY(source,session_id));
          CREATE TABLE speed_jobs(id TEXT PRIMARY KEY,request TEXT NOT NULL,state TEXT NOT NULL,
            completed_inputs INTEGER NOT NULL DEFAULT 0,total_inputs INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,updated_at REAL NOT NULL,error TEXT,pid INTEGER);
        ''')
        c.execute('CREATE TABLE speed_responses(' + ','.join(columns) + ',PRIMARY KEY(source,entry_key))')
        c.execute('CREATE INDEX idx_speed_report ON speed_responses(source,timestamp,model,eligible,speed_kind,'
                  'speed_token_basis,speed_status,speed_tokens,speed_ms,speed_calls)')
        c.execute('INSERT INTO speed_responses VALUES (' + ','.join('?' * len(columns)) + ')',
                  ('codex', 'k', 'f', 's', 5000, 'gpt-6', 60, 0, 1, 60, 2000.0, 1, 'response_window',
                   'output_including_reasoning', 'measured'))
        c.commit()
    with closing(speed_cache.connect(path, readonly=True)) as c:
        (_key, _ts, begin, end, *_), = speed_trend._responses(c, 0, 10_000, '')
        assert (begin, end) == (3000, 5000)  # read before the upgrade: the anchor
    with closing(speed_cache.connect(path)) as c:
        assert 'speed_start' in {r[1] for r in c.execute('PRAGMA table_info(speed_responses)')}
        speed_cache.ensure_report_index(c)
        assert 'speed_start' in {r[2] for r in c.execute("PRAGMA index_info('idx_speed_report')")}
        plan = ' '.join(r[3] for r in c.execute('EXPLAIN QUERY PLAN SELECT speed_start,speed_ms FROM speed_responses'
                                                ' INDEXED BY idx_speed_report WHERE timestamp>0'))
        assert 'COVERING INDEX idx_speed_report' in plan
        assert [r[2:4] for r in speed_trend._responses(c, 0, 10_000, '')] == [(3000, 5000)]
