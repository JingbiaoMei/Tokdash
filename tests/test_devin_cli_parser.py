"""Tests for DevinParser: the SQLite store Devin CLI keeps in its own data dir.

The store is WAL-mode SQLite and the billable record is JSON inside
``message_nodes.chat_message``, so these fixtures build that shape from the
schema compiled into v3000.10.31 (docs/local/20260921_devin_cli_support/
evidence/03-binary-schema-3000.10.31.txt) with invented numbers. Nothing here
came from a real Devin session, because no real store exists yet; the
assumptions that still need a capture are named in the test that holds them.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tokdash import clientpaths
from tokdash.pricing import PricingDatabase
from tokdash.sources import coding_tools as ct
from tokdash.sources.coding_tools import DevinParser

T0_MS = 1_760_000_000_000  # 2025-10-09, above the ms/s threshold
T0_S = T0_MS // 1000

SESSIONS_DDL = """
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  working_directory TEXT NOT NULL,
  backend_type TEXT NOT NULL,
  model TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  last_activity_at INTEGER NOT NULL,
  workspace_dirs TEXT,
  agent_mode TEXT,
  hidden INTEGER NOT NULL DEFAULT 0,
  title TEXT,
  main_chain_id INTEGER,
  metadata TEXT,
  shell_last_seen_index INTEGER DEFAULT 0,
  cogs_json TEXT
)
"""

NODES_DDL = """
CREATE TABLE message_nodes (
  row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  node_id INTEGER NOT NULL,
  parent_node_id INTEGER,
  chat_message TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  metadata TEXT,
  UNIQUE(session_id, node_id)
)
"""


def node_json(msg_id, role="assistant", usage=None, model=None, tool_noise=None):
    node = {"id": msg_id, "role": role, "message": {"content": f"placeholder {role}"}}
    if usage is not None:
        node["usage"] = usage
    if model is not None:
        node["model"] = model
    if tool_noise is not None:
        node["message"]["tool_result"] = tool_noise
    return node


def make_store(
    root: Path,
    *,
    db_name="sessions.db",
    sessions=(),
    nodes=(),
    wal=False,
    with_sessions_table=True,
):
    """sessions: (id, model, hidden). nodes: (session_id, node_id, payload, ts)."""
    root.mkdir(parents=True, exist_ok=True)
    db = root / db_name
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(db)
    try:
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        if with_sessions_table:
            conn.execute(SESSIONS_DDL)
            for sid, model, hidden in sessions:
                conn.execute(
                    "INSERT INTO sessions (id, working_directory, backend_type, model,"
                    " created_at, last_activity_at, hidden) VALUES (?,?,?,?,?,?,?)",
                    (sid, "/tmp/repo", "local", model, T0_MS, T0_MS, hidden),
                )
        conn.execute(NODES_DDL)
        for session_id, node_id, payload, ts in nodes:
            conn.execute(
                "INSERT INTO message_nodes (session_id, node_id, chat_message, created_at)"
                " VALUES (?,?,?,?)",
                (session_id, node_id, json.dumps(payload), ts),
            )
        conn.commit()
    finally:
        conn.close()
    return db


def parser_for(*_args, **_kwargs):
    return DevinParser(PricingDatabase())


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("DEVIN_CLI_DATA_DIRS", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(clientpaths.osinfo, "os_kind", lambda: "linux")
    DevinParser._query_cache.clear()
    DevinParser._query_cache_sig = ()
    yield


# --- path resolution --------------------------------------------------------


def test_default_linux_store_is_found(tmp_path):
    store = tmp_path / ".local" / "share" / "devin" / "cli"
    make_store(store)
    assert clientpaths.devin_db_paths() == [store / "sessions.db"]


def test_xdg_data_home_is_honoured(tmp_path, monkeypatch):
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    store = xdg / "devin" / "cli"
    make_store(store)
    assert clientpaths.devin_db_paths() == [store / "sessions.db"]


def test_relative_xdg_data_home_is_ignored(tmp_path, monkeypatch):
    # Base Directory spec: relative XDG_DATA_HOME is invalid. Resolving it
    # against the working directory would read an unrelated project tree.
    monkeypatch.setenv("XDG_DATA_HOME", "relative-data")
    other = tmp_path / ".local" / "share" / "devin" / "cli"
    make_store(other)
    assert clientpaths.devin_db_paths() == [other / "sessions.db"]


def test_empty_data_dir_is_not_usage(tmp_path):
    (tmp_path / ".local" / "share" / "devin" / "cli").mkdir(parents=True)
    assert clientpaths.devin_db_paths() == []


def test_env_override_adds_a_relocated_store(tmp_path, monkeypatch):
    primary = tmp_path / ".local" / "share" / "devin" / "cli"
    moved = tmp_path / "elsewhere" / "devin-cli"
    make_store(primary)
    make_store(moved)
    monkeypatch.setenv("DEVIN_CLI_DATA_DIRS", str(moved))
    paths = clientpaths.devin_db_paths()
    # The override ADDS, so the normal store is never lost by relocating.
    assert paths == [moved / "sessions.db", primary / "sessions.db"]


def test_wsl_glob_finds_the_windows_host_store(tmp_path, monkeypatch):
    guest = tmp_path / ".local" / "share" / "devin" / "cli"
    host = tmp_path / "drvfs" / "Users" / "H1937" / "AppData" / "Roaming" / "devin" / "cli"
    make_store(guest)
    make_store(host)
    monkeypatch.setattr(clientpaths.osinfo, "os_kind", lambda: "wsl")
    monkeypatch.setattr(clientpaths, "_wsl_windows_root", lambda: tmp_path / "drvfs")
    paths = clientpaths.devin_db_paths()
    assert paths == [guest / "sessions.db", host / "sessions.db"]


def test_windows_uses_roaming_appdata(tmp_path, monkeypatch):
    monkeypatch.setattr(clientpaths.osinfo, "os_kind", lambda: "windows")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    store = tmp_path / "Roaming" / "devin" / "cli"
    make_store(store)
    assert clientpaths.devin_db_paths() == [store / "sessions.db"]


def test_sessions_db_wins_over_the_legacy_name(tmp_path):
    store = tmp_path / ".local" / "share" / "devin" / "cli"
    make_store(store, db_name="sessions.db")
    make_store(store, db_name="cli_sessions.db")
    # Both read would count the same history twice.
    assert clientpaths.devin_db_paths() == [store / "sessions.db"]


def test_legacy_store_is_read_when_it_is_the_only_one(tmp_path):
    store = tmp_path / ".local" / "share" / "devin" / "cli"
    make_store(store, db_name="cli_sessions.db")
    assert clientpaths.devin_db_paths() == [store / "cli_sessions.db"]


# --- parsing ----------------------------------------------------------------


def _parser(tmp_path, monkeypatch, nodes, sessions=(("s1", "sonnet", 0),), **kwargs):
    store = tmp_path / ".local" / "share" / "devin" / "cli"
    make_store(store, sessions=list(sessions), nodes=list(nodes), **kwargs)
    monkeypatch.setenv("DEVIN_CLI_DATA_DIRS", str(store))
    return DevinParser(PricingDatabase())


def test_billable_nodes_become_one_entry_each(tmp_path, monkeypatch):
    parser = _parser(
        tmp_path,
        monkeypatch,
        [
            ("s1", 1, node_json("m1", role="user"), T0_MS),
            (
                "s1",
                2,
                node_json(
                    "m2",
                    usage={
                        "input_tokens": 1200,
                        "output_tokens": 180,
                        "cache_read_tokens": 900,
                        "cache_creation_tokens": 40,
                    },
                ),
                T0_MS + 1000,
            ),
        ],
    )
    entries = parser.collect()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["source"] == "devin"
    assert entry["input"] == 1200
    assert entry["output"] == 180
    assert entry["cacheRead"] == 900
    assert entry["cacheWrite"] == 40
    assert entry["timestamp"] == T0_MS + 1000
    assert entry["entry_id"] == "devin:s1:m2"


def test_cache_split_is_exclusive_and_documented(tmp_path, monkeypatch):
    """Q3 is capture-pending. The field names are the Anthropic usage names,
    where cache reads are a bucket beside input, so input is NOT reduced. If the
    capture shows Devin folds cache reads into input, this test is the one that
    flips, together with the parser docstring."""
    parser = _parser(
        tmp_path,
        monkeypatch,
        [
            (
                "s1",
                1,
                node_json(
                    "m1",
                    usage={"input_tokens": 1200, "output_tokens": 20, "cache_read_tokens": 900},
                ),
                T0_MS,
            )
        ],
    )
    entry = parser.collect()[0]
    assert (entry["input"], entry["cacheRead"]) == (1200, 900)
    assert entry["reasoning"] == 0  # no reasoning bucket exists upstream


def test_model_prefers_the_node_and_falls_back_to_the_session(tmp_path, monkeypatch):
    usage = {"input_tokens": 10, "output_tokens": 5}
    parser = _parser(
        tmp_path,
        monkeypatch,
        [
            ("s1", 1, node_json("m1", usage=usage, model="swe-1-6-fast"), T0_MS),
            ("s1", 2, node_json("m2", usage=usage), T0_MS + 1),
        ],
        sessions=(("s1", "sonnet", 0),),
    )
    models = {e["entry_id"]: e["model"] for e in parser.collect()}
    assert models == {"devin:s1:m1": "swe-1-6-fast", "devin:s1:m2": "sonnet"}


def test_zero_usage_records_are_skipped(tmp_path, monkeypatch):
    parser = _parser(
        tmp_path,
        monkeypatch,
        [
            ("s1", 1, node_json("m1", usage={"input_tokens": 0, "output_tokens": 0}), T0_MS),
            ("s1", 2, node_json("m2", role="user"), T0_MS + 1),
            ("s1", 3, {"id": "m3", "role": "assistant"}, T0_MS + 2),
        ],
    )
    assert parser.collect() == []


def test_usage_shaped_data_in_tool_output_is_not_billed(tmp_path, monkeypatch):
    noise = {"input_tokens": 999999, "output_tokens": 999999}
    parser = _parser(
        tmp_path,
        monkeypatch,
        [
            (
                "s1",
                1,
                node_json(
                    "m1",
                    usage={"input_tokens": 30, "output_tokens": 7},
                    tool_noise=noise,
                ),
                T0_MS,
            )
        ],
    )
    entry = parser.collect()[0]
    assert (entry["input"], entry["output"]) == (30, 7)


def test_usage_may_sit_at_the_root_of_the_record(tmp_path, monkeypatch):
    payload = {
        "id": "m1",
        "role": "assistant",
        "input_tokens": 11,
        "output_tokens": 12,
        "cache_read_tokens": 13,
        "cache_creation_tokens": 14,
    }
    parser = _parser(tmp_path, monkeypatch, [("s1", 1, payload, T0_MS)])
    entry = parser.collect()[0]
    assert (entry["input"], entry["output"], entry["cacheRead"], entry["cacheWrite"]) == (
        11,
        12,
        13,
        14,
    )


def test_hidden_helper_sessions_count(tmp_path, monkeypatch):
    """The summarizer is a real spend, in its own session row, so it is not a
    copy of a counted parent call. Q9 re-checks this in the capture."""
    usage = {"input_tokens": 100, "output_tokens": 5}
    parser = _parser(
        tmp_path,
        monkeypatch,
        [
            ("s1", 1, node_json("m1", usage=usage), T0_MS),
            ("s2", 1, node_json("m2", usage=usage), T0_MS + 1),
        ],
        sessions=(("s1", "sonnet", 0), ("s2", "swe-1-6-fast", 1)),
    )
    ids = {e["entry_id"] for e in parser.collect()}
    assert ids == {"devin:s1:m1", "devin:s2:m2"}


def test_seconds_timestamps_are_normalised(tmp_path, monkeypatch):
    """Q7 is capture-pending. The store's own column is an INTEGER with no unit
    in the schema, so the magnitude test decides; a seconds stamp must land in
    the same period bucket as the millisecond stamps around it."""
    usage = {"input_tokens": 1, "output_tokens": 1}
    parser = _parser(
        tmp_path,
        monkeypatch,
        [
            ("s1", 1, node_json("ms", usage=usage), T0_MS),
            ("s1", 2, node_json("sec", usage=usage), T0_S + 37),
        ],
    )
    assert sorted(e["timestamp"] for e in parser.collect()) == [T0_MS, T0_MS + 37_000]


def test_date_window_filters_at_sql_level(tmp_path, monkeypatch):
    usage = {"input_tokens": 1, "output_tokens": 1}
    day = 86_400_000
    parser = _parser(
        tmp_path,
        monkeypatch,
        [
            ("s1", 1, node_json("early", usage=usage), T0_MS),
            ("s1", 2, node_json("late", usage=usage), T0_MS + 10 * day),
        ],
    )
    since = datetime.fromtimestamp(T0_MS / 1000, tz=timezone.utc)
    until = datetime.fromtimestamp((T0_MS + day) / 1000, tz=timezone.utc)
    entries = parser.collect(since, until)
    assert [e["entry_id"] for e in entries] == ["devin:s1:early"]


def test_stable_message_id_dedupes_across_two_stores(tmp_path, monkeypatch):
    """A copied or symlinked store must not double count. Both stores hold the
    same session id and message id, so the stable key owns the row once."""
    usage = {"input_tokens": 50, "output_tokens": 5}
    a = tmp_path / "store-a"
    b = tmp_path / "store-b"
    make_store(a, sessions=[("s1", "sonnet", 0)], nodes=[("s1", 1, node_json("m1", usage=usage), T0_MS)])
    make_store(b, sessions=[("s1", "sonnet", 0)], nodes=[("s1", 7, node_json("m1", usage=usage), T0_MS)])
    monkeypatch.setenv("DEVIN_CLI_DATA_DIRS", f"{a},{b}")
    entries = DevinParser(PricingDatabase()).collect()
    assert len(entries) == 1


def test_node_id_is_the_fallback_key(tmp_path, monkeypatch):
    usage = {"input_tokens": 5, "output_tokens": 5}
    parser = _parser(
        tmp_path,
        monkeypatch,
        [("s1", 42, {"role": "assistant", "usage": usage}, T0_MS)],
    )
    assert parser.collect()[0]["entry_id"] == "devin:s1:42"


def test_nodes_without_a_session_row_still_count(tmp_path, monkeypatch):
    """A node whose session row is gone still spent tokens. The LEFT JOIN yields
    a NULL session model, so the record prices as unknown rather than vanishing
    -- counting tokens at 0.00 beats dropping them."""
    store = tmp_path / ".local" / "share" / "devin" / "cli"
    make_store(
        store,
        sessions=[],
        nodes=[("gone", 1, node_json("m1", usage={"input_tokens": 20, "output_tokens": 3}), T0_MS)],
    )
    monkeypatch.setenv("DEVIN_CLI_DATA_DIRS", str(store))
    entry = DevinParser(PricingDatabase()).collect()[0]
    assert entry["model"] == "unknown"
    assert (entry["input"], entry["output"]) == (20, 3)
    assert entry["cost"] == 0.0


def test_non_devin_database_yields_nothing(tmp_path, monkeypatch):
    store = tmp_path / ".local" / "share" / "devin" / "cli"
    store.mkdir(parents=True)
    conn = sqlite3.connect(store / "sessions.db")
    conn.execute("CREATE TABLE unrelated (id INTEGER)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("DEVIN_CLI_DATA_DIRS", str(store))
    assert DevinParser(PricingDatabase()).collect() == []


def test_unresolvable_model_costs_zero(tmp_path, monkeypatch):
    parser = _parser(
        tmp_path,
        monkeypatch,
        [
            (
                "s1",
                1,
                node_json(
                    "m1",
                    model="swe-2-fusion-router-alias",
                    usage={"input_tokens": 1000, "output_tokens": 1000},
                ),
                T0_MS,
            )
        ],
    )
    entry = parser.collect()[0]
    assert entry["cost"] == 0.0


def test_cost_follows_the_pricing_database(tmp_path, monkeypatch):
    pricing = PricingDatabase()
    usage = {"input_tokens": 4000, "output_tokens": 900, "cache_read_tokens": 1000}
    parser = None
    store = tmp_path / ".local" / "share" / "devin" / "cli"
    make_store(
        store,
        sessions=[("s1", "claude-sonnet-4-5", 0)],
        nodes=[("s1", 1, node_json("m1", usage=usage), T0_MS)],
    )
    monkeypatch.setenv("DEVIN_CLI_DATA_DIRS", str(store))
    parser = DevinParser(pricing)
    entry = parser.collect()[0]
    expected = pricing.get_cost("claude-sonnet-4-5", 4000, 900, 1000, 0)
    assert entry["cost"] == expected


def test_drvfs_store_is_read_through_a_snapshot(tmp_path, monkeypatch):
    real = make_store(
        tmp_path / "mnt" / "c" / "Users" / "H1937" / "AppData" / "Roaming" / "devin" / "cli",
        sessions=[("s1", "sonnet", 0)],
        nodes=[("s1", 1, node_json("m1", usage={"input_tokens": 3, "output_tokens": 4}), T0_MS)],
    )
    monkeypatch.setenv("DEVIN_CLI_DATA_DIRS", str(real.parent))
    opened = {"snapshot": False}

    @contextmanager
    def fake_snapshot(db_path):
        opened["snapshot"] = True
        # close_failed mirrors the real _ZCodeSnapshot, which always sets it.
        yield SimpleNamespace(
            conn=sqlite3.connect(str(db_path)), close_failed=False
        )

    monkeypatch.setattr(DevinParser, "_needs_snapshot", staticmethod(lambda p: True))
    monkeypatch.setattr(ct, "zcode_snapshot", fake_snapshot)
    entries = DevinParser(PricingDatabase()).collect()
    assert opened["snapshot"] is True
    assert len(entries) == 1


def test_collect_is_cached_until_the_store_changes(tmp_path, monkeypatch):
    parser = _parser(
        tmp_path,
        monkeypatch,
        [("s1", 1, node_json("m1", usage={"input_tokens": 7, "output_tokens": 8}), T0_MS)],
    )
    first = parser.collect()
    assert len(first) == 1
    store = Path(parser.db_paths[0]).parent
    make_store(
        store,
        sessions=[("s1", "sonnet", 0)],
        nodes=[
            ("s1", 1, node_json("m1", usage={"input_tokens": 7, "output_tokens": 8}), T0_MS),
            ("s1", 2, node_json("m2", usage={"input_tokens": 1, "output_tokens": 1}), T0_MS + 5),
        ],
    )
    parser = DevinParser(PricingDatabase())  # re-resolves signatures
    assert len(parser.collect()) == 2


def test_registry_entry_and_identity():
    tracker = ct.CodingToolsUsageTracker()
    assert isinstance(tracker.parsers["devin"], DevinParser)
    parser = tracker.parsers["devin"]
    # Live source: never copied into the usage store, so no stored version.
    assert parser.sync_capability.mode == "source_native_db"
    assert parser.persistent_parser_version is None
    assert parser.source_name == "devin"


def test_collect_survives_an_unreadable_store(tmp_path, monkeypatch):
    store = tmp_path / ".local" / "share" / "devin" / "cli"
    store.mkdir(parents=True)
    (store / "sessions.db").write_text("not a database")
    monkeypatch.setenv("DEVIN_CLI_DATA_DIRS", str(store))
    assert DevinParser(PricingDatabase()).collect() == []


# --- failed reads must never be cached --------------------------------------


def _drvfs_store(tmp_path, monkeypatch):
    """One Windows-host store, read as if through WSL's drvfs mount."""
    real = make_store(
        tmp_path / "mnt" / "c" / "Users" / "H1937" / "AppData" / "Roaming" / "devin" / "cli",
        sessions=[("s1", "sonnet", 0)],
        nodes=[("s1", 1, node_json("m1", usage={"input_tokens": 3, "output_tokens": 4}), T0_MS)],
    )
    monkeypatch.setenv("DEVIN_CLI_DATA_DIRS", str(real.parent))
    monkeypatch.setattr(DevinParser, "_needs_snapshot", staticmethod(lambda p: True))
    return real


def test_failed_snapshot_read_is_returned_but_not_cached(tmp_path, monkeypatch, caplog):
    """A transient failure must not settle into a cached zero.

    File signatures are the only thing that busts this cache, and a drvfs
    hiccup leaves mtime and size alone -- so a cached empty result would show
    "no Devin usage" until the next Devin run. Devin follows the rule the ZCode
    collectors document: read failures are logged and left uncached.
    """
    real = _drvfs_store(tmp_path, monkeypatch)
    calls = {"n": 0}

    @contextmanager
    def flaky_snapshot(db_path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ct.ZCodeSnapshotError("drvfs hiccup")
        conn = sqlite3.connect(str(db_path))
        try:
            yield SimpleNamespace(conn=conn, close_failed=False)
        finally:
            conn.close()

    monkeypatch.setattr(ct, "zcode_snapshot", flaky_snapshot)
    parser = DevinParser(PricingDatabase())

    assert parser.collect() == []
    assert DevinParser._query_cache == {}
    # Same parser, identical file signatures: the second read must retry, not
    # serve the cached zero.
    assert len(parser.collect()) == 1
    assert calls["n"] == 2
    assert [r.getMessage() for r in caplog.records if "not cached" in r.getMessage()]
    assert real.exists()


def test_snapshot_close_failure_is_returned_but_not_cached(tmp_path, monkeypatch):
    """Rows read from a snapshot that could not be closed stay uncacheable.

    The data itself is fine and is still returned; only the cache write is
    skipped, which is what makes the next collect retry the cleanup.
    """
    _drvfs_store(tmp_path, monkeypatch)

    @contextmanager
    def close_fails(db_path):
        conn = sqlite3.connect(str(db_path))
        snap = SimpleNamespace(conn=conn, close_failed=False)
        try:
            yield snap
        finally:
            # This is what zcode_snapshot does when conn.close() raises.
            snap.close_failed = True
            conn.close()

    monkeypatch.setattr(ct, "zcode_snapshot", close_fails)
    assert len(DevinParser(PricingDatabase()).collect()) == 1
    assert DevinParser._query_cache == {}


def test_corrupt_store_is_reported_and_not_cached(tmp_path, monkeypatch, caplog):
    """A store that is not SQLite at all is a failed read, not an empty one.

    The distinction matters because the sqlite_master probe is also the table
    check: swallowing the error there would let a truncated store be mistaken
    for a verified-empty one and cached as such until the file changed.
    """
    store = tmp_path / ".local" / "share" / "devin" / "cli"
    store.mkdir(parents=True)
    (store / "sessions.db").write_text("not a database")
    monkeypatch.setenv("DEVIN_CLI_DATA_DIRS", str(store))

    assert DevinParser(PricingDatabase()).collect() == []
    assert DevinParser._query_cache == {}
    assert [r.getMessage() for r in caplog.records if "not cached" in r.getMessage()]


def test_one_broken_store_does_not_hide_the_good_one(tmp_path, monkeypatch):
    """Per-store failures: partial data is still reported."""
    good = make_store(
        tmp_path / "good",
        sessions=[("s1", "sonnet", 0)],
        nodes=[("s1", 1, node_json("m1", usage={"input_tokens": 3, "output_tokens": 4}), T0_MS)],
    )
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "sessions.db").write_text("not a database")
    monkeypatch.setenv("DEVIN_CLI_DATA_DIRS", f"{good.parent},{broken}")

    entries = DevinParser(PricingDatabase()).collect()
    assert [e["input"] for e in entries] == [3]
    # The broken store poisons the cache write for the whole pass, which is the
    # cheap way to guarantee the good store's next read is a read and not a hit.
    assert DevinParser._query_cache == {}


# --- timestamp units ---------------------------------------------------------


@pytest.mark.parametrize(
    "unit,ts",
    [
        ("s", T0_S),
        ("ms", T0_MS),
        ("us", T0_MS * 1_000),
        ("ns", T0_MS * 1_000_000),
    ],
)
def test_every_epoch_unit_lands_on_the_same_day(tmp_path, monkeypatch, unit, ts):
    """A wrong unit guess is the one error this source must not make silently.

    The store's unit is pinned by no capture, and a Rust CLI can reach for
    as_micros() or as_nanos() in any release. Verified by experiment: with the
    old seconds-or-milliseconds test, a us row and an ns row both read as
    milliseconds, landed a thousand years past the window ceiling, and produced
    zero entries for the all-time window with nothing in the log.
    """
    parser = _parser(
        tmp_path,
        monkeypatch,
        [("s1", 1, node_json("m1", usage={"input_tokens": 5, "output_tokens": 6}), ts)],
    )
    entries = parser.collect()
    assert [e["input"] for e in entries] == [5], f"{unit} row was dropped"
    assert entries[0]["timestamp"] == T0_MS



def test_the_window_predicate_and_the_reported_timestamp_agree(tmp_path, monkeypatch):
    """Both use the same normalized expression, so a window filter cannot
    exclude a row it would otherwise have reported."""
    parser = _parser(
        tmp_path,
        monkeypatch,
        [
            ("s1", 1, node_json("micro", usage={"input_tokens": 1, "output_tokens": 1}), T0_MS * 1000),
            ("s1", 2, node_json("milli", usage={"input_tokens": 2, "output_tokens": 2}), T0_MS),
        ],
    )
    start = datetime.fromtimestamp((T0_MS // 1000) - 60, tz=timezone.utc)
    end = datetime.fromtimestamp((T0_MS // 1000) + 60, tz=timezone.utc)
    entries = parser.collect(start, end)
    assert sorted(e["input"] for e in entries) == [1, 2]


# --- read transport ----------------------------------------------------------


def test_the_snapshot_decision_is_about_transport_not_origin():
    """drvfs, UNC and a live sidecar all need a copy; a plain local store does not."""
    assert DevinParser._needs_snapshot(Path("/mnt/c/Users/x/devin/cli/sessions.db")) is True
    assert DevinParser._needs_snapshot(Path("\\\\wsl.localhost\\Ubuntu\\home\\x\\sessions.db")) is True
    assert DevinParser._needs_snapshot(Path("\\\\wsl$\\Ubuntu\\home\\x\\sessions.db")) is True
    assert DevinParser._needs_snapshot(Path("/home/x/.local/share/devin/cli/sessions.db")) is False
    # A path that merely contains mnt somewhere is not a 9p mount.
    assert DevinParser._needs_snapshot(Path("/data/mnt/c/sessions.db")) is False


def test_a_live_wal_sidecar_sends_the_store_through_the_snapshot(tmp_path, monkeypatch):
    """Presence of -wal means a writer is live or crashed mid-write.

    That is exactly when a mode=ro open cannot run recovery, and
    connect_sqlite_readonly answers the failure with a read-WRITE connect:
    Tokdash would replay the WAL into the user's store. The copy path is the
    only one that cannot write there.
    """
    store = make_store(
        tmp_path / "walside",
        wal=True,
        sessions=[("s1", "sonnet", 0)],
        nodes=[("s1", 1, node_json("m1", usage={"input_tokens": 3, "output_tokens": 4}), T0_MS)],
    )
    monkeypatch.setenv("DEVIN_CLI_DATA_DIRS", str(store.parent))
    used = {"snapshot": False}
    real = ct.zcode_snapshot

    @contextmanager
    def spy(db_path):
        used["snapshot"] = True
        with real(db_path) as snap:
            yield snap

    monkeypatch.setattr(ct, "zcode_snapshot", spy)

    # A clean close checkpoints and deletes the -wal, so the writer is held open
    # across the read: that is the case that matters, Devin running while the
    # dashboard polls.
    writer = sqlite3.connect(store)
    try:
        writer.execute(
            "INSERT INTO message_nodes (session_id, node_id, chat_message, created_at)"
            " VALUES ('s1', 2, '{}', ?)",
            (T0_MS + 5,),
        )
        writer.commit()
        assert Path(str(store) + "-wal").exists()
        assert DevinParser._needs_snapshot(store) is True
        assert len(DevinParser(PricingDatabase()).collect()) == 1
        assert used["snapshot"] is True
    finally:
        writer.close()


def test_a_read_only_failure_is_retried_once_through_a_copy(tmp_path, monkeypatch):
    """The WAL-needs-recovery shape: local store, no sidecars, first open fails.

    Without the retry this is a permanent zero -- a failed read is never cached,
    but every poll fails the same way until the user happens to rerun Devin. The
    copy is the safe direction, so the failure is only reported once that fails
    too.
    """
    parser = _parser(
        tmp_path,
        monkeypatch,
        [("s1", 1, node_json("m1", usage={"input_tokens": 9, "output_tokens": 9}), T0_MS)],
    )
    calls = {"n": 0}
    real_rows = DevinParser._rows

    def flaky(self, conn, s_ms, u_ms):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("cannot read WAL database in read-only mode")
        return real_rows(self, conn, s_ms, u_ms)

    monkeypatch.setattr(DevinParser, "_rows", flaky)
    entries = parser.collect()
    assert [e["input"] for e in entries] == [9]
    assert calls["n"] == 2  # failed read-only, then succeeded from the copy


def test_a_store_that_is_not_a_database_is_not_copied_for_no_reason(tmp_path, monkeypatch):
    """DatabaseError means the file is not SQLite, which copying cannot fix.

    The snapshot retry is for OperationalError only, so a corrupt store does not
    get its whole (transcript-sized) file copied on every dashboard poll.
    """
    store = tmp_path / ".local" / "share" / "devin" / "cli"
    store.mkdir(parents=True)
    (store / "sessions.db").write_text("not a database" * 100)
    monkeypatch.setenv("DEVIN_CLI_DATA_DIRS", str(store))
    used = {"snapshot": False}

    @contextmanager
    def spy(db_path):
        used["snapshot"] = True
        yield SimpleNamespace(conn=sqlite3.connect(str(db_path)), close_failed=False)

    monkeypatch.setattr(ct, "zcode_snapshot", spy)
    assert DevinParser(PricingDatabase()).collect() == []
    assert used["snapshot"] is False


# --- drift and invalidation --------------------------------------------------


def test_rows_with_no_usage_container_are_reported(tmp_path, monkeypatch, caplog):
    """A usage object that moved is otherwise indistinguishable from an idle week."""
    parser = _parser(
        tmp_path,
        monkeypatch,
        [
            ("s1", 1, {"id": "m1", "role": "user", "content": "hello"}, T0_MS),
            ("s1", 2, {"id": "m2", "role": "assistant", "tokens": {"in": 9}}, T0_MS + 5),
        ],
    )
    assert parser.collect() == []
    msgs = [r.getMessage() for r in caplog.records if "usage container" in r.getMessage()]
    assert len(msgs) == 1
    assert "2 node(s)" in msgs[0]


def test_zero_usage_rows_do_not_trip_the_drift_warning(tmp_path, monkeypatch, caplog):
    """User rows and cancelled requests carry a usage object reading zero.

    That is normal traffic; only a window where NO row carries a container at
    all is evidence that the layout moved.
    """
    parser = _parser(
        tmp_path,
        monkeypatch,
        [
            (
                "s1",
                1,
                node_json("m1", usage={"input_tokens": 0, "output_tokens": 0}),
                T0_MS,
            ),
        ],
    )
    assert parser.collect() == []
    assert not [r for r in caplog.records if "usage container" in r.getMessage()]


def test_the_shm_sidecar_is_part_of_the_invalidation_clock(tmp_path):
    """Deliberate, and easy to "optimize" away by mistake.

    zcode_snapshot_signatures() excludes the -shm because it signs what gets
    COPIED; this signs when a cached answer is stale. A live WAL store moves
    through -wal and -shm between checkpoints, so leaving the -shm out serves
    lagged usage while the CLI runs. Same choice as ZCodeParser._file_signatures.
    """
    store = make_store(tmp_path / "sig", sessions=[("s1", "sonnet", 0)], nodes=[])
    parser = DevinParser(PricingDatabase())
    parser.db_paths = [store]
    names = [Path(p).name for p, _, _ in parser._file_signatures()]
    assert names == ["sessions.db", "sessions.db-wal", "sessions.db-shm"]


# --- macOS path --------------------------------------------------------------


def test_macos_keeps_the_documented_xdg_path_and_addes_library(tmp_path, monkeypatch):
    """XDG is what the vendor's own macOS docs describe, so it stays first.

    Library/Application Support is tried as well because a Rust app-data crate
    would map there, and the cost of guessing wrong is asymmetric: an absent
    candidate is one stat, a missing one is a macOS user seeing zero forever.
    """
    monkeypatch.setattr(clientpaths.osinfo, "os_kind", lambda: "macos")
    xdg_store = tmp_path / ".local" / "share" / "devin" / "cli"
    make_store(xdg_store, sessions=[("s1", "sonnet", 0)], nodes=[])
    assert clientpaths.devin_cli_roots() == [xdg_store.resolve()]

    (xdg_store / "sessions.db").unlink()
    library = tmp_path / "Library" / "Application Support" / "devin" / "cli"
    make_store(library, sessions=[("s1", "sonnet", 0)], nodes=[])
    assert clientpaths.devin_cli_roots() == [library.resolve()]
