"""Response ownership, retry boundaries, native/WAL reads and surface parity."""
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

import pytest
from tokdash.output_speed_readers import (
    CodexResponseAssociation, QwenTimingAssociation, clipped_union_ms,
    request_window_timing,
    native_message_timing,
)
from tokdash.sources.dsh_log import fold_dsh_usage_samples
from tokdash.sources.coding_tools import (
    CodexParser, DSHParser, QwenCodeParser, OpenCodeParser, KiloCodeParser, MimoParser,
    qwen_session_file,
)
from tokdash.pricing import PricingDatabase
from tokdash import sessions, speed_native, api
from tokdash import speed_report, speed_cache, speed_worker
from contextlib import closing
from tokdash import clientpaths
from tokdash.usage_store import UsageTailSliceIncomplete, UsageEntryStore

@pytest.fixture(autouse=True)
def _timing_reader_mode():
    from tokdash.speed_mode import collect_timings
    with collect_timings():
        yield


TOKENS = {'input_tokens': 100, 'cached_input_tokens': 20, 'output_tokens': 60,
          'reasoning_output_tokens': 0, 'total_tokens': 160}


@pytest.mark.parametrize('legacy_unknown', [False, True])
def test_covering_eligibility_preserves_unknown_and_reasoning_only_populations(tmp_path,legacy_unknown):
    ts=int(datetime(2026,9,1,12,tzinfo=timezone.utc).timestamp()*1000)
    with closing(speed_cache.connect(tmp_path/'speed.sqlite3')) as c:
        rows=[('codex',60,0,'measured',1),('codex',60,0,'missing_timing',0),
              ('codex',60,0,'unknown',0),('codex',0,7,'unknown',0),
              ('qwen_code',0,10,'unknown',0),('qwen_code',2,1,'measured',1)]
        for i,(source,output,reasoning,status,calls) in enumerate(rows):
            if not legacy_unknown and status=='unknown':status='missing_timing'
            eligible=int(output>0 or source=='qwen_code' and reasoning>0)
            c.execute('INSERT INTO speed_responses VALUES ('+','.join('?' for _ in range(15))+')',
                (source,str(i),'fixture','s',ts,'gpt-6',output,reasoning,eligible,
                 output+reasoning if calls else 0,100 if calls else 0,calls,
                 'response_window' if calls else '', 'output_including_reasoning' if calls else '',status))
        lo,hi=speed_report._day_bounds_ms('2026-09-01','2026-09-01')
        assert speed_report._eligible_rows(c,lo,hi,{'codex','qwen_code'})=={('codex','gpt-6'):3,('qwen_code','gpt-6'):2}
        queries=speed_report._eligible_queries(c,lo,hi,{'codex','qwen_code'},'source,model,count(*) AS n',group='GROUP BY source,model')
        assert len(queries)==1,'eligibility has one indexed population, including unknown timing'
        plans=' '.join(r[3] for sql,args in queries for r in c.execute('EXPLAIN QUERY PLAN '+sql,args))
        assert 'COVERING INDEX idx_speed_report' in plans
        temporal=speed_report.temporal_rows(c,'2026-09-01','2026-09-01',source='codex',model='gpt-6',measurement_kind='response_window',token_basis='output_including_reasoning',hour_of_local=lambda ms:12,date_of_local=lambda ms:'2026-09-01')
        assert temporal['eligible_calls']==3 and sum(r.get('eligible_calls') or 0 for r in temporal['hourly'])==3
        assert sum(r['speed_calls'] for r in temporal['hourly'])==1


def event(kind, **payload):
    return {'type': 'event_msg', 'timestamp': '2026-09-01T12:00:01Z', 'payload': {'type': kind, **payload}}

def item(kind, lo, hi, id='item', turn='turn'):
    return event('item_completed', turn_id=turn, item={'type': kind, 'id': id}, started_at_ms=lo, completed_at_ms=hi)

def owner(usage=TOKENS, total=TOKENS, rid='response', turn='turn'):
    return {'type': 'token_usage_record', 'payload': {'turn_id': turn, 'response_id': rid,
              'usage': usage, 'thread_token_usage': total}}

def codex(records):
    a = CodexResponseAssociation()
    a.observe(event('task_started', turn_id='turn'))
    for r in records:
        a.observe(r)
    a.register('call', {'last_token_usage': TOKENS, 'total_token_usage': TOKENS})
    return a.resolve()['call']

def test_codex_clipped_tool_union_and_reasoning_inclusive_numerator():
    assert clipped_union_ms([(0, 30), (20, 50), (70, 200)], 10, 100) == 70
    speed = codex([item('Reasoning', 10, 20), item('AgentMessage', 80, 100, 'text'),
                   item('CommandExecution', 0, 30, 'a'), item('FileChange', 20, 50, 'b'),
                   item('CommandExecution', 70, 200, 'c'), owner()])
    assert speed['speed_ms'] == 20
    assert speed['speed_tokens'] == 60  # reasoning is a subset, never added again
    assert speed['speed_token_basis'] == 'output_including_reasoning'

@pytest.mark.parametrize('records,status', [
    ([item('Reasoning', 10, 10), item('AgentMessage', 20, 50, 'text'), owner()], 'invalid_timing'),
    ([item('FutureItem', 10, 50), owner()], 'ambiguous_pair'),
    ([item('AgentMessage', 10, 50), owner(turn='other')], 'ambiguous_pair'),
    ([item('AgentMessage', 10, 50)], 'missing_timing'),
    ([item('AgentMessage', 10, 50), owner(), event('turn_aborted')], 'incomplete'),
    ([item('AgentMessage', 10, 50), item('ContextCompaction', 51, 80, 'compact'), owner()], 'incomplete'),
    ([item('AgentMessage', 10, 50), {'type':'response_item','payload':{'type':'function_call'}}, owner()], 'missing_timing'),
    ([item('AgentMessage', 10, 50), owner(total={**TOKENS,'output_tokens':61})], 'ambiguous_pair'),
    ([item('AgentMessage', 50, 10), owner()], 'invalid_timing'),
])
def test_codex_rejects_unowned_or_partial_windows(records, status):
    assert codex(records)['speed_status'] == status

def test_codex_dedup_and_invalid_snapshot_cannot_donate_items():
    a = CodexResponseAssociation()
    a.observe(event('task_started', turn_id='turn'))
    a.observe(item('AgentMessage', 10, 50)); a.observe(item('AgentMessage', 10, 50))
    a.observe(owner()); a.register('one', {'last_token_usage':TOKENS,'total_token_usage':TOKENS})
    a.register('one', {'last_token_usage':TOKENS,'total_token_usage':TOKENS})
    a.register('two', {})
    assert a.resolve()['one']['speed_ms'] == 40
    assert a.resolve()['two']['speed_calls'] == 0

def test_codex_late_parallel_tools_are_subtracted_from_their_actual_bracket():
    a = CodexResponseAssociation();a.observe(event('task_started', turn_id='turn'))
    a.observe(item('AgentMessage', 10, 100));a.observe(owner())
    a.register('call', {'last_token_usage':TOKENS,'total_token_usage':TOKENS})
    a.observe(item('CommandExecution', 20, 50, 'late'))
    assert a.resolve()['call']['speed_ms'] == 60

def test_codex_reasoning_tokens_require_their_own_noncollapsed_item():
    usage = {**TOKENS, 'reasoning_output_tokens': 20}
    a = CodexResponseAssociation(); a.observe(event('task_started', turn_id='turn'))
    a.observe(item('AgentMessage', 20, 100)); a.observe(owner(usage, usage))
    a.register('call', {'last_token_usage':usage,'total_token_usage':usage})
    assert a.resolve()['call']['speed_status'] == 'missing_timing'
    b = CodexResponseAssociation(); b.observe(event('task_started', turn_id='turn'))
    b.observe(item('Reasoning', 10, 20)); b.observe(item('AgentMessage', 20, 100, 'text')); b.observe(owner(usage, usage))
    b.register('call', {'last_token_usage':usage,'total_token_usage':usage})
    assert b.resolve()['call']['speed_tokens'] == 60
    assert b.resolve()['call']['speed_ms'] == 90

@pytest.mark.parametrize('usage', [{**TOKENS,'reasoning_output_tokens':61},
                                   {**TOKENS,'total_tokens':180},
                                   {**TOKENS,'output_tokens':60.5},None])
def test_codex_rejects_unverified_reasoning_counter_semantics(usage):
    a=CodexResponseAssociation();a.observe(event('task_started',turn_id='turn'))
    a.observe(item('AgentMessage',10,100));a.observe(owner(usage,usage))
    a.register('call',{'last_token_usage':usage,'total_token_usage':usage})
    assert a.resolve()['call']['speed_status']=='ambiguous_pair'

def test_codex_collapsed_or_malformed_late_tool_clock_excludes_owned_response():
    for tool in (item('CommandExecution', 50, 10, 'late'),
                 event('item_completed',turn_id='turn',item={'type':'CommandExecution','id':'late','duration_ms':40},started_at_ms=50,completed_at_ms=50),
                 event('item_completed',turn_id='turn',item={'type':'DynamicToolCall','id':'late','duration':{'secs':1,'nanos':1}},started_at_ms=50,completed_at_ms=50)):
        a=CodexResponseAssociation();a.observe(event('task_started',turn_id='turn'))
        a.observe(item('AgentMessage',10,100));a.observe(owner());a.register('call',{'last_token_usage':TOKENS,'total_token_usage':TOKENS})
        a.observe(tool)
        assert a.resolve()['call']['speed_status']=='invalid_timing'

def qwen_records():
    usage = {'promptTokenCount':100,'candidatesTokenCount':40,'thoughtsTokenCount':20,'totalTokenCount':160}
    telemetry={'type':'system','uuid':'clock','sessionId':'session', 'systemPayload':{'uiEvent':{
        'event.name':'qwen-code.api_response','response_id':'response','model':'provider/model',
        'duration_ms':2000,'ttft_ms':0,'input_token_count':100,'output_token_count':40,'thoughts_token_count':20,'total_token_count':160}}}
    assistant={'type':'assistant','uuid':'answer','parentUuid':'clock','sessionId':'session',
               'model':'model','timestamp':'2026-09-01T12:00:00Z','usageMetadata':usage}
    return telemetry,assistant

def test_qwen_direct_identity_zero_ttft_and_absent_telemetry(tmp_path):
    t,r=qwen_records();p=tmp_path/'session.jsonl'
    p.write_text('\n'.join(map(json.dumps,[t,r]))+'\n')
    rows,_=qwen_session_file(str(p));assert rows[0][1]['_speed']['speed_tokens']==60
    assert rows[0][1]['_speed']['speed_ms']==2000
    r['parentUuid']='elsewhere';p.write_text('\n'.join(map(json.dumps,[t,r]))+'\n')
    assert qwen_session_file(str(p))[0][0][1]['_speed']['speed_status']=='missing_timing'
    with pytest.raises(UsageTailSliceIncomplete):qwen_session_file(str(p),tail_slice=True)

@pytest.mark.parametrize('total,expected',[(140,40),(160,60),(None,None),(159,None)])
def test_qwen_reasoning_inclusion_is_proven_by_the_recorded_total_identity(total,expected):
    t,r=qwen_records();r['model']='MODEL';t['systemPayload']['uiEvent']['model']='provider/model'
    r['usageMetadata']['totalTokenCount']=total;t['systemPayload']['uiEvent']['total_token_count']=total
    a=QwenTimingAssociation();a.observe(t);row={'model':'MODEL','output':40,'reasoning':20};a.observe(r,row);a.resolve()
    assert row['_speed']['speed_tokens']==(expected or 0)
    assert bool(row['_speed']['speed_calls'])==(expected is not None)

@pytest.mark.parametrize('change', ['model','scope','tokens','duplicate_owner','two_answers'])
def test_qwen_refuses_conflicting_linked_telemetry(change):
    t,r=qwen_records();a=QwenTimingAssociation();row={'model':'model','output':40,'reasoning':20}
    if change=='model':t['systemPayload']['uiEvent']['model']='other'
    if change=='scope':t['agentId']='other'
    if change=='tokens':t['systemPayload']['uiEvent']['output_token_count']=41
    a.observe(t);a.observe(r,row)
    if change=='duplicate_owner':
        t2=json.loads(json.dumps(t));t2['systemPayload']['uiEvent']['duration_ms']=3000;a.observe(t2)
    if change=='two_answers':a.observe({**r,'uuid':'second'},dict(row))
    a.resolve();assert row['_speed']['speed_status']=='ambiguous_pair'

def dsh_event(kind, seq, time, **data):
    return {'type':kind,'seq':seq,'time':time,'data':{'turn':1,'step':1,**data}}

def dsh_stream(usage, start=100, end=1100):
    return [{'type':'chunk','time':start,'chunk':{'type':'block-start'}},
            {'type':'reasoning-chunks','time0':start,'dt':[10]},
            {'type':'chunk','time':end,'chunk':{'type':'usage','usage':usage}}]

def test_dsh_final_fold_owns_one_window_and_retry_is_retrospective():
    usage={'inputTokens':100,'outputTokens':60,'reasoningTokens':20}
    events=(dsh_event('assistant/chunk',1,100,chunk={'type':'block-start'}),
            dsh_event('assistant/chunk',2,1100,chunk={'type':'usage','usage':usage}),
            dsh_event('assistant/message',3,1200,usage=usage,stream=dsh_stream(usage)))
    rows=fold_dsh_usage_samples({'version':4},events)
    assert len(rows)==1 and rows[0]['_speed']['speed_ms']==1000
    retried=events+(dsh_event('llm/retry-started',4,2000),dsh_event('assistant/message',5,3100,usage=usage,stream=dsh_stream(usage,2100,3100)))
    rows=fold_dsh_usage_samples({'version':4},retried)
    assert [r['entry_key'] for r in rows]==['1:1','1:1:r:1']
    assert all(r['_speed']['speed_status']=='retry_contaminated' for r in rows)

def test_dsh_seed_boundaries_do_not_leak_clocks_or_retries():
    usage={'inputTokens':100,'outputTokens':60}
    events=(dsh_event('assistant/chunk',1,100,chunk={'type':'block-start'}),
            dsh_event('llm/retry-started',2,200),
            {'type':'session/end-seed','seq':3,'data':{'inherited':True}},
            dsh_event('assistant/chunk',4,1100,chunk={'type':'usage','usage':usage}))
    row=fold_dsh_usage_samples({'version':4,'isSeeded':True},events)[0]
    assert row['entry_key']=='1:1' and row['_speed']['speed_status']=='missing_timing'

def test_dsh_superseding_usage_and_untimed_coalescing_are_not_measured():
    usage={'inputTokens':100,'outputTokens':60};other={**usage,'outputTokens':61}
    e=dsh_event('assistant/message',1,1200,usage=other,stream=dsh_stream(usage))
    assert fold_dsh_usage_samples({},(e,))[0]['_speed']['speed_status']=='ambiguous_pair'
    stream=dsh_stream(usage);stream[1].pop('time0')
    e=dsh_event('assistant/message',1,1200,usage=usage,stream=stream)
    assert fold_dsh_usage_samples({},(e,))[0]['_speed']['speed_status']=='missing_timing'

@pytest.mark.parametrize('error,finish,completed,status', [
    ({'name':'AbortError'},'stop',2000,'incomplete'),(None,'error',2000,'incomplete'),
    (None,'length',2000,'incomplete'),(None,'stop',None,'incomplete'),
    (None,'stop',500,'invalid_timing'),(None,'tool-calls',2000,'measured'),
])
def test_native_calls_use_disjoint_tokens_and_explicit_end_state(error,finish,completed,status):
    s=request_window_timing(40,20,1000,completed,error,finish)
    assert s['speed_status']==status
    if status=='measured':assert s['speed_tokens']==60 and s['speed_ms']==1000

def test_mimo_ensemble_window_is_not_a_single_call():
    data={'tokens':{'output':40,'reasoning':20},'time':{'created':100,'completed':1100},'agent':'max','finish':'stop'}
    assert native_message_timing(data,source='mimo')['speed_status']=='ambiguous_pair'
    assert native_message_timing(data,source='opencode')['speed_calls']==1

def native_db(path):
    c=sqlite3.connect(path);c.execute('PRAGMA journal_mode=WAL')
    c.executescript('''CREATE TABLE project(id TEXT, worktree TEXT);
      CREATE TABLE session(id TEXT, directory TEXT, project_id TEXT, title TEXT);
      CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT);
      CREATE INDEX message_time ON message(time_created);
      INSERT INTO project VALUES ('p','/fixture'); INSERT INTO session VALUES ('s','/fixture','p','fixture');''')
    ts=1788264000000
    data={'role':'assistant','modelID':'provider/MODEL-preview','tokens':{'input':100,'output':40,'reasoning':20},
          'time':{'created':ts,'completed':ts+2000},'finish':'stop'}
    c.execute('INSERT INTO message VALUES (?,?,?,?)',('a','s',ts,json.dumps(data)));c.commit()
    return c,data

def _native_cache(monkeypatch):
    monkeypatch.setattr(speed_cache,'launch_worker',lambda conn:None)
    UsageEntryStore()._connect().close()


def _run_pending(payload):
    with closing(speed_cache.connect()) as c:
        row=c.execute('SELECT * FROM speed_jobs WHERE id=?',(payload['cache']['job_id'],)).fetchone()
        return speed_worker.run_job(c,row,sync=False)


@pytest.mark.parametrize('source,cls', [('opencode',OpenCodeParser),('kilocode',KiloCodeParser),('mimo',MimoParser)])
def test_native_speed_api_and_session_parity_wal_and_removal(tmp_path,monkeypatch,source,cls):
    db=tmp_path/'native.db';writer,data=native_db(db)
    monkeypatch.setattr(speed_native,'paths',lambda:{source:[db]})
    parser=cls(PricingDatabase());parser.db_path=db
    if source=='kilocode':parser.db_paths=[db]
    cls._query_cache.clear();cls._query_cache_sig=()
    usage=parser.collect()[0]['_speed']
    raw=(sessions._load_mimo_sessions_scalar(db) if source=='mimo' else sessions._load_opencode_sessions_scalar(db,tool=source))
    assert raw['s']['turns'][0]['_speed']==usage
    api._clear_cache()
    params=dict(period='custom',date_from='2026-09-01',date_to='2026-09-01')
    _native_cache(monkeypatch)
    queued=api.get_output_speed(**params);assert queued['cache']['state']=='building'
    assert _run_pending(queued)
    first=api.get_output_speed(**params)
    assert first['rows'][0]['source']==source and first['rows'][0]['speed_tokens']==60
    assert first['rows'][0]['model']=='model'
    selected=first['rows'][0]
    temporal=api.get_output_speed(**params,view='time-of-day',source=source,model='model',
        measurement_kind=selected['measurement_kind'],token_basis=selected['token_basis'])
    assert sum(h['speed_tokens'] for h in temporal['hourly'])==60
    before=speed_native.signature();data['tokens']['output']=100
    writer.execute('UPDATE message SET data=?',(json.dumps(data),));writer.commit()
    assert speed_native.signature()!=before
    stale=api.get_output_speed(**params);assert stale['cache']['state']=='stale' and stale['rows'][0]['speed_tokens']==60
    assert _run_pending(stale)
    second=api.get_output_speed(**params);assert second['rows'][0]['speed_tokens']==120
    writer.execute('DELETE FROM message');writer.commit()
    assert _run_pending(api.get_output_speed(**params))
    assert api.get_output_speed(**params)['rows']==[]
    writer.close()

def test_native_failures_stay_distinct_and_are_not_response_cached(tmp_path,monkeypatch):
    db=tmp_path/'broken.db';db.write_text('not sqlite')
    monkeypatch.setattr(speed_native,'paths',lambda:{'opencode':[db]})
    _native_cache(monkeypatch);api._clear_cache()
    params=dict(date_from='2026-09-01',date_to='2026-09-01')
    queued=api.get_output_speed(**params);assert not _run_pending(queued)
    payload=api.get_output_speed(**params)
    assert payload['cache']['state']=='error' and payload['cache']['error']
    assert str(db) not in payload['cache']['error']
    assert payload['rows']==[] and payload['available_measurement_range'] is None
    assert payload['cache']['job_id'] is None, 'failed empty reads must not auto-retry or become ready'
    assert not any(k.startswith('output_speed_') for k in api._cache)


@pytest.mark.parametrize('output,expected_calls', [('40',1), ({'bad':'counter'},0)])
def test_native_projection_uses_billing_counter_conversion_without_inventing_timing(tmp_path,monkeypatch,output,expected_calls):
    db=tmp_path/'native.db';writer,data=native_db(db)
    data['tokens']['output']=output
    writer.execute('UPDATE message SET data=?',(json.dumps(data),));writer.commit()
    monkeypatch.setattr(speed_native,'paths',lambda:{'opencode':[db]})
    entries,failures=speed_native.read_window(0,2**63-1)
    assert not failures and entries[0]['_speed']['speed_calls']==expected_calls
    projection=speed_native.projection(entries)
    assert projection.execute('SELECT count(*) FROM usage_entries').fetchone()[0]==1
    projection.close();writer.close()

def test_codex_counted_usage_and_session_keep_speed_after_duplicate_snapshot(tmp_path):
    records=[{'type':'session_meta','payload':{'id':'session','cwd':'/fixture'}},
             {'type':'turn_context','payload':{'turn_id':'turn','model':'gpt-6'}},
             item('AgentMessage',10,100),owner(),event('token_count',info={'last_token_usage':TOKENS,'total_token_usage':TOKENS})]
    records.append(records[-1])
    path=tmp_path/'rollout.jsonl';path.write_text('\n'.join(map(json.dumps,records))+'\n')
    parser=CodexParser(PricingDatabase());parser.sessions_dir=tmp_path;parser.archived_sessions_dir=tmp_path/'absent'
    entries=parser._parse_all();st=path.stat();raw=sessions._parse_codex_session_file(str(path),st.st_mtime_ns,st.st_size)
    assert len(entries)==len(raw['turns'])==1
    assert entries[0]['_speed']==raw['turns'][0]['_speed']
    assert entries[0]['_speed']['speed_ms']==90
    assert (entries[0]['input'],entries[0]['output'],entries[0]['reasoning'])==(80,60,0)

def test_dsh_counted_final_and_session_speed_parity(tmp_path):
    usage={'inputTokens':100,'outputTokens':60,'reasoningTokens':20}
    header={'type':'session','version':4,'id':'session','createdAt':100,'cwd':'/fixture'}
    stream=dsh_stream(usage)+[{'type':'chunk','time':1101,'chunk':{'type':'finish','reason':{'kind':'stop'}}}]
    records=[header,{'type':'request/context','seq':0,'time':100,'data':{'model':'deepseek-v4-flash','provider':'deepseek'}},
             dsh_event('assistant/message',1,1200,usage=usage,stream=stream)]
    path=tmp_path/'session.jsonl';path.write_text('\n'.join(map(json.dumps,records))+'\n')
    parser=DSHParser(PricingDatabase());parser.sessions_dir=tmp_path
    entries=parser._parse_all();st=path.stat();raw=sessions._parse_dsh_session_file(str(path),st.st_mtime_ns,st.st_size)
    assert len(entries)==len(raw['turns'])==1
    assert entries[0]['_speed']==raw['turns'][0]['_speed']
    assert entries[0]['_speed']['speed_tokens']==60
    assert entries[0]['output']==raw['turns'][0]['tokens_out']==60

def test_qwen_counted_response_and_session_speed_parity(tmp_path,monkeypatch):
    t,r=qwen_records();path=tmp_path/'projects/p/chats/session.jsonl';path.parent.mkdir(parents=True)
    path.write_text('\n'.join(map(json.dumps,[t,r]))+'\n')
    monkeypatch.setattr(clientpaths,'qwen_runtime_base',lambda:tmp_path)
    parser=QwenCodeParser(PricingDatabase());entries=parser._parse_all();st=path.stat()
    turns,_=sessions._parse_qwen_code_session_file(str(path),st.st_mtime_ns,st.st_size)
    assert len(entries)==len(turns)==1
    assert entries[0]['_speed']==turns[0]['_speed']
    assert entries[0]['output']==turns[0]['tokens_out']==40
    assert entries[0]['reasoning']==turns[0]['tokens_reasoning']==20

def test_native_change_during_projection_is_never_cached(tmp_path,monkeypatch):
    db=tmp_path/'native.db';writer,data=native_db(db)
    monkeypatch.setattr(speed_native,'paths',lambda:{'opencode':[db]})
    _native_cache(monkeypatch);params=dict(date_from='2026-09-01',date_to='2026-09-01')
    assert _run_pending(api.get_output_speed(**params))
    original=speed_worker.native_rows
    data['tokens']['output']=41
    writer.execute('UPDATE message SET data=?',(json.dumps(data),));writer.commit()
    def racing(*args,**kwargs):
        yield from original(*args,**kwargs)
        data['tokens']['output']=99
        writer.execute('UPDATE message SET data=?',(json.dumps(data),));writer.commit()
    monkeypatch.setattr(speed_worker,'native_rows',racing)
    refresh=api.get_output_speed(**params,refresh=True)
    assert not _run_pending(refresh)
    terminal=api.get_output_speed(**params,cache_only=True)
    assert terminal['cache']['state']=='error' and terminal['rows'][0]['speed_tokens']==60
    payload=api.get_output_speed(**params)
    assert payload['cache']['state']=='stale' and payload['cache']['job_id']
    assert payload['rows'][0]['speed_tokens']==60
    assert not any(k.startswith('output_speed_') for k in api._cache)
    writer.close()


@pytest.mark.parametrize('source,cls',[('opencode',OpenCodeParser),('kilocode',KiloCodeParser)])
def test_native_v2_migration_uses_one_table_and_both_session_paths(tmp_path,monkeypatch,source,cls):
    db=tmp_path/'native.db';writer,data=native_db(db)
    writer.execute('CREATE TABLE session_message(id TEXT PRIMARY KEY,session_id TEXT,type TEXT,seq INTEGER,time_created INTEGER,time_updated INTEGER,data TEXT)')
    writer.execute('CREATE TABLE session_v2 AS SELECT * FROM session')
    ts=int(datetime(2026,9,1,12,tzinfo=timezone.utc).timestamp()*1000)
    data.pop('role');data['model']={'id':data.pop('modelID'),'providerID':'fixture'}
    writer.execute('INSERT INTO session_message VALUES(?,?,?,?,?,?,?)',('migration','s','assistant',0,ts,ts,json.dumps(data)));writer.commit()
    monkeypatch.setattr(speed_native,'paths',lambda:{source:[db]})
    entries,failures=speed_native.read_window(ts-1,ts+1)
    assert len(entries)==1 and not failures
    parser=cls(PricingDatabase());parser.db_path=db
    if source=='kilocode':parser.db_paths=[db]
    cls._query_cache.clear();cls._query_cache_sig=()
    usage=parser.collect()
    scalar=sessions._load_opencode_sessions_scalar(db,tool=source)
    fallback=sessions._load_opencode_sessions_raw_json(db,tool=source)
    assert len(usage)==len(scalar['s']['turns'])==len(fallback['s']['turns'])==1
    assert entries[0]['_speed']==usage[0]['_speed']==scalar['s']['turns'][0]['_speed']==fallback['s']['turns'][0]['_speed']
    writer.close()
