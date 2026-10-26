"""Publishing a lazy-speed build without disturbing what is already published.

A one-session build stages and writes only that session, keeps unrelated rows
(and their rowids) as they were, and does work that does not grow with the
history beside it. A changed stream mapping publishes even when the response
figures match, and a failure before the commit leaves the last good
publication in place.
"""
import json
from contextlib import closing
from pathlib import Path

from test_lazy_speed_cache import corpus, build_requested
from tokdash import speed_cache as cache, speed_worker, compute
from tokdash.sources.coding_tools import CodingToolsUsageTracker


def test_session_build_reuses_history_without_rewriting_it(corpus, monkeypatch):
    import sqlite3
    from output_speed_expansion_fixtures import codex_call
    root, _, inventory = corpus
    monkeypatch.setenv('TOKDASH_SPEED_CPU_FRACTION', '1')
    with sqlite3.connect(root / 'opencode.db') as conn:
        template = conn.execute("SELECT data FROM message WHERE id='m1'").fetchone()[0]
        conn.executemany('INSERT INTO message VALUES(?,?,?,?,?)', ((f'history-{i}', 's', 1788264000000, 1788264002000, template) for i in range(1000)))
    compute._sync_usage_store(CodingToolsUsageTracker())
    request = {'from': '2026-09-01', 'to': '2026-09-28', 'source': ''}
    build_requested(request)
    new_file = Path(inventory['codex_file']).with_name('rollout-new-session.jsonl')
    records = [dict(type='session_meta', payload=dict(id='one-new-session', cwd='/fixture')), *codex_call(999)]
    new_file.write_text(''.join((json.dumps(row) + '\n' for row in records)))
    import tokdash.sources.coding_tools as readers
    readers._sig_cache.clear()
    store, _ = compute._sync_usage_store(CodingToolsUsageTracker())
    with closing(store.read_connection()) as primary:
        assert primary.execute('SELECT count(*) FROM usage_entries WHERE file_path=?', (str(new_file),)).fetchone()[0] == 1
        assert primary.execute("SELECT 1 FROM usage_session_inputs WHERE source='codex' AND session_id='one-new-session' AND file_path=?", (str(new_file),)).fetchone()
    observed = []
    original = speed_worker._progress

    def progress(conn, job, completed, total):
        observed.append(conn.execute('SELECT count(*) FROM speed_stage').fetchone()[0])
        return original(conn, job, completed, total)
    monkeypatch.setattr(speed_worker, '_progress', progress)
    with closing(cache.connect()) as conn:
        before = conn.execute('SELECT count(*) FROM speed_responses').fetchone()[0]
        changes = conn.total_changes
        speed_worker.build(conn, 'test', {**request, 'source': 'codex', 'session_id': 'one-new-session'}, sync=False)
        changes = conn.total_changes - changes
        final = conn.execute('SELECT count(*) FROM speed_responses').fetchone()[0]
    assert max(observed) <= 1, 'one-session build stages and rewrites unrelated cached history'
    assert final == before + 1
    assert changes < 100, 'one-session publication rewrote unrelated rows'


def test_session_publication_keeps_unrelated_rowids_and_scales_in_sql_work(corpus,monkeypatch):
    import sqlite3
    import output_speed_expansion_fixtures as fixtures
    _,_,inventory=corpus
    monkeypatch.setenv('TOKDASH_SPEED_CPU_FRACTION','1')
    store,_=compute._sync_usage_store(CodingToolsUsageTracker())
    request={'from':'2026-09-01','to':'2026-09-28','source':''}
    build_requested(request)
    original=Path(inventory['codex_file']); new_file=original.with_name('one-short-session.jsonl')
    records=[dict(type='session_meta',payload=dict(id='one-short-session',cwd='/fixture')),*fixtures.codex_call(999)]
    new_file.write_text(''.join(json.dumps(row)+'\n' for row in records))
    from tokdash.sources.coding_tools import _sig_cache
    _sig_cache.clear();compute._sync_usage_store(CodingToolsUsageTracker())
    with closing(cache.connect()) as conn:
        base=list(conn.execute("SELECT rowid,* FROM speed_responses WHERE source='opencode'"))
        template=tuple(conn.execute("SELECT * FROM speed_responses WHERE source='opencode' LIMIT 1").fetchone())
        def add_history(first,last):
            for i in range(first,last):
                row=list(template); row[1]='unrelated-'+str(i);row[3]='unrelated'
                conn.execute('INSERT INTO speed_responses VALUES ('+','.join('?' for _ in row)+')',row)
                conn.execute("INSERT INTO speed_session_members VALUES('opencode',?,'unrelated',?,'main',0,1000,'recorded')",(row[1],row[1]))
            conn.commit()
        def measure():
            steps=[];conn.set_progress_handler(lambda:steps.append(1) or 0,100)
            changes=conn.total_changes
            speed_worker.build(conn,'test',dict(request,source='codex',session_id='one-short-session'),sync=False)
            conn.set_progress_handler(None,0)
            return len(steps)*100,conn.total_changes-changes
        add_history(0,1000);small_steps,small_changes=measure()
        # Remove only the selected timing input to repeat a cold bounded build.
        conn.execute("DELETE FROM speed_responses WHERE session_id='one-short-session'")
        conn.execute("DELETE FROM speed_session_members WHERE session_id='one-short-session'")
        conn.execute("DELETE FROM speed_inputs WHERE input_owner=?",(str(new_file),));conn.commit()
        add_history(1000,20000);large_steps,large_changes=measure()
        assert large_steps<=small_steps+1000, (small_steps,large_steps)
        assert large_changes<100 and small_changes<100
        assert [tuple(row) for row in conn.execute("SELECT rowid,* FROM speed_responses WHERE source='opencode' AND entry_key NOT LIKE 'unrelated-%'")]==[tuple(row) for row in base]


def test_changed_stream_mapping_publishes_even_when_response_scalars_match(corpus,monkeypatch):
    monkeypatch.setenv('TOKDASH_SPEED_CPU_FRACTION','1')
    compute._sync_usage_store(CodingToolsUsageTracker())
    request={'from':'2026-09-01','to':'2026-09-28','source':'codex','session_id':'bench-codex'}
    build_requested(request)
    original=speed_worker.session_mapping
    def changed(*args):
        return {key:(*values[:2],'changed-stream',*values[3:]) for key,values in original(*args).items()}
    monkeypatch.setattr(speed_worker,'session_mapping',changed)
    with closing(cache.connect()) as conn:
        conn.execute("UPDATE speed_inputs SET signature='' WHERE source='codex'");conn.commit()
        speed_worker.build(conn,'test',request,sync=False)
        assert {r[0] for r in conn.execute("SELECT stream_id FROM speed_session_members WHERE source='codex'")}=={'changed-stream'}


def test_atomic_failure_before_commit_keeps_unrelated_and_selected_last_good(corpus,monkeypatch):
    monkeypatch.setenv('TOKDASH_SPEED_CPU_FRACTION','1')
    compute._sync_usage_store(CodingToolsUsageTracker())
    request={'from':'2026-09-01','to':'2026-09-28','source':''}
    build_requested(request)
    with closing(cache.connect()) as conn:
        before=[tuple(row) for row in conn.execute('SELECT * FROM speed_responses ORDER BY source,entry_key')]
        generation=cache.meta(conn)['publication_revision']
        def broken(*args,**kwargs):
            if 'publication_revision' in kwargs:raise RuntimeError('publication interrupted')
            return original(*args,**kwargs)
        original=cache.put_meta;monkeypatch.setattr(cache,'put_meta',broken)
        job=cache.ensure(request['from'],request['to'],source='codex',refresh=True)['job_id']
        row=conn.execute('SELECT * FROM speed_jobs WHERE id=?',(job,)).fetchone()
        assert not speed_worker.run_job(conn,row,sync=False)
        assert before==[tuple(row) for row in conn.execute('SELECT * FROM speed_responses ORDER BY source,entry_key')]
        assert cache.meta(conn)['publication_revision']==generation
