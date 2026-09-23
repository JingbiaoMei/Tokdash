import json
import sqlite3
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from tokdash import sessions, clientpaths
from tokdash.api import app


def _create_mock_hermes_db(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            model TEXT,
            billing_provider TEXT,
            started_at REAL,
            ended_at REAL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            reasoning_tokens INTEGER,
            estimated_cost_usd REAL,
            actual_cost_usd REAL,
            title TEXT,
            cwd TEXT,
            git_repo_root TEXT,
            git_branch TEXT,
            tool_call_count INTEGER,
            message_count INTEGER,
            profile_name TEXT,
            last_activity_at REAL
        )
    """)
    cur.execute("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            tool_name TEXT,
            tool_call_id TEXT,
            tool_calls TEXT,
            reasoning TEXT,
            token_count INTEGER,
            finish_reason TEXT,
            timestamp REAL
        )
    """)
    cur.execute("""
        CREATE TABLE session_model_usage (
            session_id TEXT,
            model TEXT,
            billing_provider TEXT,
            task TEXT,
            api_call_count INTEGER,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            reasoning_tokens INTEGER,
            estimated_cost_usd REAL,
            actual_cost_usd REAL,
            first_seen REAL,
            last_seen REAL
        )
    """)

    # Insert sample session
    cur.execute("""
        INSERT INTO sessions (
            id, model, billing_provider, started_at, ended_at,
            input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
            reasoning_tokens, estimated_cost_usd, actual_cost_usd,
            title, cwd, git_repo_root, git_branch, tool_call_count, message_count,
            profile_name, last_activity_at
        ) VALUES (
            'test_sess_01', 'hermes-model', 'nous', 1700000000.0, 1700003600.0,
            1000, 200, 500, 100, 150, 0.05, 0.05,
            'Test Hermes Session', 'C:/projects/my-app', 'C:/projects/my-app', 'main',
            2, 4, 'default', 1700003600.0
        )
    """)

    # Insert sample messages
    cur.execute("""
        INSERT INTO messages (id, session_id, role, content, tool_name, tool_call_id, tool_calls, reasoning, token_count, finish_reason, timestamp)
        VALUES
        (1, 'test_sess_01', 'user', 'Check files in repository', NULL, NULL, NULL, NULL, 50, NULL, 1700000000.0),
        (2, 'test_sess_01', 'assistant', 'I will look up the files.', NULL, NULL, '[{"id":"call_1","function":{"name":"search_files","arguments":"{\\"path\\":\\".\\"}"}}]', 'Analyzing user goal', 100, 'tool_calls', 1700000005.0),
        (3, 'test_sess_01', 'tool', 'file1.py\nfile2.py', 'search_files', 'call_1', NULL, NULL, 20, NULL, 1700000010.0),
        (4, 'test_sess_01', 'assistant', 'Found file1.py and file2.py', NULL, NULL, NULL, 'Finished search', 80, 'stop', 1700000015.0)
    """)

    # Insert sample model usage
    cur.execute("""
        INSERT INTO session_model_usage (session_id, model, billing_provider, task, api_call_count, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, estimated_cost_usd, actual_cost_usd, first_seen, last_seen)
        VALUES ('test_sess_01', 'hermes-model', 'nous', 'main', 2, 1000, 200, 500, 100, 150, 0.05, 0.05, 1700000000.0, 1700000015.0)
    """)

    conn.commit()
    conn.close()


def test_hermes_deep_features(tmp_path, monkeypatch):
    mock_dir = tmp_path / "hermes_test"
    db_file = mock_dir / "state.db"
    _create_mock_hermes_db(db_file)

    monkeypatch.setattr(clientpaths, "hermes_search_dirs", lambda: [mock_dir])
    sessions._load_hermes_sessions.cache_clear()

    # 1. Test session extraction
    data = sessions.get_sessions_data("hermes", "all")
    assert len(data.get("sessions", [])) == 1
    sess = data["sessions"][0]
    assert sess["session_id"] == "test_sess_01"
    assert sess["project"] == "my-app"
    assert sess["display_name"] == "Test Hermes Session"
    assert sess["span_ms"] == 3600000

    # 2. Test rich detail
    detail = sessions.get_session_detail("hermes", "test_sess_01")
    assert len(detail.get("messages", [])) == 4
    assert len(detail.get("tool_executions", [])) == 1
    tool_exec = detail["tool_executions"][0]
    assert tool_exec["name"] == "search_files"
    assert "file1.py" in tool_exec["result"]

    # 3. Test Hermes analytics
    analytics = sessions.get_hermes_analytics()
    assert analytics["total_tool_calls"] == 1
    assert analytics["tools"][0]["name"] == "search_files"
    assert analytics["projects"][0]["name"] == "my-app"

    # 4. Test API Endpoints
    client = TestClient(app)
    res_analytics = client.get("/api/hermes/analytics")
    assert res_analytics.status_code == 200
    assert res_analytics.json()["total_tool_calls"] == 1

    res_csv = client.get("/api/export/csv?period=all&tool=hermes")
    assert res_csv.status_code == 200
    assert "test_sess_01" in res_csv.text
    assert "my-app" in res_csv.text

    res_md = client.get("/api/export/markdown?period=all&tool=hermes")
    assert res_md.status_code == 200
    assert "Test Hermes Session" in res_md.text
