"""Regression coverage for the six independently reproduced lazy-cache defects."""
import json
from contextlib import closing
from pathlib import Path
import pytest
from test_lazy_speed_cache import corpus, build_requested
from tokdash import speed_cache as cache, speed_worker, session_speed, compute, speed_process, speed_results
from tokdash.sources.coding_tools import CodingToolsUsageTracker
REAL_LAUNCH_WORKER = cache.launch_worker


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

def test_dead_worker_is_terminal_without_another_ensure(corpus):
    with closing(cache.connect()) as conn:
        conn.execute("INSERT INTO speed_jobs(id,request,state,created_at,updated_at,pid) VALUES('dead','{}','building',0,0,2147483647)")
        cache.put_meta(conn, worker_pid=2147483647)
        conn.commit()
    alive = cache._alive(2147483647)
    status = cache.job_status('dead')
    assert not alive
    assert status['state'] == 'error', 'SSE keeps waiting for a worker that no longer exists'

def test_missing_posix_resource_module_does_not_break_publication(corpus, monkeypatch):
    import builtins
    monkeypatch.setenv('TOKDASH_SPEED_CPU_FRACTION', '1')
    compute._sync_usage_store(CodingToolsUsageTracker())
    request = cache.ensure('2026-09-01', '2026-09-28', source='codex')
    original_import = builtins.__import__

    def no_resource(name, *args, **kwargs):
        if name == 'resource':
            raise ModuleNotFoundError("No module named 'resource'", name='resource')
        return original_import(name, *args, **kwargs)
    exception = None
    with closing(cache.connect()) as conn:
        row = conn.execute('SELECT * FROM speed_jobs WHERE id=?', (request['job_id'],)).fetchone()
        with monkeypatch.context() as patch:
            patch.setattr(builtins, '__import__', no_resource)
            try:
                speed_worker.run_job(conn, row, sync=False)
            except Exception as exc:
                exception = type(exc).__name__
        job = dict(conn.execute('SELECT * FROM speed_jobs WHERE id=?', (request['job_id'],)).fetchone())
        count = conn.execute('SELECT count(*) FROM speed_responses').fetchone()[0]
    assert job['state'] == 'ready', 'optional POSIX memory telemetry rolls back successful timing work'
    assert exception is None

def test_windows_liveness_probe_does_not_terminate_the_worker(monkeypatch):
    from types import SimpleNamespace
    worker = {'running': True}
    calls = []

    def windows_kill(pid, sig):
        calls.append((pid, sig))
        worker['running'] = False
    monkeypatch.setattr(cache, 'os', SimpleNamespace(name='nt', kill=windows_kill))
    reported_alive = cache._alive(2147483646)
    assert worker['running'], 'the liveness probe uses a terminating Windows signal'

def test_migration_removes_legacy_usage_speed_json(corpus):
    store, _ = compute._sync_usage_store(CodingToolsUsageTracker())
    with closing(store._connect()) as conn:
        row = conn.execute('SELECT id,raw_json FROM usage_entries LIMIT 1').fetchone()
        raw = json.loads(row['raw_json'])
        raw['_speed'] = dict(speed_tokens=60, speed_ms=1500, speed_calls=1, speed_kind='response_window', speed_token_basis='output_including_reasoning', speed_status='measured')
        conn.execute('UPDATE usage_entries SET raw_json=? WHERE id=?', (json.dumps(raw), row['id']))
        for field in cache.FIELDS:
            kind = 'REAL' if field == 'speed_ms' else 'INTEGER' if field in {'speed_tokens', 'speed_calls'} else 'TEXT'
            default = '0' if kind in {'REAL', 'INTEGER'} else "''"
            conn.execute(f'ALTER TABLE usage_entries ADD COLUMN {field} {kind} NOT NULL DEFAULT {default}')
        conn.execute("UPDATE meta SET value='11' WHERE key='schema_version'")
        conn.commit()
        selected = row['id']
    with closing(store._connect()) as conn:
        raw = json.loads(conn.execute('SELECT raw_json FROM usage_entries WHERE id=?', (selected,)).fetchone()[0])
        schema = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        fields = [r['name'] for r in conn.execute('PRAGMA table_info(usage_entries)')]
    assert '_speed' not in raw, 'copied legacy usage rows retain their timing JSON'

def test_session_build_reuses_history_without_rewriting_it(corpus, monkeypatch):
    import sqlite3
    from output_speed_expansion_fixtures import codex_call
    root, _, inventory = corpus
    monkeypatch.setenv('TOKDASH_SPEED_CPU_FRACTION', '1')
    with sqlite3.connect(root / 'opencode.db') as conn:
        template = conn.execute("SELECT data FROM message WHERE id='m1'").fetchone()[0]
        conn.executemany('INSERT INTO message VALUES(?,?,?,?,?)', ((f'history-{i}', 's', 1788264000000, 1788264002000, template) for i in range(1000)))
    compute._sync_usage_store(CodingToolsUsageTracker())
    request = {'from': '2026-09-01', 'to': '2026-09-28', 'source': ''}
    build_requested(request)
    new_file = Path(inventory['codex_file']).with_name('rollout-new-session.jsonl')
    records = [dict(type='session_meta', payload=dict(id='one-new-session', cwd='/fixture')), *codex_call(999)]
    new_file.write_text(''.join((json.dumps(row) + '\n' for row in records)))
    import tokdash.sources.coding_tools as readers
    readers._sig_cache.clear()
    store, _ = compute._sync_usage_store(CodingToolsUsageTracker())
    with closing(store.read_connection()) as primary:
        assert primary.execute('SELECT count(*) FROM usage_entries WHERE file_path=?', (str(new_file),)).fetchone()[0] == 1
        assert primary.execute("SELECT 1 FROM usage_session_inputs WHERE source='codex' AND session_id='one-new-session' AND file_path=?", (str(new_file),)).fetchone()
    observed = []
    original = speed_worker._progress

    def progress(conn, job, completed, total):
        observed.append(conn.execute('SELECT count(*) FROM speed_stage').fetchone()[0])
        return original(conn, job, completed, total)
    monkeypatch.setattr(speed_worker, '_progress', progress)
    with closing(cache.connect()) as conn:
        before = conn.execute('SELECT count(*) FROM speed_responses').fetchone()[0]
        changes = conn.total_changes
        speed_worker.build(conn, 'test', {**request, 'source': 'codex', 'session_id': 'one-new-session'}, sync=False)
        changes = conn.total_changes - changes
        final = conn.execute('SELECT count(*) FROM speed_responses').fetchone()[0]
    assert max(observed) <= 1, 'one-session build stages and rewrites unrelated cached history'
    assert final == before + 1
    assert changes < 100, 'one-session publication rewrote unrelated rows'


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


def test_pid_reuse_and_quiet_live_worker_health(corpus,monkeypatch):
    import os
    alive,token=speed_process.snapshot(os.getpid())
    assert alive and token
    assert cache._alive(os.getpid(),token)
    assert not cache._alive(os.getpid(),token+'reused')
    with closing(cache.connect()) as conn:
        for job,identity in [('quiet',token),('reused',token+'reused')]:
            conn.execute("INSERT INTO speed_jobs(id,request,state,created_at,updated_at,pid,worker_token) VALUES(?,'{}','building',0,0,?,?)",(job,os.getpid(),identity))
        conn.commit()
    assert cache.job_status('quiet')['state']=='building'  # No progress/heartbeat timeout.
    assert cache.job_status('reused')['state']=='error'


def test_windows_second_enqueue_reuses_worker_without_signalling(corpus,monkeypatch):
    from types import SimpleNamespace
    from tokdash import speed_cache
    monkeypatch.setattr(cache,'os',SimpleNamespace(name='nt',kill=lambda *a:pytest.fail('Windows process signalled')))
    monkeypatch.setattr(speed_process,'_windows_snapshot',lambda pid:(True,'windows:123'))
    monkeypatch.setattr(cache.subprocess,'Popen',lambda *a,**k:pytest.fail('second worker launched'))
    with closing(cache.connect()) as conn:
        cache.put_meta(conn,worker_pid=123,worker_token='windows:123');conn.commit()
        REAL_LAUNCH_WORKER(conn)


def test_optional_resource_rss_units(monkeypatch):
    import sys
    from types import SimpleNamespace
    monkeypatch.setitem(sys.modules,'resource',SimpleNamespace(RUSAGE_SELF=0,getrusage=lambda _:SimpleNamespace(ru_maxrss=1048576)))
    monkeypatch.setattr(sys,'platform','darwin')
    assert speed_worker.peak_rss_mb()==1
    monkeypatch.setattr(sys,'platform','linux')
    assert speed_worker.peak_rss_mb()==1024


def test_schema12_repair_preserves_accounting_when_input_is_gone(corpus):
    store,_=compute._sync_usage_store(CodingToolsUsageTracker())
    with closing(store._connect()) as conn:
        row=conn.execute('SELECT * FROM usage_entries LIMIT 1').fetchone()
        original=json.loads(row['raw_json'])
        legacy=dict(original,_speed={'speed_ms':999},output_speed=12,speed_ms=23,speed_kind='decode')
        conn.execute('UPDATE usage_entries SET raw_json=? WHERE id=?',(json.dumps(legacy),row['id']))
        conn.execute("UPDATE meta SET value='12' WHERE key='schema_version'")
        conn.commit()
    Path(row['file_path']).unlink()
    with closing(store._connect()) as conn:
        repaired=conn.execute('SELECT * FROM usage_entries WHERE id=?',(row['id'],)).fetchone()
        assert json.loads(repaired['raw_json'])==original
        assert dict(repaired,raw_json=row['raw_json'])==dict(row)


def test_actual_subscription_finishes_when_worker_dies(corpus):
    import asyncio
    from types import SimpleNamespace
    from tokdash.api import output_speed_job_events
    with closing(cache.connect()) as conn:
        conn.execute("INSERT INTO speed_jobs(id,request,state,created_at,updated_at,pid,worker_token) VALUES('subscription','{}','building',0,0,2147483647,'dead-instance')")
        conn.commit()
    async def disconnected():
        return False
    async def collect():
        response=await output_speed_job_events('subscription',SimpleNamespace(is_disconnected=disconnected))
        return [event async for event in response.body_iterator]
    events=asyncio.run(collect())
    assert len(events)==1 and 'worker interrupted' in events[0] and '"state":"error"' in events[0]


def test_result_cache_revision_filters_and_live_failure_metadata(corpus,monkeypatch):
    monkeypatch.setenv('TOKDASH_SPEED_CPU_FRACTION','1')
    compute._sync_usage_store(CodingToolsUsageTracker())
    request={'from':'2026-09-01','to':'2026-09-28','source':''}
    build_requested(request)
    speed_results.clear()
    original=cache.speed_report.across_model_rows; reads=[]
    def counted(*args,**kwargs):
        reads.append(kwargs.get('source')); return original(*args,**kwargs)
    monkeypatch.setattr(cache.speed_report,'across_model_rows',counted)
    a=cache.payload(request['from'],request['to'],cache_only=True)
    b=cache.payload(request['from'],request['to'],cache_only=True)
    assert a==b and len(reads)==1
    a['rows'].clear()  # Caller mutation must not corrupt the stored snapshot.
    assert cache.payload(request['from'],request['to'],cache_only=True)['rows']==b['rows']
    cache.payload(request['from'],request['to'],source='codex',cache_only=True)
    assert len(reads)==2
    with closing(cache.connect()) as conn:
        cache.record_failure(conn,'bad-codex',dict(request,source='codex'),'unreadable');conn.commit()
    failed=cache.payload(request['from'],request['to'],cache_only=True)
    assert failed['cache']['state']=='error' and failed['rows']==b['rows'] and len(reads)==2
    root,_,_=corpus
    before=cache.primary_generation()
    import sqlite3
    with sqlite3.connect(root/'opencode.db') as conn:
        conn.execute("UPDATE message SET data=json_set(data,'$.tokens.output',77) WHERE id='m1'");conn.commit()
    with closing(cache.connect()) as conn:
        speed_worker.build(conn,'test',dict(request,source='opencode',session_id='s'),sync=False)
    after=cache.payload(request['from'],request['to'],cache_only=True)
    assert cache.primary_generation()==before and len(reads)==3
    assert after['rows']!=b['rows'] and after['cache']['publication_revision']>b['cache']['publication_revision']
    assert after['cache']['state']=='error'  # Unrelated success does not erase Codex's failure.


def test_result_cache_coalesces_identical_reads_and_never_caches_failures(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    speed_results.clear(); entered=Event(); release=Event(); counts=[]
    def build():
        counts.append(1);entered.set();assert release.wait(5);return {'rows':[123]}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures=[pool.submit(speed_results.read,('same',),build) for _ in range(4)]
        assert entered.wait(5);release.set()
        assert [future.result() for future in futures]==[{'rows':[123]}]*4
    assert len(counts)==1
    def broken():
        raise OSError('broken snapshot')
    with pytest.raises(OSError):speed_results.read(('broken',),broken)
    assert speed_results.read(('broken',),lambda:{'rows':[456]})=={'rows':[456]}
    monkeypatch.setattr(speed_results,'MAX_ENTRIES',2)
    for i in range(3):speed_results.read(('bounded',i),lambda:{'rows':[i]})
    assert len(speed_results._values)==2
    assert speed_results._bytes<=speed_results.MAX_BYTES
    speed_results.clear()


def test_session_publication_keeps_unrelated_rowids_and_scales_in_sql_work(corpus,monkeypatch):
    import sqlite3
    import output_speed_expansion_fixtures as fixtures
    _,_,inventory=corpus
    monkeypatch.setenv('TOKDASH_SPEED_CPU_FRACTION','1')
    store,_=compute._sync_usage_store(CodingToolsUsageTracker())
    request={'from':'2026-09-01','to':'2026-09-28','source':''}
    build_requested(request)
    original=Path(inventory['codex_file']); new_file=original.with_name('one-short-session.jsonl')
    records=[dict(type='session_meta',payload=dict(id='one-short-session',cwd='/fixture')),*fixtures.codex_call(999)]
    new_file.write_text(''.join(json.dumps(row)+'\n' for row in records))
    from tokdash.sources.coding_tools import _sig_cache
    _sig_cache.clear();compute._sync_usage_store(CodingToolsUsageTracker())
    with closing(cache.connect()) as conn:
        base=list(conn.execute("SELECT rowid,* FROM speed_responses WHERE source='opencode'"))
        template=tuple(conn.execute("SELECT * FROM speed_responses WHERE source='opencode' LIMIT 1").fetchone())
        def add_history(first,last):
            for i in range(first,last):
                row=list(template); row[1]='unrelated-'+str(i);row[3]='unrelated'
                conn.execute('INSERT INTO speed_responses VALUES ('+','.join('?' for _ in row)+')',row)
                conn.execute("INSERT INTO speed_session_members VALUES('opencode',?,'unrelated',?,'main',0,1000,'recorded')",(row[1],row[1]))
            conn.commit()
        def measure():
            steps=[];conn.set_progress_handler(lambda:steps.append(1) or 0,100)
            changes=conn.total_changes
            speed_worker.build(conn,'test',dict(request,source='codex',session_id='one-short-session'),sync=False)
            conn.set_progress_handler(None,0)
            return len(steps)*100,conn.total_changes-changes
        add_history(0,1000);small_steps,small_changes=measure()
        # Remove only the selected timing input to repeat a cold bounded build.
        conn.execute("DELETE FROM speed_responses WHERE session_id='one-short-session'")
        conn.execute("DELETE FROM speed_session_members WHERE session_id='one-short-session'")
        conn.execute("DELETE FROM speed_inputs WHERE input_owner=?",(str(new_file),));conn.commit()
        add_history(1000,20000);large_steps,large_changes=measure()
        assert large_steps<=small_steps+1000, (small_steps,large_steps)
        assert large_changes<100 and small_changes<100
        assert [tuple(row) for row in conn.execute("SELECT rowid,* FROM speed_responses WHERE source='opencode' AND entry_key NOT LIKE 'unrelated-%'")]==[tuple(row) for row in base]


def test_changed_stream_mapping_publishes_even_when_response_scalars_match(corpus,monkeypatch):
    monkeypatch.setenv('TOKDASH_SPEED_CPU_FRACTION','1')
    compute._sync_usage_store(CodingToolsUsageTracker())
    request={'from':'2026-09-01','to':'2026-09-28','source':'codex','session_id':'bench-codex'}
    build_requested(request)
    original=speed_worker.session_mapping
    def changed(*args):
        return {key:(*values[:2],'changed-stream',*values[3:]) for key,values in original(*args).items()}
    monkeypatch.setattr(speed_worker,'session_mapping',changed)
    with closing(cache.connect()) as conn:
        conn.execute("UPDATE speed_inputs SET signature='' WHERE source='codex'");conn.commit()
        speed_worker.build(conn,'test',request,sync=False)
        assert {r[0] for r in conn.execute("SELECT stream_id FROM speed_session_members WHERE source='codex'")}=={'changed-stream'}


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


def test_atomic_failure_before_commit_keeps_unrelated_and_selected_last_good(corpus,monkeypatch):
    monkeypatch.setenv('TOKDASH_SPEED_CPU_FRACTION','1')
    compute._sync_usage_store(CodingToolsUsageTracker())
    request={'from':'2026-09-01','to':'2026-09-28','source':''}
    build_requested(request)
    with closing(cache.connect()) as conn:
        before=[tuple(row) for row in conn.execute('SELECT * FROM speed_responses ORDER BY source,entry_key')]
        generation=cache.meta(conn)['publication_revision']
        def broken(*args,**kwargs):
            if 'publication_revision' in kwargs:raise RuntimeError('publication interrupted')
            return original(*args,**kwargs)
        original=cache.put_meta;monkeypatch.setattr(cache,'put_meta',broken)
        job=cache.ensure(request['from'],request['to'],source='codex',refresh=True)['job_id']
        row=conn.execute('SELECT * FROM speed_jobs WHERE id=?',(job,)).fetchone()
        assert not speed_worker.run_job(conn,row,sync=False)
        assert before==[tuple(row) for row in conn.execute('SELECT * FROM speed_responses ORDER BY source,entry_key')]
        assert cache.meta(conn)['publication_revision']==generation


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


def test_recreated_database_instance_does_not_reuse_aggregate_snapshot(corpus,monkeypatch):
    import os
    import uuid
    monkeypatch.setenv('TOKDASH_SPEED_CPU_FRACTION','1')
    compute._sync_usage_store(CodingToolsUsageTracker())
    request={'from':'2026-09-01','to':'2026-09-28','source':''}
    build_requested(request)
    before=cache.payload(request['from'],request['to'],cache_only=True)
    with closing(cache.connect()) as conn:
        old=cache.meta(conn)['instance_id'];revision=cache.meta(conn)['publication_revision']
        # Model an in-place restore/recreation at the same pathname/inode/revision.
        cache.put_meta(conn,instance_id=uuid.uuid4().hex)
        conn.execute("UPDATE speed_responses SET speed_tokens=speed_tokens+1 WHERE source='codex' AND speed_calls>0")
        conn.commit()
        assert cache.meta(conn)['publication_revision']==revision and cache.meta(conn)['instance_id']!=old
    after=cache.payload(request['from'],request['to'],cache_only=True)
    assert after['rows']!=before['rows']


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
