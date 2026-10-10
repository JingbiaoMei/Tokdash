"""Contradictory late clocks must reject timing without moving response ownership."""
import pytest
from tokdash.output_speed_readers import CodexResponseAssociation, dsh_stream_timing


USAGE = {'input_tokens': 100, 'cached_input_tokens': 20, 'output_tokens': 60,
         'reasoning_output_tokens': 0, 'total_tokens': 160}


def event(kind, **payload):
    return {'type': 'event_msg', 'payload': {'type': kind, **payload}}


def item(identity, end):
    return event('item_completed', turn_id='turn',
                 item={'type': 'AgentMessage', 'id': identity},
                 started_at_ms=10, completed_at_ms=end)


def settle(a, identity):
    a.observe({'type': 'token_usage_record', 'payload': {
        'turn_id': 'turn', 'response_id': identity, 'usage': USAGE,
        'thread_token_usage': USAGE}})


@pytest.mark.parametrize('phase', ['before_marker', 'before_register', 'after_register', 'next_response'])
def test_conflicting_model_clock_invalidates_its_owning_response(phase):
    a = CodexResponseAssociation()
    a.observe(event('task_started', turn_id='turn'))
    a.observe(item('text', 100))
    if phase == 'before_marker':
        a.observe(item('text', 500))
    settle(a, 'response')
    if phase == 'before_register':
        a.observe(item('text', 500))
    a.register('call', {'last_token_usage': USAGE, 'total_token_usage': USAGE})
    if phase == 'after_register':
        a.observe(item('text', 500))
    if phase == 'next_response':
        a.observe(item('next-text', 200))
        a.observe(item('text', 500))
        settle(a, 'next-response')
        a.register('next-call', {'last_token_usage': USAGE, 'total_token_usage': USAGE})
    result = a.resolve()
    assert result['call']['speed_status'] == 'ambiguous_pair'
    assert result['call']['speed_calls'] == 0
    if phase == 'next_response':
        assert result['next-call']['speed_status'] == 'measured'
        assert result['next-call']['speed_ms'] == 190
    assert a.calls['call'][0]['usage'] == USAGE


def test_identical_late_clock_keeps_the_original_owner():
    a = CodexResponseAssociation()
    a.observe(event('task_started', turn_id='turn'))
    a.observe(item('text', 100)); settle(a, 'response')
    a.register('call', {'last_token_usage': USAGE, 'total_token_usage': USAGE})
    a.observe(item('text', 100))
    assert a.resolve()['call']['speed_ms'] == 90


@pytest.mark.parametrize('end,status', [(25, 'invalid_timing'), (30, 'measured'), (35, 'measured')])
def test_dsh_usage_boundary_follows_every_observed_output_clock(end, status):
    usage = {'outputTokens': 60}
    stream = [{'type': 'chunk', 'time': t, 'chunk': {'type': 'text-delta'}}
              for t in (10, 30, 20)]
    stream.append({'type': 'chunk', 'time': end, 'chunk': {'type': 'usage', 'usage': usage}})
    result = dsh_stream_timing(stream, usage)
    assert result['speed_status'] == status
    assert usage == {'outputTokens': 60}
    if status == 'measured':
        assert result['speed_ms'] == end - 10
