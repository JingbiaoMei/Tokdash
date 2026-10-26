"""The lazy-speed worker's health checks, on every platform.

A worker that died must turn its jobs terminal without another request, a
reused pid must not pass for the old worker, a quiet live worker is not a dead
one, and the Windows probes must never signal or relaunch a running worker. The
optional POSIX memory telemetry is optional: missing, it cannot roll back a
finished build, and its units follow the platform.
"""
from contextlib import closing

import pytest
from test_lazy_speed_cache import corpus
from tokdash import speed_cache as cache, speed_worker, compute, speed_process
from tokdash.sources.coding_tools import CodingToolsUsageTracker

# The corpus fixture stubs the launcher out; the Windows reuse test needs the real one.
REAL_LAUNCH_WORKER = cache.launch_worker


def test_dead_worker_is_terminal_without_another_ensure(corpus):
    with closing(cache.connect()) as conn:
        conn.execute("INSERT INTO speed_jobs(id,request,state,created_at,updated_at,pid) VALUES('dead','{}','building',0,0,2147483647)")
        cache.put_meta(conn, worker_pid=2147483647)
        conn.commit()
    alive = cache._alive(2147483647)
    status = cache.job_status('dead')
    assert not alive
    assert status['state'] == 'error', 'SSE keeps waiting for a worker that no longer exists'


def test_missing_posix_resource_module_does_not_break_publication(corpus, monkeypatch):
    import builtins
    monkeypatch.setenv('TOKDASH_SPEED_CPU_FRACTION', '1')
    compute._sync_usage_store(CodingToolsUsageTracker())
    request = cache.ensure('2026-09-01', '2026-09-28', source='codex')
    original_import = builtins.__import__

    def no_resource(name, *args, **kwargs):
        if name == 'resource':
            raise ModuleNotFoundError("No module named 'resource'", name='resource')
        return original_import(name, *args, **kwargs)
    exception = None
    with closing(cache.connect()) as conn:
        row = conn.execute('SELECT * FROM speed_jobs WHERE id=?', (request['job_id'],)).fetchone()
        with monkeypatch.context() as patch:
            patch.setattr(builtins, '__import__', no_resource)
            try:
                speed_worker.run_job(conn, row, sync=False)
            except Exception as exc:
                exception = type(exc).__name__
        job = dict(conn.execute('SELECT * FROM speed_jobs WHERE id=?', (request['job_id'],)).fetchone())
        count = conn.execute('SELECT count(*) FROM speed_responses').fetchone()[0]
    assert job['state'] == 'ready', 'optional POSIX memory telemetry rolls back successful timing work'
    assert exception is None


def test_windows_liveness_probe_does_not_terminate_the_worker(monkeypatch):
    from types import SimpleNamespace
    worker = {'running': True}
    calls = []

    def windows_kill(pid, sig):
        calls.append((pid, sig))
        worker['running'] = False
    monkeypatch.setattr(cache, 'os', SimpleNamespace(name='nt', kill=windows_kill))
    reported_alive = cache._alive(2147483646)
    assert worker['running'], 'the liveness probe uses a terminating Windows signal'


def test_pid_reuse_and_quiet_live_worker_health(corpus,monkeypatch):
    import os
    alive,token=speed_process.snapshot(os.getpid())
    assert alive and token
    assert cache._alive(os.getpid(),token)
    assert not cache._alive(os.getpid(),token+'reused')
    with closing(cache.connect()) as conn:
        for job,identity in [('quiet',token),('reused',token+'reused')]:
            conn.execute("INSERT INTO speed_jobs(id,request,state,created_at,updated_at,pid,worker_token) VALUES(?,'{}','building',0,0,?,?)",(job,os.getpid(),identity))
        conn.commit()
    assert cache.job_status('quiet')['state']=='building'  # No progress/heartbeat timeout.
    assert cache.job_status('reused')['state']=='error'


def test_windows_second_enqueue_reuses_worker_without_signalling(corpus,monkeypatch):
    from types import SimpleNamespace
    from tokdash import speed_cache
    monkeypatch.setattr(cache,'os',SimpleNamespace(name='nt',kill=lambda *a:pytest.fail('Windows process signalled')))
    monkeypatch.setattr(speed_process,'_windows_snapshot',lambda pid:(True,'windows:123'))
    monkeypatch.setattr(cache.subprocess,'Popen',lambda *a,**k:pytest.fail('second worker launched'))
    with closing(cache.connect()) as conn:
        cache.put_meta(conn,worker_pid=123,worker_token='windows:123');conn.commit()
        REAL_LAUNCH_WORKER(conn)


def test_optional_resource_rss_units(monkeypatch):
    import sys
    from types import SimpleNamespace
    monkeypatch.setitem(sys.modules,'resource',SimpleNamespace(RUSAGE_SELF=0,getrusage=lambda _:SimpleNamespace(ru_maxrss=1048576)))
    monkeypatch.setattr(sys,'platform','darwin')
    assert speed_worker.peak_rss_mb()==1
    monkeypatch.setattr(sys,'platform','linux')
    assert speed_worker.peak_rss_mb()==1024


def test_actual_subscription_finishes_when_worker_dies(corpus):
    import asyncio
    from types import SimpleNamespace
    from tokdash.api import output_speed_job_events
    with closing(cache.connect()) as conn:
        conn.execute("INSERT INTO speed_jobs(id,request,state,created_at,updated_at,pid,worker_token) VALUES('subscription','{}','building',0,0,2147483647,'dead-instance')")
        conn.commit()
    async def disconnected():
        return False
    async def collect():
        response=await output_speed_job_events('subscription',SimpleNamespace(is_disconnected=disconnected))
        return [event async for event in response.body_iterator]
    events=asyncio.run(collect())
    assert len(events)==1 and 'worker interrupted' in events[0] and '"state":"error"' in events[0]
