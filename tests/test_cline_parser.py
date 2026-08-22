"""Tests for ClineParser: the file-first message store, the cache-inclusive
input split (C1), the source-global dedup key behind resume/fork replay (C7),
and windowing on the message ts (C2)."""
import json
from datetime import datetime, timezone
from pathlib import Path

from tokdash import clientpaths
from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import BaseParser, ClineParser, _sig_cache

TS_A = 1_700_000_000_000
TS_B = 1_700_010_000_000


def _assistant(msg_id, ts, model="cline-test-model", provider="test-provider",
               input_tokens=0, output_tokens=0, cache_read=0, cache_write=0, cost=None):
    metrics = {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "cacheReadTokens": cache_read,
        "cacheWriteTokens": cache_write,
    }
    if cost is not None:
        metrics["cost"] = cost
    return {
        "role": "assistant",
        "id": msg_id,
        "ts": ts,
        "metrics": metrics,
        "modelInfo": {"id": model, "provider": provider},
    }


def _user(msg_id, ts):
    return {"role": "user", "id": msg_id, "ts": ts, "content": [{"type": "text", "text": "hi"}]}


def _write_session(data_dir: Path, session_id: str, messages, agent_id=None) -> Path:
    session_dir = data_dir / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    name = f"{agent_id}.messages.json" if agent_id else f"{session_id}.messages.json"
    path = session_dir / name
    path.write_text(
        json.dumps({
            "version": 1,
            "sessionId": session_id,
            "origin": {"source": "cli", "mode": "user"},
            "messages": messages,
        }),
        encoding="utf-8",
    )
    return path


def _fresh(data_dir: Path):
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    parser = ClineParser(PricingDatabase())
    assert parser.data_dir == data_dir
    return parser


def test_cline_parser_reads_message_files(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("CLINE_DATA_DIR", str(data_dir))
    _write_session(
        data_dir,
        "s1",
        [
            _user("msg_u1", TS_A - 1000),
            _assistant("msg_a1", TS_A, input_tokens=7049, output_tokens=27),
            _assistant("msg_no_metrics", TS_A + 1, input_tokens=1),  # overwritten below
        ],
    )
    # Replace the last message with one lacking metrics (assistant without usage).
    path = data_dir / "sessions" / "s1" / "s1.messages.json"
    doc = json.loads(path.read_text())
    doc["messages"][2] = {"role": "assistant", "id": "msg_no_metrics", "ts": TS_A + 1}
    path.write_text(json.dumps(doc))

    entries = _fresh(data_dir).collect(None, None)

    assert len(entries) == 1
    e = entries[0]
    assert e["source"] == "cline"
    assert e["model"] == "cline-test-model"
    assert e["provider"] == "test-provider"
    assert (e["input"], e["output"], e["cacheRead"], e["cacheWrite"], e["reasoning"]) == (7049, 27, 0, 0, 0)
    assert e["timestamp"] == TS_A
    assert e["entry_id"] == "cline:msg_a1"
    # Cost: pricing DB only; the self-hosted test model is absent from it.
    assert e["cost"] == 0.0
    assert e["_billing"]["kind"] == "pricing"


def test_cline_cache_inclusive_input_split(monkeypatch, tmp_path):
    """C1: inputTokens is the full prompt for every provider; the parser must
    emit disjoint buckets (7049 with 6272 cached -> 777 fresh + 6272 cached)."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("CLINE_DATA_DIR", str(data_dir))
    _write_session(
        data_dir,
        "s1",
        [
            _assistant("msg_cold", TS_A, input_tokens=7049, output_tokens=27, cost=0.0004),
            _assistant("msg_warm", TS_B, input_tokens=7049, output_tokens=57, cache_read=6272, cost=0.0002),
            _assistant("msg_write", TS_B + 1, input_tokens=1000, output_tokens=10, cache_read=200, cache_write=300),
        ],
    )

    entries = _fresh(data_dir).collect(None, None)
    by_id = {e["entry_id"]: e for e in entries}

    cold = by_id["cline:msg_cold"]
    assert (cold["input"], cold["cacheRead"], cold["cacheWrite"]) == (7049, 0, 0)
    # Recorded cost ignored (C6): pricing DB decides.
    assert cold["cost"] == 0.0
    assert cold["_billing"]["kind"] == "pricing"

    warm = by_id["cline:msg_warm"]
    assert (warm["input"], warm["cacheRead"], warm["cacheWrite"]) == (777, 6272, 0)
    # Disjoint: the buckets sum back to Cline's full prompt.
    assert warm["input"] + warm["cacheRead"] + warm["cacheWrite"] == 7049

    write = by_id["cline:msg_write"]
    assert (write["input"], write["cacheRead"], write["cacheWrite"]) == (500, 200, 300)


def test_cline_clamps_malformed_cache_share(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("CLINE_DATA_DIR", str(data_dir))
    _write_session(
        data_dir,
        "s1",
        [_assistant("msg_bad", TS_A, input_tokens=100, output_tokens=5, cache_read=5000, cache_write=9999)],
    )

    entries = _fresh(data_dir).collect(None, None)
    assert len(entries) == 1
    e = entries[0]
    assert (e["input"], e["cacheRead"], e["cacheWrite"]) == (0, 100, 0)
    assert e["input"] + e["cacheRead"] + e["cacheWrite"] <= 100


def test_cline_fork_copies_dedup_globsally(monkeypatch, tmp_path):
    """C7: a forked session file carries the parent's message ids and metrics
    under a NEW session id. The source-global key must count each real model
    call exactly once — a session-scoped key would count the fork's copies
    again."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("CLINE_DATA_DIR", str(data_dir))
    parent_msgs = [
        _user("msg_u1", TS_A - 1000),
        _assistant("msg_shared_1", TS_A, input_tokens=100, output_tokens=5),
        _assistant("msg_shared_2", TS_B, input_tokens=200, output_tokens=7, cache_read=50),
    ]
    _write_session(data_dir, "parent-s1", parent_msgs)
    # Fork: same ids, same metrics, one fork-only new call.
    fork_msgs = list(parent_msgs) + [_assistant("msg_fork_new", TS_B + 1000, input_tokens=300, output_tokens=9)]
    _write_session(data_dir, "fork-s2", fork_msgs)

    entries = _fresh(data_dir).collect(None, None)

    assert sorted(e["entry_id"] for e in entries) == [
        "cline:msg_fork_new",
        "cline:msg_shared_1",
        "cline:msg_shared_2",
    ]
    totals = (sum(e["input"] for e in entries), sum(e["output"] for e in entries))
    # Each shared call counted once, fork-only call once.
    assert totals == (100 + 150 + 300, 5 + 7 + 9)


def test_cline_subagent_files_are_counted(monkeypatch, tmp_path):
    """The sessions table undercounts subagent runs; the message files are
    complete. Parent + agent_* files in one session dir both count."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("CLINE_DATA_DIR", str(data_dir))
    _write_session(
        data_dir,
        "s1",
        [
            _user("msg_u1", TS_A - 1000),
            _assistant("msg_p1", TS_A, input_tokens=7063, output_tokens=219),
            _assistant("msg_p2", TS_B, input_tokens=7330, output_tokens=36, cache_read=7056),
        ],
    )
    _write_session(
        data_dir,
        "s1",
        [_assistant("msg_sub1", TS_B + 1000, input_tokens=3908, output_tokens=31)],
        agent_id="agent_1",
    )

    entries = _fresh(data_dir).collect(None, None)

    assert sorted(e["entry_id"] for e in entries) == [
        "cline:msg_p1",
        "cline:msg_p2",
        "cline:msg_sub1",
    ]
    # Parent's own DB usage (14393/255/7056) == sum of its message metrics.
    parent = [e for e in entries if e["entry_id"] in ("cline:msg_p1", "cline:msg_p2")]
    assert sum(e["input"] + e["cacheRead"] for e in parent) == 14393
    assert sum(e["output"] for e in parent) == 255
    assert sum(e["cacheRead"] for e in parent) == 7056


def test_cline_window_uses_message_ts(monkeypatch, tmp_path):
    """C2 shape: the row's window and its timestamp come from the same column
    (the message ts), so a row cannot be selected by one day's window and
    attributed to another."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("CLINE_DATA_DIR", str(data_dir))
    day = TS_A // 86_400_000 * 86_400_000
    prev_day_ts = day - 60_000
    _write_session(
        data_dir,
        "s1",
        [
            _assistant("msg_prev", prev_day_ts, input_tokens=10, output_tokens=1),
            _assistant("msg_today", day + 1000, input_tokens=20, output_tokens=2),
        ],
    )

    since = datetime.fromtimestamp(day / 1000, tz=timezone.utc)
    until = datetime.fromtimestamp((day + 86_400_000) / 1000, tz=timezone.utc)
    entries = _fresh(data_dir).collect(since, until)

    assert [e["entry_id"] for e in entries] == ["cline:msg_today"]
    assert entries[0]["timestamp"] == day + 1000


def test_cline_skips_unreadable_and_empty(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("CLINE_DATA_DIR", str(data_dir))
    bad = data_dir / "sessions" / "s-bad"
    bad.mkdir(parents=True)
    (bad / "s-bad.messages.json").write_text("{not json", encoding="utf-8")
    _write_session(
        data_dir,
        "s1",
        [
            _assistant("msg_zero", TS_A, input_tokens=0, output_tokens=0),
            _assistant("msg_real", TS_A + 1, input_tokens=1, output_tokens=1),
            {"role": "assistant", "ts": TS_A + 2},  # no metrics at all
        ],
    )

    entries = _fresh(data_dir).collect(None, None)
    assert [e["entry_id"] for e in entries] == ["cline:msg_real"]


def test_cline_data_dir_resolution(monkeypatch, tmp_path):
    monkeypatch.delenv("CLINE_DATA_DIR", raising=False)
    monkeypatch.delenv("CLINE_DIR", raising=False)
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    assert clientpaths.cline_data_dir() == home / ".cline" / "data"

    monkeypatch.setenv("CLINE_DIR", str(tmp_path / "alt"))
    assert clientpaths.cline_data_dir() == tmp_path / "alt" / "data"

    monkeypatch.setenv("CLINE_DATA_DIR", str(tmp_path / "explicit"))
    assert clientpaths.cline_data_dir() == tmp_path / "explicit"
