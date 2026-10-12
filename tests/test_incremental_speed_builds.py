"""Model timing builds read only the inputs whose accounting moved, once they settle."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from test_lazy_speed_cache import corpus, build_requested
from tokdash import speed_cache as cache, speed_worker, compute
from tokdash.sources.coding_tools import CodingToolsUsageTracker, _sig_cache

WINDOW = ('2026-09-01', '2026-09-28')


@pytest.fixture
def built(corpus, monkeypatch):
    monkeypatch.setenv('TOKDASH_SPEED_CPU_FRACTION', '1')
    compute._sync_usage_store(CodingToolsUsageTracker())
    build_requested({'from': WINDOW[0], 'to': WINDOW[1], 'source': ''})
    return corpus


def append_codex_call(inventory, output=100):
    import output_speed_expansion_fixtures as fixtures
    with Path(inventory['codex_file']).open('a') as stream:
        stream.write(''.join(json.dumps(row) + '\n' for row in fixtures.codex_call(output)))
    _sig_cache.clear()
    compute._sync_usage_store(CodingToolsUsageTracker())


def run(status, request=None):
    with closing(cache.connect()) as c:
        speed_worker.build(c, status['job_id'], request or {'source': ''}, sync=False)
        job = c.execute('SELECT * FROM speed_jobs WHERE id=?', (status['job_id'],)).fetchone()
        return dict(job), json.loads(cache.meta(c)['last_build_counts'])


def codex_calls():
    rows = cache.payload(*WINDOW, cache_only=True)['rows']
    return next(r['speed_calls'] for r in rows if r['source'] == 'codex')


def forbid_reads(monkeypatch, allowed=()):
    original = speed_worker._collect_parser_file

    def collect(parser, sig, **kw):
        assert sig[0] in allowed, f'unchanged input reread: {sig[0]}'
        return original(parser, sig, **kw)
    monkeypatch.setattr(speed_worker, '_collect_parser_file', collect)
    monkeypatch.setattr(speed_worker, 'native_rows', lambda *a: pytest.fail('unchanged native database reread'))


def test_a_settled_change_reads_only_its_own_input(built, monkeypatch):
    _, _, inventory = built
    append_codex_call(inventory)
    status = cache.ensure(*WINDOW)
    assert status['state'] == 'stale' and status['pending_inputs'] == 1
    forbid_reads(monkeypatch, allowed=(inventory['codex_file'],))
    job, counts = run(status)
    assert (job['completed_inputs'], job['total_inputs']) == (1, 1), 'progress counts only what is read'
    assert counts['parsed_files'] == 1 and counts['reused_files'] == 0 and counts['parsed_native_scopes'] == 0
    assert codex_calls() == 21
    assert cache.ensure(*WINDOW)['state'] == 'ready'


def test_new_usage_elsewhere_keeps_every_range_ready_without_a_job(built, monkeypatch):
    target = cache.primary_generation()
    monkeypatch.setattr(cache, 'primary_generation', lambda: target + 5)
    with closing(cache.connect()) as c:
        jobs = c.execute('SELECT count(*) FROM speed_jobs').fetchone()[0]
    for day_from, day_to in (WINDOW, ('2026-09-28', '2026-09-28'), ('2025-10-01', '2026-10-12')):
        status = cache.ensure(day_from, day_to)
        assert status['state'] == 'ready' and status['job_id'] is None
    with closing(cache.connect()) as c:
        assert c.execute('SELECT count(*) FROM speed_jobs').fetchone()[0] == jobs


def test_every_range_shares_one_model_build(built):
    _, _, inventory = built
    append_codex_call(inventory)
    jobs = {cache.ensure(day_from, day_to)['job_id'] for day_from, day_to in
            (WINDOW, ('2026-09-28', '2026-09-28'), ('2025-10-01', '2026-10-12'))}
    assert len(jobs) == 1 and None not in jobs


def test_an_input_still_being_written_keeps_its_last_timing_until_it_settles(built, monkeypatch):
    _, _, inventory = built
    monkeypatch.setenv('TOKDASH_SPEED_SETTLE_SECONDS', '3600')
    append_codex_call(inventory)
    status = cache.ensure(*WINDOW)
    assert status['state'] == 'ready' and status['job_id'] is None
    assert (status['pending_inputs'], status['deferred_inputs']) == (0, 1)
    assert codex_calls() == 20, 'the published rows stand while the log is live'
    # A build for anything else leaves the live log alone.
    forbid_reads(monkeypatch)
    with closing(cache.connect()) as c:
        c.execute("INSERT INTO speed_jobs(id,request,state,created_at,updated_at) VALUES('other','{}','building',0,0)"); c.commit()
    job, counts = run({'job_id': 'other'})
    assert job['total_inputs'] == 0 and counts['deferred_inputs'] == 1
    with closing(cache.connect()) as c:
        assert c.execute("SELECT state FROM speed_inputs WHERE input_owner=?", (inventory['codex_file'],)).fetchone()[0] == 'ready'
    monkeypatch.setenv('TOKDASH_SPEED_SETTLE_SECONDS', '0')
    monkeypatch.setattr(speed_worker, '_collect_parser_file', compute._collect_parser_file)
    status = cache.ensure(*WINDOW)
    assert status['state'] == 'stale' and status['job_id']
    run(status)
    assert codex_calls() == 21


def test_an_explicit_refresh_reads_a_live_input_now(built, monkeypatch):
    _, _, inventory = built
    monkeypatch.setenv('TOKDASH_SPEED_SETTLE_SECONDS', '3600')
    append_codex_call(inventory)
    status = cache.ensure(*WINDOW, refresh=True)
    assert status['job_id']
    job, counts = run(status, {'source': '', 'refresh': True})
    assert counts['parsed_files'] == 1 and counts['deferred_inputs'] == 0
    assert codex_calls() == 21


def test_a_live_native_database_waits_without_being_marked_pending(built, monkeypatch):
    root, _, _ = built
    monkeypatch.setenv('TOKDASH_SPEED_SETTLE_SECONDS', '3600')
    with sqlite3.connect(root / 'opencode.db') as c:
        c.execute("UPDATE message SET data=json_set(data,'$.tokens.output',77) WHERE id='m1'")
    status = cache.ensure(*WINDOW)
    assert status['state'] == 'ready' and status['deferred_inputs'] == 1
    forbid_reads(monkeypatch)
    with closing(cache.connect()) as c:
        c.execute("INSERT INTO speed_jobs(id,request,state,created_at,updated_at) VALUES('other','{}','building',0,0)"); c.commit()
    run({'job_id': 'other'})
    with closing(cache.connect()) as c:
        assert c.execute("SELECT state FROM speed_inputs WHERE source='opencode'").fetchone()[0] == 'ready'
    statuses = {r['source'] for r in cache.payload(*WINDOW, cache_only=True)['source_status']}
    assert 'opencode' not in statuses


@pytest.mark.parametrize('gone', ['deleted-file', 'dropped-accounting'])
def test_a_vanished_input_follows_accounting(built, gone):
    _, _, inventory = built
    from tokdash.usage_store import UsageEntryStore
    owner = inventory['codex_file']
    Path(owner).unlink()
    _sig_cache.clear()
    compute._sync_usage_store(CodingToolsUsageTracker())
    if gone == 'dropped-accounting':
        with closing(UsageEntryStore()._connect()) as c:
            c.execute("DELETE FROM usage_entries WHERE source='codex' AND file_path=?", (owner,))
            c.execute("DELETE FROM file_state WHERE source='codex' AND path=?", (owner,))
            c.commit()
    status = cache.ensure(*WINDOW)
    assert status['state'] == 'stale'
    run(status)
    with closing(cache.connect()) as c:
        timed = c.execute("SELECT count(*) FROM speed_responses WHERE source='codex' AND input_owner=?", (owner,)).fetchone()[0]
        state = c.execute("SELECT state FROM speed_inputs WHERE source='codex' AND input_owner=?", (owner,)).fetchone()
    if gone == 'deleted-file':
        # Codex accounting outlives its rollout, and so does the last read of it.
        assert timed == 20 and state[0] == 'ready'
    else:
        assert timed == 0 and state is None
    assert cache.ensure(*WINDOW)['state'] == 'ready'


def test_a_build_with_nothing_to_read_keeps_the_published_revision(built, monkeypatch):
    forbid_reads(monkeypatch)
    with closing(cache.connect()) as c:
        revision = cache.meta(c)['publication_revision']
        c.execute("INSERT INTO speed_jobs(id,request,state,created_at,updated_at) VALUES('noop','{}','building',0,0)"); c.commit()
    job, counts = run({'job_id': 'noop'})
    assert job['state'] == 'ready' and job['total_inputs'] == 0
    with closing(cache.connect()) as c:
        assert cache.meta(c)['publication_revision'] == revision, 'an unchanged snapshot keeps its cached reports'


def test_a_moved_key_reaches_the_input_that_now_owns_it(built, monkeypatch):
    """Rows can change owner without their files changing; entry_count catches it."""
    _, _, inventory = built
    from tokdash.usage_store import UsageEntryStore
    original = inventory['codex_file']
    with closing(UsageEntryStore()._connect()) as c:
        key = c.execute("SELECT entry_key FROM usage_entries WHERE source='codex' AND file_path=? LIMIT 1", (original,)).fetchone()[0]
    with closing(cache.connect(readonly=True)) as c:
        before = c.execute("SELECT input_owner FROM speed_responses WHERE source='codex' AND entry_key=?", (key,)).fetchone()[0]
    assert before == original
    # Simulate the store handing one key to another file of the same session.
    other = str(Path(original).with_name('rollout-copy.jsonl'))
    Path(other).write_text(Path(original).read_text())
    with closing(UsageEntryStore()._connect()) as c:
        c.execute("UPDATE usage_entries SET file_path=? WHERE source='codex' AND entry_key=?", (other, key))
        for path in (original, other):
            c.execute("""INSERT INTO file_state(source,path,mtime_ns,size,safe_offset,missing,signature,updated_at_ms,entry_count)
                VALUES('codex',?,0,0,0,0,'copy',0,0) ON CONFLICT(source,path) DO NOTHING""", (path,))
        c.execute("""UPDATE file_state SET entry_count=(SELECT count(*) FROM usage_entries u
            WHERE u.source=file_state.source AND u.file_path=file_state.path) WHERE source='codex'""")
        c.commit()
    status = cache.ensure(*WINDOW)
    assert status['pending_inputs'] == 2
    run(status)
    with closing(cache.connect(readonly=True)) as c:
        assert c.execute("SELECT input_owner FROM speed_responses WHERE source='codex' AND entry_key=?", (key,)).fetchone()[0] == other
    assert cache.ensure(*WINDOW)['state'] == 'ready'
