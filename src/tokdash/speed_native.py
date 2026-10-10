"""Native database identities and legacy scalar projection adapters.

These sources are intentionally absent from the primary usage_entries table.
Production speed reads use speed_worker and the separate durable derived cache;
read_window/projection/augment remain compatibility adapters for reader tests.
DB/WAL identities retire derived measurements independently of pricing.
"""
from pathlib import Path
import sqlite3

from . import clientpaths
from .sources.coding_tools import BaseParser, _opencode_message_table, _mimo_imported_message_ids, connect_sqlite_readonly
from .output_speed_readers import request_window_timing

SOURCES = ('opencode', 'kilocode', 'mimo')

def paths():
    return {'opencode': [clientpaths.opencode_db_path()],
            'kilocode': clientpaths.kilo_db_paths(),
            'mimo': [clientpaths.mimocode_db_path()]}

def signature(source=None):
    """No TTL: include discovery, replacement, removal and WAL-only commits.

    SHM is excluded: read connections themselves alter it without a data write.
    Inode/device distinguish replacements with preserved timestamps and sizes.
    """
    parts = []
    for name, dbs in paths().items():
        if source and source != name:
            continue
        for db in dbs:
            for path in (db, Path(str(db) + '-wal')):
                try:
                    st = path.stat()
                except FileNotFoundError:
                    continue
                parts.append((name, str(path), st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size))
    return tuple(parts)

def read_window(since_ms, until_ms, source=None):
    entries, failures = [], []
    for name, dbs in paths().items():
        if source and source != name:
            continue
        source_entries = []
        try:
            for db in dbs:
                if not db.exists():
                    continue
                conn = connect_sqlite_readonly(db)
                try:
                    conn.execute('BEGIN')
                    table = _opencode_message_table(conn) if name != 'mimo' else 'message'
                    role = "type = 'assistant'" if table == 'session_message' else "json_extract(data, '$.role') = 'assistant'"
                    imported = _mimo_imported_message_ids(conn) if name == 'mimo' else set()
                    query = f"""
                      SELECT id, time_created,
                        COALESCE(NULLIF(json_extract(data, '$.modelID'), ''), json_extract(data, '$.model.id'), 'unknown'),
                        json_extract(data, '$.tokens.output'), json_extract(data, '$.tokens.reasoning'),
                        json_extract(data, '$.time.created'), json_extract(data, '$.time.completed'),
                        json_extract(data, '$.error'), json_extract(data, '$.finish'),
                        json_extract(data, '$.agent'), json_extract(data, '$.mode')
                      FROM {table} WHERE time_created >= ? AND time_created < ?
                        AND json_valid(data) AND {role}
                        AND json_type(data, '$.tokens') = 'object'
                    """
                    for identity, ts, model, output, reasoning, created, completed, error, finish, agent, mode in conn.execute(query, (since_ms, until_ms)):
                        if str(identity) in imported:
                            continue
                        source_entries.append({'source': name, 'model': model, 'timestamp': ts,
                                               'output': BaseParser._i(output), 'reasoning': BaseParser._i(reasoning),
                                               '_speed': request_window_timing(output, reasoning, created, completed, error, finish,
                                                   ensemble=name == 'mimo' and (agent == 'max' or mode == 'max'))})
                finally:
                    conn.close()
        except (OSError, sqlite3.Error, ValueError):
            # A partially read source never becomes a successful empty population.
            failures.append({'source': name, 'status': 'read_failure', 'usage_rows': None})
        else:
            entries.extend(source_entries)
    return entries, failures

def projection(entries):
    """A bounded ephemeral adapter, not a second persistent cache/database."""
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('''CREATE TABLE usage_entries (
        source TEXT, model TEXT, timestamp INTEGER, output INTEGER, reasoning INTEGER,
        speed_tokens INTEGER, speed_ms REAL, speed_calls INTEGER, speed_kind TEXT,
        speed_token_basis TEXT, speed_status TEXT)''')
    conn.executemany('INSERT INTO usage_entries VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                     ((e['source'], e['model'], e['timestamp'], e['output'] + e['reasoning'], e['reasoning'],
                       *[e['_speed'][k] for k in ('speed_tokens','speed_ms','speed_calls','speed_kind','speed_token_basis','speed_status')]) for e in entries))
    conn.execute('CREATE INDEX idx_usage_entries_source_time ON usage_entries(source,timestamp)')
    conn.commit()
    return conn


def available_range():
    """Published measured bounds, including an indexed empty result.

    Native source JSON is never scanned on this read. The demand worker indexes
    each DB/WAL identity once; read failures retain the last good bounds and are
    exposed separately through cache/job metadata.
    """
    from contextlib import closing
    from .speed_cache import cache_path, connect
    from .speed_report import available_measurement_range
    if not cache_path().exists():
        return None
    with closing(connect()) as conn:
        return available_measurement_range(conn)


def augment(payload, store_conn, day_from, day_to, *, view, source, model, kind, basis):
    from . import speed_report as report
    start, end = report._day_bounds_ms(day_from, day_to)
    if not signature(source if view == 'time-of-day' else None):
        payload.pop('_days', None)
        return payload
    entries, failures = read_window(start, end, source if view == 'time-of-day' else None)
    conn = projection(entries)
    try:
        if view == 'time-of-day' and source in SOURCES:
            hour, day = report.local_clock()
            payload.update(report.temporal_rows(conn, day_from, day_to, source=source, model=model,
                            measurement_kind=kind, token_basis=basis, hour_of_local=hour, date_of_local=day))
            payload['source_status'] = failures
        elif view == 'across-models':
            rows, _meta = report.across_model_rows(conn, day_from, day_to, source=source or '')
            payload['rows'].extend(rows)
            payload['rows'].sort(key=lambda r: (-r['speed_calls'], -(r['output_tok_per_s'] or 0), r['source'], r['model']))
            payload['source_status'].extend(report.source_statuses(conn, day_from, day_to))
            payload['source_status'].extend(failures)
            _, day = report.local_clock()
            days = set(payload.pop('_days', ()))
            for (name, *_), values in report._measured_day_map(conn, start, end, day).items():
                if not source or source == name:
                    days.update(values)
            payload['days_with_measurements'] = len(days)
            # Failed sources do not supply global measurement bounds either.
            if not failures:
                native_bounds = available_range()
                bounds = payload.get('available_measurement_range')
                if native_bounds:
                    payload['available_measurement_range'] = ({'from': min(bounds['from'], native_bounds['from']),
                                                              'to': max(bounds['to'], native_bounds['to'])} if bounds else native_bounds)
        return payload
    finally:
        conn.close()
