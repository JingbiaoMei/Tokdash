"""Single-flight, resource-bounded timing extraction in a separate process."""
from __future__ import annotations
import gc
import json
import os
import sys
from pathlib import Path
import sqlite3
import time
from contextlib import closing
from datetime import datetime

from . import speed_cache as cache, speed_native
from .compute import _collect_parser_file, _sync_usage_store
from .sources.coding_tools import BaseParser, CodingToolsUsageTracker, connect_sqlite_readonly, _opencode_message_table, _mimo_imported_message_ids
from .usage_store import UsageEntryStore, usage_db_path, _entry_key, MEASUREMENT_GENERATION_META_KEY
from .speed_mode import collect_timings, canonical_turn_key
from .output_speed import unmeasured, sanitize_stored
from .output_speed_readers import request_window_timing


def file_signature(path):
    st=Path(path).stat()
    return json.dumps((st.st_dev,st.st_ino,st.st_mtime_ns,st.st_size))


def session_mapping(source, file_sig):
    """Use the existing physical-file session reader, never a harness-wide loader."""
    from . import sessions
    parser = {'codex':sessions._parse_codex_session_file,'kimi':sessions._parse_kimi_session_file,
              'omp':sessions._parse_omp_session_file,'dsh':sessions._parse_dsh_session_file,
              'qwen_code':sessions._parse_qwen_code_session_file}[source]
    with collect_timings(False):
        parsed = getattr(parser,'raising',parser)(*file_sig)
    if not parsed:
        return {}
    if source == 'qwen_code':
        turns,meta=parsed
        sid=str(meta.get('session_id') or '')
    else:
        turns=parsed.get('turns',[])
        sid=str(parsed['session_id'])
    mapping={}
    for i,t in enumerate(turns):
        key=t.get('_event_key')
        if key:
            key = canonical_turn_key(source,str(key))
            mapping[key]=(sid,key,str(t.get('_stream_id') or 'main'),i,int(t.get('timestamp_ms') or 0),'usage_record')
    return mapping


def native_rows(source, session_id=''):
    """One read-only snapshot per DB. Native eligibility and timing share rows."""
    for db in speed_native.paths().get(source,()):
        if not db.exists():
            continue
        with closing(connect_sqlite_readonly(db)) as conn:
            conn.execute('BEGIN')
            table = _opencode_message_table(conn) if source != 'mimo' else 'message'
            role = "type='assistant'" if table=='session_message' else "json_extract(data,'$.role')='assistant'"
            imported = _mimo_imported_message_ids(conn) if source=='mimo' else set()
            clause=' AND session_id=?' if session_id else ''
            sql=f'''SELECT id,session_id,time_created,
              COALESCE(NULLIF(json_extract(data,'$.modelID'),''),json_extract(data,'$.model.id'),'unknown'),
              json_extract(data,'$.tokens.input'),json_extract(data,'$.tokens.output'),
              json_extract(data,'$.tokens.reasoning'),json_extract(data,'$.tokens.cache.read'),
              json_extract(data,'$.tokens.cache.write'),json_extract(data,'$.time.created'),
              json_extract(data,'$.time.completed'),json_extract(data,'$.error'),json_extract(data,'$.finish'),
              json_extract(data,'$.agent'),json_extract(data,'$.mode')
              FROM {table} WHERE json_valid(data) AND {role}
              AND json_type(data,'$.tokens')='object' {clause} ORDER BY time_created,id'''
            sequence={}
            for key,sid,ts,model,tin,out,reason,read,write,start,end,error,finish,agent,mode in conn.execute(sql,(session_id,) if session_id else ()):
                if str(key) in imported:
                    continue
                raw_out,raw_reason=out,reason
                out=BaseParser._i(out); reason=BaseParser._i(reason)
                if not any((BaseParser._i(tin),out,reason,BaseParser._i(read),BaseParser._i(write))):
                    continue
                timing=request_window_timing(raw_out,raw_reason,start,end,error,finish,
                         ensemble=source=='mimo' and (agent=='max' or mode=='max'))
                sid=str(sid); n=sequence.get(sid,0); sequence[sid]=n+1
                row=(source,str(key),'native:all' if not session_id else 'session:'+session_id,sid,int(ts),str(model),out+reason,reason,int(out+reason>0),*[timing[k] for k in cache.FIELDS])
                member=(source,str(key),sid,str(key),str(agent or 'main'),n,int(ts),'message_time_created')
                yield row,member


def _discard_file_timing(conn,source,owner):
    empty=unmeasured()
    conn.execute('UPDATE speed_stage SET '+','.join(k+'=?' for k in cache.FIELDS)+' WHERE source=? AND input_owner=?',(*[empty[k] for k in cache.FIELDS],source,owner))


def _progress(conn, job, completed, total):
    conn.execute('UPDATE speed_jobs SET completed_inputs=?,total_inputs=?,updated_at=? WHERE id=?', (completed,total,time.time(),job))
    conn.commit()


def peak_rss_mb():
    """Optional telemetry; macOS reports bytes, other resource platforms KiB."""
    try:
        import resource
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return value / (1024 * 1024 if sys.platform == 'darwin' else 1024)
    except (ImportError, AttributeError, OSError, ValueError):
        return None


def build(conn, job, request, *, sync=True, failure_context=None):
    owned_job = failure_context is not None
    failure_context = failure_context if failure_context is not None else {}
    tracker=CodingToolsUsageTracker()
    wanted=request.get('source') or ''
    file_sources=set(cache.READER_VERSIONS)-set(speed_native.SOURCES)
    if wanted:
        file_sources &= {wanted}
    tracker.parsers={k:v for k,v in tracker.parsers.items() if k in file_sources}
    # Lightweight accounting sync owns append freshness; no report, activity or
    # source-native aggregation and timing mode stays disabled throughout.
    with collect_timings(False):
        if sync:
            _sync_usage_store(tracker)
    native_sources=set(speed_native.SOURCES) & ({wanted} if wanted else set(speed_native.SOURCES))
    native_scopes={(source,('session:'+request['session_id']) if request.get('session_id') else 'native:all') for source in native_sources}
    if request.get('sessions'):
        native_scopes={(item['tool'],'session:'+item['session_id']) for item in request['sessions'] if item['tool'] in speed_native.SOURCES}
    # Every session of a native source shares its DB/WAL identity. Discover it
    # once for freshness reconciliation; SQL filters out unchanged unrelated
    # scopes instead of materializing them and statting the same DB per session.
    build_identity=cache.identity()
    native_snapshot=json.loads(build_identity)['native']
    native_signatures={source:json.dumps([part for part in native_snapshot if part[0]==source]) for source in speed_native.SOURCES}
    store=UsageEntryStore()
    with closing(store.read_connection()) as primary:
        primary.execute('BEGIN')
        row=primary.execute('SELECT value FROM meta WHERE key=?',(MEASUREMENT_GENERATION_META_KEY,)).fetchone()
        generation=int(row[0]) if row else 0
        previous=[]
        for source in speed_native.SOURCES:
            owners={owner for name,owner in native_scopes if name==source}
            if owners:
                owners.add('native:all')  # Check whether a full index covers requested sessions.
            clause=' OR input_owner IN ('+','.join('?' for _ in owners)+')' if owners else ''
            previous.extend(conn.execute('SELECT * FROM speed_inputs WHERE source=? AND (signature IS NOT ? OR reader_version IS NOT ?'+clause+')',
                                         (source,native_signatures[source],cache.READER_VERSIONS[source],*sorted(owners))))
        selected=set()
        if request.get('sessions'):
            for item in request['sessions']:
                selected.update((item['tool'],r[0]) for r in primary.execute('SELECT file_path FROM usage_session_inputs WHERE source=? AND session_id=?',(item['tool'],item['session_id'])))
                selected.update((item['tool'],r[0]) for r in primary.execute('SELECT file_path FROM session_records WHERE tool=? AND session_id=?',(item['tool'],item['session_id'])))
        elif request.get('session_id'):
            keys=request.get('keys',[])
            for offset in range(0,len(keys),800):
                chunk=keys[offset:offset+800]
                selected.update((r[0],r[1]) for r in primary.execute('SELECT DISTINCT source,file_path FROM usage_entries WHERE source=? AND entry_key IN ('+','.join('?' for _ in chunk)+')',(wanted,*chunk)))
            # Recorded logical membership includes continuation/subagent inputs.
            selected.update((wanted,r[0]) for r in primary.execute('SELECT file_path FROM usage_session_inputs WHERE source=? AND session_id=?',(wanted,request['session_id'])))
            selected.update((wanted,r[0]) for r in primary.execute('SELECT file_path FROM session_records WHERE tool=? AND session_id=?',(wanted,request['session_id'])))
        else:
            start,end=cache.speed_report._day_bounds_ms(request['from'],request['to'])
            selected.update((r[0],r[1]) for r in primary.execute('SELECT DISTINCT source,file_path FROM usage_entries WHERE timestamp>=? AND timestamp<?',(start,end)) if r[0] in file_sources)
        requested_inputs=set(selected)
        bounded = bool(request.get('session_id') or request.get('sessions'))
        if bounded:
            for source,owner in selected:
                if source not in speed_native.SOURCES:
                    previous.extend(conn.execute('SELECT * FROM speed_inputs WHERE source=? AND input_owner=?',(source,owner)))
        else:
            for source in file_sources:
                previous.extend(conn.execute('SELECT * FROM speed_inputs WHERE source=?',(source,)))
        # Full model builds reconcile previously indexed owners, including removals.
        # A session build never stages an unrelated population.
        if not bounded:
            selected.update((r['source'],r['input_owner']) for r in previous if r['source'] in file_sources)
        conn.execute('DROP TABLE IF EXISTS speed_stage')
        conn.execute('CREATE TABLE speed_stage AS SELECT * FROM speed_responses WHERE 0')
        conn.execute('CREATE UNIQUE INDEX idx_speed_stage_key ON speed_stage(source,entry_key)')
        conn.execute('CREATE INDEX idx_speed_stage_owner ON speed_stage(source,input_owner)')
        conn.execute('DROP TABLE IF EXISTS speed_member_stage')
        conn.execute('CREATE TABLE speed_member_stage AS SELECT * FROM speed_session_members WHERE 0')
        # Native overlays replace canonical membership by call and remove a
        # session's old calls. CTAS does not inherit the published indexes;
        # leaving these deletes unindexed makes full native ingest quadratic.
        conn.execute('CREATE INDEX idx_speed_member_stage_key ON speed_member_stage(source,call_key)')
        conn.execute('CREATE INDEX idx_speed_member_stage_session ON speed_member_stage(source,session_id)')
        staged_inputs=[]
        conn.execute('DROP TABLE IF EXISTS temp.speed_accounting_basis')
        conn.execute('CREATE TEMP TABLE speed_accounting_basis(source TEXT,entry_key TEXT,input INTEGER,cache_read INTEGER,cache_write INTEGER,PRIMARY KEY(source,entry_key))')
        for source,owner in sorted(selected):
            if not owner:
                continue
            rows=primary.execute('SELECT source,entry_key,file_path,timestamp,model,input,output,cache_read,cache_write,reasoning FROM usage_entries WHERE source=? AND file_path=?',(source,owner))
            conn.executemany('INSERT INTO speed_stage VALUES ('+','.join('?' for _ in range(15))+')',
               ((r['source'],r['entry_key'],owner,'',r['timestamp'],r['model'],r['output'],r['reasoning'],int(r['output']>0 or source=='qwen_code' and r['reasoning']>0),*[unmeasured()[k] for k in cache.FIELDS]) for r in rows))
        session_inputs=[]
        for source,owner in sorted(selected):
            session_inputs.extend(primary.execute('SELECT source,session_id,file_path FROM usage_session_inputs WHERE source=? AND file_path=?',(source,owner)))
        affected_sessions={(r[0],r[1]) for r in session_inputs}
        for source,owner in selected:
            affected_sessions.update((r[0],r[1]) for r in conn.execute('SELECT DISTINCT m.source,m.session_id FROM speed_responses r INDEXED BY idx_speed_owner CROSS JOIN speed_session_members m ON m.source=r.source AND m.call_key=r.entry_key WHERE r.source=? AND r.input_owner=?',(source,owner)))
        session_inputs=[]
        for source,sid in affected_sessions:
            session_inputs.extend(primary.execute('SELECT source,session_id,file_path FROM usage_session_inputs WHERE source=? AND session_id=?',(source,sid)))
        for source,owner in sorted(selected):
            conn.executemany('INSERT OR REPLACE INTO speed_accounting_basis VALUES (?,?,?,?,?)',
                (tuple(r) for r in primary.execute('SELECT source,entry_key,input,cache_read,cache_write FROM usage_entries WHERE source=? AND file_path=?',(source,owner))))
        if request.get('session_id') or request.get('sessions'):
            # Keep the last full-window census during bounded session work.
            # Its windows retain their verified generations; model work rebuilds
            # the census instead of presenting a partial session as a full window.
            days=None
        else:
            days=list(primary.execute("SELECT source,date(timestamp/1000,'unixepoch','localtime'),count(*) FROM usage_entries GROUP BY source,date(timestamp/1000,'unixepoch','localtime')"))
        # Canonical snapshot now lives as scalars in staging; do not pin primary
        # WAL during expensive file parsing.
        primary.commit()
    conn.commit()
    previous={(r['source'],r['input_owner']):dict(r) for r in previous}
    identities={}
    requested_native=set(native_scopes)
    # A requested full read supersedes partial scopes. An existing full index
    # can cover a requested session only while its signature/version is current.
    # A stale full index must not suppress the bounded session read.
    full_sources={source for source,owner in requested_native if owner=='native:all'}
    for source,owner in tuple(requested_native):
        full=previous.get((source,'native:all'))
        if owner.startswith('session:') and full and full['state']=='ready' \
                and full['reader_version']==cache.READER_VERSIONS[source] \
                and full['signature']==native_signatures[source]:
            requested_native.discard((source,owner))
            requested_native.add((source,'native:all'))
            full_sources.add(source)
    native_scopes={pair for pair in native_scopes if pair[1]=='native:all' or pair[0] not in full_sources}
    native_scopes |= requested_native
    retired_native=set()
    deferred_native=[]
    native_freshness_scopes=0
    # Freshness-only invalidation does not copy or rewrite the last-good rows.
    for (source,owner),old in previous.items():
        if source not in speed_native.SOURCES or (source,owner) in native_scopes:
            continue
        native_freshness_scopes+=1
        observed=native_signatures[source]
        old_dbs={row[1] for row in json.loads(old['signature']) if not row[1].endswith('-wal')} if old['signature'] else set()
        current_dbs={row[1] for row in json.loads(observed) if not row[1].endswith('-wal')}
        if not old_dbs <= current_dbs:
            retired_native.add((source,owner))
        elif old['signature']!=observed or old['reader_version']!=cache.READER_VERSIONS[source]:
            deferred_native.append((source,owner))
    total=len(selected)+len(native_scopes); completed=0
    parsed_files=reused_files=parsed_native=reused_native=0
    cpu_start=time.process_time(); wall_start=time.monotonic()
    for source,owner in sorted(selected):
        failure_context.clear(); failure_context.update(source=source,input_owner=owner,signature=cache.input_signature(owner),usage_generation=generation)
        old=previous.get((source,owner))
        if not conn.execute('SELECT 1 FROM speed_stage WHERE source=? AND input_owner=?',(source,owner)).fetchone():
            completed+=1; _progress(conn,job,completed,total); continue
        parser=tracker.parsers.get(source)
        if parser is None:
            parser=CodingToolsUsageTracker().parsers[source]
        try:
            observed=file_signature(owner)
        except FileNotFoundError:
            # Durable accounting whose original input was already absent retains
            # an explicit unknown population. It is not missing telemetry.
            staged_inputs.append((source,owner,'missing',cache.READER_VERSIONS[source],generation,'pending','original input unavailable'))
            completed+=1; _progress(conn,job,completed,total); continue
        identities[(source,owner)]=observed
        failure_context['signature']=observed
        if old and old['signature']==observed and old['reader_version']==cache.READER_VERSIONS[source] and old['state']=='ready':
            reused_files+=1
            conn.execute('''UPDATE speed_stage AS s SET (session_id,speed_tokens,speed_ms,speed_calls,speed_kind,speed_token_basis,speed_status)=
               (SELECT session_id,speed_tokens,speed_ms,speed_calls,speed_kind,speed_token_basis,speed_status FROM speed_responses p
                WHERE p.source=s.source AND p.entry_key=s.entry_key AND p.input_owner=s.input_owner AND p.timestamp=s.timestamp AND p.model=s.model AND p.output=s.output AND p.reasoning=s.reasoning)
               WHERE source=? AND input_owner=? AND EXISTS (SELECT 1 FROM speed_responses p WHERE p.source=s.source AND p.entry_key=s.entry_key AND p.input_owner=s.input_owner AND p.timestamp=s.timestamp AND p.model=s.model AND p.output=s.output AND p.reasoning=s.reasoning)''',(source,owner))
            # Force the narrow owning-input seek before membership lookup. With
            # JOIN SQLite otherwise walks every source member once per file.
            conn.execute('INSERT INTO speed_member_stage SELECT m.* FROM speed_stage s CROSS JOIN speed_session_members m ON s.source=m.source AND s.entry_key=m.call_key WHERE s.source=? AND s.input_owner=?',(source,owner))
        elif (request.get('session_id') or request.get('sessions')) and (source,owner) not in requested_inputs:
            staged_inputs.append((source,owner,observed,cache.READER_VERSIONS[source],generation,'pending','input outside requested session changed'))
            completed+=1; _progress(conn,job,completed,total); continue
        else:
            parsed_files+=1
            st=Path(owner).stat(); sig=(owner,st.st_mtime_ns,st.st_size)
            # Fail an unavailable read before permissive source readers can turn
            # it into a successful empty population.
            with Path(owner).open('rb'):
                pass
            context=store.file_contexts(source,paths=(owner,))
            with collect_timings():
                entries=_collect_parser_file(parser,sig,file_context=context)
            mapping=session_mapping(source,sig)
            matched=set()
            for entry in entries:
                key=_entry_key(entry)
                canonical=conn.execute('SELECT * FROM speed_stage WHERE source=? AND entry_key=? AND input_owner=?',(source,key,owner)).fetchone()
                if canonical is None:
                    continue  # canonical usage owner suppresses forks/copies
                matched.add(key)
                basis=conn.execute('SELECT input,cache_read,cache_write FROM speed_accounting_basis WHERE source=? AND entry_key=?',(source,key)).fetchone()
                if basis is None or tuple(basis)!=(int(entry.get('input') or 0),int(entry.get('cacheRead') or 0),int(entry.get('cacheWrite') or 0)) or int(entry['timestamp'])!=canonical['timestamp']:
                    raise RuntimeError('timing/accounting snapshot mismatch')
                if (int(entry.get('output') or 0),int(entry.get('reasoning') or 0),str(entry.get('model') or 'unknown')) != (canonical['output'],canonical['reasoning'],canonical['model']):
                    raise RuntimeError('timing/accounting snapshot mismatch')
                speed=sanitize_stored(entry.get('_speed') or unmeasured('missing_timing'))
                member=mapping.get(key)
                sid=member[0] if member else ''
                conn.execute('UPDATE speed_stage SET session_id=?,'+','.join(k+'=?' for k in cache.FIELDS)+' WHERE source=? AND entry_key=?',(sid,*[speed[k] for k in cache.FIELDS],source,key))
                if member:
                    conn.execute('INSERT INTO speed_member_stage VALUES (?,?,?,?,?,?,?,?)',(source,key,*member))
            expected={r[0] for r in conn.execute('SELECT entry_key FROM speed_stage WHERE source=? AND input_owner=?',(source,owner))}
            if matched!=expected and file_signature(owner)==observed:
                raise RuntimeError('timing/accounting snapshot mismatch: incomplete input read')
            del entries,mapping
            gc.collect()
        if file_signature(owner)!=observed:
            # A hot log is discarded individually. Its pinned accounting rows
            # remain eligible but unknown; stable inputs can still publish.
            _discard_file_timing(conn,source,owner)
            staged_inputs.append((source,owner,observed,cache.READER_VERSIONS[source],generation,'pending','input changed during timing extraction'))
            identities.pop((source,owner),None)
            completed+=1; _progress(conn,job,completed,total); continue
        staged_inputs.append((source,owner,observed,cache.READER_VERSIONS[source],generation,'ready',None))
        completed+=1; _progress(conn,job,completed,total)
        # Duty budget between inputs plus niceness/one CPU limits contention.
        duty=float(os.environ.get('TOKDASH_SPEED_CPU_FRACTION','0.25'))
        delay=(time.process_time()-cpu_start)/max(.05,min(1,duty))-(time.monotonic()-wall_start)
        if delay>0:
            time.sleep(min(delay,30))
    for source,owner in sorted(native_scopes):
        failure_context.clear(); failure_context.update(source=source,input_owner=owner,usage_generation=generation)
        observed=json.dumps(speed_native.signature(source))
        failure_context['signature']=observed
        old=previous.get((source,owner))
        current=bool(old and old['signature']==observed and old['reader_version']==cache.READER_VERSIONS[source] and old['state']=='ready')
        if (source,owner) not in requested_native and not current:
            # Retain the last-good population without reading unrelated inputs.
            # Its original signature stays visible and the scope stays pending.
            # A removed database retires the scope, rather than retaining ghosts.
            old_dbs={row[1] for row in json.loads(old['signature']) if not row[1].endswith('-wal')} if old else set()
            current_dbs={row[1] for row in json.loads(observed) if not row[1].endswith('-wal')}
            if old and old_dbs <= current_dbs:
                conn.execute('INSERT INTO speed_stage SELECT * FROM speed_responses WHERE source=? AND input_owner=?',(source,owner))
                conn.execute('INSERT INTO speed_member_stage SELECT m.* FROM speed_responses r CROSS JOIN speed_session_members m ON r.source=m.source AND r.entry_key=m.call_key WHERE r.source=? AND r.input_owner=?',(source,owner))
                staged_inputs.append((source,owner,old['signature'],old['reader_version'],generation,'pending','native input outside requested scope changed'))
            completed+=1; _progress(conn,job,completed,total); continue
        identities[(source,owner)]=observed
        if current:
            reused_native+=1
            # Reuse even a large full index in place when it covers this session.
        else:
            parsed_native+=1
            sid=owner[8:] if owner.startswith('session:') else ''
            if sid:
                # Overlay this session on a retained stale full population.
                # Delete removed calls too; all native memberships use the
                # verified relational session ID and canonical message ID.
                conn.execute('DELETE FROM speed_stage WHERE source=? AND session_id=?',(source,sid))
                conn.execute('DELETE FROM speed_member_stage WHERE source=? AND session_id=?',(source,sid))
            count=0
            for row,member in native_rows(source,sid):
                conn.execute('INSERT OR REPLACE INTO speed_stage VALUES ('+','.join('?' for _ in row)+')',row)
                conn.execute('DELETE FROM speed_member_stage WHERE source=? AND call_key=?',row[:2])
                conn.execute('INSERT INTO speed_member_stage VALUES (?,?,?,?,?,?,?,?)',member)
                count+=1
                if count%1000==0:
                    time.sleep(.02)
            cache.put_meta(conn,**{f'{source}_bounds_scans':int(cache.meta(conn).get(f'{source}_bounds_scans',0))+int(not sid)})
        if json.dumps(speed_native.signature(source))!=observed:
            raise RuntimeError('native DB/WAL changed during timing extraction')
        staged_inputs.append((source,owner,observed,cache.READER_VERSIONS[source],generation,'ready',None))
        completed+=1; _progress(conn,job,completed,total)
    # Final signature check before atomic publication. Generation is the pinned
    # snapshot, even if an unrelated primary writer advanced in the meantime.
    final_native_signatures={}
    for (source,owner),observed in identities.items():
        failure_context.clear(); failure_context.update(source=source,input_owner=owner,signature=observed,usage_generation=generation)
        if source in speed_native.SOURCES:
            if source not in final_native_signatures:
                final_native_signatures[source]=json.dumps(speed_native.signature(source))
            current=final_native_signatures[source]
        else:
            current=file_signature(owner)
        if current!=observed:
            if source in speed_native.SOURCES:
                raise RuntimeError('native DB/WAL changed before publication')
            _discard_file_timing(conn,source,owner)
            staged_inputs=[tuple(list(r[:5])+['pending','input changed before publication']) if r[:2]==(source,owner) else r for r in staged_inputs]
    # Transaction/ownership/publication failures belong to the job, rather than
    # whichever input happened to be validated last.
    failure_context.clear(); failure_context.update(usage_generation=generation)
    conn.commit()
    conn.execute('BEGIN IMMEDIATE')
    if owned_job:
        owner=conn.execute('SELECT state,pid,worker_token FROM speed_jobs WHERE id=?',(job,)).fetchone()
        if not owner or owner['state']!='building' or owner['pid']!=os.getpid() or not cache._alive(owner['pid'],owner['worker_token']):
            raise RuntimeError('timing job ownership lost before publication')
    cache.ensure_report_index(conn)
    affected_sessions.update((r[0],r[1]) for r in conn.execute('SELECT m.source,m.session_id FROM speed_stage s CROSS JOIN speed_session_members m ON m.source=s.source AND m.call_key=s.entry_key'))
    unchanged_files=set()
    for source,owner in selected:
        if conn.execute('''SELECT EXISTS(SELECT * FROM speed_stage WHERE source=? AND input_owner=? EXCEPT SELECT * FROM speed_responses WHERE source=? AND input_owner=?)
            OR EXISTS(SELECT * FROM speed_responses WHERE source=? AND input_owner=? EXCEPT SELECT * FROM speed_stage WHERE source=? AND input_owner=?)''',(source,owner,source,owner,source,owner,source,owner)).fetchone()[0]==0:
            members='SELECT m.* FROM {table} m WHERE m.source=? AND m.call_key IN (SELECT entry_key FROM speed_stage WHERE source=? AND input_owner=?)'
            fresh=members.format(table='speed_member_stage')
            published=members.format(table='speed_session_members')
            args=(source,source,owner)*4
            if conn.execute(f'SELECT EXISTS({fresh} EXCEPT {published}) OR EXISTS({published} EXCEPT {fresh})',args).fetchone()[0]:
                continue
            unchanged_files.add((source,owner))
            conn.execute('DELETE FROM speed_member_stage WHERE source=? AND call_key IN (SELECT entry_key FROM speed_stage WHERE source=? AND input_owner=?)',(source,source,owner))
            conn.execute('DELETE FROM speed_stage WHERE source=? AND input_owner=?',(source,owner))
    # Replace only contributing populations, in one publication transaction.
    staged_rows=conn.execute('SELECT count(*) FROM speed_stage').fetchone()[0]
    publication_start=conn.total_changes
    replaced=(set(selected)-unchanged_files) | retired_native | {(r[0],r[1]) for r in staged_inputs if r[0] in speed_native.SOURCES and (r[0],r[1]) in requested_native and not (previous.get((r[0],r[1]),{}).get('signature')==r[2] and previous.get((r[0],r[1]),{}).get('state')=='ready' and previous.get((r[0],r[1]),{}).get('reader_version')==r[3])}
    for source,owner in replaced:
        if source in speed_native.SOURCES and owner=='native:all':
            conn.execute('DELETE FROM speed_session_members WHERE source=?',(source,))
            conn.execute('DELETE FROM speed_responses WHERE source=?',(source,))
            conn.execute('DELETE FROM speed_inputs WHERE source=?',(source,))
            conn.execute('DELETE FROM speed_sessions WHERE source=?',(source,))
        elif source in speed_native.SOURCES:
            sid=owner[8:]
            conn.execute('DELETE FROM speed_responses WHERE source=? AND entry_key IN (SELECT call_key FROM speed_session_members WHERE source=? AND session_id=?)',(source,source,sid))
            conn.execute('DELETE FROM speed_session_members WHERE source=? AND session_id=?',(source,sid))
            conn.execute('DELETE FROM speed_sessions WHERE source=? AND session_id=?',(source,sid))
            conn.execute('DELETE FROM speed_inputs WHERE source=? AND input_owner=?',(source,owner))
        else:
            conn.execute('DELETE FROM speed_session_members WHERE source=? AND call_key IN (SELECT entry_key FROM speed_responses WHERE source=? AND input_owner=?)',(source,source,owner))
            conn.execute('DELETE FROM speed_responses WHERE source=? AND input_owner=?',(source,owner))
            conn.execute('DELETE FROM speed_inputs WHERE source=? AND input_owner=?',(source,owner))
    # Ownership can move between physical inputs after deduplication. Remove the
    # old membership for those exact canonical keys before installing new owners.
    conn.execute('DELETE FROM speed_session_members WHERE (source,call_key) IN (SELECT source,entry_key FROM speed_stage)')
    conn.execute('INSERT OR REPLACE INTO speed_responses SELECT * FROM speed_stage')
    conn.execute('INSERT OR REPLACE INTO speed_session_members SELECT * FROM speed_member_stage')
    conn.executemany('INSERT OR REPLACE INTO speed_inputs VALUES (?,?,?,?,?,?,?)',staged_inputs)
    conn.executemany("UPDATE speed_inputs SET state='pending',last_error='native input outside requested scope changed' WHERE source=? AND input_owner=?",deferred_native)
    conn.executemany('DELETE FROM speed_sessions WHERE source=? AND session_id=?',affected_sessions)
    ready_inputs={(r[0],r[1]) for r in staged_inputs if r[5]=='ready'}
    grouped_inputs={}
    for source,sid,path in session_inputs:
        grouped_inputs.setdefault((source,sid),set()).add((source,path))
    for (source,sid),owners in grouped_inputs.items():
        if owners <= ready_inputs:
            conn.execute('INSERT INTO speed_sessions VALUES (?,?,?,?)',(source,sid,generation,'ready'))
    for source,owner in ready_inputs:
        if source in speed_native.SOURCES and owner.startswith('session:'):
            conn.execute('INSERT OR REPLACE INTO speed_sessions VALUES (?,?,?,?)',(source,owner[8:],generation,'ready'))
    for source,sid in conn.execute('SELECT DISTINCT source,session_id FROM speed_member_stage'):
        if (source,'native:all') in ready_inputs:
            conn.execute('INSERT OR REPLACE INTO speed_sessions VALUES (?,?,?,?)',(source,sid,generation,'ready'))
    if days is not None:
        conn.execute('DELETE FROM speed_source_days')
        conn.executemany('INSERT INTO speed_source_days VALUES (?,?,?)',days)
    # Older publications could promote every model window after a bounded or
    # differently filtered build. Retire those markers once, retaining valid
    # timing rows. Only the exact requested full window receives this generation.
    if cache.meta(conn).get('publication_version')!=str(cache.PUBLICATION_VERSION):
        conn.execute('DELETE FROM speed_windows')
    if not request.get('session_id') and not request.get('sessions'):
        conn.execute('INSERT OR REPLACE INTO speed_windows VALUES (?,?,?,?)',(wanted,request['from'],request['to'],generation))
    revision=int(cache.meta(conn).get('publication_revision',0))+1
    cache.put_meta(conn,published_generation=generation,identity=build_identity,updated_at=datetime.now().astimezone().isoformat(),schema_version=cache.SCHEMA_VERSION,publication_version=cache.PUBLICATION_VERSION,publication_revision=revision)
    for failed in conn.execute('SELECT * FROM speed_failures').fetchall():
        old_request=json.loads(failed['request']); scope=json.loads(failed['scope'])
        if (failed['source'],scope.get('input_owner')) in (selected|requested_native) or (failed['source'],'native:all') in requested_native or (not scope.get('input_owner') and old_request.get('source','')==wanted and old_request.get('session_id','')==request.get('session_id','') and old_request.get('sessions')==request.get('sessions') and old_request.get('from')==request.get('from') and old_request.get('to')==request.get('to')):
            conn.execute('DELETE FROM speed_failures WHERE job_id=?',(failed['job_id'],))
    conn.execute('DROP TABLE speed_stage')
    conn.execute('DROP TABLE speed_member_stage')
    cache.put_meta(conn,last_build_cpu_ms=(time.process_time()-cpu_start)*1000,
                   last_build_wall_ms=(time.monotonic()-wall_start)*1000,
                   last_build_peak_rss_mb=peak_rss_mb() or '',
                   last_build_counts=json.dumps(dict(parsed_files=parsed_files,reused_files=reused_files,
                       parsed_native_scopes=parsed_native,reused_native_scopes=reused_native,
                       native_freshness_scopes=native_freshness_scopes,
                       requested_file_inputs=len(requested_inputs),staged_rows=staged_rows,
                       publication_changes=conn.total_changes-publication_start)))
    conn.execute('UPDATE speed_jobs SET state=\'ready\',updated_at=? WHERE id=?',(time.time(),job))
    conn.commit()


def run_job(conn,row,*,sync=True):
    started=time.monotonic();cpu_started=time.process_time()
    job=row['id']
    context={}
    token=cache.speed_process.snapshot(os.getpid())[1] or ''
    claimed=conn.execute("UPDATE speed_jobs SET state='building',pid=?,worker_token=?,updated_at=? WHERE id=? AND state='queued'",(os.getpid(),token,time.time(),job)).rowcount
    conn.commit()
    if not claimed:
        return False
    try:
        build(conn,job,json.loads(row['request']),sync=sync,failure_context=context)
        return True
    except Exception as exc:
        conn.rollback()
        # Filesystem/SQLite exceptions can contain private paths. Keep detailed
        # diagnostics in the isolated worker log, not in public job payloads.
        error=str(exc) if isinstance(exc,RuntimeError) else f'{type(exc).__name__}: timing input read failed'
        conn.execute("UPDATE speed_jobs SET state='error',error=?,updated_at=? WHERE id=?",(error,time.time(),job))
        cache.record_failure(conn,job,json.loads(row['request']),error,context)
        conn.commit()
        print(f'{job}: {type(exc).__name__}: {exc}',flush=True)
        return False
    finally:
        try:
            cache.put_meta(conn,last_job_cpu_ms=(time.process_time()-cpu_started)*1000,
                           last_job_wall_ms=(time.monotonic()-started)*1000,
                           last_job_peak_rss_mb=peak_rss_mb() or '')
            conn.commit()
        except sqlite3.Error:
            conn.rollback()  # Telemetry never changes a committed job result.


def main():
    # These controls apply to the child only, never the API or live installation.
    if hasattr(os,'nice'):
        os.nice(10)
    if hasattr(os,'sched_getaffinity'):
        os.sched_setaffinity(0,{min(os.sched_getaffinity(0))})
    try:
        import resource
        cap=int(os.environ.get('TOKDASH_SPEED_MEMORY_MB','1024'))*1024*1024
        resource.setrlimit(resource.RLIMIT_AS,(cap,cap))
    except (ImportError,ValueError,OSError):
        pass
    from .filelock import process_lock
    with process_lock(Path(str(cache.cache_path())+'.worker.lock')):
        with closing(cache.connect()) as conn:
            cache.put_meta(conn,worker_pid=os.getpid(),worker_token=cache.speed_process.snapshot(os.getpid())[1] or '')
            conn.commit()
            while True:
                row=conn.execute("SELECT * FROM speed_jobs WHERE state='queued' ORDER BY created_at LIMIT 1").fetchone()
                if not row:
                    # Coordinate draining with launch_worker: an enqueue at the
                    # exit boundary must either be drained here or launch a child.
                    with process_lock(Path(str(cache.cache_path())+'.launch.lock')):
                        row=conn.execute("SELECT * FROM speed_jobs WHERE state='queued' ORDER BY created_at LIMIT 1").fetchone()
                        if not row:
                            cache.put_meta(conn,worker_pid=0,worker_token=''); conn.commit(); break
                run_job(conn,row)
                gc.collect()

if __name__=='__main__':
    main()
