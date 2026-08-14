"""Tests for Kimi as a session source in sessions.py."""
import json
from datetime import datetime, timezone

import pytest

from tokdash import sessions
from tokdash.sessions import SESSION_TOOLS, get_sessions_data, reload_pricing_db

# Fixed past instants so ``period="all"`` always covers them.
CODE_TS_MS = int(datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
LEGACY_TS_S = datetime(2026, 6, 2, 9, 30, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def _isolated_kimi_roots(monkeypatch, tmp_path):
    """Point both Kimi roots at tmp dirs and drop parsed-session caches."""
    code_root = tmp_path / "kimi-code"
    legacy_root = tmp_path / "kimi"
    (code_root / "sessions").mkdir(parents=True)
    (legacy_root / "sessions").mkdir(parents=True)
    monkeypatch.setenv("KIMI_CODE_HOME", str(code_root))
    monkeypatch.setenv("KIMI_SHARE_DIR", str(legacy_root))
    reload_pricing_db()
    yield code_root, legacy_root
    reload_pricing_db()


def _usage_record(input_other, output, cache_read, cache_creation, *, scope="turn", ts_ms=CODE_TS_MS, model="kimi-code/k3"):
    return {
        "type": "usage.record",
        "model": model,
        "usage": {
            "inputOther": input_other,
            "output": output,
            "inputCacheRead": cache_read,
            "inputCacheCreation": cache_creation,
        },
        "usageScope": scope,
        "time": ts_ms,
    }


def _status_update(input_other, output, cache_read, cache_creation, message_id, ts_s=LEGACY_TS_S):
    return {
        "timestamp": ts_s,
        "message": {
            "type": "StatusUpdate",
            "payload": {
                "token_usage": {
                    "input_other": input_other,
                    "output": output,
                    "input_cache_read": cache_read,
                    "input_cache_creation": cache_creation,
                },
                "message_id": message_id,
            },
        },
    }


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _write_code_session(code_root, rows, *, workspace="wd_myproj_0123456789ab", session="session_aaa", agent="main"):
    path = code_root / "sessions" / workspace / session / "agents" / agent / "wire.jsonl"
    _write_jsonl(path, rows)
    return path


def _write_legacy_session(legacy_root, rows, *, user="user1", session="sess-legacy"):
    path = legacy_root / "sessions" / user / session / "wire.jsonl"
    _write_jsonl(path, rows)
    return path


def test_kimi_is_a_session_tool():
    assert "kimi" in SESSION_TOOLS
    assert sessions.TOOL_LABELS["kimi"] == "Kimi"


def test_unsupported_tool_rejection_no_longer_applies_to_kimi():
    # Previously "kimi" raised; only genuinely unknown tools should now.
    get_sessions_data("kimi", "all")
    with pytest.raises(ValueError):
        get_sessions_data("not_a_tool", "all")


def test_kimi_code_usage_records(_isolated_kimi_roots):
    """usage.record rows parse, fold cache-creation into input, and keep session scope."""
    code_root, _ = _isolated_kimi_roots
    _write_code_session(
        code_root,
        [
            {"type": "metadata", "protocol_version": "1.4", "created_at": CODE_TS_MS},
            {"type": "config.update", "cwd": "/home/user/myproj", "modelAlias": "kimi-code/k3", "time": CODE_TS_MS},
            {"type": "turn.prompt", "input": [{"type": "text", "text": "Add Kimi sessions"}]},
            _usage_record(1000, 200, 5000, 100),
            # Compaction rows carry "session" scope and are real usage.
            _usage_record(50, 10, 0, 0, scope="session", ts_ms=CODE_TS_MS + 1000),
        ],
    )

    raw = sessions._kimi_sessions()
    assert list(raw) == ["session_aaa"]
    session = raw["session_aaa"]
    assert session["tool"] == "kimi"
    assert session["project"] == "myproj"
    assert session["display_name"] == "Add Kimi sessions"

    turns = session["turns"]
    assert len(turns) == 2
    first, second = turns
    assert (first["tokens_in"], first["tokens_cache"], first["tokens_out"], first["tokens_reasoning"]) == (1100, 5000, 200, 0)
    assert first["model"] == "kimi-k3"
    assert (second["tokens_in"], second["tokens_cache"], second["tokens_out"]) == (50, 0, 10)
    # Cost folds cache-creation into billable input and passes no cache-write.
    assert first["cost"] == pytest.approx(sessions._PRICING_DB.get_cost("kimi-k3", 1100, 200, 5000, 0))


def test_kimi_code_duplicate_row_is_deduped(_isolated_kimi_roots):
    code_root, _ = _isolated_kimi_roots
    row = _usage_record(1000, 200, 5000, 100)
    _write_code_session(code_root, [row, dict(row), _usage_record(7, 3, 0, 0, ts_ms=CODE_TS_MS + 1)])

    turns = sessions._kimi_sessions()["session_aaa"]["turns"]
    assert len(turns) == 2
    assert sum(turn["tokens"] for turn in turns) == 1100 + 5000 + 200 + 10


def test_kimi_code_zero_token_rows_are_skipped(_isolated_kimi_roots):
    code_root, _ = _isolated_kimi_roots
    _write_code_session(code_root, [_usage_record(0, 0, 0, 0), _usage_record(0, 0, 0, 0, ts_ms=CODE_TS_MS + 5)])
    assert sessions._kimi_sessions() == {}


def test_kimi_code_agents_merge_into_one_session(_isolated_kimi_roots):
    """Sibling agents/*/wire.jsonl files collapse into a single session."""
    code_root, _ = _isolated_kimi_roots
    _write_code_session(
        code_root,
        [
            {"type": "config.update", "cwd": "/home/user/myproj", "time": CODE_TS_MS},
            {"type": "turn.prompt", "input": [{"type": "text", "text": "Add Kimi sessions"}]},
            _usage_record(1000, 200, 5000, 100),
        ],
        agent="main",
    )
    _write_code_session(
        code_root,
        [_usage_record(300, 30, 1000, 0, ts_ms=CODE_TS_MS + 2000)],
        agent="agent-0",
    )

    raw = sessions._kimi_sessions()
    assert list(raw) == ["session_aaa"]
    session = raw["session_aaa"]
    assert len(session["turns"]) == 2
    assert [turn["turn_index"] for turn in session["turns"]] == [1, 2]
    # The cwd-bearing main file wins the project over the bare agent-0 file.
    assert session["project"] == "myproj"
    assert sum(turn["tokens"] for turn in session["turns"]) == (1100 + 5000 + 200) + (300 + 1000 + 30)


def test_kimi_code_project_falls_back_to_workspace_slug(_isolated_kimi_roots):
    """No cwd row: the wd_<slug>_<hash> workspace dir supplies the project."""
    code_root, _ = _isolated_kimi_roots
    _write_code_session(code_root, [_usage_record(10, 5, 0, 0)], workspace="wd_some_tool_abcdef012345")
    assert sessions._kimi_sessions()["session_aaa"]["project"] == "some_tool"


def test_legacy_status_updates(_isolated_kimi_roots):
    """Legacy StatusUpdate rows parse and dedup on message_id."""
    _, legacy_root = _isolated_kimi_roots
    _write_legacy_session(
        legacy_root,
        [
            {"type": "metadata", "protocol_version": "1.3"},
            {"timestamp": LEGACY_TS_S, "message": {"type": "TurnBegin", "payload": {"user_input": [{"type": "text", "text": "Show kimi usage"}]}}},
            _status_update(5555, 128, 5376, 64, "chatcmpl-a"),
            _status_update(5555, 128, 5376, 64, "chatcmpl-a"),
            _status_update(100, 20, 0, 0, "chatcmpl-b", ts_s=LEGACY_TS_S + 5),
        ],
    )

    raw = sessions._kimi_sessions()
    assert list(raw) == ["sess-legacy"]
    session = raw["sess-legacy"]
    assert session["display_name"] == "Show kimi usage"
    # No cwd anywhere in the legacy schema.
    assert session["project"] == "unknown"

    turns = session["turns"]
    assert len(turns) == 2
    assert (turns[0]["tokens_in"], turns[0]["tokens_cache"], turns[0]["tokens_out"]) == (5619, 5376, 128)
    # The legacy schema carries no model, so the time-window default is used.
    assert turns[0]["model"] == "kimi-k2.5"
    assert turns[0]["timestamp_ms"] == int(LEGACY_TS_S * 1000)


def test_both_layouts_are_scanned(_isolated_kimi_roots):
    code_root, legacy_root = _isolated_kimi_roots
    _write_code_session(code_root, [_usage_record(10, 5, 0, 0)])
    _write_legacy_session(legacy_root, [_status_update(20, 7, 0, 0, "chatcmpl-a")])
    assert set(sessions._kimi_sessions()) == {"session_aaa", "sess-legacy"}


def test_get_sessions_data_totals(_isolated_kimi_roots):
    code_root, _ = _isolated_kimi_roots
    _write_code_session(
        code_root,
        [
            {"type": "config.update", "cwd": "/home/user/myproj", "time": CODE_TS_MS},
            {"type": "turn.prompt", "input": [{"type": "text", "text": "Add Kimi sessions"}]},
            _usage_record(1000, 200, 5000, 100),
            _usage_record(50, 10, 0, 0, scope="session", ts_ms=CODE_TS_MS + 1000),
        ],
    )

    data = get_sessions_data("kimi", "all")
    assert data["tool"] == "kimi"
    assert len(data["sessions"]) == 1
    row = data["sessions"][0]
    assert row["session_id"] == "session_aaa"
    assert row["project"] == "myproj"
    assert row["token_events"] == 2
    assert row["tokens_in"] == 1150
    assert row["tokens_cache"] == 5000
    assert row["tokens_out"] == 210
    assert row["tokens"] == 6360
    expected_cost = sessions._PRICING_DB.get_cost("kimi-k3", 1100, 200, 5000, 0) + sessions._PRICING_DB.get_cost(
        "kimi-k3", 50, 10, 0, 0
    )
    assert row["cost"] == pytest.approx(expected_cost)


def test_missing_roots_are_tolerated(monkeypatch, tmp_path):
    monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path / "absent-code"))
    monkeypatch.setenv("KIMI_SHARE_DIR", str(tmp_path / "absent-legacy"))
    reload_pricing_db()
    assert sessions._kimi_session_signatures() == ()
    assert sessions._kimi_sessions() == {}
    assert get_sessions_data("kimi", "all")["sessions"] == []


def _write_workspaces_json(code_root, workspaces):
    (code_root / "workspaces.json").write_text(
        json.dumps({"version": 1, "workspaces": workspaces}), encoding="utf-8"
    )


def test_workspaces_json_gives_one_project_name_per_workspace(_isolated_kimi_roots):
    """Sessions in one workspace agree even when only some recorded a cwd."""
    code_root, _ = _isolated_kimi_roots
    workspace = "wd_tokdash_project_4ac9ada67742"
    _write_code_session(
        code_root,
        [
            {"type": "config.update", "cwd": "/mnt/h/Developing/Agent/Tokdash_Project", "time": CODE_TS_MS},
            _usage_record(100, 20, 0, 0),
        ],
        workspace=workspace,
        session="session_with_cwd",
    )
    _write_code_session(
        code_root,
        [_usage_record(100, 20, 0, 0, ts_ms=CODE_TS_MS + 1000)],
        workspace=workspace,
        session="session_without_cwd",
    )
    _write_workspaces_json(
        code_root,
        {workspace: {"root": "/mnt/h/Developing/Agent/Tokdash_Project", "name": "Tokdash_Project"}},
    )

    raw = sessions._kimi_sessions()
    assert {session["project"] for session in raw.values()} == {"Tokdash_Project"}


def test_workspaces_json_falls_back_to_the_slug_when_absent(_isolated_kimi_roots):
    code_root, _ = _isolated_kimi_roots
    _write_code_session(
        code_root,
        [_usage_record(100, 20, 0, 0)],
        workspace="wd_myproj_0123456789ab",
        session="session_bare",
    )

    assert sessions._kimi_sessions()["session_bare"]["project"] == "myproj"


def test_workspaces_json_ignores_unknown_and_malformed_entries(_isolated_kimi_roots):
    code_root, _ = _isolated_kimi_roots
    _write_code_session(
        code_root,
        [_usage_record(100, 20, 0, 0)],
        workspace="wd_myproj_0123456789ab",
        session="session_bare",
    )
    _write_workspaces_json(
        code_root,
        {"wd_other_ffffffffffff": {"root": "/tmp/other"}, "wd_myproj_0123456789ab": "not-a-dict"},
    )

    assert sessions._kimi_sessions()["session_bare"]["project"] == "myproj"


def test_workspace_project_rename_updates_a_stand_in_display_name(_isolated_kimi_roots):
    """A display name that was only the old project label follows the correction."""
    code_root, _ = _isolated_kimi_roots
    workspace = "wd_tokdash_project_4ac9ada67742"
    _write_code_session(
        code_root,
        [_usage_record(100, 20, 0, 0)],
        workspace=workspace,
        session="session_noprompt",
    )
    _write_workspaces_json(
        code_root, {workspace: {"root": "/mnt/h/Developing/Agent/Tokdash_Project"}}
    )

    session = sessions._kimi_sessions()["session_noprompt"]
    assert session["project"] == "Tokdash_Project"
    assert session["display_name"] == "Tokdash_Project"


def _totals(data):
    return data["summary"]["session_count"], data["summary"]["tokens"], data["summary"]["cost"]


def test_stored_and_live_paths_agree(monkeypatch, _isolated_kimi_roots):
    """The persistent cache is a parse cache: it must not change what is served."""
    code_root, legacy_root = _isolated_kimi_roots
    _write_code_session(
        code_root,
        [
            {"type": "config.update", "cwd": "/home/user/myproj", "time": CODE_TS_MS},
            {"type": "turn.prompt", "input": [{"type": "text", "text": "main agent prompt"}]},
            _usage_record(1000, 200, 5000, 100),
        ],
        agent="main",
    )
    _write_code_session(
        code_root,
        [
            {"type": "turn.prompt", "input": [{"type": "text", "text": "subagent preamble"}]},
            _usage_record(7, 3, 0, 0, ts_ms=CODE_TS_MS + 5000),
        ],
        agent="agent-0",
    )
    _write_legacy_session(legacy_root, [_status_update(10, 5, 0, 0, "m1")])

    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")
    reload_pricing_db()
    live = get_sessions_data("kimi", "all")

    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")
    reload_pricing_db()
    cached = get_sessions_data("kimi", "all")
    warm = get_sessions_data("kimi", "all")

    for payload in (live, cached, warm):
        payload.pop("timestamp")
    assert cached == live
    assert warm == live
    # The main agent's prompt names the session, not the subagent's preamble.
    by_id = {row["session_id"]: row for row in live["sessions"]}
    assert by_id["session_aaa"]["display_name"] == "main agent prompt"


def test_store_reparses_an_agent_file_that_grew(_isolated_kimi_roots):
    """Appending to one agent file must not drop the sibling agent's turns."""
    code_root, _ = _isolated_kimi_roots
    _write_code_session(code_root, [_usage_record(1000, 200, 0, 0)], agent="main")
    path = _write_code_session(
        code_root, [_usage_record(10, 5, 0, 0, ts_ms=CODE_TS_MS + 1000)], agent="agent-0"
    )

    before = _totals(get_sessions_data("kimi", "all"))
    assert before[0] == 1 and before[1] == 1215

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_usage_record(70, 30, 0, 0, ts_ms=CODE_TS_MS + 2000)) + "\n")
    reload_pricing_db()

    after = _totals(get_sessions_data("kimi", "all"))
    assert after[0] == 1
    assert after[1] == 1215 + 100


def test_store_forgets_a_deleted_file_when_not_durable(monkeypatch, _isolated_kimi_roots):
    code_root, _ = _isolated_kimi_roots
    monkeypatch.setenv("TOKDASH_USAGE_DB_DURABLE", "0")
    kept = _write_code_session(code_root, [_usage_record(1000, 200, 0, 0)], session="session_keep")
    dropped = _write_code_session(code_root, [_usage_record(500, 100, 0, 0)], session="session_drop")

    assert _totals(get_sessions_data("kimi", "all"))[0] == 2

    dropped.unlink()
    reload_pricing_db()

    data = get_sessions_data("kimi", "all")
    assert [row["session_id"] for row in data["sessions"]] == ["session_keep"]
    assert kept.exists()


def test_store_rows_are_rebuilt_when_the_parser_version_changes(monkeypatch, _isolated_kimi_roots):
    """A parser bump must invalidate cached rows rather than serve stale output."""
    code_root, _ = _isolated_kimi_roots
    _write_code_session(code_root, [_usage_record(1000, 200, 0, 0)])
    assert _totals(get_sessions_data("kimi", "all"))[1] == 1200

    calls = []
    original = sessions._parse_kimi_session_file

    def counting_parse(path_str, mtime_ns, size, pricing_sig=()):
        calls.append(path_str)
        return original(path_str, mtime_ns, size, pricing_sig)

    # reload_pricing_db() clears this parser's lru_cache; the stub needs the hook.
    counting_parse.cache_clear = original.cache_clear
    monkeypatch.setattr(sessions, "_parse_kimi_session_file", counting_parse)
    monkeypatch.setitem(sessions._SESSION_FILE_PARSER_VERSIONS, "_parse_kimi_session_file", 99)
    reload_pricing_db()

    assert _totals(get_sessions_data("kimi", "all"))[1] == 1200
    assert calls, "a parser version bump must force a reparse"


def test_kimi_parser_signature_has_no_v159_compat_token():
    """Only the two original parsers carry the frozen v1.5.9 token."""
    signature = sessions._session_file_parser_signature("_parse_kimi_session_file")
    assert signature == {
        "object": f"{sessions.__name__}._parse_kimi_session_file",
        # Whatever the current version is; bumping it must invalidate cached rows.
        "version": sessions._SESSION_FILE_PARSER_VERSIONS["_parse_kimi_session_file"],
    }
    assert "content_sha1" not in signature
    assert (
        sessions._session_file_parser_signature("_parse_codex_session_file")["content_sha1"]
        == sessions._SESSION_FILE_PARSER_V1_COMPAT_TOKEN
    )


def test_workspace_project_correction_survives_a_cached_read(_isolated_kimi_roots):
    """workspaces.json is applied on read, so it lands without reparsing wire files."""
    code_root, _ = _isolated_kimi_roots
    workspace = "wd_myproj_0123456789ab"
    _write_code_session(code_root, [_usage_record(1000, 200, 0, 0)], workspace=workspace)

    assert get_sessions_data("kimi", "all")["sessions"][0]["project"] == "myproj"

    _write_workspaces_json(code_root, {workspace: {"root": "/home/user/RealProject"}})
    reload_pricing_db()

    assert get_sessions_data("kimi", "all")["sessions"][0]["project"] == "RealProject"


def test_windowed_cached_read_keeps_main_agent_metadata(monkeypatch, _isolated_kimi_roots):
    """A window that only catches a subagent must still name the session properly.

    The store rows are per file, so windowing rows before merging can drop the
    main agent's file — the one carrying the prompt and cwd — for a session whose
    subagent ran later.
    """
    code_root, _ = _isolated_kimi_roots
    _write_code_session(
        code_root,
        [
            {"type": "config.update", "cwd": "/home/user/myproj", "time": CODE_TS_MS},
            {"type": "turn.prompt", "input": [{"type": "text", "text": "main agent prompt"}]},
            _usage_record(1000, 200, 0, 0),
        ],
        agent="main",
    )
    _write_code_session(
        code_root,
        [
            {"type": "turn.prompt", "input": [{"type": "text", "text": "subagent preamble"}]},
            _usage_record(7, 3, 0, 0, ts_ms=CODE_TS_MS + 3 * 86_400_000),
        ],
        agent="agent-0",
    )

    # Window covers the subagent's day only; the main agent ran three days earlier.
    window = {"period": "custom", "date_from": "2026-06-04", "date_to": "2026-06-04"}

    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")
    reload_pricing_db()
    live = get_sessions_data("kimi", **window)

    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")
    reload_pricing_db()
    cached = get_sessions_data("kimi", **window)

    assert live["sessions"], "fixture must produce an in-window session"
    assert live["sessions"][0]["display_name"] == "main agent prompt"
    assert live["sessions"][0]["project"] == "myproj"
    for key in ("display_name", "project", "tokens", "cost"):
        assert cached["sessions"][0][key] == live["sessions"][0][key]


def test_parallel_kimi_agents_are_timed_per_stream(_isolated_kimi_roots):
    """Two agents working the same minute are one minute of clock, two of agent time."""
    code_root, _ = _isolated_kimi_roots
    _write_code_session(
        code_root,
        [_usage_record(100, 20, 0, 0), _usage_record(100, 20, 0, 0, ts_ms=CODE_TS_MS + 60_000)],
        agent="main",
    )
    _write_code_session(
        code_root,
        [
            _usage_record(50, 10, 0, 0),
            _usage_record(50, 10, 0, 0, ts_ms=CODE_TS_MS + 60_000),
        ],
        agent="agent-0",
    )

    session = get_sessions_data("kimi", "all")["sessions"][0]

    # Stitched into one timeline both numbers would read 60s; per stream, the
    # agents' minute overlaps on the clock but counts twice as agent time.
    assert session["active_ms"] == 60_000
    assert session["active_ms_sum"] == 120_000


def test_kimi_turns_carry_a_stream_id_that_never_reaches_the_api(_isolated_kimi_roots):
    code_root, _ = _isolated_kimi_roots
    _write_code_session(code_root, [_usage_record(100, 20, 0, 0)], agent="main")
    _write_code_session(
        code_root, [_usage_record(50, 10, 0, 0, ts_ms=CODE_TS_MS + 1000)], agent="agent-0"
    )

    raw = sessions._kimi_sessions()["session_aaa"]
    assert {turn["_stream_id"] for turn in raw["turns"]} == {"main", "agent-0"}

    detail = sessions.get_session_detail("kimi", "session_aaa")
    assert all("_stream_id" not in turn for turn in detail["turns"])
    assert all("_event_key" not in turn for turn in detail["turns"])


def test_legacy_kimi_sessions_are_a_single_stream(_isolated_kimi_roots):
    _, legacy_root = _isolated_kimi_roots
    _write_legacy_session(
        legacy_root,
        [
            _status_update(100, 20, 0, 0, "m1"),
            _status_update(100, 20, 0, 0, "m2", ts_s=LEGACY_TS_S + 60),
        ],
    )

    raw = sessions._kimi_sessions()["sess-legacy"]
    assert {turn["_stream_id"] for turn in raw["turns"]} == {"main"}

    session = get_sessions_data("kimi", "all")["sessions"][0]
    assert session["active_ms"] == session["active_ms_sum"] == 60_000
