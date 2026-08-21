"""Tests for QoderIdeParser (Qoder IDE GUI local.db, source "qoder")."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tokdash import clientpaths, osinfo
from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import BaseParser, QoderIdeParser

FIXTURES = Path(__file__).parent / "fixtures" / "qoder"
CN_ROOT = FIXTURES / "localdb_cn"
INTL_ROOT = FIXTURES / "localdb_intl"
DB_SUFFIX = Path("SharedClientCache") / "cache" / "db" / "local.db"
CHAT_MESSAGE_DDL = """
CREATE TABLE chat_message (
    id varchar(64) primary key,
    session_id VARCHAR(64),
    request_id VARCHAR(64),
    role       VARCHAR(64),
    content text,
    summary text,
    summary_modified INTEGER,
    summary_trigger INTEGER DEFAULT 0,
    tool_result text,
    token_info text,
    model_info text,
    extra text DEFAULT '',
    gmt_create INTEGER
)
"""


@pytest.fixture(autouse=True)
def _clear_qoder_caches():
    BaseParser._entry_cache.clear()
    QoderIdeParser._query_cache.clear()
    QoderIdeParser._query_cache_sig = ()
    yield
    BaseParser._entry_cache.clear()
    QoderIdeParser._query_cache.clear()
    QoderIdeParser._query_cache_sig = ()


def _parser(monkeypatch, root: Path) -> QoderIdeParser:
    monkeypatch.delenv("QODER_IDE_DATA_DIR", raising=False)
    monkeypatch.setenv("QODER_IDE_DATA_DIR", str(root))
    return QoderIdeParser(PricingDatabase())


def _make_db(root: Path, rows: list) -> Path:
    db = root / DB_SUFFIX
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(CHAT_MESSAGE_DDL)
        conn.executemany(
            "INSERT INTO chat_message (id, session_id, request_id, role, token_info, model_info, gmt_create) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return db


def test_cn_fixture_db_matches_session_summary(monkeypatch):
    parser = _parser(monkeypatch, CN_ROOT)
    entries = parser.collect(None, None)

    assert len(entries) == 60
    assert sum(e["input"] for e in entries) == 2_319_522
    assert sum(e["output"] for e in entries) == 10_808
    # cache is 0 on every row, so input == prompt and cacheRead is 0.
    assert all(e["cacheRead"] == 0 and e["cacheWrite"] == 0 for e in entries)
    assert all(e["model"] == "auto" for e in entries)
    assert all(e["cost"] == 0.0 for e in entries)
    assert len({e["entry_id"] for e in entries}) == 60
    assert all(e["source"] == "qoder" for e in entries)

    # Timestamps are the raw gmt_create epoch ms, and every row is inside
    # the 2026-06-16 session window.
    days = {datetime.fromtimestamp(e["timestamp"] / 1000, timezone.utc).date() for e in entries}
    assert days == {datetime(2026, 6, 16).date()}
    in_window = parser.collect(
        datetime(2026, 6, 16, tzinfo=timezone.utc),
        datetime(2026, 6, 17, tzinfo=timezone.utc),
    )
    out_of_window = parser.collect(
        datetime(2026, 6, 17, tzinfo=timezone.utc),
        datetime(2026, 6, 18, tzinfo=timezone.utc),
    )
    assert len(in_window) == 60
    assert out_of_window == []


def test_intl_fixture_db_maps_model_info(monkeypatch):
    parser = _parser(monkeypatch, INTL_ROOT)
    entries = parser.collect(None, None)

    # The user row carries no token_info and is filtered out.
    assert len(entries) == 1
    e = entries[0]
    assert e["model"] == "auto"
    assert e["input"] == 17_553
    assert e["output"] == 115
    assert e["cacheRead"] == 0
    assert e["entry_id"] == "qoder:39f8185a-ac69-40ee-998c-b3d19d60239c"
    assert e["timestamp"] == 1_787_314_430_153
    assert e["cost"] == 0.0


def test_cached_slice_is_included_in_prompt(monkeypatch, tmp_path):
    root = tmp_path / "qoder"
    _make_db(
        root,
        [
            # cached 400 is a slice of prompt 1000
            ("m1", "s1", "r1", "assistant",
             json.dumps({"prompt_tokens": 1000, "completion_tokens": 50, "cached_tokens": 400}),
             json.dumps({"model_key": "auto"}), 1_700_000_000_000),
            # torn row: cached above prompt clamps to prompt
            ("m2", "s1", "r2", "assistant",
             json.dumps({"prompt_tokens": 100, "completion_tokens": 10, "cached_tokens": 300}),
             "", 1_700_000_001_000),
            # malformed and empty token_info are skipped
            ("m3", "s1", "r3", "assistant", "not-json", "", 1_700_000_002_000),
            ("m4", "s1", "r4", "user", "", "", 1_700_000_003_000),
            # fully zero rows are skipped
            ("m5", "s1", "r5", "tool",
             json.dumps({"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}),
             "", 1_700_000_004_000),
        ],
    )
    entries = _parser(monkeypatch, root).collect(None, None)
    by_id = {e["entry_id"]: e for e in entries}
    assert set(by_id) == {"qoder:m1", "qoder:m2"}
    assert by_id["qoder:m1"]["input"] == 600
    assert by_id["qoder:m1"]["cacheRead"] == 400
    assert by_id["qoder:m1"]["output"] == 50
    assert by_id["qoder:m2"]["input"] == 0
    assert by_id["qoder:m2"]["cacheRead"] == 100


def test_missing_db_and_missing_table_are_empty_success(monkeypatch, tmp_path):
    # No DB at all.
    empty_root = tmp_path / "absent"
    empty_root.mkdir()
    assert _parser(monkeypatch, empty_root).collect(None, None) == []

    # DB without the chat_message table.
    other_root = tmp_path / "other"
    db = other_root / DB_SUFFIX
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE chat_session (session_id varchar(64) primary key)")
    conn.commit()
    conn.close()
    assert _parser(monkeypatch, other_root).collect(None, None) == []


def test_file_signatures_include_wal(monkeypatch, tmp_path):
    root = tmp_path / "qoder"
    db = _make_db(root, [
        ("m1", "s1", "r1", "assistant",
         json.dumps({"prompt_tokens": 10, "completion_tokens": 1, "cached_tokens": 0}),
         "", 1_700_000_000_000),
    ])
    (db.parent / (db.name + "-wal")).write_bytes(b"wal")
    parser = _parser(monkeypatch, root)
    sigs = parser._file_signatures()
    assert tuple(s[0] for s in sigs) == (str(db), str(db) + "-wal")
    assert all(s[1] is not None for s in sigs)
    (db.parent / (db.name + "-wal")).unlink()
    sigs = parser._file_signatures()
    assert sigs[0][0] == str(db) and sigs[0][1] is not None
    assert sigs[1] == (str(db) + "-wal", None, None)
    # an absent WAL is a stable state: same signature twice
    assert parser._file_signatures() == sigs


def test_mid_copy_wal_disappearance_retries(monkeypatch, tmp_path):
    """The WAL vanishes between the pre- and post-copy signature checks.

    The first attempt must be dropped as a generation change and the retry
    must succeed (shared snapshot contract with ZCode).
    """
    import tokdash.sources.coding_tools as coding_tools

    root = tmp_path / "qoder"
    db = _make_db(root, [
        ("m1", "s1", "r1", "assistant",
         json.dumps({"prompt_tokens": 10, "completion_tokens": 1, "cached_tokens": 0}),
         "", 1_700_000_000_000),
    ])
    real = coding_tools.zcode_snapshot_signatures
    wal_sig = (str(db) + "-wal", 1, 1)

    def flaky(db_path):
        out = real(db_path)
        if calls["n"] == 1:
            # first attempt: pre-copy signature claims a WAL that the
            # post-copy check no longer sees
            calls["n"] += 1
            return out + (wal_sig,)
        calls["n"] += 1
        return out

    calls = {"n": 0}
    monkeypatch.setattr(coding_tools, "zcode_snapshot_signatures", flaky)
    parser = _parser(monkeypatch, root)
    entries = parser.collect(None, None)
    assert len(entries) == 1
    assert entries[0]["entry_id"] == "qoder:m1"
    # a second collect is served from the (now stable) cache
    assert len(parser.collect(None, None)) == 1


def test_coexisting_brand_dirs_select_the_priority_winner(monkeypatch, tmp_path):
    appdata = tmp_path / "appdata"
    cn_root = appdata / "QoderCN"
    intl_root = appdata / "Qoder"
    _make_db(cn_root, [
        ("m-cn", "s1", "r1", "assistant",
         json.dumps({"prompt_tokens": 111, "completion_tokens": 1, "cached_tokens": 0}),
         "", 1_700_000_000_000),
    ])
    _make_db(intl_root, [
        ("m-intl", "s2", "r2", "assistant",
         json.dumps({"prompt_tokens": 222, "completion_tokens": 2, "cached_tokens": 0}),
         json.dumps({"model_key": "auto"}), 1_700_000_000_000),
    ])
    monkeypatch.delenv("QODER_IDE_DATA_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(osinfo, "os_kind", lambda: "windows")
    parser = QoderIdeParser(PricingDatabase())
    entries = parser.collect(None, None)
    # Windows brand priority: only the QoderCN rows are counted.
    assert [e["entry_id"] for e in entries] == ["qoder:m-cn"]


def test_no_candidates_is_empty_success(monkeypatch, tmp_path):
    monkeypatch.delenv("QODER_IDE_DATA_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "absent-appdata"))
    monkeypatch.setattr(osinfo, "os_kind", lambda: "windows")
    parser = QoderIdeParser(PricingDatabase())
    assert parser.db_path is None
    assert parser.collect(None, None) == []
