"""Codex timing beyond the response marker: line clocks, client tools, and markers no snapshot carries."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from tokdash.output_speed_readers import CodexResponseAssociation
from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import CodexParser
from tokdash import sessions


@pytest.fixture(autouse=True)
def _timing_reader_mode():
    from tokdash.speed_mode import collect_timings
    with collect_timings():
        yield


BASE = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
EPOCH = int(BASE.timestamp() * 1000)
USAGE = {'input_tokens': 100, 'cached_input_tokens': 20, 'output_tokens': 60,
         'reasoning_output_tokens': 0, 'total_tokens': 160}
REASONED = {**USAGE, 'reasoning_output_tokens': 20}


def stamp(ms):
    """Codex's own format: whole milliseconds, UTC, a trailing Z."""
    t = BASE + timedelta(milliseconds=ms)
    return t.strftime('%Y-%m-%dT%H:%M:%S.') + f'{t.microsecond // 1000:03d}Z'


def line(ms, kind, **payload):
    return {'timestamp': stamp(ms), 'type': kind, 'payload': payload}


def event(ms, kind, **payload):
    return line(ms, 'event_msg', type=kind, **payload)


def turn(ms, name='turn'):
    return [event(ms, 'task_started', turn_id=name), line(ms, 'turn_context', turn_id=name, model='gpt-6')]


def user(ms):
    return line(ms, 'response_item', type='message', role='user')


def output(ms, kind='function_call'):
    if kind == 'message':
        return line(ms, 'response_item', type='message', role='assistant')
    return line(ms, 'response_item', type=kind)


def tool_output(ms):
    return line(ms, 'response_item', type='function_call_output')


def item(ms, item_type, lo, hi, identity, name='turn', **fields):
    return event(ms, 'item_completed', turn_id=name, item={'type': item_type, 'id': identity, **fields},
                 started_at_ms=EPOCH + lo, completed_at_ms=EPOCH + hi)


def snapshot(ms, usage=USAGE, total=USAGE):
    return event(ms, 'token_count', info={'last_token_usage': usage, 'total_token_usage': total})


def marker(ms, rid, usage=USAGE, total=USAGE, name='turn'):
    return line(ms, 'token_usage_record', turn_id=name, response_id=rid, usage=usage, thread_token_usage=total)


def timed(records):
    a = CodexResponseAssociation()
    calls = []
    for n, record in enumerate(records):
        a.observe(record)
        if record.get('type') == 'event_msg' and record['payload'].get('type') == 'token_count':
            a.register(f'call{n}', record['payload'].get('info') or {})
            calls.append(f'call{n}')
    speeds = a.resolve()
    return [speeds[c] for c in calls]


def window(speed):
    return speed['speed_status'], speed['speed_kind'], speed['speed_ms']


def test_a_log_without_item_clocks_times_each_request_from_its_sender():
    """Before 0.147: the sender's line to the response's last output line."""
    speeds = timed([*turn(0), user(10), output(1500, 'reasoning'), output(2500), tool_output(4000), snapshot(4001),
                    output(5000, 'reasoning'), output(6000, 'message'), snapshot(6001)])
    assert [window(s) for s in speeds] == [('measured', 'request_window', 2490.0),
                                           ('measured', 'request_window', 2000.0)]
    assert speeds[0]['speed_tokens'] == 60 and speeds[0]['speed_token_basis'] == 'output_including_reasoning'


def test_the_counted_snapshot_closes_a_response_wherever_it_lands():
    """Codex writes the snapshot before or after the tool result; both pair the same response."""
    before = timed([*turn(0), user(10), output(2500), snapshot(2501), tool_output(4000), output(6000, 'message'), snapshot(6001)])
    after = timed([*turn(0), user(10), output(2500), tool_output(4000), snapshot(4001), output(6000, 'message'), snapshot(6001)])
    assert [window(s) for s in before] == [window(s) for s in after] == [
        ('measured', 'request_window', 2490.0), ('measured', 'request_window', 2000.0)]


def test_item_clocks_time_a_response_ending_in_a_tool_call_from_its_first_model_item():
    """0.147-0.152: no response marker, but the reasoning item's clock covers the start."""
    speeds = timed([*turn(0), user(10), item(1500, 'Reasoning', 1000, 1500, 'r'), output(1500, 'reasoning'),
                    output(2500), tool_output(4000), snapshot(4001)])
    assert window(speeds[0]) == ('measured', 'response_window', 1500.0)


def test_a_marked_response_ending_in_a_tool_call_is_timed_like_its_message_ending_peers():
    """0.153+: the marker owns the snapshot but has no clock for the streamed call."""
    speeds = timed([*turn(0), user(10), item(1500, 'Reasoning', 1000, 1500, 'r'), output(1500, 'reasoning'),
                    output(2500), marker(2510, 'first'), tool_output(4000), snapshot(4001),
                    item(5000, 'AgentMessage', 4500, 5000, 'm'), output(5050, 'message'), marker(5060, 'second', total={
                        k: 2 * v for k, v in USAGE.items()}),
                    snapshot(5061, total={k: 2 * v for k, v in USAGE.items()})])
    assert window(speeds[0]) == ('measured', 'response_window', 1500.0)
    # The message ending keeps its marker's item-clock bracket, not the line clock (550).
    assert window(speeds[1]) == ('measured', 'response_window', 500.0)


def test_the_model_window_leaves_out_a_hosted_search_inside_it():
    speeds = timed([*turn(0), user(10), item(1500, 'Reasoning', 1000, 1500, 'r'), output(1500, 'reasoning'),
                    item(1600, 'WebSearch', 1200, 1400, 'search'), output(2500), tool_output(4000), snapshot(4001)])
    assert window(speeds[0]) == ('measured', 'response_window', 1300.0)


CLIENT_TOOL_CLOCKS = [
    # A background command started two responses ago, still running through this one.
    {'lo': -30_000, 'hi': 90_000, 'fields': {}},
    # A call that started as soon as it streamed, while the rest of the response streamed.
    {'lo': 1800, 'hi': 2400, 'fields': {}},
    # Clocks that cannot bound anything: collapsed with real work, or reversed.
    {'lo': 1800, 'hi': 1800, 'fields': {'duration_ms': 400}},
    {'lo': 2400, 'hi': 1800, 'fields': {}},
]


@pytest.mark.parametrize('clock', CLIENT_TOOL_CLOCKS)
def test_a_client_tool_running_alongside_a_response_takes_nothing_from_it(clock):
    """Within one response the model never waits on a client tool: it streams on."""
    tool = item(2450, 'CommandExecution', clock['lo'], clock['hi'], 'cmd', **clock['fields'])
    second = {k: 2 * v for k, v in USAGE.items()}
    speeds = timed([*turn(0), user(10), item(1500, 'Reasoning', 1000, 1500, 'r'), output(1500, 'reasoning'),
                    tool, output(2500), marker(2510, 'call'), tool_output(4000), snapshot(4001),
                    item(5000, 'AgentMessage', 4500, 5000, 'm'), output(5000, 'message'),
                    marker(5010, 'message', total=second), snapshot(5011, total=second)])
    assert [window(s) for s in speeds] == [('measured', 'response_window', 1500.0),
                                           ('measured', 'response_window', 500.0)]


@pytest.mark.parametrize('records,expected', [
    # Reasoning tokens with no reasoning clock: the message clock would leave them out.
    ([item(2500, 'AgentMessage', 1000, 2500, 'm'), output(2500, 'message'), snapshot(2501, REASONED, REASONED)],
     ('measured', 'request_window', 2490.0)),
    # A model clock that starts after the first output line does not cover it.
    ([output(1500), item(2400, 'AgentMessage', 2000, 2400, 'm'), output(2500, 'message'), snapshot(2501)],
     ('measured', 'request_window', 2490.0)),
    # A collapsed model clock cannot bound its item.
    ([item(1500, 'Reasoning', 1500, 1500, 'r'), output(1500, 'reasoning'), output(2500), snapshot(2501)],
     ('measured', 'request_window', 2490.0)),
])
def test_item_clocks_that_do_not_cover_the_response_fall_back_to_its_request(records, expected):
    assert window(timed([*turn(0), user(10), *records])[0]) == expected


@pytest.mark.parametrize('records,status', [
    # Two responses before one snapshot: neither owns it.
    ([user(10), output(1000), tool_output(2000), output(3000), snapshot(3001)], 'ambiguous_pair'),
    # No new input since the previous response: nothing marks when this request was sent.
    ([user(10), output(1000), snapshot(1001), output(3000, 'message'), snapshot(3001, total={k: 2 * v for k, v in USAGE.items()})], 'missing_timing'),
    ([user(10), event(500, 'stream_error'), output(1000), snapshot(1001)], 'retry_contaminated'),
    ([user(10), output(1000), event(1500, 'turn_aborted'), output(2000), snapshot(2001)], 'incomplete'),
    ([user(10), line(500, 'compacted'), output(1000), snapshot(1001)], 'incomplete'),
    ([user(10), output(10 + 600_001), snapshot(10 + 600_002)], 'invalid_timing'),
    # One stamp for the whole request (Codex 0.58-0.64 writes the snapshot first).
    ([user(10), output(10), snapshot(10)], 'invalid_timing'),
    # 60 tokens in 10 ms is not a model streaming.
    ([user(10), output(20), snapshot(21)], 'invalid_timing'),
    ([user(10), output(1000), snapshot(1001, {**USAGE, 'total_tokens': 170})], 'ambiguous_pair'),
])
def test_line_clock_exclusions(records, status):
    speeds = timed([*turn(0), *records])
    assert speeds[-1]['speed_status'] == status and speeds[-1]['speed_calls'] == 0


def test_a_snapshot_the_parser_cannot_bill_does_not_claim_the_response():
    speeds = timed([*turn(0), user(10), output(2500), event(2501, 'token_count', info=None), snapshot(2502)])
    assert speeds[0]['speed_status'] == 'missing_timing'
    assert window(speeds[1]) == ('measured', 'request_window', 2490.0)


def test_marker_exclusions_stand_over_line_clocks():
    """An excluded marked response is not re-timed from its lines."""
    speeds = timed([*turn(0), user(10), item(1500, 'AgentMessage', 1000, 1500, 'm'), output(1500, 'message'),
                    item(1600, 'FutureItem', 1550, 1600, 'x'), marker(1610, 'r'), snapshot(1611)])
    assert speeds[0]['speed_status'] == 'ambiguous_pair'


@pytest.mark.parametrize('kind,fields', [
    ('Extension', {'kind': 'clock.sleep', 'durationMs': 60000}),
    ('CollabAgentToolCall', {'tool': 'wait', 'status': 'completed'}),
    ('ImageView', {'path': 'file:///shot.png'}),
    ('SubAgentActivity', {'kind': 'interacted'}),
])
def test_newer_client_tool_items_are_not_unknown_items(kind, fields):
    """0.153 logs these after the call that ran them; they used to void the response."""
    speeds = timed([*turn(0), user(10), item(1500, 'Reasoning', 1000, 1500, 'r'), output(1500, 'reasoning'),
                    output(2500), marker(2510, 'first'), item(62_500, kind, 2520, 62_520, 'tool', **fields),
                    tool_output(62_530), snapshot(62_531)])
    assert window(speeds[0]) == ('measured', 'response_window', 1500.0)


def test_usage_and_session_readers_time_old_rollouts_alike(tmp_path):
    records = [line(0, 'session_meta', id='session', cwd='/fixture', cli_version='0.140.0'), *turn(0), user(10),
               output(1500, 'reasoning'), output(2500), tool_output(4000), snapshot(4001),
               output(6000, 'message'), snapshot(6001, total={k: 2 * v for k, v in USAGE.items()})]
    path = tmp_path / 'rollout.jsonl'
    path.write_text('\n'.join(map(json.dumps, records)) + '\n')
    parser = CodexParser(PricingDatabase())
    parser.sessions_dir = tmp_path
    parser.archived_sessions_dir = tmp_path / 'absent'
    entries = parser._parse_all()
    st = path.stat()
    raw = sessions._parse_codex_session_file(str(path), st.st_mtime_ns, st.st_size)
    assert [e['_speed'] for e in entries] == [t['_speed'] for t in raw['turns']]
    assert [e['_speed']['speed_ms'] for e in entries] == [2490.0, 2000.0]
    assert {e['_speed']['speed_kind'] for e in entries} == {'request_window'}


def test_each_measured_window_records_where_it_opened(tmp_path):
    """The snapshot of a tool-call ending lands behind the tool, not at the window's end."""
    second = {k: 2 * v for k, v in USAGE.items()}
    records = [line(0, 'session_meta', id='session', cwd='/fixture', cli_version='0.153.0'), *turn(0), user(10),
               item(1500, 'Reasoning', 1000, 1500, 'r'), output(1500, 'reasoning'), output(2500),
               marker(2510, 'first'), item(62_500, 'Extension', 2520, 62_520, 'sleep', kind='clock.sleep', durationMs=60000),
               tool_output(62_530), snapshot(62_531),
               item(64_000, 'AgentMessage', 63_000, 64_000, 'm'), output(64_000, 'message'),
               marker(64_010, 'second', total=second), snapshot(64_011, total=second)]
    path = tmp_path / 'rollout.jsonl'
    path.write_text('\n'.join(map(json.dumps, records)) + '\n')
    parser = CodexParser(PricingDatabase())
    parser.sessions_dir = tmp_path
    parser.archived_sessions_dir = tmp_path / 'absent'
    entries = parser._parse_all()
    assert [(e['_speed_start'] - EPOCH, e['_speed']['speed_ms']) for e in entries] == [(1000, 1500.0), (63_000, 1000.0)]
    assert entries[0]['timestamp'] - EPOCH == 62_531


# ---------------------------------------------------------------------------
# Markers no counted snapshot carries
# ---------------------------------------------------------------------------

ZERO = {k: 0 for k in USAGE}
COMPACTION = {'input_tokens': 1000, 'cached_input_tokens': 900, 'output_tokens': 50,
              'reasoning_output_tokens': 0, 'total_tokens': 1050}


def add(*usages):
    return {k: sum(u[k] for u in usages) for k in USAGE}


def message_response(at, name, rid, total, thread):
    """One message-ending response at `at`, timed 500 ms by its marker."""
    return [user(at + 10), item(at + 1500, 'AgentMessage', at + 1000, at + 1500, rid, name),
            output(at + 1500, 'message'), marker(at + 1510, rid, total=thread, name=name), snapshot(at + 1511, total=total)]


def compaction(at, thread, inline=False):
    record = {'response_id': 'compact', 'usage': COMPACTION, 'thread_token_usage': thread}
    compacted = line(at + 1, 'compacted', compaction_response_id='compact', latest_token_usage_record=record)
    return [compacted] if inline else [marker(at, 'compact', COMPACTION, thread), compacted]


def test_a_remote_compaction_no_snapshot_carries_shifts_every_later_total():
    """OpenAI models: Codex writes no snapshot for the compaction, and later totals exclude it (#222)."""
    speeds = timed([*turn(0), *message_response(0, 'turn', 'r1', USAGE, USAGE),
                    *compaction(2000, add(USAGE, COMPACTION)), *turn(2002, 't2'), snapshot(2003, ZERO, USAGE),
                    *message_response(3000, 't2', 'r2', add(USAGE, USAGE), add(USAGE, COMPACTION, USAGE)),
                    *message_response(6000, 't2', 'r3', add(USAGE, USAGE, USAGE), add(USAGE, COMPACTION, USAGE, USAGE))])
    # The zero snapshot after the compaction is never billed.
    assert [window(s) for s in speeds] == [('measured', 'response_window', 500.0), ('missing_timing', '', 0.0),
                                           ('measured', 'response_window', 500.0), ('measured', 'response_window', 500.0)]


def test_a_compaction_its_own_snapshot_carries_shifts_nothing():
    """Local providers: the snapshot after the compaction carries it."""
    speeds = timed([*turn(0), *message_response(0, 'turn', 'r1', USAGE, USAGE),
                    *compaction(2000, add(USAGE, COMPACTION)), snapshot(2002, COMPACTION, add(USAGE, COMPACTION)),
                    *turn(2003, 't2'), *message_response(3000, 't2', 'r2', add(USAGE, COMPACTION, USAGE), add(USAGE, COMPACTION, USAGE))])
    assert [s['speed_status'] for s in speeds] == ['measured', 'incomplete', 'measured']


def test_a_response_whose_turn_ended_before_its_snapshot_shifts_later_totals():
    speeds = timed([*turn(0), user(10), output(1500, 'message'), marker(1510, 'orphan', total=USAGE),
                    *turn(2000, 't2'), *message_response(3000, 't2', 'r2', USAGE, add(USAGE, USAGE))])
    assert window(speeds[0]) == ('measured', 'response_window', 500.0)


def test_a_window_that_opens_on_its_compaction_names_the_marker_inline():
    speeds = timed([*compaction(0, COMPACTION, inline=True), *turn(2, 't2'),
                    *message_response(1000, 't2', 'r1', USAGE, add(COMPACTION, USAGE))])
    assert window(speeds[0]) == ('measured', 'response_window', 500.0)


def test_a_total_gap_no_uncarried_marker_explains_stays_ambiguous():
    stray = {**COMPACTION, 'output_tokens': 51, 'total_tokens': 1051}
    speeds = timed([*turn(0), *message_response(0, 'turn', 'r1', USAGE, USAGE),
                    *compaction(2000, add(USAGE, COMPACTION)), *turn(2002, 't2'),
                    *message_response(3000, 't2', 'r2', add(USAGE, USAGE), add(USAGE, stray, USAGE))])
    assert speeds[-1]['speed_status'] == 'ambiguous_pair'
