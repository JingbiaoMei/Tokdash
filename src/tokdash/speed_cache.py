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

SCHEMA_VERSION = 4
PUBLICATION_VERSION = 3
READER_VERSIONS = {'kimi': 1, 'omp': 1, 'codex': 4, 'dsh': 3, 'qwen_code': 2,
                   'opencode': 3, 'kilocode': 3, 'mimo': 3, 'claude': 1}
FIELDS = ('speed_tokens', 'speed_ms', 'speed_calls', 'speed_kind', 'speed_token_basis', 'speed_status')
COLUMNS = 'source,entry_key,input_owner,session_id,timestamp,model,output,reasoning,eligible,' + ','.join(FIELDS)
# speed_start is where a window opened, for readers whose stored timestamp is
# neither end of it; NULL leaves the busy interval to the source's anchor.
WINDOW_START_READERS = ('claude',)
REPORT_INDEX = ('source,timestamp,model,eligible,speed_kind,speed_token_basis,speed_status,'
                'speed_tokens,speed_ms,speed_calls,speed_start')


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
        current = controls==2 and 'worker_token' in {r[1] for r in conn.execute('PRAGMA table_info(speed_jobs)')} and bool(conn.execute("SELECT 1 FROM speed_meta WHERE key='instance_id'").fetchone()) \
            and 'speed_start' in {r[1] for r in conn.execute('PRAGMA table_info(speed_responses)')} \
            and 'usage_state' in ({r[1] for r in conn.execute('PRAGMA table_info(speed_inputs)')} or {'usage_state'})
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
        usage_generation INTEGER,state TEXT,last_error TEXT,usage_state TEXT,
        PRIMARY KEY(source,input_owner));
      CREATE TABLE IF NOT EXISTS speed_responses(
        source TEXT NOT NULL,entry_key TEXT NOT NULL,input_owner TEXT NOT NULL,
        session_id TEXT NOT NULL DEFAULT '',timestamp INTEGER NOT NULL,model TEXT NOT NULL,
        output INTEGER NOT NULL,reasoning INTEGER NOT NULL,eligible INTEGER NOT NULL,
        speed_tokens INTEGER NOT NULL,speed_ms REAL NOT NULL,speed_calls INTEGER NOT NULL,
        speed_kind TEXT NOT NULL,speed_token_basis TEXT NOT NULL,speed_status TEXT NOT NULL,
        speed_start REAL,PRIMARY KEY(source,entry_key));
      CREATE INDEX IF NOT EXISTS idx_speed_source_time ON speed_responses(source,timestamp);
      CREATE INDEX IF NOT EXISTS idx_speed_owner ON speed_responses(source,input_owner);
      CREATE INDEX IF NOT EXISTS idx_speed_measured_time ON speed_responses(timestamp)
        WHERE speed_calls > 0;
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
    conn.execute('CREATE INDEX IF NOT EXISTS idx_speed_report ON speed_responses(' + REPORT_INDEX + ')')
    _control_schema(conn)
    # Existing SQL aggregation is shared with the old ephemeral test adapter.
    conn.execute('CREATE TEMP VIEW usage_entries AS SELECT rowid AS rowid,* FROM speed_responses')
    conn.commit()
    return conn


def _control_schema(conn):
    # Small control tables only; upgrading an existing history never rebuilds its indexes here.
    if 'worker_token' not in {r[1] for r in conn.execute('PRAGMA table_info(speed_jobs)')}:
        conn.execute('ALTER TABLE speed_jobs ADD COLUMN worker_token TEXT')
    if 'speed_start' not in {r[1] for r in conn.execute('PRAGMA table_info(speed_responses)')}:
        conn.execute('ALTER TABLE speed_responses ADD COLUMN speed_start REAL')  # metadata only
    inputs = {r[1] for r in conn.execute('PRAGMA table_info(speed_inputs)')}
    if inputs and 'usage_state' not in inputs:
        # NULL reads as never checked, so each input is read once more and records it.
        conn.execute('ALTER TABLE speed_inputs ADD COLUMN usage_state TEXT')
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
    if 'speed_start' not in {r[2] for r in conn.execute("PRAGMA index_info('idx_speed_report')")}:
        conn.execute('DROP INDEX IF EXISTS idx_speed_report')  # a schema-3 projection
    conn.execute('CREATE INDEX IF NOT EXISTS idx_speed_report ON speed_responses(' + REPORT_INDEX + ')')


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


SETTLE_SECONDS = 300
UNTRACKED = 'untracked'


def settle_seconds():
    """How long a changed input must stay unwritten before a model build reads it."""
    try:
        value = float(os.environ.get('TOKDASH_SPEED_SETTLE_SECONDS', SETTLE_SECONDS))
    except ValueError:
        return float(SETTLE_SECONDS)
    return max(0.0, value) if value == value else float(SETTLE_SECONDS)


def _contract():
    # Reader versions are kept per input; these are the rest of what a row means.
    return [SCHEMA_VERSION, speed_report.SPEED_MEASUREMENT_CONTRACT_VERSION, PUBLICATION_VERSION]


def usage_token(kind=UNTRACKED):
    """The token of an input with no file state: a native database, or UNTRACKED.

    A file's token is the usage store's own state for that file plus the timing
    contract, built by SQLite in usage_tokens so both sides compare it as one
    string. The store stamps updated_at_ms whenever it rewrites a file's rows or
    marks it missing, and recounts entry_count when a copy elsewhere takes over
    one of the file's keys, so an unchanged token means unchanged rows. Only
    those integers are read, because a request checks every file. The store
    keeps a state for every file it bills from; UNTRACKED covers a row without
    one, so it settles instead of being read on every build.
    """
    return json.dumps([kind, _contract()], separators=(',', ':'))


def usage_tokens(primary, sources):
    """{(source, path): (token, entry_count, mtime_ns)} from the usage store's file states."""
    if primary is None or not sources:
        return {}
    marks = ','.join('?' for _ in sources)
    return {(r[0], r[1]): (r[2], int(r[3] or 0), int(r[4] or 0)) for r in primary.execute(
        f'SELECT source,path,json_array(json_array(updated_at_ms,entry_count,missing),json(?)),entry_count,mtime_ns '
        f'FROM file_state WHERE source IN ({marks})', (json.dumps(_contract()), *sources))}


def _contract_changed(recorded):
    try:
        return json.loads(recorded)[1] != _contract()
    except (TypeError, ValueError, IndexError, KeyError):
        return False  # never recorded: a first check, not a newer contract


def _settling(newest_ns, now, settle):
    return settle > 0 and now - (newest_ns or 0) / 1e9 < settle


def _file_settling(path, recorded_ns, now, settle):
    if settle <= 0:
        return False
    newest = int(recorded_ns or 0)
    try:
        newest = max(newest, Path(path).stat().st_mtime_ns)
    except OSError:
        pass
    return _settling(newest, now, settle)


def pending_work(conn, primary, *, source='', include_active=False, now=None):
    """Inputs whose published timing no longer matches accounting.

    Returns (dirty, deferred). A changed input is deferred while it is still
    being written: reading a live log is the expensive part of a build and its
    result rarely survives the build, so its last-good rows stand until it has
    been quiet for settle_seconds(). A newer reader or contract never waits.
    A native database is judged whole, as its timing index is.
    """
    now = time.time() if now is None else now
    settle = settle_seconds()
    names = [name for name in READER_VERSIONS if not source or name == source]
    files = [name for name in names if name not in speed_native.SOURCES]
    indexed = {}
    if names:
        marks = ','.join('?' for _ in names)
        indexed = {(r['source'], r['input_owner']): r for r in conn.execute(
            f'SELECT source,input_owner,signature,reader_version,state,usage_state FROM speed_inputs WHERE source IN ({marks})', names)}
    usage = usage_tokens(primary, files)
    dirty, deferred = set(), set()

    def judge(key, token, signature=None, settling=lambda: False):
        old = indexed.get(key)
        if old is not None and old['state'] == 'ready' and old['reader_version'] == READER_VERSIONS[key[0]] \
                and old['usage_state'] == token and (signature is None or old['signature'] == signature):
            return
        if old is not None and (old['reader_version'] != READER_VERSIONS[key[0]] or _contract_changed(old['usage_state'])):
            dirty.add(key)
        elif not include_active and settling():
            deferred.add(key)
        else:
            dirty.add(key)

    for key, (token, entries, mtime_ns) in usage.items():
        if key not in indexed and not entries:
            continue  # nothing is billed from it, so there is nothing to time
        judge(key, token, settling=lambda key=key, mtime_ns=mtime_ns: _file_settling(key[1], mtime_ns, now, settle))
    for key in indexed:
        if key[0] not in speed_native.SOURCES and key not in usage:
            judge(key, usage_token())
    for name in names:
        if name in speed_native.SOURCES:
            parts = speed_native.signature(name)
            judge((name, 'native:all'), usage_token('native'), json.dumps(parts),
                  lambda parts=parts: _settling(max((p[4] for p in parts), default=0), now, settle))
    return dirty, deferred


def _model_freshness(conn, source):
    """(dirty, deferred) for a model view, read without writing either database."""
    path = Path(usage_db_path())
    if not path.exists():
        return pending_work(conn, None, source=source)
    with closing(UsageEntryStore(path).read_connection()) as primary:
        if not primary.execute("SELECT 1 FROM sqlite_master WHERE name='file_state'").fetchone():
            return pending_work(conn, None, source=source)
        return pending_work(conn, primary, source=source)


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


def _session_ready(conn, state, source, session_id, target, expected):
    same = state.get('identity') == expected
    if source and not same:
        try:
            old, new = json.loads(state.get('identity','{}')), json.loads(expected)
            same = all(old.get(k)==new.get(k) for k in ('schema','measurement','publication')) and old.get('readers',{}).get(source)==new['readers'].get(source)
            if source in speed_native.SOURCES:
                same &= [p for p in old.get('native',[]) if p[0]==source]==[p for p in new.get('native',[]) if p[0]==source]
        except (ValueError,KeyError,TypeError):
            same = False
    if source in speed_native.SOURCES:
        return same and bool(conn.execute("SELECT 1 FROM speed_inputs WHERE source=? AND input_owner IN (?, 'native:all') AND state='ready' AND reader_version=? AND signature=? LIMIT 1", (source, 'session:' + session_id, READER_VERSIONS[source],json.dumps(speed_native.signature(source)))).fetchone())
    ready = same and int(state.get('published_generation', -1)) == target
    complete = ready and bool(conn.execute('SELECT 1 FROM speed_sessions WHERE source=? AND session_id=? AND usage_generation=? AND state="ready"', (source,session_id,target)).fetchone())
    outdated = conn.execute('''SELECT 1 FROM speed_session_members m INDEXED BY idx_speed_session
        CROSS JOIN speed_responses r ON r.source=m.source AND r.entry_key=m.call_key
        JOIN speed_inputs i ON i.source=r.source AND i.input_owner=r.input_owner
        WHERE m.source=? AND m.session_id=? AND (i.state!='ready' OR i.reader_version!=?) LIMIT 1''',(source,session_id,READER_VERSIONS[source])).fetchone()
    return complete and not outdated


def _freshness(conn, state, source, session_id, target, expected):
    """(ready, pending_inputs, deferred_inputs) for one view.

    A model view does not depend on its dates or on the usage generation: every
    model build reads whatever input has changed, so one build serves every
    range, and new usage only matters for the inputs it landed in. A session
    view keeps its own generation-pinned membership.
    """
    if session_id:
        return _session_ready(conn, state, source, session_id, target, expected), 0, 0
    dirty, deferred = _model_freshness(conn, source)
    return not dirty, len(dirty), len(deferred)


def snapshot(conn, day_from, day_to, *, source='', session_id=''):
    """Read published freshness without queuing work, including after a job ends.

    Live inputs may advance while a job runs. Completion must expose that pinned
    result honestly rather than automatically rebuilding until inputs stop.
    Call inside the same read transaction as the report or timeline.
    """
    target = primary_generation()
    state = meta(conn)
    ready, pending, deferred = _freshness(conn,state,source,session_id,target,identity())
    last = failure(conn,day_from,day_to,source,session_id,target=target,current_only=False)
    return {'state':'error' if last else 'ready' if ready else 'stale',
            'job_id':None, 'pending_inputs':pending, 'deferred_inputs':deferred,
            'published_generation':int(state.get('published_generation',0)),
            'target_usage_generation':target, 'updated_at':state.get('updated_at'),
            'error':last['error'] if last else None}


def job_request(day_from, day_to, *, source='', refresh=False, session_id='', keys=(), target=0):
    """The job a view queues. A model build is the same job for every range."""
    if not session_id:
        return json.dumps({'source': source, 'refresh': refresh}, sort_keys=True)
    return json.dumps({'from': day_from, 'to': day_to, 'source': source,
                       'session_id': session_id, 'keys': sorted(set(keys)), 'refresh': refresh,
                       'target_generation':target}, sort_keys=True)


def ensure(day_from, day_to, *, source='', refresh=False, session_id='', keys=()):
    """Only called by speed or detail. No corpus discovery on this request path."""
    target = primary_generation()
    expected = identity()
    request = job_request(day_from, day_to, source=source, refresh=refresh, session_id=session_id, keys=keys, target=target)
    with closing(connect()) as conn:
        conn.execute('BEGIN IMMEDIATE')
        state = meta(conn)
        ready, pending, deferred = _freshness(conn,state,source,session_id,target,expected)
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
        result.update(state=('building' if not state.get('updated_at') else 'stale') if queued else ('error' if failed_same else 'ready' if ready else 'stale'),
                      job_id=result.get('id') if queued else None, pending_inputs=pending, deferred_inputs=deferred,
                      published_generation=published, target_usage_generation=target,updated_at=state.get('updated_at'),
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


def payload(day_from, day_to, *, view='across-models', source='', model='', kind='', basis='', bucket='auto', refresh=False, cache_only=False):
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
            elif view == 'trend':
                from . import speed_trend
                hour, day = speed_report.local_clock()
                result.update(speed_trend.trend_rows(conn,day_from,day_to,bucket=bucket,source=source,
                            hour_of_local=hour,date_of_local=day))
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
               view,source,model,kind,basis,bucket if view == 'trend' else '',day_from,day_to,clock) if revision and st and state.get('instance_id') else None
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
        # Pending only where an input still owes these dates its timing, counted
        # in its responses here. One waiting elsewhere says nothing about them.
        for name,count in conn.execute('''SELECT i.source,count(*) FROM speed_inputs i
            CROSS JOIN speed_responses r INDEXED BY idx_speed_owner ON r.source=i.source AND r.input_owner=i.input_owner
            WHERE i.state='pending' AND (?='' OR i.source=?) AND r.timestamp>=? AND r.timestamp<? GROUP BY i.source''',
            (source,source,start,end)).fetchall():
            statuses=[r for r in statuses if r['source']!=name]
            statuses.append({'source':name,'status':'pending_reprocessing','usage_rows':count})
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
