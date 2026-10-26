"""Tests for QwenCodeParser: the append-only session JSONL, the
cache-inclusive Gemini prompt split, the source-global uuid dedup behind
/branch fork copies, and runtime-base resolution."""
import json
from pathlib import Path

from tokdash import clientpaths
from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import (
    BaseParser,
    QwenCodeParser,
    _sig_cache,
)

TS_A = "2024-01-15T10:30:00Z"
TS_B = "2024-01-15T10:31:00Z"
TS_A_MS = 1_705_314_600_000
TS_B_MS = 1_705_314_660_000


def _rec(uuid, ts, rtype="assistant", model="qwen3-max", usage=None):
    rec = {"uuid": uuid, "parentUuid": None, "sessionId": "s1", "timestamp": ts, "type": rtype}
    if model is not None:
        rec["model"] = model
    if usage is not None:
        rec["usageMetadata"] = usage
    return rec


def _usage(prompt=100, cached=0, candidates=20, thoughts=0):
    return {
        "promptTokenCount": prompt,
        "cachedContentTokenCount": cached,
        "candidatesTokenCount": candidates,
        "thoughtsTokenCount": thoughts,
    }


def _write_session(base: Path, project: str, session_id: str, records, layout="projects") -> Path:
    chats = base / layout / project / "chats"
    chats.mkdir(parents=True, exist_ok=True)
    path = chats / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _fresh(base: Path, monkeypatch) -> QwenCodeParser:
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    monkeypatch.setattr(clientpaths, "qwen_runtime_base", lambda: base)
    return QwenCodeParser(PricingDatabase())


def test_qwen_cache_inclusive_split_and_reasoning(monkeypatch, tmp_path):
    """promptTokenCount is cache-inclusive (Gemini semantics): the cached
    share must move to its own bucket, not count twice."""
    base = tmp_path / "qwen"
    _write_session(
        base, "p1", "s1",
        [_rec("u1", TS_A, usage=_usage(prompt=100, cached=40, candidates=20, thoughts=5))],
    )
    entries = _fresh(base, monkeypatch).collect(None, None)

    assert len(entries) == 1
    e = entries[0]
    assert (e["input"], e["output"], e["cacheRead"], e["cacheWrite"], e["reasoning"]) == (60, 20, 40, 0, 5)
    assert e["timestamp"] == TS_A_MS
    assert e["entry_id"] == "qwen:u1"
    assert e["model"] == "qwen3-max"
    # Records carry no provider; the model id is the pricing key.
    assert e["provider"] == ""
    assert e["cost"] == PricingDatabase().get_cost("qwen3-max", 60, 20, 40, 0)
    assert e["_billing"]["kind"] == "pricing"


def test_qwen_ignores_non_assistant_records(monkeypatch, tmp_path):
    base = tmp_path / "qwen"
    _write_session(
        base, "p1", "s1",
        [
            _rec("u1", TS_A, rtype="user", usage=_usage()),
            _rec("u2", TS_A, rtype="system", usage=_usage()),
            _rec("u3", TS_A, rtype="tool_result", usage=_usage()),
            _rec("u4", TS_A, model=None),  # assistant without usageMetadata
        ],
    )
    assert _fresh(base, monkeypatch).collect(None, None) == []


def test_qwen_skips_non_integer_counts(monkeypatch, tmp_path):
    base = tmp_path / "qwen"
    _write_session(
        base, "p1", "s1",
        [
            _rec("b1", TS_A, usage={"promptTokenCount": "100", "candidatesTokenCount": 20}),
            _rec("b2", TS_A, usage={"candidatesTokenCount": 20}),  # promptTokenCount missing
            _rec("b3", TS_A, usage={"promptTokenCount": 100, "candidatesTokenCount": None}),
            _rec("b4", TS_A, usage=_usage(prompt=True)),  # bool is not a count
            _rec("g1", TS_A, usage=_usage(prompt=10, candidates=5)),
        ],
    )
    entries = _fresh(base, monkeypatch).collect(None, None)

    assert [e["entry_id"] for e in entries] == ["qwen:g1"]


def test_qwen_missing_model_is_unknown_and_unpriced(monkeypatch, tmp_path):
    base = tmp_path / "qwen"
    _write_session(base, "p1", "s1", [_rec("u1", TS_A, model=None, usage=_usage())])
    entries = _fresh(base, monkeypatch).collect(None, None)

    assert len(entries) == 1
    e = entries[0]
    assert (e["model"], e["provider"]) == ("unknown", "")
    assert e["cost"] == 0.0


def test_qwen_all_zero_usage_is_skipped(monkeypatch, tmp_path):
    base = tmp_path / "qwen"
    _write_session(
        base, "p1", "s1",
        [_rec("u1", TS_A, usage=_usage(prompt=0, cached=0, candidates=0, thoughts=0))],
    )
    assert _fresh(base, monkeypatch).collect(None, None) == []


def test_qwen_fully_cached_prompt_is_kept(monkeypatch, tmp_path):
    """prompt == cached is a real call (fresh input 0), not an empty one."""
    base = tmp_path / "qwen"
    _write_session(
        base, "p1", "s1",
        [_rec("u1", TS_A, usage=_usage(prompt=50, cached=50, candidates=10))],
    )
    entries = _fresh(base, monkeypatch).collect(None, None)

    assert len(entries) == 1
    assert (entries[0]["input"], entries[0]["cacheRead"], entries[0]["output"]) == (0, 50, 10)


def test_qwen_legacy_tmp_layout_is_read(monkeypatch, tmp_path):
    base = tmp_path / "qwen"
    _write_session(
        base, "p1", "s1", [_rec("u1", TS_A, usage=_usage(prompt=5, candidates=1))], layout="tmp",
    )
    entries = _fresh(base, monkeypatch).collect(None, None)
    assert [e["entry_id"] for e in entries] == ["qwen:u1"]


def test_qwen_runtime_base_env_order(monkeypatch, tmp_path):
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    _write_session(r1, "p1", "s1", [_rec("u1", TS_A, usage=_usage(prompt=5, candidates=1))])
    _write_session(r2, "p1", "s2", [_rec("u2", TS_A, usage=_usage(prompt=5, candidates=1))])

    monkeypatch.setenv("QWEN_RUNTIME_DIR", str(r1))
    monkeypatch.setenv("QWEN_HOME", str(r2))
    # QWEN_RUNTIME_DIR outranks QWEN_HOME.
    assert clientpaths.qwen_runtime_base() == r1
    assert clientpaths.qwen_chat_files() == [r1 / "projects" / "p1" / "chats" / "s1.jsonl"]

    monkeypatch.delenv("QWEN_RUNTIME_DIR")
    assert clientpaths.qwen_runtime_base() == r2
    assert clientpaths.qwen_chat_files() == [r2 / "projects" / "p1" / "chats" / "s2.jsonl"]

    monkeypatch.delenv("QWEN_HOME")
    assert clientpaths.qwen_runtime_base() == Path.home() / ".qwen"


def test_qwen_fork_copies_dedupe_to_earliest(monkeypatch, tmp_path):
    """/branch copies every record (same uuids) into the fork's file with
    restamped timestamps; the earliest occurrence is the canonical one."""
    base = tmp_path / "qwen"
    _write_session(
        base, "p1", "s1",
        [
            _rec("u1", TS_A, usage=_usage(prompt=10, candidates=5)),
            _rec("u2", TS_B, usage=_usage(prompt=10, candidates=5)),
        ],
    )
    _write_session(
        base, "p1", "s2",
        [
            _rec("u1", "2024-01-15T10:32:00Z", usage=_usage(prompt=10, candidates=5)),
            _rec("u2", "2024-01-15T10:33:00Z", usage=_usage(prompt=10, candidates=5)),
            _rec("u3", "2024-01-15T10:34:00Z", usage=_usage(prompt=10, candidates=5)),
        ],
    )
    entries = _fresh(base, monkeypatch).collect(None, None)

    assert [(e["entry_id"], e["timestamp"]) for e in entries] == [
        ("qwen:u1", TS_A_MS),
        ("qwen:u2", TS_B_MS),
        ("qwen:u3", 1_705_314_840_000),
    ]


def test_qwen_absent_base_has_no_rows(monkeypatch, tmp_path):
    base = tmp_path / "does-not-exist"
    assert _fresh(base, monkeypatch).collect(None, None) == []


def test_qwen_chat_files_dedupe_symlinked_tree(tmp_path):
    base = tmp_path / "qwen"
    path = _write_session(base, "p1", "s1", [_rec("u1", TS_A, usage=_usage())])
    link = base / "tmp" / "p1"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(base / "projects" / "p1")

    assert clientpaths.qwen_chat_files(base) == [path]


def test_qwen_record_without_uuid_is_anonymous(monkeypatch, tmp_path):
    base = tmp_path / "qwen"
    rec = _rec("u1", TS_A, usage=_usage(prompt=5, candidates=1))
    del rec["uuid"]
    _write_session(base, "p1", "s1", [rec])
    entries = _fresh(base, monkeypatch).collect(None, None)

    assert len(entries) == 1
    assert entries[0]["entry_id"] == ""


def test_qwen_offset_timestamp_normalized(monkeypatch, tmp_path):
    base = tmp_path / "qwen"
    _write_session(base, "p1", "s1", [_rec("u1", "2024-01-15T12:30:00+02:00", usage=_usage())])
    entries = _fresh(base, monkeypatch).collect(None, None)

    assert [e["timestamp"] for e in entries] == [TS_A_MS]
