"""Tests for KiloCodeParser (Kilo CLI/extension) and the K1 query-cache
isolation regression: with both Kilo and OpenCode installed, each source
must serve only its own rows for the same date window."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from tokdash import clientpaths
from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import KiloCodeParser, OpenCodeParser

TS_COLD = 1_700_000_000_000
TS_WARM = 1_700_010_000_000


def _make_message_db(path: Path, rows) -> Path:
    """One OpenCode-shaped message table.

    rows: (time_created_ms, data_dict) pairs; data may omit tokens or carry
    tokens: None (user rows) to exercise the skip path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)"
    )
    for i, (ts, data) in enumerate(rows):
        conn.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
            (f"msg-{i}", "sess-1", ts, ts, json.dumps(data)),
        )
    conn.commit()
    conn.close()
    return path


def _assistant_data(model="kilo-test-model", provider="test-provider", **tokens) -> dict:
    data = {"modelID": model, "providerID": provider}
    if "none" not in tokens:
        data["tokens"] = tokens
    else:
        data["tokens"] = None
    return data


def _fresh(tmp_path: Path):
    """Both tools sharing one XDG data root under tmp_path."""
    (tmp_path / "opencode").mkdir(parents=True, exist_ok=True)
    (tmp_path / "kilo").mkdir(parents=True, exist_ok=True)
    OpenCodeParser._query_cache.clear()
    OpenCodeParser._query_cache_sig = ()
    KiloCodeParser._query_cache.clear()
    KiloCodeParser._query_cache_sig = ()
    return (
        OpenCodeParser(PricingDatabase()),
        KiloCodeParser(PricingDatabase()),
    )


def test_kilo_parser_reads_message_rows(monkeypatch, tmp_path):
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    _make_message_db(
        xdg / "kilo" / "kilo.db",
        [
            (TS_COLD, _assistant_data(input=14579, output=37, cache={"read": 0, "write": 0})),
            (TS_WARM, _assistant_data(input=467, output=58, cache={"read": 14112, "write": 0})),
            (TS_WARM + 1, _assistant_data(**{"none": 1})),  # user row: tokens None
        ],
    )

    _opencode, kilo = _fresh(tmp_path)
    entries = kilo.collect(None, None)

    assert len(entries) == 2
    assert all(e["source"] == "kilocode" for e in entries)
    cold, warm = entries
    assert cold["model"] == "kilo-test-model"
    assert cold["provider"] == "test-provider"
    assert (cold["input"], cold["output"], cold["cacheRead"], cold["cacheWrite"]) == (14579, 37, 0, 0)
    assert cold["timestamp"] == TS_COLD
    # Cache semantics: pass-through (467 + 14112 = 14579 = the cold full input).
    assert (warm["input"], warm["cacheRead"], warm["output"]) == (467, 14112, 58)


def test_kilo_query_cache_isolated_from_opencode(monkeypatch, tmp_path):
    """K1: the inherited _query_cache is a shared dict object; without the
    redeclared ClassVars, a Kilo query would poison OpenCode's cache for the
    same window (and vice versa)."""
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    _make_message_db(
        xdg / "kilo" / "kilo.db",
        [(TS_COLD, _assistant_data(model="kilo-model", input=10, output=1, cache={"read": 0, "write": 0}))],
    )
    _make_message_db(
        xdg / "opencode" / "opencode.db",
        [(TS_COLD, _assistant_data(model="oc-model", input=20, output=2, cache={"read": 0, "write": 0}))],
    )

    opencode, kilo = _fresh(tmp_path)

    kilo_rows = kilo.collect(None, None)
    assert [e["model"] for e in kilo_rows] == ["kilo-model"]

    # Same window, other source: must see only its own row.
    opencode_rows = opencode.collect(None, None)
    assert [e["model"] for e in opencode_rows] == ["oc-model"]

    # Cached second Kilo query still serves Kilo rows, not OpenCode's.
    kilo_again = kilo.collect(None, None)
    assert [e["model"] for e in kilo_again] == ["kilo-model"]
    assert all(e["source"] == "kilocode" for e in kilo_again)


def test_kilo_db_paths_prefers_kilo_named(monkeypatch, tmp_path):
    xdg = tmp_path / "xdg"
    kilo_root = xdg / "kilo"
    kilo_root.mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))

    # No DBs at all.
    assert clientpaths.kilo_db_paths() == []

    # Legacy opencode*.db is read while no kilo-named file exists.
    legacy = kilo_root / "opencode-dev.db"
    legacy.write_bytes(b"sqlite")
    assert clientpaths.kilo_db_paths() == [legacy]

    # A stable-channel install shadows the legacy file (migrated install).
    stable = kilo_root / "kilo.db"
    stable.write_bytes(b"sqlite")
    assert clientpaths.kilo_db_paths() == [stable]

    # Dev-channel DBs join the stable one, canonical first.
    dev = kilo_root / "kilo-dev.db"
    dev.write_bytes(b"sqlite")
    assert clientpaths.kilo_db_paths() == [stable, dev]


def test_kilo_merges_channel_dbs(monkeypatch, tmp_path):
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    _make_message_db(
        xdg / "kilo" / "kilo.db",
        [(TS_COLD, _assistant_data(model="stable-model", input=1, output=1, cache={"read": 0, "write": 0}))],
    )
    _make_message_db(
        xdg / "kilo" / "kilo-dev.db",
        [(TS_WARM, _assistant_data(model="dev-model", input=2, output=2, cache={"read": 0, "write": 0}))],
    )

    _opencode, kilo = _fresh(tmp_path)
    entries = kilo.collect(None, None)
    assert sorted(e["model"] for e in entries) == ["dev-model", "stable-model"]


def test_kilo_signature_covers_wal_and_shm(monkeypatch, tmp_path):
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    db = _make_message_db(xdg / "kilo" / "kilo.db", [])
    (xdg / "kilo" / "kilo.db-wal").write_bytes(b"wal")
    (xdg / "kilo" / "kilo.db-shm").write_bytes(b"shm")

    _opencode, kilo = _fresh(tmp_path)
    paths = {sig[0] for sig in kilo._file_signatures()}
    assert paths == {str(db), str(db) + "-wal", str(db) + "-shm"}


def test_kilo_window_filters_outside_rows(monkeypatch, tmp_path):
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    _make_message_db(
        xdg / "kilo" / "kilo.db",
        [
            (TS_COLD, _assistant_data(model="inside", input=1, output=1, cache={"read": 0, "write": 0})),
            (TS_WARM, _assistant_data(model="outside", input=2, output=2, cache={"read": 0, "write": 0})),
        ],
    )

    _opencode, kilo = _fresh(tmp_path)
    since = datetime.fromtimestamp(TS_COLD / 1000, tz=timezone.utc)
    until = datetime.fromtimestamp(TS_WARM / 1000, tz=timezone.utc)
    entries = kilo.collect(since, until)
    assert [e["model"] for e in entries] == ["inside"]
