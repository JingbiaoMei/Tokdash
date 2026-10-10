"""Cache-only explorer summaries and demand-built, bounded session timelines."""
from __future__ import annotations
from contextlib import closing
from datetime import datetime
import json
import time
import uuid

from . import speed_cache as cache
from .output_speed import rate_from_totals
from .model_normalization import normalize_model_name
from .usage_store import UsageEntryStore, usage_db_path


def validate_sessions(items):
    from .sessions import SESSION_TOOLS
    if not isinstance(items,list) or len(items)>50:
        raise ValueError('sessions must contain at most 50 entries')
    result=[]; seen=set()
    for item in items:
        if not isinstance(item,dict):
            raise ValueError('invalid session key')
        tool=str(item.get('tool') or '').lower(); sid=str(item.get('session_id') or '')
        if tool not in SESSION_TOOLS or not sid or len(sid)>256:
            raise ValueError('invalid session key')
        if (tool,sid) not in seen:
            result.append({'tool':tool,'session_id':sid}); seen.add((tool,sid))
    return result


def summarize(rows):
    groups={}; eligible_models=set(); eligible=0; calls=0
    for r in rows:
        if r['eligible']:
            eligible+=int(r['eligible']); eligible_models.add(normalize_model_name(r['model']))
        if r['speed_calls']<=0:
            continue
        key=(normalize_model_name(r['model']),r['speed_kind'],r['speed_token_basis'])
        group=groups.setdefault(key,{'model':key[0],'measurement_kind':key[1],'token_basis':key[2],
                                     'speed_tokens':0,'speed_ms':0.0,'speed_calls':0})
        for field in ('speed_tokens','speed_ms','speed_calls'):
            group[field]+=r[field]
        calls+=r['speed_calls']
    for g in groups.values():
        g['output_tok_per_s']=rate_from_totals(g['speed_tokens'],g['speed_ms'])
        g['eligible_calls']=sum(r['eligible'] for r in rows if normalize_model_name(r['model'])==g['model'])
        g['coverage']=g['speed_calls']/g['eligible_calls'] if g['eligible_calls'] else None
    groups=sorted(groups.values(),key=lambda g:(-g['speed_calls'],g['model'],g['measurement_kind'],g['token_basis']))
    mixed=len(groups)>1 or len(eligible_models)>1
    return {'measurement_status':'mixed' if mixed and calls else 'measured' if calls else 'timing_unavailable',
            'output_tok_per_s':groups[0]['output_tok_per_s'] if len(groups)==1 and not mixed else None,
            'speed_calls':calls,'eligible_calls':eligible,'coverage':calls/eligible if eligible else None,
            'partial_model_scope':bool(calls and eligible_models-set(g['model'] for g in groups)), 'groups':groups}


def _rows(conn,tool,sid,start=None,end=None):
    clause=' AND r.timestamp>=? AND r.timestamp<?' if start is not None else ''
    args=(tool,sid,start,end) if start is not None else (tool,sid)
    return list(conn.execute('''SELECT r.*,m.turn_key,m.stream_id,m.sequence,m.recorded_at,m.timestamp_basis
       FROM speed_session_members m JOIN speed_responses r ON r.source=m.source AND r.entry_key=m.call_key
       WHERE m.source=? AND m.session_id=?'''+clause+' ORDER BY m.stream_id,m.recorded_at,m.sequence,r.entry_key',args))


def _state(conn,state,tool,sid,target):
    if tool not in cache.READER_VERSIONS:
        return 'ready','unsupported_reader'
    member=conn.execute('SELECT 1 FROM speed_session_members WHERE source=? AND session_id=? LIMIT 1',(tool,sid)).fetchone()
    complete=conn.execute('SELECT 1 FROM speed_sessions WHERE source=? AND session_id=? AND usage_generation=? AND state="ready"',(tool,sid,target)).fetchone()
    if cache.failure(conn,'0001-01-01','9999-12-31',tool,sid,target=target,current_only=False):
        return 'error',None
    if cache._scope_ready(conn,state,'0001-01-01','9999-12-31',tool,sid,target,cache.identity()):
        return 'ready',None
    if not member and not complete:
        return 'not-indexed','pending_reprocessing'
    if not complete:
        return 'stale','pending_reprocessing'
    return 'stale',None


def _session_summary(conn,tool,sid,start,end):
    # Batch enrichment transfers grouped scalars, never every response in each
    # visible session. Normalization and metric grouping share summarize().
    # Keep membership first: a source/date-first plan rescans unrelated history
    # for every requested session, despite the bounded session index.
    rows=list(conn.execute('''SELECT r.model,r.speed_kind,r.speed_token_basis,
        sum(r.eligible) AS eligible,sum(r.speed_tokens) AS speed_tokens,
        sum(r.speed_ms) AS speed_ms,sum(r.speed_calls) AS speed_calls
        FROM speed_session_members m INDEXED BY idx_speed_session CROSS JOIN speed_responses r
          ON r.source=m.source AND r.entry_key=m.call_key
        WHERE m.source=? AND m.session_id=? AND r.timestamp>=? AND r.timestamp<?
        GROUP BY r.model,r.speed_kind,r.speed_token_basis''',(tool,sid,start,end)))
    return summarize(rows)


def batch_summaries(items,day_from,day_to):
    items=validate_sessions(items)
    start,end=cache.speed_report._day_bounds_ms(day_from,day_to)
    target=cache.primary_generation()
    scope={'kind':'date_range','from':day_from,'to':day_to}
    if not cache.cache_path().exists():
        return {'schema_version':1,'scope':scope,'measurement_generation':0,
                'sessions':[dict(i,cache_state='not-indexed' if i['tool'] in cache.READER_VERSIONS else 'ready',
                  measurement_status='pending_reprocessing' if i['tool'] in cache.READER_VERSIONS else 'unsupported_reader',
                  output_tok_per_s=None,speed_calls=0,eligible_calls=None,coverage=None,groups=[]) for i in items]}
    with closing(cache.connect(readonly=True)) as conn:
        conn.execute('BEGIN'); state=cache.meta(conn); result=[]
        for item in items:
            tool=item['tool'];sid=item['session_id']
            status,reason=_state(conn,state,tool,sid,target)
            summary=_session_summary(conn,tool,sid,start,end)
            if reason:
                summary['measurement_status']=reason
                if reason=='pending_reprocessing':
                    summary['eligible_calls']=None;summary['coverage']=None
            result.append(dict(item,cache_state=status,**summary))
        conn.commit()
        return {'schema_version':1,'scope':scope,'measurement_generation':int(state.get('published_generation',0)), 'sessions':result}


def ensure_sessions(items,day_from,day_to,refresh=False):
    items=validate_sessions(items)
    cache.speed_report._day_bounds_ms(day_from,day_to)
    needed=sorted((i for i in items if i['tool'] in cache.READER_VERSIONS),key=lambda i:(i['tool'],i['session_id']))
    if not needed:
        return {'cache':{'state':'ready','job_id':None}}
    target=cache.primary_generation()
    with closing(cache.connect()) as conn:
        conn.execute('BEGIN IMMEDIATE')
        target=cache.primary_generation(); metadata=cache.meta(conn)
        if not refresh:
            needed=[i for i in needed if not cache.failure(conn,day_from,day_to,i['tool'],i['session_id'],target=target)]
        if not needed:
            conn.commit()
            return {'cache':{'state':'error','job_id':None,'target_usage_generation':target}}
        request=json.dumps({'from':day_from,'to':day_to,'sessions':needed,'refresh':refresh,'target_generation':target},sort_keys=True)
        if not refresh and all(_state(conn,metadata,i['tool'],i['session_id'],target)[0]=='ready' for i in needed):
            conn.commit()
            return {'cache':{'state':'ready','job_id':None,'target_usage_generation':target}}
        row=conn.execute("SELECT * FROM speed_jobs WHERE request=? AND state IN ('queued','building') ORDER BY created_at DESC LIMIT 1",(request,)).fetchone()
        if row is None:
            job='speed-'+uuid.uuid4().hex
            conn.execute('INSERT INTO speed_jobs(id,request,state,created_at,updated_at) VALUES (?,?,?,?,?)',(job,request,'queued',time.time(),time.time()));conn.commit()
        else:
            job=row['id']
        conn.commit()
        cache.launch_worker(conn)
    return {'cache':{'state':'building','job_id':job,'target_usage_generation':cache.primary_generation()}}


def _safe_key(key):
    # Most sources already use opaque accounting IDs. Legacy anonymous file/line
    # identities remain private and have no public turn mapping.
    return key if key and '/' not in key and '\\' not in key else None


def timeline(tool,sid,*,date_from=None,date_to=None,model=None,kind=None,basis=None,max_points=1000,refresh=False,cache_only=False):
    validate_sessions([{'tool':tool,'session_id':sid}])
    if not 1<=max_points<=1000:
        raise ValueError('max_points must be between 1 and 1000')
    if bool(date_from)!=bool(date_to):
        raise ValueError('date_from and date_to must be paired')
    if tool not in cache.READER_VERSIONS:
        return {'schema_version':1,'tool':tool,'session_id':sid,'scope':{'kind':'date_range','from':date_from,'to':date_to} if date_from else {'kind':'entire_session'},
                'cache':{'state':'ready','job_id':None},'measurement_generation':0,
                'summary':{'measurement_status':'unsupported_reader','output_tok_per_s':None,
                           'speed_calls':0,'eligible_calls':None,'coverage':None,'groups':[]},
                'groups':[],'series':[],'bucketed':False,'total_responses':0,'returned_points':0,'truncated':False}
    today=datetime.now().date().isoformat()
    state=None if cache_only else cache.ensure(date_from or today,date_to or today,source=tool,session_id=sid,refresh=refresh)
    start,end=cache.speed_report._day_bounds_ms(date_from,date_to) if date_from else (None,None)
    with closing(cache.connect(readonly=cache_only)) as conn:
        conn.execute('BEGIN'); metadata=cache.meta(conn)
        if cache_only:
            state=cache.snapshot(conn,date_from or today,date_to or today,source=tool,session_id=sid)
        rows=_rows(conn,tool,sid,start,end)
        summary=summarize(rows)
        if not rows and state['state'] in ('building','stale','error'):
            summary['measurement_status']='read_failure' if state['state']=='error' else 'pending_reprocessing'
            summary['eligible_calls']=None;summary['coverage']=None
        # Summaries always use the complete population, independent of chart size.
        selected=rows  # Nonselected groups remain explicit gaps in this stream.
        series=[{'call_key':_safe_key(r['entry_key']),'turn_key':_safe_key(r['turn_key']),
                 'sequence':r['sequence'],'recorded_at_ms':r['recorded_at'] or None,
                 'timestamp_basis':r['timestamp_basis'],'stream_id':r['stream_id'],
                 'model':normalize_model_name(r['model']),'measurement_kind':r['speed_kind'],
                 'token_basis':r['speed_token_basis'],'speed_tokens':r['speed_tokens'],
                 'speed_ms':r['speed_ms'],'speed_calls':r['speed_calls'],
                 'output_tok_per_s':rate_from_totals(r['speed_tokens'],r['speed_ms']),
                 'measurement_status':r['speed_status'],'eligible':bool(r['eligible'])} for r in selected]
        if model or kind or basis:
            for p in series:
                if (model and p['model']!=normalize_model_name(model)) or (kind and p['measurement_kind']!=kind) or (basis and p['token_basis']!=basis):
                    p['output_tok_per_s']=None
                    p['measurement_status']='group_boundary'
        previous_owner={}
        for p,r in zip(series,selected):
            prior=previous_owner.get(p['stream_id'])
            p['break_before']=prior is not None and prior!=r['input_owner']
            previous_owner[p['stream_id']]=r['input_owner']
        bucketed=False;truncated=False
        if len(series)>max_points:
            # Boundaries and explicit gaps remain separate. If there are more
            # boundaries than the point budget, paginate rather than drop gaps.
            series,bucketed,truncated=_bucket(series,max_points)
        conn.commit()
    state['published_generation']=int(metadata.get('published_generation',0))
    return {'schema_version':1,'tool':tool,'session_id':sid,
            'scope':{'kind':'date_range','from':date_from,'to':date_to} if date_from else {'kind':'entire_session'},
            'cache':state,'measurement_generation':state['published_generation'],
            'summary':summary,'groups':summary['groups'],'series':series,'bucketed':bucketed,
            'total_responses':len(selected),'returned_points':len(series),
            'truncated':truncated}


def _bucket(series,limit):
    # Ordered additive buckets never cross a missing row, stream or metric group.
    # Consecutive missing/filtered responses need one explicit gap marker. Keeping
    # thousands of null rows can otherwise hide every later measured response.
    compact=[]
    for p in series:
        if p['output_tok_per_s'] is None and compact and compact[-1]['output_tok_per_s'] is None and compact[-1]['stream_id']==p['stream_id']:
            gap=compact[-1]
            gap['gap_responses']=gap.get('gap_responses',1)+p.get('gap_responses',1)
            gap['call_key']=None;gap['turn_key']=None;gap['bucketed']=True
            gap['last_recorded_at_ms']=p['recorded_at_ms']
        else:
            compact.append(dict(p))
    width=max(2,(len(compact)+limit-1)//limit); result=[]; pending=[]; previous=None
    def flush():
        if not pending:
            return
        item=dict(pending[0]); item['bucketed']=len(pending)>1
        if len(pending)>1:
            for k in ('speed_tokens','speed_ms','speed_calls'):
                item[k]=sum(p[k] for p in pending)
            item['output_tok_per_s']=rate_from_totals(item['speed_tokens'],item['speed_ms'])
            item['call_key']=None; item['turn_key']=None
            item['min_call_rate']=min(p['output_tok_per_s'] for p in pending)
            item['max_call_rate']=max(p['output_tok_per_s'] for p in pending)
            item['last_recorded_at_ms']=pending[-1]['recorded_at_ms']
            item['eligible_calls']=sum(p['eligible'] for p in pending)
            item['coverage']=item['speed_calls']/item['eligible_calls'] if item['eligible_calls'] else None
        result.append(item); pending.clear()
    for p in compact:
        key=(p['stream_id'],p['model'],p['measurement_kind'],p['token_basis'])
        if p['output_tok_per_s'] is None:
            flush(); result.append(p); previous=None
        else:
            if p.get('break_before') or key!=previous or len(pending)>=width:
                flush()
            pending.append(p); previous=key
    flush()
    # Truncation is explicit. Aggregate remains complete; no false links because
    # later points are omitted, never subsampled across intervening boundaries.
    return result[:limit],True,len(result)>limit
