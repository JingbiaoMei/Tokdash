from pathlib import Path

import tokdash.sessions as sessions
from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import BaseParser, ClaudeParser, _sig_cache


def _write_claude_session(root: Path, session_id: str, message_id: str, tokens: int) -> None:
    session_dir = root / "projects" / "project"
    session_dir.mkdir(parents=True)
    session_file = session_dir / f"{session_id}.jsonl"
    session_file.write_text(
        (
            "{"
            f'"sessionId":"{session_id}",'
            '"cwd":"/work/project",'
            '"timestamp":"2026-05-19T12:00:00Z",'
            '"message":{'
            '"role":"assistant",'
            f'"id":"{message_id}",'
            '"model":"claude-sonnet-4.5",'
            f'"usage":{{"input_tokens":{tokens},"output_tokens":5}}'
            "}"
            "}\n"
        ),
        encoding="utf-8",
    )


def test_claude_parser_reads_all_claude_project_directories(monkeypatch, tmp_path):
    _write_claude_session(tmp_path / ".claude", "base-session", "msg-base", 11)
    _write_claude_session(tmp_path / ".claude-opus", "opus-session", "msg-opus", 22)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    entries = ClaudeParser(PricingDatabase()).collect(None, None)

    assert sorted(entry["input"] for entry in entries) == [11, 22]
    assert {entry["source"] for entry in entries} == {"claude"}


def test_claude_session_drilldown_reads_all_claude_project_directories(monkeypatch, tmp_path):
    _write_claude_session(tmp_path / ".claude", "base-session", "msg-base", 11)
    _write_claude_session(tmp_path / ".claude-opus", "opus-session", "msg-opus", 22)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    sessions._parse_claude_session_file.cache_clear()
    sessions._load_claude_sessions.cache_clear()

    raw = sessions._claude_sessions()

    assert sorted(raw) == ["base-session", "opus-session"]
    assert raw["base-session"]["turns"][0]["tokens_in"] == 11
    assert raw["opus-session"]["turns"][0]["tokens_in"] == 22


def _write_claude_session_with_placeholder(root: Path, session_id: str, message_id: str, real_tokens: int) -> None:
    """Write a session where each assistant turn has a zero-token placeholder
    followed by the real entry sharing the same ``message.id``. This is the
    pattern produced by some Claude Code forks (e.g. ``~/.claude-mi``)."""
    session_dir = root / "projects" / "project"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / f"{session_id}.jsonl"

    def _entry(tokens: int, timestamp: str) -> str:
        return (
            "{"
            f'"sessionId":"{session_id}",'
            '"cwd":"/work/project",'
            f'"timestamp":"{timestamp}",'
            '"message":{'
            '"role":"assistant",'
            f'"id":"{message_id}",'
            '"model":"claude-sonnet-4.5",'
            f'"usage":{{"input_tokens":{tokens},"output_tokens":{5 if tokens else 0}}}'
            "}"
            "}\n"
        )

    session_file.write_text(
        _entry(0, "2026-05-19T12:00:00Z") + _entry(real_tokens, "2026-05-19T12:00:01Z"),
        encoding="utf-8",
    )


def test_claude_parser_keeps_real_entry_when_zero_token_placeholder_shares_message_id(
    monkeypatch, tmp_path
):
    _write_claude_session_with_placeholder(tmp_path / ".claude-mi", "sess-mi", "msg-mi", 42)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    entries = ClaudeParser(PricingDatabase()).collect(None, None)

    assert [entry["input"] for entry in entries] == [42]


def test_claude_session_drilldown_keeps_real_entry_when_zero_token_placeholder_shares_message_id(
    monkeypatch, tmp_path
):
    _write_claude_session_with_placeholder(tmp_path / ".claude-mi", "sess-mi", "msg-mi", 42)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    sessions._parse_claude_session_file.cache_clear()
    sessions._load_claude_sessions.cache_clear()

    raw = sessions._claude_sessions()

    assert "sess-mi" in raw
    assert raw["sess-mi"]["turns"][0]["tokens_in"] == 42


def test_legacy_claude_records_keep_first_nonzero_snapshot(monkeypatch, tmp_path):
    session_dir = tmp_path / ".claude" / "projects" / "project"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "sess-legacy.jsonl"
    session_file.write_text(
        (
            '{"sessionId":"sess-legacy","timestamp":"2026-06-12T12:00:00Z",'
            '"message":{"role":"assistant","id":"msg-legacy","model":"claude-sonnet-4.5",'
            '"usage":{"input_tokens":42,"output_tokens":5}}}\n'
            '{"sessionId":"sess-legacy","timestamp":"2026-06-12T12:00:01Z",'
            '"message":{"role":"assistant","id":"msg-legacy","model":"claude-sonnet-4.5",'
            '"usage":{"input_tokens":42,"output_tokens":9}}}\n'
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    sessions._parse_claude_session_file.cache_clear()
    sessions._load_claude_sessions.cache_clear()

    entries = ClaudeParser(PricingDatabase()).collect(None, None)
    raw = sessions._claude_sessions()

    assert len(entries) == 1
    assert entries[0]["output"] == 5
    assert len(raw["sess-legacy"]["turns"]) == 1
    assert raw["sess-legacy"]["turns"][0]["tokens_out"] == 5


def _write_claude_open_streaming_session(root: Path) -> None:
    session_dir = root / "projects" / "project"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "sess-open.jsonl"

    def _entry(output_tokens: int, timestamp: str) -> str:
        return (
            "{"
            '"type":"assistant",'
            '"sessionId":"sess-open",'
            '"cwd":"/work/project",'
            f'"timestamp":"{timestamp}",'
            '"message":{'
            '"id":"chatcmpl-open",'
            '"model":"Qwen/Qwen3.6-27B-FP8",'
            f'"usage":{{"input_tokens":42,"output_tokens":{output_tokens}}}'
            "}"
            "}\n"
        )

    session_file.write_text(
        _entry(0, "2026-06-12T12:00:00Z")
        + _entry(9, "2026-06-12T12:00:01Z")
        + _entry(9, "2026-06-12T12:00:02Z"),
        encoding="utf-8",
    )


def test_claude_parser_reads_top_level_assistant_and_keeps_latest_snapshot(monkeypatch, tmp_path):
    _write_claude_open_streaming_session(tmp_path / ".claude-open")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    entries = ClaudeParser(PricingDatabase()).collect(None, None)

    assert len(entries) == 1
    assert entries[0]["input"] == 42
    assert entries[0]["output"] == 9


def test_claude_session_drilldown_reads_top_level_assistant_and_keeps_latest_snapshot(
    monkeypatch, tmp_path
):
    _write_claude_open_streaming_session(tmp_path / ".claude-open")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    sessions._parse_claude_session_file.cache_clear()
    sessions._load_claude_sessions.cache_clear()

    raw = sessions._claude_sessions()

    assert len(raw["sess-open"]["turns"]) == 1
    assert raw["sess-open"]["turns"][0]["tokens_in"] == 42
    assert raw["sess-open"]["turns"][0]["tokens_out"] == 9
