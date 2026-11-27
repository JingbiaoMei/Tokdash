"""MiniMax Code (mcode) Sessions harness tests.

Transcripts follow the row shape of tests/parsers/test_minimax_parser.py (the
captured v0.4.12 envelope): ``{message_id, turn_id, message}`` per line, the
assistant row stamped when its call *started*, toolResult rows stamped as the
tool returns, and every row appended when its message completes.
"""

import json
import os
from pathlib import Path

import pytest

from tokdash import sessions
from tokdash.pricing import PricingDatabase
from tokdash.sessions import SESSION_TOOLS, TOOL_LABELS, get_session_detail, get_sessions_data
from tokdash.sources.coding_tools import MiniMaxCodeParser

T0 = 1_795_762_629_304  # a captured user-prompt stamp, epoch ms
PROMPT = "check whether the pi has the usual harnesses installed"


@pytest.fixture(autouse=True)
def _clean_caches():
    sessions.reload_pricing_db()
    yield
    sessions.reload_pricing_db()


def _isolate_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MINIMAX_DATA_DIR", raising=False)
    monkeypatch.delenv("MAVIS_DATA_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def _session_dir(home, rel="2026/11/27/06-57-09-111-session_a", session_id="mvs_a", cwd="/work/demo"):
    d = home / ".minimax" / "v2" / "sessions" / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps({
        "schemaVersion": 1, "sessionId": session_id, "createdAtMs": T0,
        "layout": "v2-final-dated-session",
    }))
    if cwd is not None:
        prompt = f"You are Mavis.\nPrimary working directory: {cwd}\nUse tools."
        (d / "llm-call.json").write_text(json.dumps({"model": "MiniMax-M3", "systemPrompt": prompt}))
    return d


def _js_len(text):
    """String length as JavaScript counts it (UTF-16 code units): mcode is Node."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _user(msg_id, ts, typed=PROMPT):
    reminder = "<system-reminder>\nUse `tool_search` to find tools.\n</system-reminder>\n"
    text = reminder + typed
    return {
        "message_id": msg_id, "turn_id": "turn_1",
        "message": {
            "role": "user", "content": [{"type": "text", "text": text}], "timestamp": ts,
            "canonicalTextRange": {"startOffset": _js_len(reminder), "endOffset": _js_len(text)},
        },
    }


def _assistant(msg_id, ts, input_t=20040, output_t=386, cache_r=128, cache_w=64, model="MiniMax-M3"):
    return {
        "message_id": msg_id, "turn_id": "turn_1",
        "message": {
            "role": "assistant", "content": [{"type": "text", "text": "ok"}],
            "api": "anthropic-messages", "provider": "minimax", "model": model,
            "usage": {
                "input": input_t, "output": output_t, "cacheRead": cache_r, "cacheWrite": cache_w,
                "totalTokens": input_t + output_t + cache_r + cache_w,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
            },
            "stopReason": "toolUse", "timestamp": ts,
        },
    }


def _tool_result(msg_id, ts):
    return {
        "message_id": msg_id, "turn_id": "turn_1",
        "message": {"role": "toolResult", "toolCallId": "call_1", "toolName": "bash",
                    "content": [{"type": "text", "text": "done"}], "timestamp": ts},
    }


def _write(session_dir, rows, mtime_ms=None):
    path = session_dir / "messages.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    if mtime_ms is not None:
        os.utime(path, ns=(mtime_ms * 1_000_000, mtime_ms * 1_000_000))
    return path


def test_registered():
    assert "minimax" in SESSION_TOOLS
    assert TOOL_LABELS["minimax"] == "MiniMax Code"


def test_session_matches_overview_and_names_the_typed_prompt(monkeypatch, tmp_path):
    home = _isolate_home(monkeypatch, tmp_path)
    d = _session_dir(home)
    _write(d, [
        _user("msg-user-1", T0),
        _assistant("msg-a1", T0 + 130),
        _tool_result("msg-t1", T0 + 20_000),
        _assistant("msg-a2", T0 + 20_050, input_t=30_000, output_t=900),
    ], mtime_ms=T0 + 29_000)

    data = get_sessions_data("minimax", "all")

    assert data["summary"]["session_count"] == 1
    session = data["sessions"][0]
    assert session["session_id"] == "mvs_a"
    assert session["display_name"] == PROMPT
    assert session["project"] == "demo"
    assert session["token_events"] == 2
    entries = MiniMaxCodeParser(PricingDatabase())._parse_all()
    assert session["tokens"] == sum(
        e["input"] + e["output"] + e["cacheRead"] + e["cacheWrite"] for e in entries
    )
    assert session["cost"] == pytest.approx(sum(e["cost"] for e in entries))
    # The turn keeps the call's start, the same instant Overview buckets on.
    assert session["started_at"] == sessions._ms_to_iso(T0 + 130)


def test_active_time_runs_from_the_prompt_to_the_finish(monkeypatch, tmp_path):
    """Prompt -> first call's tool result -> last call's append (file mtime)."""
    home = _isolate_home(monkeypatch, tmp_path)
    d = _session_dir(home)
    _write(d, [
        _user("msg-user-1", T0),
        _assistant("msg-a1", T0 + 130),
        _tool_result("msg-t1", T0 + 20_000),
        _assistant("msg-a2", T0 + 20_050),
    ], mtime_ms=T0 + 29_000)

    session = get_sessions_data("minimax", "all")["sessions"][0]

    assert session["active_ms"] == 29_000


def test_a_one_call_session_is_not_zero(monkeypatch, tmp_path):
    home = _isolate_home(monkeypatch, tmp_path)
    d = _session_dir(home)
    _write(d, [_user("msg-user-1", T0), _assistant("msg-a1", T0 + 120)], mtime_ms=T0 + 9_500)

    assert get_sessions_data("minimax", "all")["sessions"][0]["active_ms"] == 9_500


def test_a_file_touched_long_after_its_last_call_does_not_stretch_it(monkeypatch, tmp_path):
    home = _isolate_home(monkeypatch, tmp_path)
    d = _session_dir(home)
    _write(d, [_user("msg-user-1", T0), _assistant("msg-a1", T0 + 120)], mtime_ms=T0 + 6 * 3_600_000)

    # Only the prompt-to-start gap is measurable; the touch is ignored.
    assert get_sessions_data("minimax", "all")["sessions"][0]["active_ms"] == 120


def test_copied_fork_rows_stay_with_the_earliest_session(monkeypatch, tmp_path):
    home = _isolate_home(monkeypatch, tmp_path)
    parent = _session_dir(home, "2026/11/27/06-00-00-000-session_p", session_id="mvs_p")
    fork = _session_dir(home, "2026/11/27/07-00-00-000-session_f", session_id="mvs_f")
    _write(parent, [_user("msg-user-1", T0), _assistant("msg-shared", T0 + 100)])
    _write(fork, [
        _assistant("msg-shared", T0 + 100),
        _user("msg-user-2", T0 + 60_000),
        _assistant("msg-own", T0 + 60_100),
    ])

    by_id = {s["session_id"]: s for s in get_sessions_data("minimax", "all")["sessions"]}

    assert by_id["mvs_p"]["token_events"] == 1
    assert by_id["mvs_f"]["token_events"] == 1
    assert sum(s["token_events"] for s in by_id.values()) == len(
        MiniMaxCodeParser(PricingDatabase())._parse_all()
    )


def test_missing_manifest_and_capture_fall_back_to_the_directory(monkeypatch, tmp_path):
    home = _isolate_home(monkeypatch, tmp_path)
    d = home / ".minimax" / "v2" / "sessions" / "2026/11/27/08-00-00-000-session_bare"
    d.mkdir(parents=True)
    _write(d, [_user("msg-user-1", T0), _assistant("msg-a1", T0 + 100)])

    session = get_sessions_data("minimax", "all")["sessions"][0]

    assert session["session_id"] == "08-00-00-000-session_bare"
    assert session["project"] == "unknown"


def test_session_detail_lists_the_turns(monkeypatch, tmp_path):
    home = _isolate_home(monkeypatch, tmp_path)
    d = _session_dir(home)
    _write(d, [_user("msg-user-1", T0), _assistant("msg-a1", T0 + 100)])

    detail = get_session_detail("minimax", "mvs_a")

    assert len(detail["turns"]) == 1
    assert "_prompt_ms" not in detail["turns"][0]
    assert "_done_ms" not in detail["turns"][0]


def test_a_windows_working_directory_names_its_project(monkeypatch, tmp_path):
    """The capture is JSON, where the path's backslashes are escaped."""
    home = _isolate_home(monkeypatch, tmp_path)
    d = _session_dir(home, cwd="C:\\Users\\me\\proj")
    _write(d, [_user("msg-user-1", T0), _assistant("msg-a1", T0 + 100)])

    assert get_sessions_data("minimax", "all")["sessions"][0]["project"] == "proj"


def test_a_rewritten_manifest_is_read_without_a_transcript_append(monkeypatch, tmp_path):
    home = _isolate_home(monkeypatch, tmp_path)
    d = _session_dir(home, session_id="mvs_old")
    _write(d, [_user("msg-user-1", T0), _assistant("msg-a1", T0 + 100)])
    assert get_sessions_data("minimax", "all")["sessions"][0]["session_id"] == "mvs_old"

    manifest = d / "manifest.json"
    manifest.write_text(json.dumps({"schemaVersion": 1, "sessionId": "mvs_new"}))
    stamp_ns = manifest.stat().st_mtime_ns + 5_000_000_000
    os.utime(manifest, ns=(stamp_ns, stamp_ns))

    assert get_sessions_data("minimax", "all")["sessions"][0]["session_id"] == "mvs_new"


def test_an_astral_character_in_the_prompt_keeps_the_typed_name(monkeypatch, tmp_path):
    """An emoji is two UTF-16 units: offsets read as code points would overrun."""
    home = _isolate_home(monkeypatch, tmp_path)
    d = _session_dir(home)
    typed = "fix the \U0001F41B in parser"
    _write(d, [_user("msg-user-1", T0, typed=typed), _assistant("msg-a1", T0 + 100)])

    assert get_sessions_data("minimax", "all")["sessions"][0]["display_name"] == typed


def test_a_prompt_cut_mid_emoji_does_not_break_the_panel(monkeypatch, tmp_path):
    """A lone surrogate (a string cut inside an emoji, as JSON.stringify writes
    it) survives json.loads; the strict UTF-16 codec would refuse it."""
    home = _isolate_home(monkeypatch, tmp_path)
    d = _session_dir(home)
    _write(d, [_user("msg-user-1", T0, typed="cut mid-emoji \ud83d here"), _assistant("msg-a1", T0 + 100)])

    name = get_sessions_data("minimax", "all")["sessions"][0]["display_name"]

    assert name.startswith("cut mid-emoji")
    assert "system-reminder" not in name
