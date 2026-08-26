"""Tests for Cline as a session source (sessions.py).

Cline's token store is per-session .messages.json files (plus
agent_*.messages.json subagent siblings); db/sessions.db is metadata-only.
The harness must reproduce the parser's file set, per-message rule, the
source-global message-id dedup (C7), and the split-cache-write billing so
windowed session sums are bit-identical to ClineParser's entries.
"""
from __future__ import annotations

import builtins
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from test_cline_parser import _assistant, _user, _write_session

from tokdash import sessions
from tokdash.pricing import PricingDatabase
from tokdash.sessions import (
    SESSION_TOOLS,
    get_session_detail,
    get_sessions_data,
    reload_pricing_db,
)
from tokdash.sources.coding_tools import BaseParser, ClineParser, _sig_cache


def _local_ms(year, month, day, hour=12):
    local = datetime.now().astimezone().tzinfo
    return int(datetime(year, month, day, hour, 0, 0, tzinfo=local).timestamp() * 1000)


# Local-midnight-anchored instants so named date windows line up in any tz.
S1 = _local_ms(2026, 8, 20, 0)  # local midnight of T_A's day (window since)
U1 = _local_ms(2026, 8, 21, 0)  # next local midnight (window until)
T_A = _local_ms(2026, 8, 20, 12)
T_B = _local_ms(2026, 8, 5, 12)
T_C = _local_ms(2026, 6, 15, 12)
T_OLD = _local_ms(2025, 12, 1, 12)

# Split rates (cache write at its own rate) so billing drift is visible.
RATES = {
    "deepseek-chat": {"input": 2.0, "output": 4.0, "cache_read": 0.2,
                      "cache_write": 4.0, "unit": "per_million_tokens"},
    "gpt-5": {"input": 1.5, "output": 3.0, "cache_read": 0.15,
              "cache_write": 1.5, "unit": "per_million_tokens"},
    "claude-opus-4": {"input": 15.0, "output": 75.0, "cache_read": 1.5,
                      "cache_write": 18.75, "unit": "per_million_tokens"},
}

DB_SCHEMA = """
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY, source TEXT NOT NULL, pid INTEGER NOT NULL,
    started_at TEXT NOT NULL, ended_at TEXT, exit_code INTEGER,
    status TEXT NOT NULL, status_lock INTEGER NOT NULL DEFAULT 0,
    interactive INTEGER NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
    cwd TEXT NOT NULL, workspace_root TEXT NOT NULL, team_name TEXT,
    enable_tools INTEGER NOT NULL, enable_spawn INTEGER NOT NULL,
    enable_teams INTEGER NOT NULL, parent_session_id TEXT, parent_agent_id TEXT,
    agent_id TEXT, conversation_id TEXT, is_subagent INTEGER NOT NULL DEFAULT 0,
    prompt TEXT, metadata_json TEXT, transcript_path TEXT NOT NULL DEFAULT '',
    hook_path TEXT NOT NULL, messages_path TEXT, updated_at TEXT NOT NULL
)
"""


def _db_row(session_id, *, subagent=0, prompt=None, cwd="/work/x",
            workspace_root="", metadata=None, parent=None, agent_id=None):
    return (session_id, "cli", 1, "2026-01-01T00:00:00Z", None, None, "idle", 0, 1,
            "test", "test-model", cwd, workspace_root, None, 1, 1, 1, parent,
            agent_id, agent_id, None, subagent, prompt,
            json.dumps(metadata) if metadata is not None else None, "", "", None,
            "2026-01-01T00:00:00Z")


def _write_db(data_dir: Path, rows) -> Path:
    db_dir = data_dir / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    db = db_dir / "sessions.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(DB_SCHEMA)
    conn.executemany(
        "INSERT INTO sessions (session_id, source, pid, started_at, ended_at,"
        " exit_code, status, status_lock, interactive, provider, model, cwd,"
        " workspace_root, team_name, enable_tools, enable_spawn, enable_teams,"
        " parent_session_id, parent_agent_id, agent_id, conversation_id,"
        " is_subagent, prompt, metadata_json, transcript_path, hook_path,"
        " messages_path, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db


def _write_record(data_dir: Path, session_id: str, doc: dict) -> Path:
    path = data_dir / "sessions" / session_id / f"{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _build_corpus(data_dir: Path) -> None:
    # s-parent: the spec's 7049/57/6272 row, a cache-write row, an all-zero
    # metrics row (guard), an id-less row, and turns at exactly S1 (in the
    # day window) and U1 (excluded).
    _write_session(data_dir, "s-parent", [
        _user("msg_u1", S1 - 60_000),
        _assistant("msg_p1", T_A, model="deepseek-chat",
                   input_tokens=7049, output_tokens=57, cache_read=6272),
        _assistant("msg_p2", T_B, model="gpt-5",
                   input_tokens=1000, output_tokens=100,
                   cache_read=200, cache_write=300),
        _assistant("msg_zero", T_A + 1000, model="gpt-5"),
        {"role": "assistant", "ts": T_C,
         "metrics": {"inputTokens": 10, "outputTokens": 5,
                     "cacheReadTokens": 0, "cacheWriteTokens": 3},
         "modelInfo": {"id": "gpt-5", "provider": "test"}},
        _assistant("msg_edge_in", S1, model="gpt-5", input_tokens=5, output_tokens=1),
        _assistant("msg_edge_out", U1, model="gpt-5", input_tokens=6, output_tokens=1),
    ])
    _write_record(data_dir, "s-parent",
                  {"version": 1, "session_id": "s-parent",
                   "cwd": "/work/record-proj", "metadata": {"title": "Record title"}})

    # s-subagent: lead turn + subagent sibling file.
    _write_session(data_dir, "s-subagent", [
        _assistant("msg_s1", T_B + 5000, model="claude-opus-4",
                   input_tokens=500, output_tokens=50),
    ])
    _write_session(data_dir, "s-subagent", [
        _assistant("msg_a1", T_B + 6000, model="deepseek-chat",
                   input_tokens=400, output_tokens=40, cache_read=50),
    ], agent_id="agent_alpha")

    # s-orig / s-fork: fork replay (C7). msg_f1 is copied into the fork with
    # a LATER ts, so the original owns it; the equal-ts pair msg_t1 goes to
    # the lexicographically earlier path (s-tie-a).
    _write_session(data_dir, "s-orig", [
        _assistant("msg_f1", T_B, model="gpt-5", input_tokens=100, output_tokens=10),
        _assistant("msg_f2", T_B + 9000, model="gpt-5", input_tokens=20, output_tokens=2),
    ])
    _write_session(data_dir, "s-fork", [
        _assistant("msg_f1", T_B + 1000, model="gpt-5", input_tokens=100, output_tokens=10),
        _assistant("msg_f3", T_B + 8000, model="gpt-5", input_tokens=30, output_tokens=3),
    ])
    _write_record(data_dir, "s-fork",
                  {"version": 1, "session_id": "s-fork",
                   "cwd": "/work/fork-proj", "metadata": {"title": "Fork record"}})
    _write_record(data_dir, "s-orig",
                  {"version": 1, "session_id": "s-orig",
                   "cwd": "/work/orig-proj", "metadata": {}})

    _write_session(data_dir, "s-tie-a",
                   [_assistant("msg_t1", T_C + 1000, model="gpt-5",
                               input_tokens=4, output_tokens=1)])
    _write_session(data_dir, "s-tie-b",
                   [_assistant("msg_t1", T_C + 1000, model="gpt-5",
                               input_tokens=4, output_tokens=1)])

    # s-cold: outside every named window except all.
    _write_session(data_dir, "s-cold", [
        _assistant("msg_c1", T_OLD, model="gpt-5", input_tokens=50, output_tokens=5),
    ])

    # s-corrupt: unparseable file, no surviving turns.
    corrupt = data_dir / "sessions" / "s-corrupt"
    corrupt.mkdir(parents=True)
    (corrupt / "s-corrupt.messages.json").write_text("{not json", encoding="utf-8")

    _write_db(data_dir, [
        _db_row("s-parent",
                prompt='<user_input mode="act">Fix the login flow</user_input>',
                cwd="/work/db-proj", workspace_root="/work/db-proj",
                metadata={"title": "DB title", "usage": {"totalCost": 0}}),
        _db_row("s-subagent",
                prompt='<user_input mode="plan">Refactor the auth module</user_input>',
                cwd="/work/sub-proj", workspace_root="",
                metadata={"sessionHistoryOrigin": "fork"}),
        # Subagent row: must never be used for metadata or as a session.
        _db_row("s-subagent__agent_alpha", subagent=1, parent="s-subagent",
                agent_id="alpha", cwd="/work/sub-proj",
                metadata={"title": "must not be used",
                          "sessionHistoryOrigin": "spawn"}),
    ])


def _setup(monkeypatch, tmp_path) -> Path:
    data_dir = tmp_path / "cline"
    data_dir.mkdir(parents=True)
    monkeypatch.setenv("CLINE_DATA_DIR", str(data_dir))
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path / "data-home"))
    override = PricingDatabase().override_path()
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(
        json.dumps({"version": "test", "aliases": {}, "models": RATES}),
        encoding="utf-8",
    )
    reload_pricing_db()
    _build_corpus(data_dir)
    return data_dir


@pytest.fixture(autouse=True)
def _clean_caches():
    sessions._load_cline_sessions.cache_clear()
    sessions._parse_cline_message_file.cache_clear()
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    reload_pricing_db()
    yield
    sessions._load_cline_sessions.cache_clear()
    sessions._parse_cline_message_file.cache_clear()
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    reload_pricing_db()


def test_registered():
    assert "cline" in SESSION_TOOLS
    assert sessions.TOOL_LABELS["cline"] == "Cline"
    with pytest.raises(ValueError):
        get_sessions_data("not_a_tool", "all")


def test_empty_dir_no_error(monkeypatch, tmp_path):
    data_dir = tmp_path / "cline-empty"
    (data_dir / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CLINE_DATA_DIR", str(data_dir))
    result = get_sessions_data("cline", "all")
    assert result["sessions"] == []
    assert result["summary"]["session_count"] == 0
    assert result["tool_label"] == "Cline"


def test_record_file_edit_invalidates_sessions(monkeypatch, tmp_path):
    """Record files (<dir>/<dir>.json) must ride in the aggregate key:
    a title edit there is picked up even when no message file and no
    sessions.db changed."""
    data_dir = _setup(monkeypatch, tmp_path)
    raw1 = sessions._cline_sessions()
    # s-fork has no sessions.db row, so its metadata comes from the record.
    assert raw1["s-fork"]["display_name"] == "Fork record"

    record = data_dir / "sessions" / "s-fork" / "s-fork.json"
    record.write_text(
        json.dumps({"version": 1, "session_id": "s-fork", "cwd": "/work/fork-proj",
                    "metadata": {"title": "Fork record v2 (changed)"}}),
        encoding="utf-8",
    )
    st = record.stat()
    os.utime(record, ns=(st.st_mtime_ns + 10_000_000, st.st_mtime_ns + 10_000_000))

    raw2 = sessions._cline_sessions()
    assert raw2["s-fork"]["display_name"] == "Fork record v2 (changed)"
    assert set(raw2) == set(raw1)


def test_transient_file_lock_is_retried_not_cached(monkeypatch, tmp_path):
    """A one-off open failure (lock/AV/indexer) must not be cached: the
    first view is partial (the locked session missing), and the next
    request — same file signature — must recover it. A cached empty
    parse or a cached partial aggregate would hide the session until
    process restart or file modification."""
    data_dir = _setup(monkeypatch, tmp_path)
    locked = str(data_dir / "sessions" / "s-cold" / "s-cold.messages.json")
    state = {"left": 1}
    real_open = builtins.open

    def flaky_open(path, *args, **kwargs):
        if str(path) == locked and state["left"]:
            state["left"] -= 1
            raise PermissionError(13, "simulated file lock", path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", flaky_open)
    raw1 = sessions._cline_sessions()
    # The locked session is missing; the rest of the view is intact.
    assert "s-cold" not in raw1
    assert "s-parent" in raw1
    # The lock clears (state consumed); the file signature is unchanged.
    raw2 = sessions._cline_sessions()
    assert "s-cold" in raw2
    assert raw2["s-cold"]["turns"][0]["tokens_in"] == 50


def test_turn_mapping(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    raw = sessions._cline_sessions()
    assert raw["s-parent"]["tool"] == "cline"
    assert raw["s-parent"]["session_id"] == "s-parent"
    turns = {t["_event_key"]: t for t in raw["s-parent"]["turns"]
             if t.get("_event_key")}
    t = turns["cline:msg_p1"]
    assert t["model"] == "deepseek-chat"
    assert t["timestamp_ms"] == T_A
    # Cache-inclusive split: 7049 with 6272 cached -> 777 fresh + 6272 cached.
    assert t["tokens_in"] == 777
    assert t["tokens_cache"] == 6272
    assert t["tokens_out"] == 57
    assert t["tokens_reasoning"] == 0
    assert t["tokens"] == 7106
    assert t["cost"] == pytest.approx(
        PricingDatabase().get_cost("deepseek-chat", 777, 57, 6272, 0))
    # Hand-computed: (777*2 + 57*4 + 6272*0.2) / 1e6.
    assert t["cost"] == pytest.approx(0.0030364)
    bill = t["_bill"]
    assert bill["rule"] == "split-cache-write"
    assert bill["model"] == "deepseek-chat"
    assert bill["input"] == 777 and bill["cache_read"] == 6272
    # Cache-write row: folded into tokens_in, billed at its own rate.
    t2 = turns["cline:msg_p2"]
    assert t2["tokens_in"] == 800  # 1000 - 200 - 300 + 300
    assert t2["tokens_cache"] == 200
    # The all-zero metrics row is dropped by the parser guard.
    all_keys = {t2.get("_event_key") for s in raw.values() for t2 in s["turns"]}
    assert "cline:msg_zero" not in all_keys
    # The id-less row: present, no event key, cacheWrite folded.
    anon = [t2 for s in raw.values() for t2 in s["turns"] if not t2.get("_event_key")]
    assert len(anon) == 1
    # tokens_in = raw input - cache_read (cacheWrite is folded back in).
    assert anon[0]["tokens_in"] == 10 and anon[0]["tokens_out"] == 5


def test_identity_names_projects(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    raw = sessions._cline_sessions()
    # DB row wins: metadata title, workspace_root.
    assert raw["s-parent"]["display_name"] == "DB title"
    assert raw["s-parent"]["project"] == "db-proj"
    # No title in metadata -> prompt with the wrapper stripped; no
    # workspace_root -> cwd.
    assert raw["s-subagent"]["display_name"] == "Refactor the auth module"
    assert raw["s-subagent"]["project"] == "sub-proj"
    # No DB row -> record file (title, cwd).
    assert raw["s-fork"]["display_name"] == "Fork record"
    assert raw["s-fork"]["project"] == "fork-proj"
    # Record file without a title: project from its cwd, fallback name.
    assert raw["s-orig"]["project"] == "orig-proj"
    assert "display_name" not in raw["s-orig"]
    # Nothing at all: unknown project, fallback name.
    assert raw["s-cold"]["project"] == "unknown"
    assert "display_name" not in raw["s-cold"]
    listing = get_sessions_data("cline", "all")["sessions"]
    assert {s["session_id"] for s in listing} == {
        "s-parent", "s-subagent", "s-orig", "s-fork", "s-tie-a", "s-cold",
    }
    by_id = {s["session_id"]: s for s in listing}
    assert by_id["s-orig"]["display_name"]  # fallback name, non-empty
    assert by_id["s-cold"]["display_name"]


def test_record_project_replaces_unknown_db_project(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    original_metadata_map = sessions._cline_metadata_map

    def metadata_with_unknown_project(db_sig, dir_ids):
        result = original_metadata_map(db_sig, dir_ids)
        result["s-fork"] = {"title": "DB title", "project": "unknown"}
        return result

    monkeypatch.setattr(sessions, "_cline_metadata_map", metadata_with_unknown_project)
    raw = sessions._cline_sessions()
    assert raw["s-fork"]["display_name"] == "DB title"
    assert raw["s-fork"]["project"] == "fork-proj"


def test_subagent_rollup(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    raw = sessions._cline_sessions()
    # The derived subagent id is never a session id.
    assert "s-subagent__agent_alpha" not in raw
    turns = raw["s-subagent"]["turns"]
    assert len(turns) == 2
    sub = [t for t in turns if t.get("_stream_id")]
    assert len(sub) == 1
    assert sub[0]["_stream_id"] == "agent_alpha"
    assert sub[0]["tokens_in"] == 350 and sub[0]["tokens_cache"] == 50
    lead = [t for t in turns if not t.get("_stream_id")]
    assert lead[0]["_event_key"] == "cline:msg_s1"
    listing = get_sessions_data("cline", "all")
    assert all(s["session_id"] != "s-subagent__agent_alpha"
               for s in listing["sessions"])


def test_fork_dedup(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    raw = sessions._cline_sessions()
    # msg_f1 counted exactly once, in the original (earlier ts).
    f1 = [(sid, t) for sid, s in raw.items() for t in s["turns"]
          if t.get("_event_key") == "cline:msg_f1"]
    assert len(f1) == 1
    assert f1[0][0] == "s-orig"
    # The fork shows only its own post-fork turn.
    assert {t["_event_key"] for t in raw["s-fork"]["turns"]} == {"cline:msg_f3"}
    # Equal-ts tie -> lexicographically earlier path wins; the loser has
    # no surviving turns and is not listed.
    t1 = [(sid, t) for sid, s in raw.items() for t in s["turns"]
          if t.get("_event_key") == "cline:msg_t1"]
    assert len(t1) == 1
    assert t1[0][0] == "s-tie-a"
    assert "s-tie-b" not in raw


def test_parity_with_usage_parser(monkeypatch, tmp_path):
    """The reconciliation gate: per-window session sums equal
    ClineParser's for every named period, and the harness event keys equal
    the parser entry ids."""
    _setup(monkeypatch, tmp_path)
    parser = ClineParser(PricingDatabase())
    all_entries = parser.collect(None, None)

    windows = [
        ("all", None, None),
        ("7d", None, None),
        ("30d", None, None),
        ("90d", None, None),
        ("month", None, None),
        ("all", "2026-08-20", "2026-08-20"),
    ]
    for period, date_from, date_to in windows:
        since_ms, until_ms = sessions._window_bounds(period, date_from, date_to)
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        s_dt = (epoch + timedelta(milliseconds=since_ms)
                if since_ms is not None else None)
        u_dt = (epoch + timedelta(milliseconds=until_ms)
                if until_ms is not None else None)
        entries = parser.collect(s_dt, u_dt)
        p_in = sum(e["input"] + e["cacheWrite"] for e in entries)
        p_cache = sum(e["cacheRead"] for e in entries)
        p_out = sum(e["output"] for e in entries)
        p_cost = sum(e["cost"] for e in entries)

        listing = get_sessions_data("cline", period,
                                    date_from=date_from, date_to=date_to)
        rows = listing["sessions"]
        assert sum(r["tokens_in"] for r in rows) == p_in, (period, date_from, date_to)
        assert sum(r["tokens_cache"] for r in rows) == p_cache, (period, date_from, date_to)
        assert sum(r["tokens_out"] for r in rows) == p_out, (period, date_from, date_to)
        assert listing["summary"]["tokens"] == p_in + p_cache + p_out
        assert listing["summary"]["cost"] == pytest.approx(p_cost, abs=1e-12)

    raw = sessions._cline_sessions()
    assert {t.get("_event_key") for s in raw.values() for t in s["turns"]
            if t.get("_event_key")} == {e["entry_id"] for e in all_entries
                                        if e["entry_id"]}


def test_window_half_open(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    day = datetime.fromtimestamp(S1 / 1000).strftime("%Y-%m-%d")
    listing = get_sessions_data("cline", "all",
                                date_from=day, date_to=day)["sessions"]
    assert [s["session_id"] for s in listing] == ["s-parent"]
    # since_ms included (turn at exactly S1), until_ms excluded (turn at
    # exactly U1).
    assert listing[0]["token_events"] == 2
    assert listing[0]["tokens"] == (5 + 1) + (777 + 6272 + 57)
    raw = sessions._cline_sessions()
    in_window = {t.get("_event_key") for t in raw["s-parent"]["turns"]
                 if S1 <= t["timestamp_ms"] < U1}
    assert in_window == {"cline:msg_edge_in", "cline:msg_p1"}
    assert any(t.get("_event_key") == "cline:msg_edge_out"
               for t in raw["s-parent"]["turns"])


def test_failure_isolation(monkeypatch, tmp_path):
    data_dir = _setup(monkeypatch, tmp_path)
    raw = sessions._cline_sessions()
    expected = {"s-parent", "s-subagent", "s-orig", "s-fork", "s-tie-a", "s-cold"}
    # Corrupt message file: its session is absent, everything else intact.
    assert "s-corrupt" not in raw
    assert set(raw) == expected
    assert sum(len(s["turns"]) for s in raw.values()) == 12

    # Corrupt sessions.db: turns intact, names fall back to the record file.
    db = data_dir / "db" / "sessions.db"
    db.write_bytes(b"definitely not sqlite")
    raw = sessions._cline_sessions()
    assert set(raw) == expected
    assert raw["s-parent"]["display_name"] == "Record title"
    assert raw["s-parent"]["project"] == "record-proj"
    assert raw["s-parent"]["turns"]

    # Missing DB: same fallback path.
    db.unlink()
    raw = sessions._cline_sessions()
    assert set(raw) == expected
    assert "display_name" not in raw["s-subagent"]  # no record file
    assert raw["s-subagent"]["project"] == "unknown"
    listing = get_sessions_data("cline", "all")["sessions"]
    by_id = {s["session_id"]: s for s in listing}
    assert by_id["s-subagent"]["display_name"]  # fallback name, non-empty
    assert sum(len(s["turns"]) for s in raw.values()) == 12


def test_session_detail(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    detail = get_session_detail("cline", "s-parent")
    assert detail["session"]["session_id"] == "s-parent"
    turns = detail["turns"]
    assert len(turns) == 5
    assert [t["turn_index"] for t in turns] == [1, 2, 3, 4, 5]
    for t in turns:
        assert "_bill" not in t and "_event_key" not in t and "_stream_id" not in t
        assert "timestamp" in t and "timestamp_ms" not in t
    # ts-sorted: anon (T_C), msg_p2 (T_B), msg_edge_in (S1), msg_p1 (T_A),
    # msg_edge_out (U1).
    assert [t["tokens_out"] for t in turns] == [5, 100, 1, 57, 1]
    assert turns[3]["tokens_in"] == 777
    with pytest.raises(FileNotFoundError):
        get_session_detail("cline", "nope")


def test_cache_invalidation(monkeypatch, tmp_path):
    data_dir = _setup(monkeypatch, tmp_path)

    sessions._cline_sessions()
    info1 = sessions._parse_cline_message_file.cache_info()
    assert info1.misses > 0
    sessions._cline_sessions()
    info2 = sessions._parse_cline_message_file.cache_info()
    assert info2.misses == info1.misses  # aggregate hit: nothing re-read

    # Rewrite one file: only that file is re-read.
    fork_file = data_dir / "sessions" / "s-fork" / "s-fork.messages.json"
    doc = json.loads(fork_file.read_text(encoding="utf-8"))
    doc["messages"].append(
        _assistant("msg_f4", T_B + 9500, model="gpt-5",
                   input_tokens=40, output_tokens=4))
    fork_file.write_text(json.dumps(doc), encoding="utf-8")
    raw = sessions._cline_sessions()
    info3 = sessions._parse_cline_message_file.cache_info()
    assert info3.misses == info2.misses + 1
    assert "cline:msg_f4" in {t["_event_key"] for t in raw["s-fork"]["turns"]}

    # A DB change refreshes metadata without re-reading any message file.
    db = data_dir / "db" / "sessions.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO sessions (session_id, source, pid, started_at, ended_at,"
        " exit_code, status, status_lock, interactive, provider, model, cwd,"
        " workspace_root, team_name, enable_tools, enable_spawn, enable_teams,"
        " parent_session_id, parent_agent_id, agent_id, conversation_id,"
        " is_subagent, prompt, metadata_json, transcript_path, hook_path,"
        " messages_path, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        _db_row("s-cold", cwd="/work/cold-proj", workspace_root="/work/cold-proj",
                prompt="Cold prompt", metadata={"title": "Cold title"}),
    )
    conn.commit()
    conn.close()
    raw = sessions._cline_sessions()
    info4 = sessions._parse_cline_message_file.cache_info()
    assert info4.misses == info3.misses  # no message file re-read
    assert raw["s-cold"]["display_name"] == "Cold title"
    assert raw["s-cold"]["project"] == "cold-proj"


def test_frontend_session_registry_includes_cline():
    index = Path(sessions.__file__).parent / "static" / "index.html"
    source = index.read_text(encoding="utf-8")
    assert "'antigravity_cli', 'cline'" in source
    assert "antigravity_cli: null, cline: null, combined: null" in source
    assert 'updateSessionPanel("cline", lastSessionsResponses.cline);' in source
    assert 'initSortHeaders("cline", renderSessionsTab);' in source
    assert "cline: { ...DEFAULT_SORT }," in source
    assert "clineSessions: 'Cline Sessions'," in source
    assert "clineSessions: 'Cline \u4f1a\u8bdd'," in source
    assert 'id="clineSessionsTable"' in source
    brand = source.split("const TOOL_BRAND_META = Object.freeze({", 1)[1].split("});", 1)[0]
    assert "cline:" in brand
    assert (index.parent / "icons" / "agents" / "cline.png").is_file()
