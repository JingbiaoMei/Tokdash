#!/usr/bin/env python3
"""Paired short builds with fixed native call count and increasing session scopes."""
import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import statistics
import subprocess
import sys
import time

from benchmark_lazy_speed_review_fixes import configure, backup, REQUEST

ROOT = Path(__file__).resolve().parents[1]


def prepare(args):
    import output_speed_expansion_fixtures as fixtures
    out = Path(args.out)
    corpus, data = out / 'corpus', out / 'prepared'
    fixtures.build(corpus, 20)
    # Keep total responses identical; only their distribution across sessions changes.
    with sqlite3.connect(corpus / 'opencode.db') as conn:
        template = conn.execute("SELECT data FROM message WHERE id='m1'").fetchone()[0]
        conn.execute('DELETE FROM message')
        conn.execute('DELETE FROM session')
        conn.executemany("INSERT INTO session VALUES (?,'p','fixture','/fixture',1788264000000,1788264002000)",
                         ((f'history-session-{i}',) for i in range(args.scopes)))
        conn.executemany('INSERT INTO message VALUES (?,?,?,?,?)',
                         ((f'history-{i}', f'history-session-{i % args.scopes}', 1788264000000, 1788264002000, template) for i in range(args.calls)))
        conn.execute('CREATE INDEX IF NOT EXISTS message_session_idx ON message(session_id)')
    configure(Path(args.src), corpus, data)
    from tokdash import compute, speed_cache, speed_worker, speed_native
    from tokdash.sources.coding_tools import CodingToolsUsageTracker, _sig_cache
    compute._sync_usage_store(CodingToolsUsageTracker())
    with closing(speed_cache.connect()) as conn:
        conn.execute("INSERT INTO speed_jobs(id,request,state,created_at,updated_at) VALUES('benchmark',?,'building',0,0)", (json.dumps(REQUEST),))
        conn.commit()
        speed_worker.build(conn, 'benchmark', REQUEST, sync=False)
        signature = json.dumps(speed_native.signature('opencode'))
        generation = speed_cache.primary_generation()
        conn.execute("DELETE FROM speed_inputs WHERE source='opencode'")
        conn.execute("UPDATE speed_responses SET input_owner='session:'||session_id WHERE source='opencode'")
        conn.executemany("INSERT INTO speed_inputs VALUES ('opencode',?,?,?,?,'ready',NULL)",
                         ((f'session:history-session-{i}', signature, speed_cache.READER_VERSIONS['opencode'], generation) for i in range(args.scopes)))
        conn.commit()
    new_file = corpus / 'codex/sessions/rollout-short-session.jsonl'
    records = [dict(type='session_meta', payload=dict(id='short-session', cwd='/fixture')), *fixtures.codex_call(999)]
    new_file.write_text(''.join(json.dumps(row) + '\n' for row in records))
    _sig_cache.clear()
    compute._sync_usage_store(CodingToolsUsageTracker())
    (out / 'fixture.json').write_text(json.dumps(dict(calls=args.calls, scopes=args.scopes, device=out.stat().st_dev), indent=2))


def measure(args):
    common, out = Path(args.common), Path(args.out)
    data = out / 'data'
    for name in ('usage.sqlite3', 'output_speed.sqlite3'):
        backup(common / 'prepared' / name, data / name)
    configure(Path(args.src), common / 'corpus', data)
    from tokdash import speed_cache, speed_worker, speed_native
    counts, staged, sql_steps = Counter(), [], []
    signature, progress = speed_native.signature, speed_worker._progress

    def counted(source=None):
        counts[source or 'all'] += 1
        return signature(source)

    def checkpoint(conn, *values):
        staged.append(conn.execute('SELECT count(*) FROM speed_stage').fetchone()[0])
        return progress(conn, *values)

    speed_native.signature, speed_worker._progress = counted, checkpoint
    with closing(speed_cache.connect()) as conn:
        before = tuple(conn.execute("SELECT count(*),sum(speed_tokens),sum(speed_ms),sum(speed_calls) FROM speed_responses WHERE source!='codex'").fetchone())
        sample = tuple(conn.execute("SELECT rowid,* FROM speed_responses WHERE source='opencode' AND entry_key='history-0'").fetchone())
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        changes = conn.total_changes
        conn.set_progress_handler(lambda: sql_steps.append(1) or 0, 1000)
        wall, cpu = time.perf_counter(), time.process_time()
        speed_worker.build(conn, 'benchmark', dict(REQUEST, source='codex', session_id='short-session'), sync=False)
        measurement = dict(wall_ms=(time.perf_counter() - wall) * 1000, cpu_ms=(time.process_time() - cpu) * 1000,
                           peak_rss_mb=speed_worker.peak_rss_mb())
        conn.set_progress_handler(None, 0)
        changes = conn.total_changes - changes
        after = tuple(conn.execute("SELECT count(*),sum(speed_tokens),sum(speed_ms),sum(speed_calls) FROM speed_responses WHERE source!='codex'").fetchone())
        sample_after = tuple(conn.execute("SELECT rowid,* FROM speed_responses WHERE source='opencode' AND entry_key='history-0'").fetchone())
        assert before == after and sample == sample_after
        assert max(staged) == 1 and changes < 100
        build_counts = json.loads(speed_cache.meta(conn)['last_build_counts'])
        if args.arm == 'fixed':
            assert sum(counts.values()) < 20
            assert build_counts['native_freshness_scopes'] == 0
        wal = Path(str(data / 'output_speed.sqlite3') + '-wal')
        wal_bytes = wal.stat().st_size if wal.exists() else 0
    result = dict(arm=args.arm, scopes=args.scopes, calls=args.calls, measurement=measurement,
                  native_signature_calls=sum(counts.values()), signatures_by_source=dict(counts),
                  sqlite_row_changes=changes, approximate_sql_vm_steps=len(sql_steps) * 1000,
                  staged_rows=max(staged), build_counts=build_counts, preserved_unrelated_totals=True,
                  preserved_sample_rowid=True, data_device=out.stat().st_dev, src_device=Path(args.src).stat().st_dev,
                  database_bytes=(data / 'output_speed.sqlite3').stat().st_size,
                  wal_bytes=wal_bytes)
    (out / 'result.json').write_text(json.dumps(result, indent=2))


def summarize(results, orders):
    def spread(values):
        return dict(median=statistics.median(values), min=min(values), max=max(values), n=len(values))
    summary = {}
    for scopes in sorted({r['scopes'] for r in results}):
        summary[str(scopes)] = {}
        for arm in ('baseline', 'fixed'):
            samples = [r for r in results if r['arm'] == arm and r['scopes'] == scopes]
            summary[str(scopes)][arm] = {field: spread([r['measurement'][field] for r in samples]) for field in ('wall_ms', 'cpu_ms', 'peak_rss_mb')}
            summary[str(scopes)][arm].update({field: spread([r[field] for r in samples]) for field in ('native_signature_calls', 'sqlite_row_changes', 'approximate_sql_vm_steps', 'staged_rows', 'database_bytes', 'wal_bytes')})
    return dict(orders=orders, results=results, summary=summary,
                interpretation='Fixed total native calls; scope count varies. Isolated build timings; browser paint is measured separately.')


def orchestrate(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results, orders = [], []
    for scopes in args.scope_counts:
        common = out / f'scopes-{scopes}'
        subprocess.run([sys.executable, __file__, '--phase', 'prepare', '--src', args.baseline,
                        '--out', str(common), '--scopes', str(scopes), '--calls', str(args.calls)], cwd=ROOT, check=True)
        for pair in range(args.pairs):
            order = ['baseline', 'fixed'] if pair % 2 == 0 else ['fixed', 'baseline']
            orders.append(dict(scopes=scopes, pair=pair, order=order))
            for arm in order:
                armout = common / f'pair-{pair}-{arm}'
                subprocess.run([sys.executable, __file__, '--phase', 'measure', '--src', args.baseline if arm == 'baseline' else args.fixed,
                                '--common', str(common), '--out', str(armout), '--arm', arm, '--scopes', str(scopes), '--calls', str(args.calls)], cwd=ROOT, check=True)
                results.append(json.loads((armout / 'result.json').read_text()))
    (out / 'benchmark.json').write_text(json.dumps(summarize(results, orders), indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--phase', choices=['prepare', 'measure', 'all'], default='all')
    ap.add_argument('--src'); ap.add_argument('--baseline'); ap.add_argument('--fixed'); ap.add_argument('--common')
    ap.add_argument('--out', required=True); ap.add_argument('--arm', choices=['baseline', 'fixed'])
    ap.add_argument('--calls', type=int, default=10000); ap.add_argument('--scopes', type=int, default=1000)
    ap.add_argument('--scope-counts', type=int, nargs='+', default=[1, 1000, 10000]); ap.add_argument('--pairs', type=int, default=3)
    args = ap.parse_args()
    if min(args.scope_counts) < 1 or max(args.scope_counts) > args.calls or not 1 <= args.scopes <= args.calls:
        ap.error('scope counts must be between one and the fixed call count')
    {'prepare': prepare, 'measure': measure, 'all': orchestrate}[args.phase](args)


if __name__ == '__main__':
    main()
