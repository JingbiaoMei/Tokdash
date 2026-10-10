"""Isolation, canonical publication and session parity for the derived index."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path
import sys

import pytest
from tokdash import speed_cache as cache, speed_worker, session_speed, compute, sessions, speed_native
from tokdash.sources.coding_tools import CodingToolsUsageTracker
from tokdash.usage_store import UsageEntryStore
from tokdash.speed_mode import collect_timings

@pytest.fixture
def corpus(tmp_path,monkeypatch):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
    import speed_fixtures, output_speed_expansion_fixtures
    from bench_tokdash_server import _prune_parsers
    root=tmp_path/'corpus';data=tmp_path/'data'
    inventory=output_speed_expansion_fixtures.build(root,20)
    for k,v in speed_fixtures.isolated_env(root,data,tmp_path/'xdg').items():
        monkeypatch.setenv(k,v)
    original=CodingToolsUsageTracker.__init__
    monkeypatch.setattr(CodingToolsUsageTracker,'__init__',original)
    from tokdash import clientpaths
    for name in ('codex_sessions_dir','codex_archived_sessions_dir','dsh_sessions_dir','qwen_runtime_base','opencode_db_path','kilo_db_paths','mimocode_db_path'):
        monkeypatch.setattr(clientpaths,name,getattr(clientpaths,name))
    monkeypatch.setattr(sessions,'SESSION_TOOLS',sessions.SESSION_TOOLS)
    _prune_parsers({'codex','dsh','qwen_code','opencode','kilocode','mimo'})
    # Restore the registry after the test; path overrides are restored separately.
    monkeypatch.setattr(cache,'launch_worker',lambda conn:None)
    yield root,data,inventory


def build_requested(request):
    with closing(cache.connect()) as c:
        c.execute("INSERT INTO speed_jobs(id,request,state,created_at,updated_at) VALUES('test',?,'building',0,0)",(json.dumps(request),));c.commit()
        speed_worker.build(c,'test',request,sync=False)


def test_overview_ingest_never_constructs_timing_helpers_or_opens_speed_database(corpus,monkeypatch):
    root,data,_=corpus
    import tokdash.sources.coding_tools as readers
    def forbidden(*args,**kwargs):
        raise AssertionError('ordinary accounting invoked a timing reader')
    for name in ('CodexResponseAssociation','KimiStepAssociation','omp_message_timing','QwenTimingAssociation','native_message_timing'):
        monkeypatch.setattr(readers,name,forbidden)
    store,_=compute._sync_usage_store(CodingToolsUsageTracker())
    assert store.query_entries()
    assert not cache.cache_path().exists()
    with store.read_connection() as c:
        assert not any(r[1].startswith('speed_') for r in c.execute('PRAGMA table_info(usage_entries)'))
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='idx_usage_entries_speed_time'").fetchone()


def test_atomic_generation_counts_cached_bounds_and_session_model_parity(corpus,monkeypatch):
    root,data,_=corpus
    store,_=compute._sync_usage_store(CodingToolsUsageTracker())
    before=store.query_entries()
    request={'from':'2026-09-01','to':'2026-09-28','source':''}
    build_requested(request)
    monkeypatch.setenv('TOKDASH_SPEED_CPU_FRACTION','1')
    payload=cache.payload(request['from'],request['to'])
    assert payload['cache']['state']=='ready'
    counts={r['source']:r['speed_calls'] for r in payload['rows']}
    assert counts=={'codex':20,'dsh':2,'qwen_code':2,'opencode':19,'kilocode':19,'mimo':19}
    assert store.query_entries()==before
    rows=session_speed.batch_summaries([{'tool':'codex','session_id':'bench-codex'}],request['from'],request['to'])['sessions']
    assert rows[0]['speed_calls']==20
    timeline=session_speed.timeline('codex','bench-codex',date_from=request['from'],date_to=request['to'])
    assert timeline['summary']['speed_calls']==rows[0]['speed_calls']
    assert sum(p['speed_tokens'] for p in timeline['series'])==next(r['speed_tokens'] for r in payload['rows'] if r['source']=='codex')
    assert all(p['turn_key'] for p in timeline['series'])
    with closing(cache.connect()) as c:
        scans={k:v for k,v in cache.meta(c).items() if k.endswith('bounds_scans')}
    monkeypatch.setattr(speed_worker,'native_rows',lambda *args: (_ for _ in ()).throw(AssertionError('native rescan')))
    monkeypatch.setattr(speed_worker,'_collect_parser_file',lambda *args,**kwargs: (_ for _ in ()).throw(AssertionError('file reparse')))
    with closing(cache.connect()) as c:
        speed_worker.build(c,'test',request,sync=False)
        assert scans=={k:v for k,v in cache.meta(c).items() if k.endswith('bounds_scans')}


def test_cache_only_batch_does_not_create_database_or_start_build(corpus):
    result=session_speed.batch_summaries([{'tool':'codex','session_id':'bench-codex'}],'2026-09-01','2026-09-28')
    assert result['sessions'][0]['cache_state']=='not-indexed'
    assert not cache.cache_path().exists()


def test_cached_session_summary_work_is_bounded_by_its_membership(corpus):
    with closing(cache.connect()) as conn:
        def add_response(key,sid,tokens,ms,measured):
            conn.execute('''INSERT INTO speed_responses VALUES
                ('codex',?,'fixture',?,1000,'gpt-6',?,0,1,?,?,?,'decode','output','measured')''',
                (key,sid,tokens,tokens if measured else 0,ms if measured else 0,int(measured)))
            conn.execute('''INSERT INTO speed_session_members VALUES
                ('codex',?,?,?,'main',0,1000,'recorded')''',(key,sid,key))
        add_response('selected-1','selected',20,100,True)
        add_response('selected-2','selected',60,900,True)
        add_response('selected-unknown','selected',10,0,False)
        def measured_summary():
            steps=[]
            conn.set_progress_handler(lambda:steps.append(1) or 0,1)
            try:
                result=session_speed._session_summary(conn,'codex','selected',0,2000)
            finally:
                conn.set_progress_handler(None,0)
            return result,len(steps)
        before,small_steps=measured_summary()
        for index in range(2000):
            add_response(f'unrelated-{index}',f'other-{index}',999,999,True)
        after,large_steps=measured_summary()
        assert after==before
        assert after['speed_calls']==2 and after['eligible_calls']==3
        assert after['output_tok_per_s']==80 and after['coverage']==pytest.approx(2/3)
        assert large_steps<=small_steps+100, 'summary scanned unrelated responses or memberships'


def test_cache_only_model_and_timeline_do_not_create_a_missing_cache(corpus,monkeypatch):
    monkeypatch.setattr(cache,'ensure',lambda *a,**kw:pytest.fail('snapshot ensured work'))
    report=cache.payload('2026-09-01','2026-09-28',cache_only=True,refresh=True)
    detail=session_speed.timeline('codex','bench-codex',cache_only=True,refresh=True)
    assert report['rows']==[] and detail['series']==[]
    assert report['cache']['job_id'] is None and detail['cache']['job_id'] is None
    assert report['cache']['state']==detail['cache']['state']=='stale'
    assert not cache.cache_path().exists()


@pytest.mark.parametrize('changed',['native','generation'])
def test_terminal_snapshot_reads_stale_publication_without_rebuilding(corpus,monkeypatch,changed):
    compute._sync_usage_store(CodingToolsUsageTracker())
    request={'from':'2026-09-01','to':'2026-09-28','source':''}
    build_requested(request)
    before=cache.payload(request['from'],request['to'],cache_only=True)
    if changed=='native':
        monkeypatch.setattr(cache,'identity',lambda:'changed native DB/WAL identity')
    else:
        target=cache.primary_generation()
        monkeypatch.setattr(cache,'primary_generation',lambda:target+1)
    monkeypatch.setattr(cache,'ensure',lambda *a,**kw:pytest.fail('terminal snapshot ensured work'))
    monkeypatch.setattr(cache,'launch_worker',lambda *a:pytest.fail('terminal snapshot launched worker'))
    # A concurrent publication writer does not block these read transactions.
    with closing(cache.connect()) as writer:
        writer.execute('BEGIN IMMEDIATE')
        jobs=writer.execute('SELECT count(*) FROM speed_jobs').fetchone()[0]
        statements=[]
        original=cache.connect
        def traced(*a,**kw):
            assert kw.get('readonly') is True
            conn=original(*a,**kw);conn.set_trace_callback(statements.append);return conn
        monkeypatch.setattr(cache,'connect',traced)
        report=cache.payload(request['from'],request['to'],cache_only=True)
        detail=session_speed.timeline('codex','bench-codex',cache_only=True)
        assert report['rows']==before['rows']
        assert detail['summary']['speed_calls']==20
        assert report['cache']['state']==detail['cache']['state']=='stale'
        assert report['cache']['job_id'] is None and detail['cache']['job_id'] is None
        assert report['measurement_generation']==before['measurement_generation']
        assert writer.execute('SELECT count(*) FROM speed_jobs').fetchone()[0]==jobs
        assert not any(s.lstrip().upper().startswith(('BEGIN IMMEDIATE','INSERT','UPDATE','DELETE')) for s in statements)
        writer.rollback()


def test_terminal_snapshot_preserves_failure_and_last_good_rows(corpus,monkeypatch):
    compute._sync_usage_store(CodingToolsUsageTracker())
    request={'from':'2026-09-01','to':'2026-09-28','source':''}
    build_requested(request)
    before=cache.payload(request['from'],request['to'],cache_only=True)
    with closing(cache.connect()) as conn:
        cache.record_failure(conn,'test',request,'timing input unreadable')
        conn.execute("UPDATE speed_jobs SET state='error',error='timing input unreadable'");conn.commit()
    result=cache.payload(request['from'],request['to'],cache_only=True)
    assert result['rows']==before['rows']
    assert result['cache']['state']=='error'
    assert result['cache']['error']=='timing input unreadable'
    assert result['cache']['job_id'] is None


def test_covering_report_index_preserves_both_views_and_upgrades_only_in_worker(corpus,monkeypatch):
    from tokdash import speed_report
    compute._sync_usage_store(CodingToolsUsageTracker())
    request={'from':'2026-09-01','to':'2026-09-28','source':''}
    build_requested(request)
    across=cache.payload(request['from'],request['to'])
    row=next(r for r in across['rows'] if r['source']=='codex')
    options=dict(view='time-of-day',source=row['source'],model=row['model'],
                 kind=row['measurement_kind'],basis=row['token_basis'])
    hourly=cache.payload(request['from'],request['to'],**options)
    with closing(cache.connect()) as c:
        hint=speed_report._report_index_hint(c)
        plan=' '.join(r[3] for r in c.execute('EXPLAIN QUERY PLAN SELECT source,model,speed_tokens,speed_ms FROM speed_responses'+hint+' WHERE timestamp>0'))
        assert 'COVERING INDEX idx_speed_report' in plan
        c.execute('DROP INDEX idx_speed_report');c.commit()
    # A request serves the linear legacy fallback, without doing schema work.
    assert cache.payload(request['from'],request['to'])==across
    assert cache.payload(request['from'],request['to'],**options)==hourly
    with closing(cache.connect()) as c:
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='idx_speed_report'").fetchone()
        monkeypatch.setattr(speed_worker,'_collect_parser_file',lambda *a,**kw: (_ for _ in ()).throw(AssertionError('unchanged input reparsed')))
        speed_worker.build(c,'test',request,sync=False)
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='idx_speed_report'").fetchone()
    assert cache.payload(request['from'],request['to'])['rows']==across['rows']


def test_first_get_enqueues_and_returns_building_without_parsing(corpus,monkeypatch):
    compute._sync_usage_store(CodingToolsUsageTracker())
    monkeypatch.setattr(speed_worker,'build',lambda *a,**kw: (_ for _ in ()).throw(AssertionError('synchronous build')))
    payload=cache.payload('2026-09-01','2026-09-28')
    assert payload['cache']['state']=='building'
    assert payload['rows']==[]
    assert payload['cache']['job_id']


def test_unrelated_pending_or_failure_does_not_relabel_published_measurements(corpus,monkeypatch):
    compute._sync_usage_store(CodingToolsUsageTracker())
    request={'from':'2026-09-01','to':'2026-09-28','source':''}
    build_requested(request)
    with closing(cache.connect()) as c:
        c.execute("UPDATE speed_responses SET speed_calls=0,speed_tokens=0,speed_ms=0,speed_status='missing_timing' WHERE source='dsh'")
        c.execute("DELETE FROM speed_responses WHERE source='qwen_code'")
        c.execute("INSERT INTO speed_source_days VALUES('claude_code','2026-09-01',1)")
        c.commit()
    for state in ('building','stale','error'):
        if state=='error':
            with closing(cache.connect()) as c:
                cache.record_failure(c,'qwen-failure',dict(request,source='qwen_code'),'timing input unreadable');c.commit()
        monkeypatch.setattr(cache,'ensure',lambda *a,_state=state,**kw:{'state':_state,'published_generation':1})
        payload=cache.payload(request['from'],request['to'])
        statuses={r['source']:r['status'] for r in payload['source_status']}
        assert 'codex' not in statuses and 'opencode' not in statuses
        assert statuses['dsh']=='timing_unavailable'
        assert statuses['qwen_code']==('read_failure' if state=='error' else 'pending_reprocessing')
        assert statuses['claude_code']=='unsupported_reader'
    with closing(cache.connect()) as c:
        c.execute("UPDATE speed_inputs SET state='pending' WHERE source='codex'");c.commit()
    statuses={r['source']:r['status'] for r in cache.payload(request['from'],request['to'])['source_status']}
    assert statuses['codex']=='pending_reprocessing'
    assert 'opencode' not in statuses


def test_mixed_and_ratio_of_sums_and_ordered_gaps():
    base=dict(model='gpt-6',eligible=1,speed_kind='response_window',speed_token_basis='output_including_reasoning')
    rows=[dict(base,speed_tokens=1000,speed_ms=1000,speed_calls=1),dict(base,speed_tokens=10,speed_ms=10000,speed_calls=1)]
    assert session_speed.summarize(rows)['output_tok_per_s']==91.8
    rows.append(dict(base,model='other',speed_tokens=0,speed_ms=0,speed_calls=0))
    assert session_speed.summarize(rows)['output_tok_per_s'] is None
    assert session_speed.summarize(rows)['measurement_status']=='mixed'


def test_failed_native_read_retains_last_good_generation(corpus,monkeypatch):
    compute._sync_usage_store(CodingToolsUsageTracker())
    request={'from':'2026-09-01','to':'2026-09-28','source':''}
    build_requested(request)
    with closing(cache.connect()) as c:
        before=[tuple(r) for r in c.execute('SELECT * FROM speed_responses')]
        generation=cache.meta(c)['published_generation']
        c.execute("UPDATE speed_inputs SET signature='' WHERE source='opencode'");c.commit()
        monkeypatch.setattr(speed_worker,'native_rows',lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError('fixture read failed')))
        with pytest.raises(sqlite3.OperationalError):
            speed_worker.build(c,'test',request,sync=False)
        assert before==[tuple(r) for r in c.execute('SELECT * FROM speed_responses')]
        assert cache.meta(c)['published_generation']==generation


def test_one_session_build_reads_only_its_contributing_timing_inputs(corpus,monkeypatch):
    _,_,inventory=corpus
    compute._sync_usage_store(CodingToolsUsageTracker())
    original=speed_worker._collect_parser_file;read=[]
    def selected(parser,sig,**kw):
        read.append(sig[0]);assert sig[0]==inventory['codex_file']
        return original(parser,sig,**kw)
    monkeypatch.setattr(speed_worker,'_collect_parser_file',selected)
    monkeypatch.setattr(speed_worker,'native_rows',lambda *a:pytest.fail('unrelated native history read'))
    build_requested({'from':'2026-09-01','to':'2026-09-28','source':'codex','session_id':'bench-codex'})
    assert read==[inventory['codex_file']]
    with closing(cache.connect()) as c:
        assert {r[0] for r in c.execute('SELECT DISTINCT source FROM speed_responses')}=={'codex'}
    batch=session_speed.batch_summaries([{'tool':'codex','session_id':'bench-codex'}],'2026-09-01','2026-09-28')
    assert batch['sessions'][0]['speed_calls']==20
    assert session_speed.ensure_sessions([{'tool':'codex','session_id':'bench-codex'}],'2026-09-01','2026-09-28')['cache']['job_id'] is None


def test_session_buckets_preserve_additive_totals_and_metric_stream_gaps():
    def point(i,**extra):
        return dict(call_key=str(i),turn_key=str(i),sequence=i,recorded_at_ms=i*1000,
                    stream_id='main',model='m',measurement_kind='response_window',
                    token_basis='output_including_reasoning',speed_tokens=100,speed_ms=1000,
                    speed_calls=1,output_tok_per_s=100,eligible=True,**extra)
    rows=[point(i) for i in range(20)]
    buckets,bucketed,truncated=session_speed._bucket(rows,10)
    assert bucketed and not truncated and len(buckets)==10
    assert sum(p['speed_tokens'] for p in buckets)==2000
    assert sum(p['speed_ms'] for p in buckets)==20000
    assert all(p['call_key'] is None for p in buckets)
    rows[5]['output_tok_per_s']=None;rows[5]['speed_calls']=0
    rows[8]['model']='other';rows[12]['stream_id']='agent'
    rows[15]['break_before']=True
    buckets,_,truncated=session_speed._bucket(rows,10)
    assert any(p['output_tok_per_s'] is None for p in buckets)
    assert truncated and len(buckets)<=10
    assert all(p.get('max_call_rate',100)==100 for p in buckets)


def test_pricing_does_not_invalidate_timings_but_reader_version_does(corpus,monkeypatch):
    compute._sync_usage_store(CodingToolsUsageTracker())
    request={'from':'2026-09-01','to':'2026-09-28','source':''}
    build_requested(request)
    generation=cache.primary_generation();before=cache.identity()
    with closing(UsageEntryStore()._connect()) as c:
        c.execute("UPDATE usage_entries SET cost=cost+1");c.commit()
    assert cache.primary_generation()==generation and cache.identity()==before
    assert cache.ensure(request['from'],request['to'])['state']=='ready'
    monkeypatch.setitem(cache.READER_VERSIONS,'codex',99)
    assert cache.ensure(request['from'],request['to'])['state']=='stale'


def test_failed_refresh_at_same_generation_remains_error_until_success(corpus,monkeypatch):
    compute._sync_usage_store(CodingToolsUsageTracker())
    request={'from':'2026-09-01','to':'2026-09-28','source':''}
    build_requested(request)
    original=speed_worker.build
    monkeypatch.setattr(speed_worker,'build',lambda *a,**k: (_ for _ in ()).throw(OSError('private /path/failed')))
    status=cache.ensure(request['from'],request['to'],refresh=True)
    with closing(cache.connect()) as c:
        row=c.execute('SELECT * FROM speed_jobs WHERE id=?',(status['job_id'],)).fetchone()
        assert not speed_worker.run_job(c,row,sync=False)
    failure=cache.payload(request['from'],request['to'])
    assert failure['cache']['state']=='error' and failure['rows']
    assert '/path/' not in failure['cache']['error']
    monkeypatch.setattr(speed_worker,'build',original)
    status=cache.ensure(request['from'],request['to'],refresh=True)
    with closing(cache.connect()) as c:
        row=c.execute('SELECT * FROM speed_jobs WHERE id=?',(status['job_id'],)).fetchone()
        assert speed_worker.run_job(c,row,sync=False)
    assert cache.payload(request['from'],request['to'])['cache']['state']=='ready'


def test_native_selected_session_and_absent_clocks_have_cached_empty_bounds(corpus,monkeypatch):
    root,_,_=corpus
    compute._sync_usage_store(CodingToolsUsageTracker())
    with sqlite3.connect(root/'opencode.db') as c:
        c.execute("UPDATE message SET data=json_remove(data,'$.time.completed')")
        c.execute("INSERT INTO session VALUES('other','p','other','/fixture',0,0)")
        c.execute("INSERT INTO message SELECT 'other-call','other',time_created,time_updated,data FROM message LIMIT 1");c.commit()
    build_requested({'from':'2026-09-01','to':'2026-09-28','source':'opencode','session_id':'s'})
    with closing(cache.connect()) as c:
        assert c.execute('SELECT count(*) FROM speed_responses').fetchone()[0]==20
        assert c.execute("SELECT count(*) FROM speed_session_members WHERE session_id='other'").fetchone()[0]==0
        assert cache.speed_report.available_measurement_range(c) is None
        calls=[];c.set_progress_handler(lambda:calls.append(True) or 0,1)
        assert cache.speed_report.available_measurement_range(c) is None
        assert len(calls)<100,'empty measured-date bounds scanned unmeasured history'


@pytest.mark.parametrize('source', ['opencode', 'kilocode', 'mimo'])
def test_stale_full_native_index_keeps_other_calls_and_refreshes_only_requested_session(corpus,monkeypatch,source):
    root,_,_=corpus
    monkeypatch.setenv('TOKDASH_SPEED_CPU_FRACTION','1')
    compute._sync_usage_store(CodingToolsUsageTracker())
    db=root/(source+'.db')
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO session VALUES('other','p','other','/fixture',0,0)")
        c.execute("INSERT INTO message SELECT 'other-call','other',time_created,time_updated,data FROM message LIMIT 1")
        c.commit()
    full={'from':'2026-09-01','to':'2026-09-28','source':source}
    build_requested(full)
    with closing(cache.connect()) as c:
        original_signature=c.execute('SELECT signature FROM speed_inputs WHERE source=? AND input_owner=?',(source,'native:all')).fetchone()[0]
        other_before=tuple(c.execute('SELECT * FROM speed_responses WHERE source=? AND entry_key=?',(source,'other-call')).fetchone())
    with sqlite3.connect(db) as c:
        c.execute("UPDATE message SET data=json_set(data,'$.tokens.output',77) WHERE session_id='s'")
        c.commit()
    original_reader=speed_worker.native_rows
    expected=list(original_reader(source,'s'))
    reads=[]
    def bounded_reader(tool,sid=''):
        reads.append((tool,sid))
        assert (tool,sid)==(source,'s'),'session refresh enumerated unrelated native history'
        for sql,parameters in (
                ('DELETE FROM speed_member_stage WHERE source=? AND call_key=?',(source,'m1')),
                ('DELETE FROM speed_member_stage WHERE source=? AND session_id=?',(source,'s'))):
            plans=[row[3] for row in c.execute('EXPLAIN QUERY PLAN '+sql,parameters)]
            assert any('SEARCH speed_member_stage USING INDEX' in plan for plan in plans),plans
            assert not any('SCAN speed_member_stage' in plan for plan in plans),plans
        yield from original_reader(tool,sid)
    monkeypatch.setattr(speed_worker,'native_rows',bounded_reader)
    request={**full,'session_id':'s'}
    with closing(cache.connect()) as c:
        speed_worker.build(c,'test',request,sync=False)
        assert reads==[(source,'s')]
        assert c.execute('SELECT count(*) FROM speed_responses WHERE source=?',(source,)).fetchone()[0]==21
        assert tuple(c.execute('SELECT * FROM speed_responses WHERE source=? AND entry_key=?',(source,'other-call')).fetchone())==other_before
        pending=c.execute('SELECT * FROM speed_inputs WHERE source=? AND input_owner=?',(source,'native:all')).fetchone()
        assert pending['state']=='pending' and pending['signature']==original_signature
        assert c.execute('SELECT state FROM speed_inputs WHERE source=? AND input_owner=?',(source,'session:s')).fetchone()[0]=='ready'
    timeline=session_speed.timeline(source,'s',cache_only=True)
    assert timeline['summary']['speed_calls']==sum(row[0][11] for row in expected)>0
    assert sum(group['speed_tokens'] for group in timeline['summary']['groups'])==sum(row[0][9] for row in expected)
    assert len(timeline['series'])==20
    assert cache.ensure(full['from'],full['to'],source=source,session_id='s')['job_id'] is None
    assert cache.ensure(full['from'],full['to'],source=source)['job_id'] is not None
    monkeypatch.setattr(speed_worker,'native_rows',original_reader)
    with closing(cache.connect()) as c:
        speed_worker.build(c,'test',full,sync=False)
        assert c.execute('SELECT count(*) FROM speed_responses WHERE source=?',(source,)).fetchone()[0]==21
        assert [(r['input_owner'],r['state']) for r in c.execute('SELECT * FROM speed_inputs WHERE source=?',(source,))]==[('native:all','ready')]
        assert c.execute('SELECT count(*) FROM speed_session_members WHERE source=?',(source,)).fetchone()[0]==21


@pytest.mark.parametrize('removed', [False, True])
def test_unrequested_changed_native_scope_retains_last_good_or_retires_removed_database(corpus,monkeypatch,removed):
    root,_,_=corpus
    monkeypatch.setenv('TOKDASH_SPEED_CPU_FRACTION','1')
    compute._sync_usage_store(CodingToolsUsageTracker())
    build_requested({'from':'2026-09-01','to':'2026-09-28','source':''})
    with closing(cache.connect()) as c:
        before=[tuple(r) for r in c.execute("SELECT * FROM speed_responses WHERE source='opencode' ORDER BY entry_key")]
        members=[tuple(r) for r in c.execute("SELECT * FROM speed_session_members WHERE source='opencode' ORDER BY call_key")]
    if removed:
        (root/'opencode.db').unlink()
    else:
        with sqlite3.connect(root/'opencode.db') as c:
            c.execute("UPDATE message SET data=json_set(data,'$.tokens.output',77)");c.commit()
    monkeypatch.setattr(speed_worker,'native_rows',lambda *a:pytest.fail('unrequested native input was read'))
    with closing(cache.connect()) as c:
        speed_worker.build(c,'test',{'from':'2026-09-01','to':'2026-09-28','source':'codex','session_id':'bench-codex'},sync=False)
        rows=[tuple(r) for r in c.execute("SELECT * FROM speed_responses WHERE source='opencode' ORDER BY entry_key")]
        actual_members=[tuple(r) for r in c.execute("SELECT * FROM speed_session_members WHERE source='opencode' ORDER BY call_key")]
        assert rows==([] if removed else before)
        assert actual_members==([] if removed else members)
        if not removed:
            assert c.execute("SELECT state FROM speed_inputs WHERE source='opencode'").fetchone()[0]=='pending'


def test_batch_limits_and_deduplicates_before_any_cache_work():
    assert session_speed.validate_sessions([{'tool':'codex','session_id':'s'}]*2)==[{'tool':'codex','session_id':'s'}]
    with pytest.raises(ValueError):session_speed.validate_sessions([{'tool':'codex','session_id':'s'}]*51)
    with pytest.raises(ValueError):session_speed.validate_sessions([{'tool':'missing','session_id':'s'}])


@pytest.mark.parametrize('scope', ['native-session','sdk-session','batch','filtered-model','other-dates'])
def test_partial_refresh_cannot_certify_model_window_after_unindexed_usage_appears(corpus,monkeypatch,scope):
    _,_,inventory=corpus
    monkeypatch.setenv('TOKDASH_SPEED_CPU_FRACTION','1')
    store,_=compute._sync_usage_store(CodingToolsUsageTracker())
    request={'from':'2026-09-01','to':'2026-09-28','source':''}
    build_requested(request)
    before=store.measurement_generation()
    import output_speed_expansion_fixtures as fixtures
    original=Path(inventory['codex_file'])
    header=json.loads(original.read_text().splitlines()[0])
    header['payload']['id']='additional-codex-session'
    new_file=original.with_name('additional-session.jsonl')
    new_file.write_text(''.join(json.dumps(row)+'\n' for row in [header,*fixtures.codex_call(100)]))
    from tokdash.sources.coding_tools import _sig_cache
    _sig_cache.clear()
    compute._sync_usage_store(CodingToolsUsageTracker())
    assert store.measurement_generation()>before
    with store.read_connection() as c:
        assert c.execute('SELECT count(*) FROM usage_entries WHERE file_path=?',(str(new_file),)).fetchone()[0]>0
    with closing(cache.connect()) as c:
        partial={**request,'source':'opencode','session_id':'s'}
        if scope=='sdk-session':partial={**request,'source':'codex','session_id':'bench-codex'}
        elif scope=='batch':partial={**request,'sessions':[{'tool':'codex','session_id':'bench-codex'},{'tool':'opencode','session_id':'s'}]}
        elif scope=='filtered-model':partial={**request,'source':'opencode'}
        elif scope=='other-dates':partial={**request,'from':'2026-09-28','to':'2026-09-28'}
        speed_worker.build(c,'test',partial,sync=False)
    status=cache.ensure(request['from'],request['to'])
    assert status['job_id'] is not None
    with closing(cache.connect()) as c:
        speed_worker.build(c,status['job_id'],request,sync=False)
        assert c.execute("SELECT count(*) FROM speed_responses WHERE source='codex'").fetchone()[0]==21
    assert cache.ensure(request['from'],request['to'])['job_id'] is None


@pytest.mark.parametrize('scoped', [False,True])
def test_publication_upgrade_retires_old_window_markers_and_reuses_measurements(corpus,monkeypatch,scoped):
    monkeypatch.setenv('TOKDASH_SPEED_CPU_FRACTION','1')
    compute._sync_usage_store(CodingToolsUsageTracker())
    request={'from':'2026-09-01','to':'2026-09-28','source':''}
    build_requested(request)
    with closing(cache.connect()) as c:
        before=[tuple(r) for r in c.execute('SELECT * FROM speed_responses ORDER BY source,entry_key')]
        old_identity=json.loads(cache.identity());old_identity.pop('publication')
        cache.put_meta(c,publication_version=1,identity=json.dumps(old_identity,separators=(',',':')))
        c.commit()
        monkeypatch.setattr(speed_worker,'native_rows',lambda *a:pytest.fail('unchanged native timings were reprocessed'))
        monkeypatch.setattr(speed_worker,'_collect_parser_file',lambda *a,**k:pytest.fail('unchanged SDK timings were reprocessed'))
        refresh={**request,'source':'opencode','session_id':'s'} if scoped else {**request,'to':'2026-09-02'}
        speed_worker.build(c,'test',refresh,sync=False)
        assert before==[tuple(r) for r in c.execute('SELECT * FROM speed_responses ORDER BY source,entry_key')]
        assert cache.meta(c)['publication_version']==str(cache.PUBLICATION_VERSION)
        windows=[tuple(r) for r in c.execute('SELECT source,day_from,day_to FROM speed_windows')]
        assert windows==([] if scoped else [('',request['from'],'2026-09-02')])
    assert cache.ensure(request['from'],request['to'])['job_id'] is not None


def test_timeline_equal_clocks_distinct_agents_filtered_models_and_summary_parity(corpus):
    compute._sync_usage_store(CodingToolsUsageTracker())
    build_requested({'from':'2026-09-01','to':'2026-09-28','source':''})
    with closing(cache.connect()) as c:
        keys=[r[0] for r in c.execute("SELECT entry_key FROM speed_responses WHERE source='codex' ORDER BY timestamp")]
        c.execute("UPDATE speed_responses SET model='other' WHERE source='codex' AND entry_key=?",(keys[1],))
        c.execute("UPDATE speed_responses SET speed_calls=0,speed_tokens=0,speed_ms=0,speed_status='missing_timing' WHERE source='codex' AND entry_key=?",(keys[2],))
        c.execute("UPDATE speed_session_members SET recorded_at=1788264000000 WHERE source='codex' AND call_key IN (?,?)",(keys[0],keys[1]))
        c.execute("UPDATE speed_session_members SET stream_id='agent' WHERE source='codex' AND call_key=?",(keys[3],));c.commit()
    timeline=session_speed.timeline('codex','bench-codex',model='gpt-6')
    assert timeline['summary']['measurement_status']=='mixed' and timeline['summary']['output_tok_per_s'] is None
    assert timeline['summary']['eligible_calls']==20 and timeline['summary']['speed_calls']==19
    assert len(timeline['series'])==20
    assert len({p['call_key'] for p in timeline['series']})==20
    assert any(p['measurement_status']=='group_boundary' and p['output_tok_per_s'] is None for p in timeline['series'])
    assert any(p['measurement_status']=='missing_timing' and p['output_tok_per_s'] is None for p in timeline['series'])
    assert {p['stream_id'] for p in timeline['series']}=={'main','agent'}
    batch=session_speed.batch_summaries([{'tool':'codex','session_id':'bench-codex'}],'2026-09-01','2026-09-28')['sessions'][0]
    assert batch['groups']==timeline['groups'] and batch['coverage']==timeline['summary']['coverage']


def test_native_opt_in_cache_cannot_contaminate_ordinary_usage(corpus):
    root,_,_=corpus
    from tokdash.sources.coding_tools import OpenCodeParser
    from tokdash.pricing import PricingDatabase
    parser=OpenCodeParser(PricingDatabase());parser.db_path=root/'opencode.db'
    with collect_timings(False):plain=parser.collect()
    with collect_timings():timed=parser.collect()
    with collect_timings(False):again=parser.collect()
    assert plain==again and all('_speed' not in e for e in plain)
    assert all('_speed' in e for e in timed)
    assert [{k:v for k,v in e.items() if k!='_speed'} for e in timed]==plain


def test_partial_permissive_file_read_is_a_failure_not_successful_empty_cache(corpus,monkeypatch):
    compute._sync_usage_store(CodingToolsUsageTracker())
    request={'from':'2026-09-01','to':'2026-09-28','source':'codex'}
    build_requested(request)
    with closing(cache.connect()) as c:
        before=[tuple(r) for r in c.execute('SELECT * FROM speed_responses')]
        c.execute("UPDATE speed_inputs SET reader_version=0 WHERE source='codex'");c.commit()
        monkeypatch.setattr(speed_worker,'_collect_parser_file',lambda *a,**kw:[])
        with pytest.raises(RuntimeError,match='incomplete input read'):
            speed_worker.build(c,'test',request,sync=False)
        assert before==[tuple(r) for r in c.execute('SELECT * FROM speed_responses')]


def test_long_filtered_gap_does_not_consume_the_entire_chart_budget():
    common=dict(stream_id='main',model='other',measurement_kind='',token_basis='',
                speed_tokens=0,speed_ms=0,speed_calls=0,eligible=True)
    gaps=[dict(common,call_key=str(i),turn_key=str(i),sequence=i,recorded_at_ms=i,
               output_tok_per_s=None) for i in range(2000)]
    measured=[dict(common,call_key=str(i),turn_key=str(i),sequence=i,recorded_at_ms=i,
                   model='selected',measurement_kind='response_window',token_basis='output_including_reasoning',
                   speed_tokens=100,speed_ms=1000,speed_calls=1,output_tok_per_s=100) for i in range(2000,2020)]
    rows,bucketed,truncated=session_speed._bucket(gaps+measured,10)
    assert bucketed and not truncated and len(rows)<=10
    assert rows[0]['output_tok_per_s'] is None and rows[0]['gap_responses']==2000
    assert sum(r['speed_tokens'] for r in rows)==2000 and sum(r['speed_calls'] for r in rows)==20


def test_zero_worker_pid_launches_the_next_job_instead_of_signalling_process_group(tmp_path,monkeypatch):
    monkeypatch.setenv('TOKDASH_DATA_DIR',str(tmp_path/'data'))
    monkeypatch.setenv('TOKDASH_USAGE_DB_PATH',str(tmp_path/'data'/'usage.sqlite3'))
    assert not cache._alive('0') and not cache._alive(0) and not cache._alive('-1')
    assert not cache._alive('not a pid')
    launched=[]
    from types import SimpleNamespace
    monkeypatch.setattr(cache.subprocess,'Popen',lambda *a,**k:launched.append(a[0]) or SimpleNamespace(pid=99))
    with closing(cache.connect()) as c:
        cache.put_meta(c,worker_pid=0);c.commit()
        cache.launch_worker(c)
        assert len(launched)==1 and cache.meta(c)['worker_pid']=='99'


def test_staging_canonical_ownership_lookups_seek_an_index(corpus,monkeypatch):
    compute._sync_usage_store(CodingToolsUsageTracker())
    original=speed_worker._collect_parser_file;plans=[]
    def verify(parser,sig,**kw):
        with closing(cache.connect()) as c:
            plans.extend(r[3] for r in c.execute('EXPLAIN QUERY PLAN SELECT * FROM speed_stage WHERE source=? AND entry_key=? AND input_owner=?',('codex','key',sig[0])))
        return original(parser,sig,**kw)
    monkeypatch.setattr(speed_worker,'_collect_parser_file',verify)
    build_requested({'from':'2026-09-01','to':'2026-09-28','source':'codex'})
    assert plans and all('idx_speed_stage_key' in p for p in plans)


def test_reused_membership_seeks_one_input_before_looking_up_call_keys(corpus):
    compute._sync_usage_store(CodingToolsUsageTracker())
    build_requested({'from':'2026-09-01','to':'2026-09-28','source':'codex'})
    with closing(cache.connect()) as c:
        c.execute('CREATE TABLE speed_stage AS SELECT * FROM speed_responses WHERE 0')
        c.execute('CREATE INDEX idx_speed_stage_owner ON speed_stage(source,input_owner)')
        for table,index in [('speed_stage','idx_speed_stage_owner'),('speed_responses','idx_speed_owner')]:
            plan=[r[3] for r in c.execute(f'''EXPLAIN QUERY PLAN SELECT m.* FROM {table} r
                CROSS JOIN speed_session_members m ON r.source=m.source AND r.entry_key=m.call_key
                WHERE r.source=? AND r.input_owner=?''',('codex','one-input'))]
            assert index in plan[0] and 'input_owner=?' in plan[0]
            assert 'call_key=?' in plan[1]
