"""Tests for PiAgentParser."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import BaseParser, PiAgentParser, _sig_cache


def _make_session_lines(session_id="abc12345"):
    """Return a minimal pi-agent JSONL session."""
    lines = [
        json.dumps({"type": "session", "id": session_id, "cwd": "/home/user/project", "timestamp": "2026-05-21T10:00:00.000Z"}),
        json.dumps({"type": "thinking_level_change", "level": "high"}),
        json.dumps({"type": "model_change", "provider": "minimax-cn", "modelId": "MiniMax-M2.7"}),
        json.dumps({
            "type": "message",
            "id": "4e5734ac",
            "timestamp": "2026-05-21T20:12:12.189Z",
            "message": {
                "role": "assistant",
                "provider": "minimax-cn",
                "model": "MiniMax-M2.7",
                "usage": {
                    "input": 7000,
                    "output": 47,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                    "totalTokens": 7047,
                    "cost": {"input": 0.0021, "output": 0.0000564, "cacheRead": 0, "cacheWrite": 0, "total": 0.0021564},
                },
            },
        }),
    ]
    return "\n".join(lines) + "\n"


def test_pi_agent_parser_basic(monkeypatch, tmp_path):
    """Parser reads a single message entry from a pi-agent session file."""
    # Build directory structure: <pi_dir>/--home-user--project/<iso>_<uuid>.jsonl
    pi_dir = tmp_path / "pi-agent"
    session_dir = pi_dir / "--home-user--project"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "2026-05-21T10-00-00_session-uuid.jsonl"
    session_file.write_text(_make_session_lines(), encoding="utf-8")

    monkeypatch.setenv("PI_AGENT_DIR", str(pi_dir))
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    parser = PiAgentParser(PricingDatabase())
    entries = parser.collect(None, None)

    assert len(entries) == 1
    e = entries[0]
    assert e["source"] == "pi_agent"
    assert e["model"] == "MiniMax-M2.7"
    assert e["provider"] == "minimax-cn"
    assert e["input"] == 7000
    assert e["output"] == 47
    assert e["cacheRead"] == 0
    assert e["cacheWrite"] == 0
    assert e["reasoning"] == 0
    # Cost should use the embedded usage.cost.total
    assert abs(e["cost"] - 0.0021564) < 1e-9
    expected_ts = int(datetime(2026, 5, 21, 20, 12, 12, 189000, timezone.utc).timestamp() * 1000)
    assert e["timestamp"] == expected_ts


def test_pi_agent_parser_nonzero_cache(monkeypatch, tmp_path):
    """Non-zero camelCase cacheRead/cacheWrite map through, and input stays fresh.

    Locks the cache mapping for the cache-hit-rate metric: usage.input is the fresh
    (uncached) portion (not cache-inclusive), so the full prompt input is
    input + cacheWrite + cacheRead and the hit rate is cacheRead over that.
    """
    pi_dir = tmp_path / "pi-agent"
    session_dir = pi_dir / "--home-user--project"
    session_dir.mkdir(parents=True)
    msg = json.dumps({
        "type": "message",
        "id": "feedface",
        "timestamp": "2026-05-21T20:30:00.000Z",
        "message": {
            "role": "assistant",
            "provider": "minimax-cn",
            "model": "MiniMax-M2.7",
            "usage": {
                "input": 1000,
                "output": 200,
                "cacheRead": 8000,
                "cacheWrite": 500,
                "totalTokens": 9700,
                "cost": {"total": 0.01},
            },
        },
    })
    (session_dir / "session.jsonl").write_text(msg + "\n", encoding="utf-8")

    monkeypatch.setenv("PI_AGENT_DIR", str(pi_dir))
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    entries = PiAgentParser(PricingDatabase()).collect(None, None)
    assert len(entries) == 1
    e = entries[0]
    assert e["input"] == 1000  # fresh input, not cache-inclusive
    assert e["cacheRead"] == 8000
    assert e["cacheWrite"] == 500
    # Full prompt input = fresh + cacheWrite + cacheRead = 9500; hit rate = 8000/9500.
    from tokdash.compute import cache_hit_rate
    assert cache_hit_rate(e["input"] + e["cacheWrite"], e["cacheRead"]) == round(8000 / 9500, 4)


def test_pi_agent_parser_dedup(monkeypatch, tmp_path):
    """Duplicate outer id is skipped."""
    pi_dir = tmp_path / "pi-agent"
    session_dir = pi_dir / "--home-user--project"
    session_dir.mkdir(parents=True)

    # Write the same id twice across two files
    msg = json.dumps({
        "type": "message",
        "id": "deadbeef",
        "timestamp": "2026-05-21T20:00:00.000Z",
        "message": {
            "role": "assistant",
            "model": "MiniMax-M2.7",
            "provider": "minimax-cn",
            "usage": {"input": 100, "output": 50, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 150},
        },
    })
    (session_dir / "session1.jsonl").write_text(msg + "\n", encoding="utf-8")
    (session_dir / "session2.jsonl").write_text(msg + "\n", encoding="utf-8")

    monkeypatch.setenv("PI_AGENT_DIR", str(pi_dir))
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    parser = PiAgentParser(PricingDatabase())
    entries = parser.collect(None, None)
    assert len(entries) == 1


def test_pi_agent_parser_totals_fallback(monkeypatch, tmp_path):
    """When all breakdown tokens are zero but totalTokens > 0, output gets the total."""
    pi_dir = tmp_path / "pi-agent"
    session_dir = pi_dir / "--home-user--project"
    session_dir.mkdir(parents=True)

    msg = json.dumps({
        "type": "message",
        "id": "cafebabe",
        "timestamp": "2026-05-21T20:00:00.000Z",
        "message": {
            "role": "assistant",
            "model": "MiniMax-M2.7",
            "provider": "minimax-cn",
            "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 999},
        },
    })
    (session_dir / "session.jsonl").write_text(msg + "\n", encoding="utf-8")

    monkeypatch.setenv("PI_AGENT_DIR", str(pi_dir))
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    parser = PiAgentParser(PricingDatabase())
    entries = parser.collect(None, None)
    assert len(entries) == 1
    assert entries[0]["output"] == 999
    assert entries[0]["input"] == 0


def test_pi_agent_parser_model_from_model_change(monkeypatch, tmp_path):
    """Falls back to model_change.modelId when message.model is absent."""
    pi_dir = tmp_path / "pi-agent"
    session_dir = pi_dir / "--project"
    session_dir.mkdir(parents=True)

    lines = "\n".join([
        json.dumps({"type": "model_change", "provider": "openai", "modelId": "gpt-5.2"}),
        json.dumps({
            "type": "message",
            "id": "aabbccdd",
            "timestamp": "2026-05-21T21:00:00.000Z",
            "message": {
                "role": "assistant",
                "usage": {"input": 50, "output": 20, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 70},
            },
        }),
    ]) + "\n"
    (session_dir / "session.jsonl").write_text(lines, encoding="utf-8")

    monkeypatch.setenv("PI_AGENT_DIR", str(pi_dir))
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    parser = PiAgentParser(PricingDatabase())
    entries = parser.collect(None, None)
    assert len(entries) == 1
    assert entries[0]["model"] == "gpt-5.2"
    assert entries[0]["provider"] == "openai"


def test_pi_agent_parser_default_dir(monkeypatch, tmp_path):
    """Without PI_AGENT_DIR, defaults to ~/.pi/agent/sessions."""
    monkeypatch.delenv("PI_AGENT_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    parser = PiAgentParser(PricingDatabase())
    assert parser.search_dirs == [tmp_path / ".pi" / "agent" / "sessions"]
    # No files → empty result without error
    entries = parser.collect(None, None)
    assert entries == []
