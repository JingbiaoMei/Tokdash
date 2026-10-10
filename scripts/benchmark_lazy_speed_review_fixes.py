#!/usr/bin/env python3
"""Paired scoped-publication/result-cache benchmarks on isolated frozen inputs."""
import argparse
from contextlib import closing
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import resource
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
ALLOWED={'codex','dsh','qwen_code','opencode','kilocode','mimo'}
REQUEST={'from':'2026-09-01','to':'2026-09-28','source':''}


def configure(src,corpus,data):
    import speed_fixtures
    from bench_tokdash_server import _prune_parsers
    os.environ.update(speed_fixtures.isolated_env(corpus,data,data.parent/'xdg'))
    os.environ['TOKDASH_SPEED_CPU_FRACTION']='1'
    sys.path.insert(0,str(src));_prune_parsers(ALLOWED)


def prepare(args):
    import output_speed_expansion_fixtures as fixtures
    out=Path(args.out);corpus=out/'corpus';data=out/'prepared'
    fixtures.build(corpus,20)
    with sqlite3.connect(corpus/'opencode.db') as conn:
        template=conn.execute("SELECT data FROM message WHERE id='m1'").fetchone()[0]
        conn.executemany('INSERT INTO message VALUES(?,?,?,?,?)',((f'history-{i}','s',1788264000000,1788264002000,template) for i in range(args.history)))
    configure(Path(args.src),corpus,data)
    from tokdash import compute,speed_cache,speed_worker
    from tokdash.sources.coding_tools import CodingToolsUsageTracker,_sig_cache
    compute._sync_usage_store(CodingToolsUsageTracker())
    with closing(speed_cache.connect()) as conn:
        conn.execute("INSERT INTO speed_jobs(id,request,state,created_at,updated_at) VALUES('benchmark',?,'building',0,0)",(json.dumps(REQUEST),));conn.commit()
        speed_worker.build(conn,'benchmark',REQUEST,sync=False)
    # Freeze the initial derived history, then add one new canonical accounting input.
    new_file=corpus/'codex/sessions/rollout-short-session.jsonl'
    records=[dict(type='session_meta',payload=dict(id='short-session',cwd='/fixture')),*fixtures.codex_call(999)]
    new_file.write_text(''.join(json.dumps(row)+'\n' for row in records))
    _sig_cache.clear();compute._sync_usage_store(CodingToolsUsageTracker())
    print(json.dumps({'prepared':str(data),'device':out.stat().st_dev}),flush=True)


def backup(source,target):
    target.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)


def measure(args):
    common=Path(args.common);out=Path(args.out);data=out/'data'
    for name in ('usage.sqlite3','output_speed.sqlite3'):
        backup(common/'prepared'/name,data/name)
    configure(Path(args.src),common/'corpus',data)
    from tokdash import speed_cache,speed_worker
    measurements={};staged=[];wal_sizes=[]
    def timing(name,fn):
        cpu=time.process_time();wall=time.perf_counter();value=fn()
        measurements.setdefault(name,[]).append(dict(wall_ms=(time.perf_counter()-wall)*1000,cpu_ms=(time.process_time()-cpu)*1000,peak_rss_mb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024))
        return value
    path=speed_cache.cache_path();wal=Path(str(path)+'-wal')
    original=speed_worker._progress
    def progress(conn,*a):
        staged.append(conn.execute('SELECT count(*) FROM speed_stage').fetchone()[0])
        result=original(conn,*a)
        wal_sizes.append(wal.stat().st_size if wal.exists() else 0)
        return result
    speed_worker._progress=progress
    with closing(speed_cache.connect()) as conn:
        before=conn.execute('SELECT count(*) FROM speed_responses').fetchone()[0]
        totals=tuple(conn.execute("SELECT count(*),sum(speed_tokens),sum(speed_ms),sum(speed_calls) FROM speed_responses WHERE source!='codex'").fetchone())
        sample=tuple(conn.execute("SELECT rowid,* FROM speed_responses WHERE source='opencode' AND entry_key='history-0'").fetchone())
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        changes=conn.total_changes
        timing('short_session_build',lambda:speed_worker.build(conn,'benchmark',dict(REQUEST,source='codex',session_id='short-session'),sync=False))
        changes=conn.total_changes-changes
        wal_sizes.append(wal.stat().st_size if wal.exists() else 0)
        after=conn.execute('SELECT count(*) FROM speed_responses').fetchone()[0]
        assert after==before+1
        assert totals==tuple(conn.execute("SELECT count(*),sum(speed_tokens),sum(speed_ms),sum(speed_calls) FROM speed_responses WHERE source!='codex'").fetchone())
        sample_after=tuple(conn.execute("SELECT rowid,* FROM speed_responses WHERE source='opencode' AND entry_key='history-0'").fetchone())
        if args.arm=='fixed':assert sample==sample_after and max(staged)<=1 and changes<100
        counts=json.loads(speed_cache.meta(conn)['last_build_counts'])
    read_kwargs={'cache_only':True}
    payload=timing('model_cold',lambda:speed_cache.payload(REQUEST['from'],REQUEST['to'],**read_kwargs))
    for _ in range(args.reps):
        assert timing('model_warm',lambda:speed_cache.payload(REQUEST['from'],REQUEST['to'],**read_kwargs))['rows']==payload['rows']
    row=next(r for r in payload['rows'] if r['source']=='opencode')
    hourly=dict(view='time-of-day',source='opencode',model=row['model'],kind=row['measurement_kind'],basis=row['token_basis'],**read_kwargs)
    hours=timing('hour_cold',lambda:speed_cache.payload(REQUEST['from'],REQUEST['to'],**hourly))
    for _ in range(args.reps):
        assert timing('hour_warm',lambda:speed_cache.payload(REQUEST['from'],REQUEST['to'],**hourly))['hourly']==hours['hourly']
    result=dict(arm=args.arm,history=args.history,src_device=Path(args.src).stat().st_dev,data_device=out.stat().st_dev,measurements=measurements,staged_calls=max(staged),sqlite_row_changes=changes,wal_peak_bytes=max(wal_sizes),cache_bytes=sum(p.stat().st_size for p in (path,wal) if p.exists()),build_counts=counts,preserved_unrelated_totals=True,preserved_sample_rowid=sample==sample_after,payload_bytes=len(json.dumps(payload).encode()))
    (out/'result.json').write_text(json.dumps(result,indent=2));print(str(out/'result.json'),flush=True)


def orchestrate(args):
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    results=[];orders=[]
    for history in args.histories:
        common=out/f'history-{history}'
        subprocess.run([sys.executable,__file__,'--phase','prepare','--src',args.baseline,'--out',str(common),'--history',str(history)],check=True,cwd=ROOT)
        for pair in range(args.pairs):
            order=['baseline','fixed'] if pair%2==0 else ['fixed','baseline'];orders.append(dict(history=history,pair=pair,order=order))
            for arm in order:
                armout=common/f'pair-{pair}-{arm}'
                src=args.baseline if arm=='baseline' else args.fixed
                subprocess.run([sys.executable,__file__,'--phase','measure','--src',src,'--common',str(common),'--out',str(armout),'--history',str(history),'--arm',arm,'--reps',str(args.reps)],check=True,cwd=ROOT)
                results.append(json.loads((armout/'result.json').read_text()))
    def spread(values):return dict(median=statistics.median(values),min=min(values),max=max(values),n=len(values))
    summary={}
    for history in args.histories:
        summary[str(history)]={}
        for arm in ('baseline','fixed'):
            selected=[r for r in results if r['history']==history and r['arm']==arm]
            metrics={}
            for name in selected[0]['measurements']:
                metrics[name]={field:spread([sample[field] for r in selected for sample in r['measurements'][name]]) for field in ('wall_ms','cpu_ms','peak_rss_mb')}
            metrics.update({name:spread([r[name] for r in selected]) for name in ('staged_calls','sqlite_row_changes','wal_peak_bytes','cache_bytes','payload_bytes')})
            summary[str(history)][arm]=metrics
    (out/'benchmark.json').write_text(json.dumps(dict(orders=orders,results=results,summary=summary,interpretation='Fixture operation timings, not full browser refreshes; browser arms are recorded separately.'),indent=2))


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--phase',choices=['prepare','measure','all'],default='all');ap.add_argument('--src');ap.add_argument('--common');ap.add_argument('--out',required=True);ap.add_argument('--history',type=int,default=1000);ap.add_argument('--histories',type=int,nargs='+',default=[1000,10000,100000]);ap.add_argument('--pairs',type=int,default=3);ap.add_argument('--reps',type=int,default=3);ap.add_argument('--arm',choices=['baseline','fixed']);ap.add_argument('--baseline');ap.add_argument('--fixed')
    args=ap.parse_args();{'prepare':prepare,'measure':measure,'all':orchestrate}[args.phase](args)
if __name__=='__main__':main()
