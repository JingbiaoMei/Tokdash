"""First-turn anchors for the Claude Code and Codex session parsers.

The other session sources carry their own anchor test beside their fixtures;
these two parse large real-world formats whose suites build whole corpora, so
the check here goes straight to the per-file parser with a minimal log.
"""

import json
from datetime import datetime, timezone

from tokdash import sessions

T0 = int(datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
CAP = 5 * 60_000


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _parse(parser, path):
    st = path.stat()
    return parser(str(path), st.st_mtime_ns, st.st_size)


def _claude_assistant(msg_id, ts, agent_id=None):
    row = {
        "type": "assistant", "sessionId": "sess-1", "timestamp": _iso(ts), "cwd": "/work/demo",
        "message": {"id": msg_id, "role": "assistant", "model": "claude-sonnet-4-5",
                    "usage": {"input_tokens": 100, "output_tokens": 20}},
    }
    if agent_id:
        row["agentId"] = agent_id
    return row


def _claude_user(ts, content="hi", agent_id=None):
    row = {"type": "user", "sessionId": "sess-1", "timestamp": _iso(ts),
           "message": {"role": "user", "content": content}}
    if agent_id:
        row["agentId"] = agent_id
    return row


def test_claude_streams_are_timed_from_their_own_prompts(tmp_path):
    main = _write(tmp_path / "proj" / "sess-1.jsonl", [
        _claude_user(T0),
        _claude_assistant("m1", T0 + 5_000),
    ])
    sub = _write(tmp_path / "proj" / "sess-1" / "subagents" / "agent-x.jsonl", [
        _claude_user(T0 + 10_000, agent_id="x"),
        _claude_assistant("s1", T0 + 12_000, agent_id="x"),
    ])
    turns = (
        _parse(sessions._parse_claude_session_file, main)["turns"]
        + _parse(sessions._parse_claude_session_file, sub)["turns"]
    )

    intervals = sessions._session_active_intervals({"turns": turns}, CAP)

    assert sorted(intervals) == [(T0, T0 + 5_000), (T0 + 10_000, T0 + 12_000)]


def test_claude_tool_results_are_not_prompts(tmp_path):
    path = _write(tmp_path / "proj" / "sess-1.jsonl", [
        _claude_user(T0),
        _claude_user(T0 + 4_000, content=[{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]),
        _claude_assistant("m1", T0 + 5_000),
    ])

    turn = _parse(sessions._parse_claude_session_file, path)["turns"][0]

    assert turn["_prompt_ms"] == T0


def _codex_rows(prompt_ms, done_ms):
    usage = {"input_tokens": 1000, "cached_input_tokens": 200, "output_tokens": 50,
             "reasoning_output_tokens": 0, "total_tokens": 1050}
    return [
        {"timestamp": _iso(prompt_ms - 1_000), "type": "session_meta",
         "payload": {"id": "thread-1", "cwd": "/work/demo", "model_provider": "openai"}},
        {"timestamp": _iso(prompt_ms), "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-1"}},
        {"timestamp": _iso(prompt_ms + 10), "type": "turn_context",
         "payload": {"turn_id": "turn-1", "model": "gpt-5.5", "cwd": "/work/demo"}},
        {"timestamp": _iso(prompt_ms + 20), "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}},
        {"timestamp": _iso(done_ms), "type": "event_msg",
         "payload": {"type": "token_count",
                     "info": {"total_token_usage": usage, "last_token_usage": usage}}},
    ]


def test_codex_one_turn_session_is_timed_from_its_prompt(tmp_path):
    path = _write(tmp_path / "rollout-2026-06-01T12-00-00-thread-1.jsonl", _codex_rows(T0, T0 + 12_500))

    raw = _parse(sessions._parse_codex_session_file, path)

    assert len(raw["turns"]) == 1
    assert raw["turns"][0]["_prompt_ms"] == T0 + 20
    assert sessions._summarize_session(raw)["active_ms"] == 12_480
