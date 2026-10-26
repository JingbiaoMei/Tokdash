"""Synthetic acceptance tests for OpenCode's privacy-filtered rich detail."""
from __future__ import annotations

import json
import hashlib
import sqlite3

import pytest

from tokdash import sessions

BASE = 1_787_000_000_000
V2 = """CREATE TABLE session_message(id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
 type TEXT NOT NULL, seq INTEGER NOT NULL, time_created INTEGER, time_updated INTEGER, data TEXT);"""
LEGACY = "CREATE TABLE message(id TEXT PRIMARY KEY,session_id TEXT,time_created INTEGER,time_updated INTEGER,data TEXT);"
PART = "CREATE TABLE part(id TEXT PRIMARY KEY,message_id TEXT,session_id TEXT,time_created INTEGER,time_updated INTEGER,data TEXT);"


def dbfile(tmp_path, monkeypatch, *, v2=True, parts=True):
    root = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(root))
    path = root / "opencode" / "opencode.db"
    path.parent.mkdir(parents=True)
    con = sqlite3.connect(path)
    con.executescript(V2 if v2 else LEGACY)
    if parts:
        con.executescript(PART)
    return path, con


def v2row(con, ident, role, data, *, seq=0, stamp=BASE, sid="sess-1"):
    con.execute("INSERT INTO session_message VALUES(?,?,?,?,?,?,?)",
                (ident, sid, role, seq, stamp, stamp, json.dumps(data)))


def part(con, ident, mid, payload, *, stamp=BASE, sid="sess-1"):
    con.execute("INSERT INTO part VALUES(?,?,?,?,?,?)",
                (ident, mid, sid, stamp, stamp, json.dumps(payload)))


def detail(tmp_path, monkeypatch, path, sid="sess-1"):
    return sessions._opencode_rich_session_detail(sid, {}, {})


def test_current_v2_inline_exact_metadata_and_all_tool_states(tmp_path, monkeypatch):
    _, c = dbfile(tmp_path, monkeypatch, parts=False)
    v2row(c, "u", "user", {"text": "hi"}, seq=0)
    v2row(c, "a", "assistant", {"model": {"id": "synthetic-model", "providerID": "synthetic-provider"},
        "tokens": {"input": 2, "output": 5, "reasoning": 4, "cache": {"read": 3, "write": 1}},
        "content": [{"type": "text", "text": "abc"}, {"type": "reasoning", "text": "secret"},
                    *[{"type": "tool", "id": x, "name": "synthetic-tool", "state": {"status": st, "input": {}}}
                      for x, st in [("t1", "pending"), ("t2", "error"), ("t3", "completed")]]]}, seq=1)
    v2row(c, "s", "system", {"text": "sys"}, seq=2)
    c.commit(); c.close()
    got = detail(tmp_path, monkeypatch, None)
    calls = [{"id": x, "name": "synthetic-tool", "tool_name": "synthetic-tool", "status": st,
              "has_args": True, "timestamp": sessions._ms_to_iso(BASE)} for x, st in [("t1", "pending"), ("t2", "error"), ("t3", "completed")]]
    assert got == {"messages": [
        {"id": "u", "role": "user", "content_chars": 2, "has_reasoning": False, "has_tool_calls": False, "timestamp": sessions._ms_to_iso(BASE)},
        {"id": "a", "role": "assistant", "content_chars": 3, "has_reasoning": True, "has_tool_calls": True, "timestamp": sessions._ms_to_iso(BASE), "model": "synthetic-model", "token_count": 11},
        {"id": "s", "role": "system", "content_chars": 3, "has_reasoning": False, "has_tool_calls": False, "timestamp": sessions._ms_to_iso(BASE)}],
        "tool_calls": calls, "tool_executions": calls}


def test_inline_works_without_part_table(tmp_path, monkeypatch):
    test_current_v2_inline_exact_metadata_and_all_tool_states(tmp_path, monkeypatch)


def test_migrated_v2_recovers_legacy_parts(tmp_path, monkeypatch):
    _, c = dbfile(tmp_path, monkeypatch)
    v2row(c, "a", "assistant", {"modelID": "flat", "tokens": {}, "time": {}}, seq=0)
    part(c, "p1", "a", {"type": "text", "text": "abcd"})
    part(c, "p2", "a", {"type": "reasoning", "text": "hidden"})
    part(c, "p3", "a", {"type": "tool", "tool": "synthetic-tool", "callID": "call-1", "state": {"status": "done", "input": {}}})
    c.commit(); c.close()
    got = detail(tmp_path, monkeypatch, None)
    assert got["messages"][0]["content_chars"] == 4 and got["messages"][0]["has_reasoning"]
    assert got["tool_calls"][0]["id"] == "call-1" and got["tool_calls"][0]["has_args"]


def test_inline_precedence_empty_content_distinct_tools_and_original_index(tmp_path, monkeypatch):
    _, c = dbfile(tmp_path, monkeypatch)
    v2row(c, "a", "assistant", {"content": ["noise", {"type": "tool", "name": "same"}, {"type": "tool", "name": "same"}]})
    part(c, "legacy", "a", {"type": "text", "text": "should-not-win"})
    v2row(c, "empty", "assistant", {"content": []}, seq=1)
    part(c, "ignored", "empty", {"type": "tool", "tool": "ignored"})
    c.commit(); c.close()
    got = detail(tmp_path, monkeypatch, None)
    assert [x["id"] for x in got["tool_calls"]] == ["a:tool:1", "a:tool:2"]
    assert got["messages"][0]["content_chars"] == 0 and not got["messages"][1]["has_tool_calls"]


def test_legacy_order_is_timestamp_then_id(tmp_path, monkeypatch):
    _, c = dbfile(tmp_path, monkeypatch, v2=False)
    for ident, role in [("z", "user"), ("b", "assistant"), ("a", "system")]:
        c.execute("INSERT INTO message VALUES(?,?,?,?,?)", (ident, "sess-1", BASE, BASE, json.dumps({"role": role, "text": ident, "content": []})))
    c.commit(); c.close()
    assert [m["id"] for m in detail(tmp_path, monkeypatch, None)["messages"]] == ["a", "b", "z"]


def test_v2_history_selection_pre_switch_and_post_switch(tmp_path, monkeypatch):
    _, c = dbfile(tmp_path, monkeypatch)
    c.executescript(LEGACY)
    v2row(c, "control", "idle", {})
    c.execute("INSERT INTO message VALUES('old','sess-1',?,?,?)", (BASE, BASE, json.dumps({"role": "assistant", "content": []})))
    c.commit(); c.close()
    assert [m["id"] for m in detail(tmp_path, monkeypatch, None)["messages"]] == ["old"]


def test_post_switch_uses_only_v2_for_same_session(tmp_path, monkeypatch):
    _, c = dbfile(tmp_path, monkeypatch)
    c.executescript(LEGACY)
    v2row(c, "current", "assistant", {"content": []})
    c.execute("INSERT INTO message VALUES('legacy-only','sess-1',?,?,?)",
              (BASE + 1, BASE + 1, json.dumps({"role": "assistant", "content": []})))
    c.execute("INSERT INTO message VALUES('same-id','sess-1',?,?,?)",
              (BASE + 2, BASE + 2, json.dumps({"role": "assistant", "content": []})))
    c.commit(); c.close()
    ids = [m["id"] for m in detail(tmp_path, monkeypatch, None)["messages"]]
    assert ids == ["current"]
    assert len(ids) == len(set(ids))


def test_seq_order_and_tool_order_beats_timestamps(tmp_path, monkeypatch):
    _, c = dbfile(tmp_path, monkeypatch, parts=False)
    v2row(c, "later", "assistant", {"content": [{"type": "tool", "id": "one", "name": "x"}]}, seq=0, stamp=BASE+5000)
    v2row(c, "earlier", "assistant", {"content": [{"type": "tool", "id": "two", "name": "x"}]}, seq=1, stamp=BASE)
    c.commit(); c.close()
    got=detail(tmp_path, monkeypatch, None)
    assert [m["id"] for m in got["messages"]] == ["later", "earlier"]
    assert [t["id"] for t in got["tool_calls"]] == ["one", "two"]


def test_controls_other_sessions_and_orphans_excluded(tmp_path, monkeypatch):
    _, c = dbfile(tmp_path, monkeypatch)
    for kind in ("idle", "switch", "shell", "synthetic"):
        v2row(c, kind, kind, {}, sid="sess-1")
    v2row(c, "other", "user", {"text": "x"}, sid="sess-2")
    part(c, "orphan", "not-a-message", {"type": "tool", "tool": "x"})
    v2row(c, "ok", "user", {"text": "ok"}, seq=9)
    c.commit(); c.close()
    assert [m["id"] for m in detail(tmp_path, monkeypatch, None)["messages"]] == ["ok"]


def test_unicode_tokens_identity_and_empty_args(tmp_path, monkeypatch):
    _, c=dbfile(tmp_path,monkeypatch,parts=False)
    v2row(c,"unicode","assistant",{"modelID":"", "model":{"id":"nested","providerID":"provider"},"tokens":{"input":2,"output":3,"reasoning":9,"cache":{"read":1,"write":0}},"content":[{"type":"text","text":"é😀"},{"type":"reasoning","text":"x"},{"type":"tool","name":"x","state":{"input":{}}}]})
    v2row(c,"missing","assistant",{"content":[]},seq=1)
    v2row(c,"zero","assistant",{"tokens":{"input":0},"content":[]},seq=2)
    c.commit(); c.close()
    msgs=detail(tmp_path,monkeypatch,None)["messages"]
    assert (msgs[0]["content_chars"],msgs[0]["token_count"],msgs[0]["model"]) == (2,6,"nested")
    assert "token_count" not in msgs[1] and msgs[2]["token_count"] == 0


@pytest.mark.parametrize("stamp", [None, -1, 1e100, 1.5])
def test_invalid_row_timestamp_omitted_but_message_retained(tmp_path, monkeypatch, stamp):
    _,c=dbfile(tmp_path,monkeypatch,parts=False)
    v2row(c,"m","user",{"text":"ok"},stamp=stamp)
    c.commit();c.close()
    msg=detail(tmp_path,monkeypatch,None)["messages"][0]
    assert msg["id"]=="m" and "timestamp" not in msg


def test_bad_json_and_wrong_shapes_warn_without_payload(tmp_path, monkeypatch, caplog):
    _,c=dbfile(tmp_path,monkeypatch,parts=False)
    c.execute("INSERT INTO session_message VALUES('bad','sess-1','assistant',0,?,?,?)",(BASE,BASE,"SENTINEL_INVALID_JSON"))
    c.execute("INSERT INTO session_message VALUES('scalar','sess-1','assistant',1,?,?,?)",(BASE,BASE,"42"))
    c.execute("INSERT INTO session_message VALUES('list','sess-1','assistant',2,?,?,?)",(BASE,BASE,"[]"))
    v2row(c,"wrong-content","assistant",{"content":"SENTINEL_CONTENT","tokens":"bad"},seq=3)
    v2row(c,"wrong-state","assistant",{"content":[{"type":"tool","name":"synthetic-tool","state":"SENTINEL_STATE"}]},seq=4)
    v2row(c,"wrong-text","assistant",{"content":[{"type":"text","text":{"payload":"SENTINEL_TEXT"}}]},seq=5)
    c.commit();c.close()
    got=detail(tmp_path,monkeypatch,None)
    assert [m["id"] for m in got["messages"]]==["wrong-content","wrong-state","wrong-text"]
    assert got["messages"][0]["content_chars"]==0 and got["tool_calls"][0]["status"]=="unknown"
    assert "bad" in caplog.text and "scalar" in caplog.text and "list" in caplog.text
    for sentinel in ("SENTINEL_INVALID_JSON", "SENTINEL_CONTENT", "SENTINEL_STATE", "SENTINEL_TEXT", "payload"):
        assert sentinel not in caplog.text + json.dumps(got)


def test_tool_timestamp_precedence_invalid_fallback_and_epoch_zero(tmp_path, monkeypatch):
    _, c = dbfile(tmp_path, monkeypatch)
    v2row(c, "inline", "assistant", {"content": [
        {"type":"tool","id":"valid","name":"synthetic-tool","time":{"created":0}},
        {"type":"tool","id":"fallback","name":"synthetic-tool","time":{"created":"bad"}},
    ]}, stamp=BASE)
    v2row(c, "inline-none", "assistant", {"content":[{"type":"tool","id":"none","name":"synthetic-tool","time":{"created":-1}}]}, seq=1, stamp=None)
    v2row(c, "part-msg", "assistant", {"modelID":"synthetic-model"}, seq=2, stamp=None)
    part(c,"p-valid","part-msg",{"type":"tool","tool":"synthetic-tool","state":{"time":{"start":0}}},stamp=BASE+1)
    part(c,"p-fallback","part-msg",{"type":"tool","tool":"synthetic-tool","state":{"time":{"start":"bad"}}},stamp=BASE+2)
    part(c,"p-none","part-msg",{"type":"tool","tool":"synthetic-tool","state":{"time":{"start":-1}}},stamp=None)
    c.commit(); c.close()
    calls=detail(tmp_path,monkeypatch,None)["tool_calls"]
    assert calls[0]["timestamp"] == sessions._ms_to_iso(0)
    assert calls[1]["timestamp"] == sessions._ms_to_iso(BASE)
    assert "timestamp" not in calls[2]
    by_id={call["id"]:call for call in calls}
    assert by_id["p-valid"]["timestamp"] == sessions._ms_to_iso(0)
    assert by_id["p-fallback"]["timestamp"] == sessions._ms_to_iso(BASE+2)
    assert "timestamp" not in by_id["p-none"]


def test_privacy_public_keys_and_payload_sentinels(tmp_path, monkeypatch, caplog):
    _,c=dbfile(tmp_path,monkeypatch,parts=False)
    v2row(c,"m","assistant",{"modelID":"synthetic-model","providerID":"SENTINEL_PROVIDER","content":[{"type":"text","text":"SENTINEL_TEXT"},{"type":"reasoning","text":"SENTINEL_REASON"},{"type":"tool","name":"synthetic-tool","title":"SENTINEL_TITLE","metadata":{"x":"SENTINEL_META"},"state":{"status":"done","input":{"x":"SENTINEL_ARGS"},"output":"SENTINEL_OUTPUT","state":"SENTINEL_ENCRYPTED"}}]})
    c.commit();c.close()
    got=detail(tmp_path,monkeypatch,None)
    serialized=json.dumps(got)+caplog.text
    for secret in ("SENTINEL_PROVIDER","SENTINEL_TEXT","SENTINEL_REASON","SENTINEL_TITLE","SENTINEL_META","SENTINEL_ARGS","SENTINEL_OUTPUT","SENTINEL_ENCRYPTED"):
        assert secret not in serialized
    assert set(got)=={"messages","tool_calls","tool_executions"}
    assert set(got["messages"][0])=={"id","role","content_chars","has_reasoning","has_tool_calls","timestamp","model"}
    assert set(got["tool_calls"][0])=={"id","name","tool_name","status","has_args","timestamp"}


def test_missing_db_errors_close_and_reads_do_not_mutate(tmp_path, monkeypatch):
    root=tmp_path/"xdg"; monkeypatch.setenv("XDG_DATA_HOME",str(root))
    missing=root/"opencode"/"opencode.db"
    with pytest.raises(sqlite3.Error): sessions._opencode_rich_session_detail("sess-1",{}, {})
    assert not missing.exists()
    path,c=dbfile(tmp_path,monkeypatch,parts=False)
    v2row(c,"ok","user",{"text":"unchanged"})
    c.commit();c.close()
    original=path.read_bytes(); original_count=sqlite3.connect(path).execute("select count(*) from session_message").fetchone()[0]
    real=sessions.sqlite3.connect; seen=[]
    class Proxy:
        def __init__(self, conn): self.conn=conn
        def execute(self,*a): return self.conn.execute(*a)
        def cursor(self,*a): return self.conn.cursor(*a)
        def rollback(self): self.conn.rollback()
        def close(self): seen.append("closed"); self.conn.close()
    def wrapped(*a,**k):
        proxy=Proxy(real(*a,**k)); seen.append(proxy); return proxy
    monkeypatch.setattr(sessions.sqlite3,"connect",wrapped)
    assert [m["id"] for m in sessions._opencode_rich_session_detail("sess-1",{}, {})["messages"]]==["ok"]
    assert "closed" in seen
    assert hashlib.sha256(path.read_bytes()).digest()==hashlib.sha256(original).digest()
    with real(path) as verify:
        assert verify.execute("select count(*) from session_message").fetchone()[0]==original_count
    seen.clear()
    class Broken(Proxy):
        def execute(self,*a): raise sqlite3.OperationalError("synthetic sqlite failure")
    def broken(*a,**k):
        proxy=Broken(real(*a,**k)); seen.append(proxy); return proxy
    monkeypatch.setattr(sessions.sqlite3,"connect",broken)
    with pytest.raises(sqlite3.OperationalError, match="synthetic sqlite failure"):
        sessions._opencode_rich_session_detail("sess-1",{}, {})
    assert "closed" in seen
    assert hashlib.sha256(path.read_bytes()).digest()==hashlib.sha256(original).digest()


def test_missing_required_message_schema_raises_without_mutation(tmp_path, monkeypatch):
    root=tmp_path/"xdg"; monkeypatch.setenv("XDG_DATA_HOME",str(root))
    path=root/"opencode"/"opencode.db"; path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE session_message(id TEXT PRIMARY KEY)")
    before=path.read_bytes()
    with pytest.raises(sqlite3.Error):
        sessions._opencode_rich_session_detail("sess-1",{}, {})
    assert path.read_bytes()==before
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM session_message").fetchone()[0]==0


def test_cleanup_failures_do_not_mask_original_error_and_close_is_attempted(tmp_path, monkeypatch):
    _, c=dbfile(tmp_path,monkeypatch,parts=False)
    c.close()
    real=sessions.sqlite3.connect
    calls=[]
    original_message="synthetic original query failure"
    class ErrorConnection:
        def __init__(self, conn): self.conn=conn
        def execute(self,sql,args=()):
            if "FROM session_message" in sql: raise sqlite3.OperationalError(original_message)
            return self.conn.execute(sql,args)
        def cursor(self): return self.conn.cursor()
        def rollback(self): calls.append("rollback"); raise RuntimeError("cleanup rollback failure")
        def close(self): calls.append("close"); self.conn.close(); raise RuntimeError("cleanup close failure")
    monkeypatch.setattr(sessions.sqlite3,"connect",lambda *a,**k: ErrorConnection(real(*a,**k)))
    with pytest.raises(sqlite3.OperationalError,match=original_message):
        sessions._opencode_rich_session_detail("sess-1",{}, {})
    assert calls==["rollback","close"]


def test_success_attempts_close_even_when_rollback_fails(tmp_path, monkeypatch):
    _,c=dbfile(tmp_path,monkeypatch,parts=False)
    v2row(c,"ok","user",{"text":"ok"}); c.commit(); c.close()
    real=sessions.sqlite3.connect; calls=[]
    class Proxy:
        def __init__(self,conn): self.conn=conn
        def execute(self,*a): return self.conn.execute(*a)
        def cursor(self): return self.conn.cursor()
        def rollback(self): calls.append("rollback"); raise RuntimeError("rollback failure")
        def close(self): calls.append("close"); self.conn.close()
    monkeypatch.setattr(sessions.sqlite3,"connect",lambda *a,**k: Proxy(real(*a,**k)))
    with pytest.raises(RuntimeError,match="rollback failure"):
        sessions._opencode_rich_session_detail("sess-1",{}, {})
    assert calls==["rollback","close"]


def public_db(tmp_path, monkeypatch):
    """A store the session list can read: one v2 session with one assistant message."""
    root=tmp_path/"xdg"; monkeypatch.setenv("XDG_DATA_HOME",str(root)); path=root/"opencode"/"opencode.db"; path.parent.mkdir(parents=True)
    c=sqlite3.connect(path)
    c.executescript("CREATE TABLE project(id TEXT PRIMARY KEY,worktree TEXT); CREATE TABLE session_v2(id TEXT PRIMARY KEY,project_id TEXT,workspace_id TEXT,parent_id TEXT,slug TEXT,directory TEXT,title TEXT,version TEXT,time_created INTEGER,time_updated INTEGER);"+V2)
    c.execute("INSERT INTO project VALUES('p','/synthetic')");c.execute("INSERT INTO session_v2 VALUES('sess-1','p',NULL,NULL,'slug','/synthetic','title','1',?,?)",(BASE,BASE))
    v2row(c,"a","assistant",{"tokens":{"input":1,"output":1},"content":[{"type":"text","text":"hello"}]})
    c.commit();c.close()


def test_public_detail_integration_and_errors(tmp_path, monkeypatch):
    public_db(tmp_path, monkeypatch)
    raw=sessions._raw_sessions_for_tool("opencode")["sess-1"]
    expected_session=sessions._summarize_session(raw)
    expected_session.pop("_active_intervals",None)
    expected_turns=sessions._public_turns(raw.get("turns",[]))
    result=sessions.get_session_detail("opencode","sess-1")
    assert set(("messages","tool_calls","tool_executions")) <= set(result)
    assert result["messages"][0]["content_chars"]==5
    assert result["session"]==expected_session
    assert result["turns"]==expected_turns
    with pytest.raises(FileNotFoundError): sessions.get_session_detail("opencode","missing")
    with pytest.raises(ValueError): sessions.get_session_detail("unsupported","sess-1")


def test_failed_rich_read_keeps_the_session_and_turns(tmp_path, monkeypatch, caplog):
    # The dashboard shows only "Failed to load session detail" for any error, so a
    # failure here must cost the message timeline, not the token summary and charts.
    public_db(tmp_path, monkeypatch)
    expected=sessions.get_session_detail("opencode","sess-1")

    def locked(*_args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sessions,"_opencode_rich_session_detail",locked)
    result=sessions.get_session_detail("opencode","sess-1")
    assert result["session"]==expected["session"]
    assert result["turns"]==expected["turns"] and result["turns"]
    assert result["messages"]==[] and result["tool_calls"]==[] and result["tool_executions"]==[]
    assert "database is locked" in caplog.text


def test_indexed_filtered_queries_and_no_n_plus_one(tmp_path, monkeypatch):
    _,c=dbfile(tmp_path,monkeypatch)
    c.execute("CREATE INDEX msg_session_seq ON session_message(session_id,seq)")
    c.execute("CREATE INDEX part_session ON part(session_id)")
    v2row(c,"a","assistant",{"content":[]})
    v2row(c,"b","assistant",{"modelID":"synthetic-model"},seq=1)
    part(c,"p","b",{"type":"text","text":"x"})
    c.commit();c.close()
    dbpath=tmp_path/"xdg"/"opencode"/"opencode.db"
    raw_connect=sessions.sqlite3.connect
    queries=[]
    class CursorProxy:
        def __init__(self, cur): self.cur=cur
        def execute(self, sql, args=()): queries.append((sql,args)); self.cur.execute(sql,args); return self
        def fetchall(self): return self.cur.fetchall()
        def fetchone(self): return self.cur.fetchone()
        def __iter__(self): return iter(self.cur)
    class ConnProxy:
        def __init__(self, conn): self.conn=conn
        def execute(self, sql, args=()): queries.append((sql,args)); return CursorProxy(self.conn.execute(sql,args))
        def cursor(self): return CursorProxy(self.conn.cursor())
        def rollback(self): return self.conn.rollback()
        def close(self): return self.conn.close()
    monkeypatch.setattr(sessions.sqlite3,"connect",lambda *a,**k: ConnProxy(raw_connect(*a,**k)))
    result=sessions._opencode_rich_session_detail("sess-1",{}, {})
    message_q=[(q,a) for q,a in queries if "FROM session_message" in q]
    part_q=[(q,a) for q,a in queries if "FROM part " in q]
    assert len(message_q)==1 and len(part_q)==1
    assert all("WHERE session_id=?" in q for q,_ in message_q+part_q)
    assert all(args==("sess-1",) for _,args in message_q+part_q)
    # Multiple rows still require only one message query and one part query.
    assert len(result["messages"])==2
    conn=raw_connect(dbpath)
    mq="SELECT id,type,seq,time_created,data FROM session_message WHERE session_id=? AND type IN ('user','assistant','system') ORDER BY seq,id"
    pq="SELECT id,message_id,time_created,data FROM part WHERE session_id=? ORDER BY time_created,id"
    for sql in (mq,pq):
        plan=conn.execute("EXPLAIN QUERY PLAN "+sql,("sess-1",)).fetchall()
        assert any("SEARCH" in row[-1].upper() and "INDEX" in row[-1].upper() for row in plan), plan
    conn.close()


@pytest.mark.parametrize("has_parts", [True, False])
def test_part_query_skipped_for_complete_inline_and_missing_table(tmp_path, monkeypatch, has_parts):
    _,c=dbfile(tmp_path,monkeypatch,parts=has_parts)
    v2row(c,"inline","assistant",{"content":[]}); c.commit(); c.close()
    raw_connect=sessions.sqlite3.connect; queries=[]
    class Proxy:
        def __init__(self,conn): self.conn=conn
        def execute(self,sql,args=()): queries.append(sql); return self.conn.execute(sql,args)
        def cursor(self): return self.conn.cursor()
        def rollback(self): return self.conn.rollback()
        def close(self): return self.conn.close()
    monkeypatch.setattr(sessions.sqlite3,"connect",lambda *a,**k: Proxy(raw_connect(*a,**k)))
    sessions._opencode_rich_session_detail("sess-1",{}, {})
    assert not any("FROM part " in sql for sql in queries)


def test_missing_part_table_is_optional_when_recovery_needed(tmp_path, monkeypatch):
    _,c=dbfile(tmp_path,monkeypatch,parts=False)
    v2row(c,"assistant","assistant",{"modelID":"synthetic-model","tokens":{}})
    v2row(c,"user","user",{"text":17},seq=1)
    c.commit(); c.close()
    result=detail(tmp_path,monkeypatch,None)
    assert [m["id"] for m in result["messages"]]==["assistant","user"]
    assert [m["content_chars"] for m in result["messages"]]==[0,0]
    assert not any(m["has_reasoning"] or m["has_tool_calls"] for m in result["messages"])
