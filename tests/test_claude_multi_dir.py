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


def test_legacy_claude_records_keep_fullest_snapshot(monkeypatch, tmp_path):
    """Role-bearing rows are streaming snapshots too, so the fullest wins.

    Streamed usage is cumulative, so the last write of a message id is the
    finished turn. Keeping the first write instead billed the partial.
    """
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
    assert entries[0]["output"] == 9
    assert len(raw["sess-legacy"]["turns"]) == 1
    assert raw["sess-legacy"]["turns"][0]["tokens_out"] == 9


def test_claude_records_with_both_type_and_role_keep_completed_usage(monkeypatch, tmp_path):
    """The shape that lost ~98% of one endpoint's output tokens.

    An Anthropic-compatible endpoint stamps BOTH top-level ``type`` and
    ``message.role``, so the role-less snapshot branch never fired and the
    zero-output first content block won on the strength of its input tokens.
    """
    session_dir = tmp_path / ".claude-open" / "projects" / "project"
    session_dir.mkdir(parents=True)

    def _block(output_tokens: int, timestamp: str, cache_read: int = 0) -> str:
        return (
            '{"type":"assistant","sessionId":"sess-both","cwd":"/work/project",'
            f'"timestamp":"{timestamp}",'
            '"message":{"role":"assistant","id":"chatcmpl-both",'
            '"model":"qwen3.8-flash-next",'
            f'"usage":{{"input_tokens":14475,"cache_read_input_tokens":{cache_read},'
            f'"output_tokens":{output_tokens}}}}}}}\n'
        )

    (session_dir / "sess-both.jsonl").write_text(
        _block(0, "2026-09-08T01:59:29Z") + _block(111, "2026-09-08T01:59:33Z"),
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
    assert entries[0]["input"] == 14475
    assert entries[0]["output"] == 111
    assert len(raw["sess-both"]["turns"]) == 1
    assert raw["sess-both"]["turns"][0]["tokens_out"] == 111


def _write_claude_reclassified_session(root: Path, roleless: bool) -> None:
    """A turn whose completed write moves fresh input into cache-read.

    The total is identical across both writes -- 10,000 fresh becomes 1,000
    fresh plus 9,000 cache-read -- so only the split distinguishes them.
    """
    session_dir = root / "projects" / "project"
    session_dir.mkdir(parents=True)

    def _block(fresh: int, cache_read: int, timestamp: str) -> str:
        role = "" if roleless else '"role":"assistant",'
        type_field = '"type":"assistant",' if roleless else ""
        return (
            "{"
            f"{type_field}"
            '"sessionId":"sess-recl","cwd":"/work/project",'
            f'"timestamp":"{timestamp}",'
            "\"message\":{"
            f"{role}"
            '"id":"msg-recl","model":"claude-sonnet-5",'
            f'"usage":{{"input_tokens":{fresh},'
            f'"cache_read_input_tokens":{cache_read},"output_tokens":10}}'
            "}}\n"
        )

    (session_dir / "sess-recl.jsonl").write_text(
        _block(10000, 0, "2026-06-12T12:00:00Z") + _block(1000, 9000, "2026-06-12T12:00:01Z"),
        encoding="utf-8",
    )


def test_claude_parser_equal_total_reclassification_keeps_the_later_split(monkeypatch, tmp_path):
    """An equal total with a changed split is a cache reclassification.

    Selecting on total alone would tie and keep the first write, pricing 9,000
    cached tokens at the fresh-input rate.
    """
    _write_claude_reclassified_session(tmp_path / ".claude-open", roleless=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    entries = ClaudeParser(PricingDatabase()).collect(None, None)

    assert len(entries) == 1
    assert entries[0]["input"] == 1000
    assert entries[0]["cacheRead"] == 9000


def test_claude_session_drilldown_equal_total_reclassification_keeps_the_later_split(
    monkeypatch, tmp_path
):
    _write_claude_reclassified_session(tmp_path / ".claude-open", roleless=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    sessions._parse_claude_session_file.cache_clear()
    sessions._load_claude_sessions.cache_clear()

    raw = sessions._claude_sessions()

    assert len(raw["sess-recl"]["turns"]) == 1
    assert raw["sess-recl"]["turns"][0]["tokens_in"] == 1000
    assert raw["sess-recl"]["turns"][0]["tokens_cache"] == 9000


def test_claude_role_bearing_equal_total_reclassification_keeps_the_later_split(
    monkeypatch, tmp_path
):
    """The same rule applies to rows that carry ``message.role``."""
    _write_claude_reclassified_session(tmp_path / ".claude", roleless=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    sessions._parse_claude_session_file.cache_clear()
    sessions._load_claude_sessions.cache_clear()

    entries = ClaudeParser(PricingDatabase()).collect(None, None)
    raw = sessions._claude_sessions()

    assert len(entries) == 1
    assert entries[0]["input"] == 1000
    assert entries[0]["cacheRead"] == 9000
    assert raw["sess-recl"]["turns"][0]["tokens_in"] == 1000
    assert raw["sess-recl"]["turns"][0]["tokens_cache"] == 9000


def test_claude_identical_duplicate_writes_keep_the_earlier_timestamp(monkeypatch, tmp_path):
    """Ties keep the first write, so a real double-write does not move later."""
    session_dir = tmp_path / ".claude" / "projects" / "project"
    session_dir.mkdir(parents=True)
    row = (
        '{{"sessionId":"sess-dup","cwd":"/work/project","timestamp":"{ts}",'
        '"message":{{"role":"assistant","id":"msg-dup","model":"claude-sonnet-4.5",'
        '"usage":{{"input_tokens":42,"output_tokens":9}}}}}}\n'
    )
    (session_dir / "sess-dup.jsonl").write_text(
        row.format(ts="2026-06-12T23:59:59Z") + row.format(ts="2026-06-13T00:00:02Z"),
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    entries = ClaudeParser(PricingDatabase()).collect(None, None)

    assert len(entries) == 1
    assert entries[0]["output"] == 9
    assert entries[0]["timestamp"] == 1781308799000  # 2026-06-12T23:59:59Z


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


def test_claude_parser_reads_top_level_assistant_and_keeps_fullest_snapshot(monkeypatch, tmp_path):
    _write_claude_open_streaming_session(tmp_path / ".claude-open")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    entries = ClaudeParser(PricingDatabase()).collect(None, None)

    assert len(entries) == 1
    assert entries[0]["input"] == 42
    assert entries[0]["output"] == 9


def test_claude_session_drilldown_reads_top_level_assistant_and_keeps_fullest_snapshot(
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
