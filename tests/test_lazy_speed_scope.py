"""How far a lazy-speed failure reaches, and how far a freshness check looks.

A failed input blocks only what depends on it: another source, another session
of the same source, or a ready session keeps building and reading. A failure is
recorded against the input it actually checked, so a changed input can retry,
and native freshness is checked once per build and per source rather than once
per session.
"""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from test_lazy_speed_cache import corpus, build_requested
from tokdash import speed_cache as cache, speed_worker, speed_native, session_speed, compute
from tokdash.sources.coding_tools import CodingToolsUsageTracker, _sig_cache

WINDOW = {'from': '2026-09-01', 'to': '2026-09-28'}


def run_requested(job):
    with closing(cache.connect()) as conn:
        row = conn.execute('SELECT * FROM speed_jobs WHERE id=?', (job,)).fetchone()
        return speed_worker.run_job(conn, row, sync=False)


def test_native_failed_member_does_not_poison_its_batch(corpus, monkeypatch):
    monkeypatch.setenv('TOKDASH_SPEED_CPU_FRACTION', '1')
    compute._sync_usage_store(CodingToolsUsageTracker())
    with sqlite3.connect(speed_native.paths()['opencode'][0]) as conn:
        conn.execute("UPDATE message SET session_id='A'")
        template = conn.execute("SELECT data FROM message WHERE id='m1'").fetchone()[0]
        conn.execute("INSERT INTO message VALUES('B-call','B',1788264000000,1788264002000,?)", (template,))
    original = speed_worker.native_rows

    def reader(source, sid=''):
        if source == 'opencode' and sid == 'A':
            raise OSError('only A is unreadable')
        yield from original(source, sid)

    monkeypatch.setattr(speed_worker, 'native_rows', reader)
    batch = [{'tool': 'opencode', 'session_id': sid} for sid in ('A', 'B')]
    state = session_speed.ensure_sessions(batch, WINDOW['from'], WINDOW['to'])
    assert not run_requested(state['cache']['job_id'])
    b = cache.ensure(WINDOW['from'], WINDOW['to'], source='opencode', session_id='B')
    assert b['job_id'] and b['state'] != 'error'
    b_batch = session_speed.ensure_sessions(batch, WINDOW['from'], WINDOW['to'])
    assert b_batch['cache']['job_id']
    with closing(cache.connect()) as conn:
        request = json.loads(conn.execute('SELECT request FROM speed_jobs WHERE id=?', (b_batch['cache']['job_id'],)).fetchone()[0])
        assert request['sessions'] == [batch[1]]
        assert cache.failure(conn, WINDOW['from'], WINDOW['to'], 'opencode') is not None
    assert run_requested(b['job_id'])
    summaries = session_speed.batch_summaries(batch, WINDOW['from'], WINDOW['to'])['sessions']
    assert [row['cache_state'] for row in summaries] == ['error', 'ready']
    assert session_speed.timeline('opencode', 'B', cache_only=True)['cache']['state'] == 'ready'
    assert session_speed.timeline('opencode', 'A', cache_only=True)['cache']['state'] == 'error'


def test_native_all_failure_applies_beyond_the_original_batch(corpus):
    compute._sync_usage_store(CodingToolsUsageTracker())
    request = dict(WINDOW, sessions=[{'tool': 'opencode', 'session_id': 'A'}])
    with closing(cache.connect()) as conn:
        cache.record_failure(conn, 'native-all', request, 'database unreadable',
                             {'source': 'opencode', 'input_owner': 'native:all',
                              'signature': json.dumps(speed_native.signature('opencode'))})
        conn.commit()
    assert cache.ensure(WINDOW['from'], WINDOW['to'], source='opencode', session_id='B')['state'] == 'error'


@pytest.mark.parametrize('change', ['late_native_write', 'file_removal'])
def test_final_validation_records_the_checked_input(corpus, monkeypatch, change):
    monkeypatch.setenv('TOKDASH_SPEED_CPU_FRACTION', '1')
    _, _, inventory = corpus
    compute._sync_usage_store(CodingToolsUsageTracker())
    db = speed_native.paths()['kilocode'][0]
    original = speed_worker.native_rows
    owner = inventory['codex_file'] if change == 'file_removal' else 'native:all'
    attempted = speed_worker.file_signature(owner) if change == 'file_removal' else json.dumps(speed_native.signature('kilocode'))
    generation = cache.primary_generation()

    def reader(source, sid=''):
        yield from original(source, sid)
        if source == 'opencode':
            if change == 'file_removal':
                Path(owner).unlink()
            else:
                with sqlite3.connect(db) as conn:
                    conn.execute("UPDATE message SET data=json_set(data,'$.tokens.output',77) WHERE id='m1'")

    monkeypatch.setattr(speed_worker, 'native_rows', reader)
    state = cache.ensure(WINDOW['from'], WINDOW['to'])
    assert not run_requested(state['job_id'])
    with closing(cache.connect()) as conn:
        failed = conn.execute('SELECT * FROM speed_failures WHERE job_id=?', (state['job_id'],)).fetchone()
        expected_source = 'codex' if change == 'file_removal' else 'kilocode'
        assert failed['source'] == expected_source
        assert json.loads(failed['scope']) == {'source': expected_source, 'input_owner': owner,
                                               'signature': attempted, 'usage_generation': generation}
    assert cache.ensure(WINDOW['from'], WINDOW['to'], source='opencode', session_id='s')['job_id']


def test_publication_failure_has_job_context_instead_of_last_input(corpus, monkeypatch):
    monkeypatch.setenv('TOKDASH_SPEED_CPU_FRACTION', '1')
    compute._sync_usage_store(CodingToolsUsageTracker())
    with monkeypatch.context() as patch:
        patch.setattr(cache, 'ensure_report_index', lambda conn: (_ for _ in ()).throw(sqlite3.OperationalError('publication unavailable')))
        state = cache.ensure(WINDOW['from'], WINDOW['to'])
        assert not run_requested(state['job_id'])
    with closing(cache.connect()) as conn:
        failed = conn.execute('SELECT * FROM speed_failures WHERE job_id=?', (state['job_id'],)).fetchone()
        assert failed['source'] == ''
        assert 'input_owner' not in json.loads(failed['scope'])
    retry = cache.ensure(WINDOW['from'], WINDOW['to'], refresh=True)
    assert run_requested(retry['job_id'])
    with closing(cache.connect()) as conn:
        assert not conn.execute('SELECT 1 FROM speed_failures').fetchone()
    assert cache.payload(WINDOW['from'], WINDOW['to'], cache_only=True)['cache']['state'] == 'ready'


@pytest.mark.parametrize('changed', [False, True])
def test_many_native_scopes_use_one_freshness_snapshot(corpus, monkeypatch, changed):
    from output_speed_expansion_fixtures import codex_call
    monkeypatch.setenv('TOKDASH_SPEED_CPU_FRACTION', '1')
    _, _, inventory = corpus
    compute._sync_usage_store(CodingToolsUsageTracker())
    build_requested(dict(WINDOW, source=''))
    signature = json.dumps(speed_native.signature('opencode'))
    generation = cache.primary_generation()
    with closing(cache.connect()) as conn:
        conn.executemany("INSERT INTO speed_inputs VALUES ('opencode',?,?,?,?,'ready',NULL,NULL)",
                         ((f'session:history-{i}', signature, cache.READER_VERSIONS['opencode'], generation) for i in range(1000)))
        conn.commit()
    new_file = Path(inventory['codex_file']).with_name('rollout-short-session.jsonl')
    new_file.write_text(''.join(json.dumps(row) + '\n' for row in [dict(type='session_meta', payload=dict(id='short-session', cwd='/fixture')), *codex_call(999)]))
    _sig_cache.clear()
    compute._sync_usage_store(CodingToolsUsageTracker())
    if changed:
        with sqlite3.connect(speed_native.paths()['opencode'][0]) as conn:
            conn.execute("UPDATE message SET data=json_set(data,'$.tokens.output',77) WHERE id='m1'")
    observed = []
    original = speed_native.signature

    def counted(source=None):
        observed.append(source)
        return original(source)

    monkeypatch.setattr(speed_native, 'signature', counted)
    with closing(cache.connect()) as conn:
        speed_worker.build(conn, 'short', dict(WINDOW, source='codex', session_id='short-session'), sync=False)
        pending = conn.execute("SELECT count(*) FROM speed_inputs WHERE source='opencode' AND state='pending'").fetchone()[0]
        assert pending == (1001 if changed else 0)
        counts = json.loads(cache.meta(conn)['last_build_counts'])
        assert counts['staged_rows'] == 1
        assert counts['native_freshness_scopes'] == (1001 if changed else 0)
    assert len(observed) < 20


def test_native_batch_rechecks_changes_between_sessions(corpus, monkeypatch):
    monkeypatch.setenv('TOKDASH_SPEED_CPU_FRACTION', '1')
    compute._sync_usage_store(CodingToolsUsageTracker())
    db = speed_native.paths()['opencode'][0]
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE message SET session_id='A'")
        template = conn.execute("SELECT data FROM message WHERE id='m1'").fetchone()[0]
        conn.execute("INSERT INTO message VALUES('B-call','B',1788264000000,1788264002000,?)", (template,))
    original = speed_worker.native_rows

    def reader(source, sid=''):
        yield from original(source, sid)
        if sid == 'B':
            with sqlite3.connect(db) as conn:
                conn.execute("UPDATE message SET data=json_set(data,'$.tokens.output',77) WHERE id='m1'")

    monkeypatch.setattr(speed_worker, 'native_rows', reader)
    state = session_speed.ensure_sessions([{'tool': 'opencode', 'session_id': sid} for sid in ('A', 'B')], WINDOW['from'], WINDOW['to'])
    assert not run_requested(state['cache']['job_id'])
    with closing(cache.connect()) as conn:
        assert not conn.execute('SELECT 1 FROM speed_responses').fetchone()


def test_an_unrelated_source_can_build_after_codex_read_failure(corpus, monkeypatch):
    monkeypatch.setenv('TOKDASH_SPEED_CPU_FRACTION', '1')
    compute._sync_usage_store(CodingToolsUsageTracker())
    request = {'from': '2026-09-01', 'to': '2026-09-28', 'source': 'codex'}
    build_requested(request)
    failure = cache.ensure(request['from'], request['to'], source='codex', refresh=True)
    with monkeypatch.context() as patch:
        patch.setattr(speed_worker, 'build', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('Codex read failed')))
        with closing(cache.connect()) as conn:
            row = conn.execute('SELECT * FROM speed_jobs WHERE id=?', (failure['job_id'],)).fetchone()
            assert not speed_worker.run_job(conn, row, sync=False)
    native = cache.ensure(request['from'], request['to'], source='opencode', session_id='s')
    batch = session_speed.ensure_sessions([{'tool': 'opencode', 'session_id': 's'}], request['from'], request['to'])
    assert native['job_id'] is not None, "an unrelated source was blocked by Codex's failure"


def test_ready_and_unindexed_sessions_are_isolated_from_another_source_failure(corpus,monkeypatch):
    monkeypatch.setenv('TOKDASH_SPEED_CPU_FRACTION','1')
    compute._sync_usage_store(CodingToolsUsageTracker())
    request={'from':'2026-09-01','to':'2026-09-28','source':'opencode','session_id':'s'}
    build_requested(request)
    with closing(cache.connect()) as conn:
        cache.record_failure(conn,'codex-error',dict(request,source='codex',session_id=''),'unreadable')
        conn.commit()
    assert cache.ensure(request['from'],request['to'],source='opencode',session_id='s')['state']=='ready'
    assert session_speed.batch_summaries([{'tool':'opencode','session_id':'s'}],request['from'],request['to'])['sessions'][0]['cache_state']=='ready'
    assert session_speed.timeline('opencode','s',cache_only=True)['cache']['state']=='ready'
    assert cache.ensure(request['from'],request['to'],source='kilocode',session_id='s')['job_id']
    assert cache.ensure(request['from'],request['to'],source='codex')['state']=='error'


def test_failed_file_does_not_block_other_sessions_and_changed_signature_permits_retry(corpus,monkeypatch):
    root,_,inventory=corpus
    monkeypatch.setenv('TOKDASH_SPEED_CPU_FRACTION','1')
    store,_=compute._sync_usage_store(CodingToolsUsageTracker())
    owner=inventory['codex_file']
    request={'from':'2026-09-01','to':'2026-09-28','source':'codex','session_id':'bench-codex'}
    with closing(cache.connect()) as conn:
        cache.record_failure(conn,'bad-file',request,'unreadable',dict(source='codex',input_owner=owner,signature=cache.input_signature(owner)))
        conn.commit()
    assert cache.ensure(request['from'],request['to'],source='codex',session_id='bench-codex')['state']=='error'
    assert cache.ensure(request['from'],request['to'],source='codex',session_id='different-session')['job_id']
    with Path(owner).open('a') as stream:
        stream.write('\n')
    assert cache.ensure(request['from'],request['to'],source='codex',session_id='bench-codex')['job_id']


def test_mixed_batch_still_builds_unaffected_session_and_dead_queued_job_is_terminal(corpus):
    compute._sync_usage_store(CodingToolsUsageTracker())
    request={'from':'2026-09-01','to':'2026-09-28','source':'codex'}
    with closing(cache.connect()) as conn:
        cache.record_failure(conn,'codex-failed',request,'unreadable')
        conn.execute("INSERT INTO speed_jobs(id,request,state,created_at,updated_at,pid,worker_token) VALUES('queued-dead','{}','queued',0,0,2147483647,'dead')");conn.commit()
    result=session_speed.ensure_sessions([{'tool':'codex','session_id':'bench-codex'},{'tool':'opencode','session_id':'s'}],request['from'],request['to'])
    assert result['cache']['job_id']
    with closing(cache.connect()) as conn:
        queued=json.loads(conn.execute('SELECT request FROM speed_jobs WHERE id=?',(result['cache']['job_id'],)).fetchone()[0])
        assert queued['sessions']==[{'tool':'opencode','session_id':'s'}]
    assert cache.job_status('queued-dead')['state']=='error'


def test_native_failure_uses_attempted_signature_so_changed_input_can_retry(corpus,monkeypatch):
    import sqlite3
    root,_,_=corpus
    compute._sync_usage_store(CodingToolsUsageTracker())
    original=speed_worker.native_rows
    def changed_during_read(source,sid=''):
        rows=list(original(source,sid))
        yield from rows
        with sqlite3.connect(root/'opencode.db') as writer:
            writer.execute("UPDATE message SET data=json_set(data,'$.tokens.output',88) WHERE id='m1'");writer.commit()
    monkeypatch.setattr(speed_worker,'native_rows',changed_during_read)
    state=cache.ensure('2026-09-01','2026-09-28',source='opencode',session_id='s')
    with closing(cache.connect()) as conn:
        row=conn.execute('SELECT * FROM speed_jobs WHERE id=?',(state['job_id'],)).fetchone()
        assert not speed_worker.run_job(conn,row,sync=False)
    # This is a new input population, not the failed snapshot, even at the same primary generation.
    retry=cache.ensure('2026-09-01','2026-09-28',source='opencode',session_id='s')
    assert retry['job_id'] and retry['state']!='error'


def test_failure_retains_attempted_usage_generation(corpus):
    compute._sync_usage_store(CodingToolsUsageTracker())
    target=cache.primary_generation()
    request={'from':'2026-09-01','to':'2026-09-28','source':'codex'}
    with closing(cache.connect()) as conn:
        cache.record_failure(conn,'older-attempt',request,'unreadable',dict(source='codex',usage_generation=target-1))
        conn.commit()
        assert cache.failure(conn,request['from'],request['to'],'codex',target=target) is None
        assert cache.failure(conn,request['from'],request['to'],'codex',target=target,current_only=False)
    assert cache.ensure(request['from'],request['to'],source='codex')['job_id']
