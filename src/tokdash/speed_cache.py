"""Disposable, demand-built timing index. Core accounting never imports this module.

Requests read a published snapshot and enqueue work; only the separate worker
extracts timing. Responses and eligible counts belong to the same usage generation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import closing

from . import speed_report, speed_native
from .usage_store import UsageEntryStore, usage_db_path, MEASUREMENT_GENERATION_META_KEY
from .output_speed import unmeasured
from . import speed_process

SCHEMA_VERSION = 3
PUBLICATION_VERSION = 3
READER_VERSIONS = {'kimi': 1, 'omp': 1, 'codex': 3, 'dsh': 3, 'qwen_code': 2,
                   'opencode': 3, 'kilocode': 3, 'mimo': 3}
FIELDS = ('speed_tokens', 'speed_ms', 'speed_calls', 'speed_kind', 'speed_token_basis', 'speed_status')
COLUMNS = 'source,entry_key,input_owner,session_id,timestamp,model,output,reasoning,eligible,' + ','.join(FIELDS)


def cache_path():
    return Path(usage_db_path()).parent / 'output_speed.sqlite3'


def connect(path=None, *, readonly=False):
    path = Path(path or cache_path())
    if readonly:
        # An absent cache is an empty snapshot, not a reason to create a cache.
        conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5) if path.exists() else sqlite3.connect(':memory:')
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    # Only demand-built timing reads pay this bounded page-cache allowance.
    conn.execute('PRAGMA cache_size=-16384')
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='speed_sessions'").fetchone():
        controls = conn.execute("SELECT count(*) FROM sqlite_master WHERE name IN ('speed_failures','idx_speed_failure_source')").fetchone()[0]
        current = controls==2 and 'worker_token' in {r[1] for r in conn.execute('PRAGMA table_info(speed_jobs)')} and bool(conn.execute("SELECT 1 FROM speed_meta WHERE key='instance_id'").fetchone())
        if not readonly and not current:
            conn.execute('BEGIN IMMEDIATE')
            _control_schema(conn)
            conn.commit()
        conn.execute('CREATE TEMP VIEW usage_entries AS SELECT rowid AS rowid,* FROM speed_responses')
        return conn
    if readonly and path.exists():
        # A concurrently initializing file is still an absent published snapshot.
        conn.close()
        conn=sqlite3.connect(':memory:')
        conn.row_factory=sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.executescript('''
      BEGIN IMMEDIATE;
      CREATE TABLE IF NOT EXISTS speed_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS speed_inputs(
        source TEXT,input_owner TEXT,signature TEXT,reader_version INTEGER,
        usage_generation INTEGER,state TEXT,last_error TEXT,
        PRIMARY KEY(source,input_owner));
      CREATE TABLE IF NOT EXISTS speed_responses(
        source TEXT NOT NULL,entry_key TEXT NOT NULL,input_owner TEXT NOT NULL,
        session_id TEXT NOT NULL DEFAULT '',timestamp INTEGER NOT NULL,model TEXT NOT NULL,
        output INTEGER NOT NULL,reasoning INTEGER NOT NULL,eligible INTEGER NOT NULL,
        speed_tokens INTEGER NOT NULL,speed_ms REAL NOT NULL,speed_calls INTEGER NOT NULL,
        speed_kind TEXT NOT NULL,speed_token_basis TEXT NOT NULL,speed_status TEXT NOT NULL,
        PRIMARY KEY(source,entry_key));
      CREATE INDEX IF NOT EXISTS idx_speed_source_time ON speed_responses(source,timestamp);
      CREATE INDEX IF NOT EXISTS idx_speed_owner ON speed_responses(source,input_owner);
      CREATE INDEX IF NOT EXISTS idx_speed_measured_time ON speed_responses(timestamp)
        WHERE speed_calls > 0;
      CREATE INDEX IF NOT EXISTS idx_speed_report ON speed_responses(
        source,timestamp,model,eligible,speed_kind,speed_token_basis,speed_status,
        speed_tokens,speed_ms,speed_calls);
      CREATE TABLE IF NOT EXISTS speed_session_members(
        source TEXT,call_key TEXT,session_id TEXT,turn_key TEXT,stream_id TEXT,
        sequence INTEGER,recorded_at INTEGER,timestamp_basis TEXT,
        PRIMARY KEY(source,call_key,session_id));
      CREATE INDEX IF NOT EXISTS idx_speed_session ON speed_session_members(source,session_id,recorded_at);
      CREATE TABLE IF NOT EXISTS speed_sessions(
        source TEXT,session_id TEXT,usage_generation INTEGER,state TEXT,
        PRIMARY KEY(source,session_id));
      CREATE TABLE IF NOT EXISTS speed_windows(
        source TEXT,day_from TEXT,day_to TEXT,usage_generation INTEGER,
        PRIMARY KEY(source,day_from,day_to));
      CREATE TABLE IF NOT EXISTS speed_jobs(
        id TEXT PRIMARY KEY,request TEXT NOT NULL,state TEXT NOT NULL,
        completed_inputs INTEGER NOT NULL DEFAULT 0,total_inputs INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,updated_at REAL NOT NULL,error TEXT,pid INTEGER);
      CREATE TABLE IF NOT EXISTS speed_source_days(
        source TEXT,day TEXT,usage_rows INTEGER,PRIMARY KEY(source,day));
    ''')
    _control_schema(conn)
    # Existing SQL aggregation is shared with the old ephemeral test adapter.
    conn.execute('CREATE TEMP VIEW usage_entries AS SELECT rowid AS rowid,* FROM speed_responses')
    conn.commit()
    return conn


def _control_schema(conn):
    # Small control tables only; upgrading an existing history never rebuilds its indexes here.
    if 'worker_token' not in {r[1] for r in conn.execute('PRAGMA table_info(speed_jobs)')}:
        conn.execute('ALTER TABLE speed_jobs ADD COLUMN worker_token TEXT')
    conn.execute('''CREATE TABLE IF NOT EXISTS speed_failures(
        job_id TEXT PRIMARY KEY,source TEXT,request TEXT,scope TEXT,identity TEXT,
        generation INTEGER,error TEXT,created_at REAL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_speed_failure_source ON speed_failures(source)')
    conn.execute("INSERT OR IGNORE INTO speed_meta(key,value) VALUES ('instance_id',?)",(uuid.uuid4().hex,))


def scope_identity(source=''):
    full = identity()
    if not source:
        return full
    try:
        value = json.loads(full)
        value['readers'] = {source: value['readers'].get(source)}
        value['native'] = speed_native.signature(source) if source in speed_native.SOURCES else []
        return json.dumps(value, sort_keys=True)
    except (ValueError, KeyError, TypeError):
        return full


def input_signature(owner):
    try:
        stat = Path(owner).stat()
        return json.dumps((stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size))
    except OSError:
        return 'missing'


def record_failure(conn, job_id, request, error, scope=None):
    scope = scope or {}
    source = scope.get('source', request.get('source', ''))
    attempted = scope_identity(source)
    if source in speed_native.SOURCES and scope.get('signature'):
        value=json.loads(attempted)
        value['native']=json.loads(scope['signature'])
        attempted=json.dumps(value,sort_keys=True)
    conn.execute('INSERT OR REPLACE INTO speed_failures VALUES (?,?,?,?,?,?,?,?)',
        (job_id, source, json.dumps(request), json.dumps(scope), attempted,
         scope.get('usage_generation',primary_generation()), error, time.time()))


def failure(conn, day_from, day_to, source='', session_id='', *, target=None, current_only=True):
    """Only failures belonging to this population suppress retries."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='speed_failures'").fetchone():
        return None
    target = primary_generation() if target is None else target
    owners = None
    for row in conn.execute('SELECT * FROM speed_failures WHERE source IN (?,\'\') OR ?=\'\' ORDER BY created_at DESC', (source,source)):
        expected=scope_identity(row['source'])
        if current_only:
            if row['generation'] != target or row['identity'] != expected:
                continue
        else:
            # Keep the last error visible for its scope, while allowing changed
            # populations to retry. Native signature changes are not reader changes.
            try:
                old,new=json.loads(row['identity']),json.loads(expected)
                old.pop('native',None);new.pop('native',None)
                if old!=new:
                    continue
            except (ValueError,TypeError):
                if row['identity']!=expected:
                    continue
        request = json.loads(row['request']); scope = json.loads(row['scope'])
        owner = scope.get('input_owner')
        if owner and row['source'] not in speed_native.SOURCES:
            if current_only and scope.get('signature') != input_signature(owner):
                continue
            if session_id:
                if owners is None:
                    with closing(UsageEntryStore().read_connection()) as primary:
                        owners = {r[0] for r in primary.execute('SELECT file_path FROM usage_session_inputs WHERE source=? AND session_id=?', (source,session_id))}
                        owners.update(r[0] for r in primary.execute('SELECT file_path FROM session_records WHERE tool=? AND session_id=?', (source,session_id)))
                if owner not in owners:
                    continue
        elif row['source'] in speed_native.SOURCES and owner and (owner.startswith('session:') or owner == 'native:all'):
            # A batch may fail before its other members are read. The recorded
            # input scope is authoritative, not the original batch population.
            if session_id and owner.startswith('session:') and owner != 'session:' + session_id:
                continue
        else:
            selected = request.get('sessions') or ([{'tool':row['source'],'session_id':request['session_id']}] if request.get('session_id') else [])
            if session_id and selected and not any(i['tool']==source and i['session_id']==session_id for i in selected):
                continue
            if not session_id and selected and source and not any(i['tool']==source for i in selected):
                continue
        if not session_id and (request.get('to','9999') < day_from or request.get('from','0000') > day_to):
            continue
        return dict(row)
    return None


def ensure_report_index(conn):
    """Upgrade existing derived caches in the worker's publication transaction.

    Requests never build an index over an existing history. Its narrow scalar
    projection avoids random table reads of ownership/session strings on mounted
    filesystems; the raw canonical rows remain the authority for session joins.
    """
    conn.execute('''CREATE INDEX IF NOT EXISTS idx_speed_report ON speed_responses(
        source,timestamp,model,eligible,speed_kind,speed_token_basis,speed_status,
        speed_tokens,speed_ms,speed_calls)''')


def meta(conn):
    return dict(conn.execute('SELECT key,value FROM speed_meta'))


def put_meta(conn, **values):
    conn.executemany('INSERT OR REPLACE INTO speed_meta VALUES (?,?)',
                     ((k, str(v)) for k, v in values.items()))


def primary_generation():
    path = Path(usage_db_path())
    if not path.exists():
        return 0
    with closing(UsageEntryStore(path).read_connection()) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone():
            return 0
        row = conn.execute('SELECT value FROM meta WHERE key=?', (MEASUREMENT_GENERATION_META_KEY,)).fetchone()
        try:
            return int(row[0]) if row else 0
        except (ValueError,TypeError):
            return 0


def identity():
    return json.dumps({'schema': SCHEMA_VERSION, 'measurement': speed_report.SPEED_MEASUREMENT_CONTRACT_VERSION,
                       'publication': PUBLICATION_VERSION,
                       'readers': READER_VERSIONS, 'native': speed_native.signature()}, separators=(',', ':'))


def _alive(pid, token=None):
    # Keep the OS branch here as well, so a simulated Windows probe cannot fall
    # through to POSIX os.kill on the host running the test.
    if os.name == 'nt':
        try:
            running, observed = speed_process._windows_snapshot(int(pid or 0))
            return running and (not token or token == observed)
        except (OSError, ValueError, ImportError, AttributeError):
            return False
    return speed_process.alive(pid, token)


def _retire_interrupted(conn, row):
    token = row['worker_token'] if 'worker_token' in row.keys() else None
    if row['state'] not in ('queued','building') or _alive(row['pid'], token):
        return False
    changed = conn.execute("UPDATE speed_jobs SET state='error',error='worker interrupted',updated_at=? WHERE id=? AND state IN ('queued','building') AND pid IS ? AND worker_token IS ?",
                           (time.time(),row['id'],row['pid'],token)).rowcount
    if changed:
        record_failure(conn,row['id'],json.loads(row['request']),'worker interrupted')
    return bool(changed)


def launch_worker(conn):
    from .filelock import process_lock
    with process_lock(Path(str(cache_path()) + '.launch.lock')):
        state = meta(conn)
        if _alive(state.get('worker_pid'), state.get('worker_token')):
            conn.execute("UPDATE speed_jobs SET pid=?,worker_token=? WHERE state='queued'",(int(state['worker_pid']),state.get('worker_token','')))
            conn.commit()
            return
        for row in conn.execute("SELECT * FROM speed_jobs WHERE state='building'").fetchall():
            _retire_interrupted(conn,row)
        log_path = cache_path().parent / 'output_speed_worker.log'
        with log_path.open('ab') as log:
            child = subprocess.Popen([sys.executable, '-m', 'tokdash.speed_worker'],
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     start_new_session=True, close_fds=True)
        put_meta(conn, worker_pid=child.pid, worker_token=speed_process.snapshot(child.pid)[1] or '')
        state = meta(conn)
        conn.execute("UPDATE speed_jobs SET pid=?,worker_token=? WHERE state='queued'",(child.pid,state['worker_token']))
        conn.commit()


def _scope_ready(conn, state, day_from, day_to, source, session_id, target, expected):
    same = state.get('identity') == expected
    if source and not same:
        try:
            old, new = json.loads(state.get('identity','{}')), json.loads(expected)
            same = all(old.get(k)==new.get(k) for k in ('schema','measurement','publication')) and old.get('readers',{}).get(source)==new['readers'].get(source)
            if source in speed_native.SOURCES:
                same &= [p for p in old.get('native',[]) if p[0]==source]==[p for p in new.get('native',[]) if p[0]==source]
        except (ValueError,KeyError,TypeError):
            same = False
    ready = same and int(state.get('published_generation', -1)) == target
    if session_id:
        if source in speed_native.SOURCES:
            return same and bool(conn.execute("SELECT 1 FROM speed_inputs WHERE source=? AND input_owner IN (?, 'native:all') AND state='ready' AND reader_version=? AND signature=? LIMIT 1", (source, 'session:' + session_id, READER_VERSIONS[source],json.dumps(speed_native.signature(source)))).fetchone())
        complete = ready and bool(conn.execute('SELECT 1 FROM speed_sessions WHERE source=? AND session_id=? AND usage_generation=? AND state="ready"', (source,session_id,target)).fetchone())
        outdated = conn.execute('''SELECT 1 FROM speed_session_members m INDEXED BY idx_speed_session
            CROSS JOIN speed_responses r ON r.source=m.source AND r.entry_key=m.call_key
            JOIN speed_inputs i ON i.source=r.source AND i.input_owner=r.input_owner
            WHERE m.source=? AND m.session_id=? AND (i.state!='ready' OR i.reader_version!=?) LIMIT 1''',(source,session_id,READER_VERSIONS[source])).fetchone()
        return complete and not outdated
    ready &= bool(conn.execute("SELECT 1 FROM speed_windows WHERE source IN (?,'') AND day_from<=? AND day_to>=? AND usage_generation=?", (source,day_from,day_to,target)).fetchone())
    start,end=speed_report._day_bounds_ms(day_from,day_to)
    for name,version in READER_VERSIONS.items():
        if source and source!=name:
            continue
        if conn.execute('''SELECT 1 FROM speed_inputs i WHERE i.source=? AND i.reader_version!=?
            AND EXISTS(SELECT 1 FROM speed_responses r WHERE r.source=i.source AND r.input_owner=i.input_owner AND r.timestamp>=? AND r.timestamp<?) LIMIT 1''',(name,version,start,end)).fetchone():
            return False
    deferred=conn.execute('''SELECT 1 FROM speed_inputs i
        WHERE i.state='pending' AND i.last_error LIKE '%outside requested %'
        AND (?='' OR i.source=?) AND (i.source IN ('opencode','kilocode','mimo')
          OR EXISTS (SELECT 1 FROM speed_responses r WHERE r.source=i.source
            AND r.input_owner=i.input_owner AND r.timestamp>=? AND r.timestamp<?)) LIMIT 1''',
        (source,source,start,end)).fetchone()
    return ready and not deferred


def snapshot(conn, day_from, day_to, *, source='', session_id=''):
    """Read published freshness without queuing work, including after a job ends.

    Live inputs may advance while a job runs. Completion must expose that pinned
    result honestly rather than automatically rebuilding until inputs stop.
    Call inside the same read transaction as the report or timeline.
    """
    target = primary_generation()
    state = meta(conn)
    ready = _scope_ready(conn,state,day_from,day_to,source,session_id,target,identity())
    pending = conn.execute("SELECT count(*) FROM speed_inputs WHERE state='pending'" + (' AND source=?' if source else ''), (source,) if source else ()).fetchone()[0]
    last = failure(conn,day_from,day_to,source,session_id,target=target,current_only=False)
    if session_id and ready:
        pending = 0
    return {'state':'error' if last else 'ready' if ready and not pending else 'stale',
            'job_id':None, 'pending_inputs':pending,
            'published_generation':int(state.get('published_generation',0)),
            'target_usage_generation':target, 'updated_at':state.get('updated_at'),
            'error':last['error'] if last else None}


def ensure(day_from, day_to, *, source='', refresh=False, session_id='', keys=()):
    """Only called by speed or detail. No corpus discovery on this request path."""
    target = primary_generation()
    expected = identity()
    request = json.dumps({'from': day_from, 'to': day_to, 'source': source,
                          'session_id': session_id, 'keys': sorted(set(keys)), 'refresh': refresh,
                          'target_generation':target}, sort_keys=True)
    with closing(connect()) as conn:
        conn.execute('BEGIN IMMEDIATE')
        state = meta(conn)
        ready = _scope_ready(conn,state,day_from,day_to,source,session_id,target,expected)
        queued = conn.execute("SELECT * FROM speed_jobs WHERE request=? AND state IN ('queued','building') ORDER BY created_at DESC LIMIT 1", (request,)).fetchone()
        last = conn.execute('SELECT * FROM speed_jobs WHERE request=? ORDER BY created_at DESC LIMIT 1', (request,)).fetchone()
        # A read failure stays visible until an explicit refresh or changed identity.
        failed_same = failure(conn,day_from,day_to,source,session_id,target=target)
        if not queued and (refresh or not ready) and (refresh or not failed_same):
            job_id = 'speed-' + uuid.uuid4().hex
            conn.execute('INSERT INTO speed_jobs(id,request,state,created_at,updated_at) VALUES (?,?,?,?,?)',
                         (job_id,request,'queued',time.time(),time.time()))
            conn.commit()
            queued = conn.execute('SELECT * FROM speed_jobs WHERE id=?', (job_id,)).fetchone()
        conn.commit()
        if queued:
            launch_worker(conn)
            queued = conn.execute('SELECT * FROM speed_jobs WHERE id=?',(queued['id'],)).fetchone()
            if queued['state'] not in ('queued','building'):
                last=queued
                queued=None
                failed_same=failure(conn,day_from,day_to,source,session_id,target=target)
        result = dict(queued or last) if queued or last else {}
        published = int(state.get('published_generation', 0))
        pending = conn.execute("SELECT count(*) FROM speed_inputs WHERE state='pending'" + (' AND source=?' if source else ''), (source,) if source else ()).fetchone()[0]
        if session_id and ready:
            pending = 0
        result.update(state=('building' if not state.get('updated_at') else 'stale') if queued else ('error' if failed_same else 'stale' if ready and pending else 'ready' if ready else 'stale'),
                      job_id=result.get('id') if queued else None, pending_inputs=pending, published_generation=published,
                      target_usage_generation=target,updated_at=state.get('updated_at'),
                      error=failed_same['error'] if failed_same else None)
        result.pop('request', None)
        result.pop('worker_token', None)
        return result


def job_status(job_id):
    from .filelock import process_lock
    with closing(connect()) as conn:
        row = conn.execute('SELECT * FROM speed_jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        if row['state'] in ('queued','building') and not _alive(row['pid'],row['worker_token']):
            with process_lock(Path(str(cache_path()) + '.launch.lock')):
                conn.execute('BEGIN IMMEDIATE')
                row = conn.execute('SELECT * FROM speed_jobs WHERE id=?',(job_id,)).fetchone()
                _retire_interrupted(conn,row)
                conn.commit()
                row = conn.execute('SELECT * FROM speed_jobs WHERE id=?',(job_id,)).fetchone()
        result = dict(row)
        result.pop('request', None)
        result.pop('worker_token', None)
        return result


def payload(day_from, day_to, *, view='across-models', source='', model='', kind='', basis='', refresh=False, cache_only=False):
    cache = None if cache_only else ensure(day_from,day_to,source=source,refresh=refresh)
    with closing(connect(readonly=cache_only)) as conn:
        conn.execute('BEGIN')
        if cache_only:
            cache = snapshot(conn,day_from,day_to,source=source)
        state = meta(conn)
        # Publication may have landed between ensure and BEGIN; label the actual snapshot.
        cache['published_generation'] = int(state.get('published_generation', 0))
        base = {'schema_version': speed_report.SPEED_CACHE_CONTRACT_VERSION, 'view': view,
                'metric': 'output_tok_per_s', 'aggregation': 'sum_tokens_over_sum_duration',
                'range': {'from': day_from,'to': day_to}, 'cache': cache,
                'measurement_generation': cache['published_generation']}
        from . import speed_results
        def aggregate():
            result = {}
            if view == 'time-of-day':
                hour, day = speed_report.local_clock()
                result.update(speed_report.temporal_rows(conn,day_from,day_to,source=source,model=model,
                            measurement_kind=kind,token_basis=basis,hour_of_local=hour,date_of_local=day))
            else:
                rows, report_meta = speed_report.across_model_rows(conn,day_from,day_to,source=source)
                result.update(report_meta, rows=rows)
                result.pop('_days', None)
                result['available_measurement_range'] = speed_report.available_measurement_range(conn)
            result['_statuses'] = speed_report.source_statuses(conn,day_from,day_to)
            result['_source_days'] = [tuple(r) for r in conn.execute('SELECT source,sum(usage_rows) FROM speed_source_days WHERE day>=? AND day<=? GROUP BY source', (day_from,day_to))]
            return result
        revision = state.get('publication_revision')
        path = cache_path()
        st = path.stat() if path.exists() else None
        clock = (os.environ.get('TZ'),time.tzname,time.timezone,time.altzone,
                 speed_report._day_bounds_ms(day_from,day_to))
        # A cache without a publication revision uses the ordinary SQL fallback.
        key = (str(path.resolve()),st.st_dev,st.st_ino,state.get('instance_id'),revision,
               speed_report.SPEED_CACHE_CONTRACT_VERSION,speed_report.SPEED_MEASUREMENT_CONTRACT_VERSION,
               view,source,model,kind,basis,day_from,day_to,clock) if revision and st and state.get('instance_id') else None
        report = speed_results.read(key,aggregate)
        statuses = report.pop('_statuses')
        source_days = report.pop('_source_days')
        base.update(report)
        cache['publication_revision'] = int(revision or 0)
        present = {r['source'] for r in statuses}
        start, end = speed_report._day_bounds_ms(day_from, day_to)
        for name, count in source_days:
            if name in present:
                continue
            if name not in speed_report.SUPPORTED_READERS:
                statuses.append({'source':name,'status':'unsupported_reader','usage_rows':count})
            elif cache['state'] in ('building','stale','error') and not conn.execute(
                'SELECT 1 FROM speed_responses WHERE source=? AND timestamp>=? AND timestamp<? LIMIT 1',
                (name, start, end),
            ).fetchone():
                # Measured sources are intentionally absent from source_statuses.
                # An unrelated pending input must not relabel their published data.
                statuses.append({'source':name,'status':'read_failure' if failure(conn,day_from,day_to,name) else 'pending_reprocessing','usage_rows':count})
        pending_sources={r[0] for r in conn.execute("SELECT DISTINCT source FROM speed_inputs WHERE state='pending'")}
        for name in pending_sources:
            statuses=[r for r in statuses if r['source']!=name]
            statuses.append({'source':name,'status':'pending_reprocessing','usage_rows':None})
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='speed_failures'").fetchone():
            for (name,) in conn.execute("SELECT DISTINCT source FROM speed_failures WHERE source!=''"):
                if (not source or source==name) and failure(conn,day_from,day_to,name,current_only=False):
                    statuses=[r for r in statuses if r['source']!=name]
                    statuses.append({'source':name,'status':'read_failure','usage_rows':None})
        base['source_status'] = statuses
        conn.commit()
        return base


def enrich_session(source, session_id, turns):
    """Join compact canonical rows. Enqueue only the selected physical session."""
    from datetime import datetime
    keys = tuple(str(t['_event_key']) for t in turns if t.get('_event_key'))
    stamps = [t.get('timestamp_ms', t.get('timestamp')) for t in turns]
    stamps = [x for x in stamps if isinstance(x, (int,float)) and x > 0]
    day = lambda ms: datetime.fromtimestamp(ms/1000).astimezone().date().isoformat()
    today = datetime.now().date().isoformat()
    cache = ensure(day(min(stamps)) if stamps else today, day(max(stamps)) if stamps else today,
                   source=source, session_id=session_id, keys=keys)
    with closing(connect()) as conn:
        conn.execute('BEGIN')
        rows = conn.execute('SELECT entry_key,' + ','.join(FIELDS) + ' FROM speed_responses WHERE source=? AND entry_key IN (SELECT call_key FROM speed_session_members WHERE source=? AND session_id=?)', (source,source,session_id)).fetchall() if source in speed_native.SOURCES else []
        if keys and source not in speed_native.SOURCES:
            # Keep below SQLite variable limits for very long sessions.
            for offset in range(0,len(keys),800):
                chunk=keys[offset:offset+800]
                rows.extend(conn.execute('SELECT entry_key,' + ','.join(FIELDS) + ' FROM speed_responses WHERE source=? AND entry_key IN (' + ','.join('?' for _ in chunk) + ')', (source,*chunk)).fetchall())
        indexed = {r['entry_key']: {k:r[k] for k in FIELDS} for r in rows}
        enriched = [dict(t, _speed=indexed.get(str(t.get('_event_key')), unmeasured())) for t in turns]
        conn.commit()
    return enriched, cache
