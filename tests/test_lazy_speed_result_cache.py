"""The in-process cache of lazy-speed results.

Identical reads share one snapshot and one query, a caller cannot corrupt the
stored copy, failures are never cached, the cache stays within its bounds, and
a new publication revision or a recreated database instance is a new snapshot.
"""
from contextlib import closing

import pytest
from test_lazy_speed_cache import corpus, build_requested
from tokdash import speed_cache as cache, speed_worker, compute, speed_results
from tokdash.sources.coding_tools import CodingToolsUsageTracker


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
