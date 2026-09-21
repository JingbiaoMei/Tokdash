"""Tests for GooseParser: one SQLite sessions.db, one entry per usage_ledger
row, the session aggregates that must never be read, seconds timestamps, and
the empty cases that must not be collapsed into one.

The DDL below is copied from the captured Goose v1.51.0 database (schema
version 16) rather than written from memory: AUTOINCREMENT on usage_ledger.id
and ON DELETE CASCADE to sessions are both load-bearing for the entry-key and
deletion rules, and a test seeded against a plain INTEGER PRIMARY KEY would
prove nothing about either.
"""
import sqlite3
from pathlib import Path

from tokdash import clientpaths
from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import (
    BaseParser,
    CodingToolsUsageTracker,
    GooseParser,
    GooseSchemaError,
    _goose_ts_to_ms,
    _sig_cache,
)

T0 = 1_700_000_000  # epoch seconds, the unit created_timestamp uses

SESSIONS_DDL = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    user_set_name BOOLEAN DEFAULT FALSE,
    session_type TEXT NOT NULL DEFAULT 'user',
    working_dir TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    extension_data TEXT DEFAULT '{}',
    total_tokens INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    accumulated_total_tokens INTEGER,
    accumulated_input_tokens INTEGER,
    accumulated_output_tokens INTEGER,
    accumulated_cache_read_tokens INTEGER,
    accumulated_cache_write_tokens INTEGER,
    accumulated_cost REAL,
    schedule_id TEXT,
    recipe_json TEXT,
    user_recipe_values_json TEXT,
    provider_name TEXT,
    model_config_json TEXT,
    goose_mode TEXT NOT NULL DEFAULT 'auto',
    archived_at TIMESTAMP,
    project_id TEXT,
    parent_session_id TEXT
)
"""

LEDGER_DDL = """
CREATE TABLE usage_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    created_timestamp INTEGER NOT NULL,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    cost REAL,
    cost_source TEXT,
    is_compaction INTEGER DEFAULT 0
)
"""

MESSAGES_DDL = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content_json TEXT NOT NULL,
    created_timestamp INTEGER NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    tokens INTEGER,
    metadata_json TEXT
)
"""


def _make_db(db, *, with_ledger=True, with_sessions=True, sessions=(), ledger=()):
    """sessions: (id, name, session_type, working_dir, total, input, output,
                  cache_read, accumulated_input, accumulated_output)
    ledger: (id, session_id, created_timestamp, model, input, output, total,
             cache_read, cache_write, cost, is_compaction)"""
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    try:
        if with_sessions:
            conn.execute(SESSIONS_DDL)
        if with_ledger:
            conn.execute(LEDGER_DDL)
        conn.execute(MESSAGES_DDL)
        for sid, name, stype, wd, total, inp, out, cache_r, acc_i, acc_o in sessions:
            conn.execute(
                "INSERT INTO sessions (id, name, session_type, working_dir, "
                "total_tokens, input_tokens, output_tokens, cache_read_tokens, "
                "accumulated_input_tokens, accumulated_output_tokens) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, name, stype, wd, total, inp, out, cache_r, acc_i, acc_o),
            )
        for row in ledger:
            conn.execute(
                "INSERT INTO usage_ledger (id, session_id, created_timestamp, "
                "model, input_tokens, output_tokens, total_tokens, "
                "cache_read_tokens, cache_write_tokens, cost, is_compaction) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
        conn.commit()
    finally:
        conn.close()
    return db


def _parser(monkeypatch, *, root=None, xdg=None, home=None):
    """A parser against a chosen GOOSE_PATH_ROOT / XDG_DATA_HOME / home.

    The signature and entry caches are process-global, so a test that leaves
    one behind hands the next test a stale view of a database that has since
    been rewritten.
    """
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    monkeypatch.delenv("GOOSE_PATH_ROOT", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    if home is not None:
        monkeypatch.setattr(Path, "home", lambda: home)
    if root is not None:
        monkeypatch.setenv("GOOSE_PATH_ROOT", str(root))
    if xdg is not None:
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    return GooseParser(PricingDatabase())


# --- path resolution ---------------------------------------------------------


def test_goose_default_path_is_the_xdg_local_share(tmp_path, monkeypatch):
    home = tmp_path / "home"
    parser_env = _parser(monkeypatch, home=home)
    db = home / ".local" / "share" / "goose" / "sessions" / "sessions.db"

    assert parser_env.db_path is None  # absent file -> nothing to read
    _make_db(db, ledger=[])
    assert clientpaths.goose_sessions_db() == db


def test_goose_root_override_uses_data_without_the_goose_segment(tmp_path, monkeypatch):
    """GOOSE_PATH_ROOT=/tmp/gpr resolves to /tmp/gpr/data/sessions/sessions.db.

    The captured ``goose info`` output puts no ``goose`` segment under the
    root's ``data`` dir, unlike the XDG layout, so a reader that joined
    ``goose`` there would look in a directory Goose never writes.
    """
    root = tmp_path / "gpr"
    root.mkdir()
    assert _parser(monkeypatch, root=root).db_path is None

    db = root / "data" / "sessions" / "sessions.db"
    _make_db(db, ledger=[])
    assert clientpaths.goose_sessions_db() == db


def test_goose_xdg_data_home_wins_over_the_home_default(tmp_path, monkeypatch):
    home = tmp_path / "home"
    xdg = tmp_path / "xdgdata"
    _parser(monkeypatch, xdg=xdg, home=home)

    db = xdg / "goose" / "sessions" / "sessions.db"
    _make_db(db, ledger=[])
    assert clientpaths.goose_sessions_db() == db
    assert not (home / ".local" / "share" / "goose").exists()


def test_goose_relative_xdg_data_home_is_ignored(tmp_path, monkeypatch):
    """A relative XDG_DATA_HOME is invalid and must not resolve against cwd."""
    home = tmp_path / "home"
    _parser(monkeypatch, xdg="relative-data", home=home)

    db = home / ".local" / "share" / "goose" / "sessions" / "sessions.db"
    _make_db(db, ledger=[])
    assert clientpaths.goose_sessions_db() == db
    assert not (tmp_path / "relative-data").exists()


# --- token accounting --------------------------------------------------------


def test_goose_one_entry_per_ledger_row(monkeypatch, tmp_path):
    root = tmp_path / "gpr"
    _make_db(
        root / "data" / "sessions" / "sessions.db",
        sessions=[("s1", "CLI Session", "user", "/tmp/w", None, None, None, None, None, None)],
        ledger=[
            (1, "s1", T0, "qwen3.8-flash-next", 8310, 24, 8334, 0, None, None, 0),
            (2, "s1", T0 + 4, "qwen3.8-flash-next", 8527, 132, 8659, 8320, None, None, 0),
        ],
    )
    entries = _parser(monkeypatch, root=root).collect(None, None)

    assert len(entries) == 2
    first, second = entries
    assert (first["input"], first["output"], first["cacheRead"], first["cacheWrite"]) == (8310, 24, 0, 0)
    # input_tokens is INCLUSIVE of the cached slice: 8527 - 8320 is the fresh part.
    assert (second["input"], second["output"], second["cacheRead"], second["cacheWrite"]) == (207, 132, 8320, 0)
    # Seconds -> ms at the boundary, so a day bucket is the request's own second.
    assert [e["timestamp"] for e in entries] == [T0 * 1000, (T0 + 4) * 1000]
    assert [e["entry_id"] for e in entries] == [f"goose:s1:1:{T0}", f"goose:s1:2:{T0 + 4}"]
    assert {e["source"] for e in entries} == {"goose"}
    assert entries[0]["reasoning"] == 0  # Goose persists no reasoning split
    # NULL cache_write_tokens and a NULL cost are the normal shape, not gaps.
    assert entries[0]["cost"] == PricingDatabase().get_cost(
        "qwen3.8-flash-next", 8310, 24, 0, 0
    )
    assert entries[0]["_billing"] == {
        "kind": "pricing",
        "models": ["qwen3.8-flash-next"],
        "input": 8310,
        "output": 24,
        "cache_read": 0,
        "cache_write": 0,
    }


def test_goose_displayed_total_equals_billed_total(monkeypatch, tmp_path):
    """The cached slice moves out of input but never leaves the totals."""
    root = tmp_path / "gpr"
    _make_db(
        root / "data" / "sessions" / "sessions.db",
        sessions=[("s1", "n", "user", "/w", None, None, None, None, None, None)],
        ledger=[(1, "s1", T0, "gpt-5.2", 8527, 132, 8659, 8320, 11, None, 0)],
    )
    entry = _parser(monkeypatch, root=root).collect(None, None)[0]

    # The ledger total is gross input + output; splitting the cache read out of
    # input keeps it: 207 + 8320 + 132 = 8659.
    assert entry["input"] + entry["cacheRead"] + entry["output"] == 8659
    assert entry["cacheWrite"] == 11


def test_goose_session_aggregates_are_never_read(monkeypatch, tmp_path):
    """accumulated_* duplicates the ledger and total_tokens is one snapshot.

    Seeded so each session-column reading gives a DIFFERENT wrong answer:
    summing accumulated_* (10x here) overcounts, summing total_tokens
    undercounts every multi-request session. Only the ledger may arrive.
    """
    root = tmp_path / "gpr"
    _make_db(
        root / "data" / "sessions" / "sessions.db",
        sessions=[("s1", "n", "user", "/w", 8659, 8527, 132, 8320, 168540, 3040)],
        ledger=[
            (1, "s1", T0, "m", 8327, 172, 8499, 0, None, None, 0),
            (2, "s1", T0 + 4, "m", 8527, 132, 8659, 8320, None, None, 0),
        ],
    )
    entries = _parser(monkeypatch, root=root).collect(None, None)

    assert len(entries) == 2
    assert sum(e["input"] + e["cacheRead"] for e in entries) == 8327 + 8527
    assert sum(e["output"] for e in entries) == 172 + 132


def test_goose_keep_guard_drops_tokenless_rows(monkeypatch, tmp_path):
    root = tmp_path / "gpr"
    _make_db(
        root / "data" / "sessions" / "sessions.db",
        sessions=[("s1", "n", "user", "/w", None, None, None, None, None, None)],
        ledger=[
            (1, "s1", T0, "m", 0, 0, 0, 0, None, None, 0),
            (2, "s1", T0 + 1, "m", None, None, None, None, None, None, 0),
            (3, "s1", T0 + 2, "m", 10, 0, 10, 0, None, None, 0),
        ],
    )
    entries = _parser(monkeypatch, root=root).collect(None, None)

    assert [e["entry_id"] for e in entries] == [f"goose:s1:3:{T0 + 2}"]


def test_goose_compaction_rows_are_billed(monkeypatch, tmp_path):
    """is_compaction is a Sessions label, not a reason to drop a real request."""
    root = tmp_path / "gpr"
    _make_db(
        root / "data" / "sessions" / "sessions.db",
        sessions=[("s1", "n", "user", "/w", None, None, None, None, None, None)],
        ledger=[(1, "s1", T0, "m", 100, 20, 120, 0, None, None, 1)],
    )

    assert len(_parser(monkeypatch, root=root).collect(None, None)) == 1


def test_goose_null_model_keeps_tokens_and_prices_zero(monkeypatch, tmp_path):
    root = tmp_path / "gpr"
    _make_db(
        root / "data" / "sessions" / "sessions.db",
        sessions=[("s1", "n", "user", "/w", None, None, None, None, None, None)],
        ledger=[(1, "s1", T0, None, 500, 20, 520, 0, None, None, 0)],
    )
    entry = _parser(monkeypatch, root=root).collect(None, None)[0]

    assert entry["model"] == "unknown"
    assert (entry["input"], entry["output"]) == (500, 20)
    assert entry["cost"] == 0.0


# --- entry keys --------------------------------------------------------------


def test_goose_deleting_a_session_removes_its_ledger_rows(monkeypatch, tmp_path):
    """The ON DELETE CASCADE the deletion story rests on.

    Goose has no tombstone: the ledger rows go with the session row, so the
    next whole-source sync is the thing that takes their usage out of Tokdash.
    """
    root = tmp_path / "gpr"
    db = _make_db(
        root / "data" / "sessions" / "sessions.db",
        sessions=[
            ("s-keep", "n", "user", "/w", None, None, None, None, None, None),
            ("s-gone", "n", "user", "/w", None, None, None, None, None, None),
        ],
        ledger=[
            (1, "s-keep", T0, "m", 100, 10, 110, 0, None, None, 0),
            (2, "s-gone", T0 + 3, "m", 200, 20, 220, 0, None, None, 0),
        ],
    )
    parser = _parser(monkeypatch, root=root)
    assert len(parser.collect(None, None)) == 2

    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM sessions WHERE id = 's-gone'")
    conn.commit()
    conn.close()
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    assert [e["entry_id"] for e in parser.collect(None, None)] == [f"goose:s-keep:1:{T0}"]


def test_goose_recreated_database_cannot_reuse_an_entry_key(monkeypatch, tmp_path):
    """The case AUTOINCREMENT does not cover, and the reason the key is composite.

    AUTOINCREMENT keeps ids unique inside one live database, so the two
    generations below cannot be seeded side by side; a recreated sessions.db
    restarts the sequence at 1, though, and the store is unique on
    (source, entry_key). Seeded with the SAME id and the SAME second, only the
    session id can tell generation two's row from the one Tokdash already
    stored, which is exactly what a bare row id would have collided on.
    """
    root = tmp_path / "gpr"
    db = _make_db(
        root / "data" / "sessions" / "sessions.db",
        sessions=[("s-old", "n", "user", "/w", None, None, None, None, None, None)],
        ledger=[(1, "s-old", T0, "m", 100, 10, 110, 0, None, None, 0)],
    )
    parser = _parser(monkeypatch, root=root)
    first = parser.collect(None, None)[0]["entry_id"]

    db.unlink()
    _make_db(
        db,
        sessions=[("s-new", "n", "user", "/w", None, None, None, None, None, None)],
        ledger=[(1, "s-new", T0, "m", 200, 20, 220, 0, None, None, 0)],
    )
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    second = parser.collect(None, None)[0]["entry_id"]

    assert (first, second) == (f"goose:s-old:1:{T0}", f"goose:s-new:1:{T0}")


# --- the empty cases, which are not one case ---------------------------------


def test_goose_absent_database_is_an_empty_success(monkeypatch, tmp_path):
    (tmp_path / "gpr").mkdir()
    parser = _parser(monkeypatch, root=tmp_path / "gpr")

    assert parser.db_path is None
    assert parser._file_signatures() == ()
    assert parser.collect(None, None) == []


def test_goose_zero_byte_database_is_an_empty_success(monkeypatch, tmp_path):
    root = tmp_path / "gpr"
    db = root / "data" / "sessions" / "sessions.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"")  # present, but nothing Goose has ever written
    parser = _parser(monkeypatch, root=root)

    assert parser.db_path == db
    assert parser.collect(None, None) == []


def test_goose_database_without_goose_tables_is_an_empty_success(monkeypatch, tmp_path):
    root = tmp_path / "gpr"
    db = root / "data" / "sessions" / "sessions.db"
    db.parent.mkdir(parents=True)
    sqlite3.connect(db).close()  # a valid SQLite file with zero tables
    parser = _parser(monkeypatch, root=root)

    assert parser.collect(None, None) == []


def test_goose_missing_ledger_on_a_goose_database_raises(monkeypatch, tmp_path):
    """A Goose database whose usage table went missing is a failure, not a zero.

    sync_source() records no signature for an empty parse, so returning [] here
    would re-probe on every collect and keep serving the rows the store already
    had, with nothing anywhere saying the table disappeared under an upgrade.
    """
    root = tmp_path / "gpr"
    _make_db(
        root / "data" / "sessions" / "sessions.db",
        with_ledger=False,
        sessions=[("s1", "n", "user", "/w", 100, 90, 10, 0, 90, 10)],
    )
    parser = _parser(monkeypatch, root=root)

    try:
        parser._parse_all()
    except GooseSchemaError:
        pass
    else:
        raise AssertionError("a Goose database with no usage_ledger must not read as empty")


def test_goose_failed_read_is_not_cached(monkeypatch, tmp_path):
    """A torn file reads as a failure now and as data once Goose writes again."""
    root = tmp_path / "gpr"
    db = root / "data" / "sessions" / "sessions.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"definitely not a database")
    parser = _parser(monkeypatch, root=root)

    assert parser.db_path == db
    try:
        parser.collect(None, None)
    except Exception:
        pass
    else:
        raise AssertionError("a corrupt database must not read as an empty success")
    # The failed read must not be parked in the cache under this signature.
    assert "goose" not in BaseParser._entry_cache

    # Goose replaces the torn file, and the very next collect reads it.
    db.unlink()
    _make_db(
        db,
        sessions=[("s1", "n", "user", "/w", None, None, None, None, None, None)],
        ledger=[(1, "s1", T0, "m", 10, 5, 15, 0, None, None, 0)],
    )
    _sig_cache.clear()
    assert [e["entry_id"] for e in parser.collect(None, None)] == [f"goose:s1:1:{T0}"]


# --- signatures --------------------------------------------------------------


def test_goose_signature_is_one_entry_per_db(monkeypatch, tmp_path):
    root = tmp_path / "gpr"
    db = _make_db(
        root / "data" / "sessions" / "sessions.db",
        sessions=[("s1", "n", "user", "/w", None, None, None, None, None, None)],
        ledger=[(1, "s1", T0, "m", 10, 5, 15, 0, None, None, 0)],
    )
    parser = _parser(monkeypatch, root=root)

    sigs = parser._file_signatures()
    assert len(sigs) == 1
    assert sigs[0][0] == str(db)

    # A WAL sidecar folds into the same entry rather than adding one; a second
    # entry would snapshot and reparse the whole database twice per sync.
    Path(str(db) + "-wal").write_bytes(b"wal")
    _sig_cache.clear()
    after = parser._file_signatures()
    assert len(after) == 1
    assert after[0][0] == str(db)


def test_goose_ts_to_ms_guards():
    assert _goose_ts_to_ms(T0) == T0 * 1000
    assert _goose_ts_to_ms(T0 * 1000) == T0 * 1000  # already milliseconds
    assert _goose_ts_to_ms(0) is None
    assert _goose_ts_to_ms(-1) is None
    assert _goose_ts_to_ms(None) is None
    assert _goose_ts_to_ms("nope") is None


def test_goose_registered_as_source_replace():
    parser = CodingToolsUsageTracker().parsers["goose"]

    assert isinstance(parser, GooseParser)
    assert parser.sync_capability.mode == "source_replace"
    assert parser.persistent_parser_version == 1
