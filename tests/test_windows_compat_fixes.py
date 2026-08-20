"""Tests for the native-Windows robustness fixes (docs/local/20260819_windows_compat_review).

Each test pins one behavior from the review's fix list that cannot be exercised on
this Linux host for real (Windows file sharing / locked files), so the guard is
simulated with monkeypatched ``Path`` methods that raise the same exception a
Windows host would (``PermissionError``, a mid-walk ``OSError``).
"""
import json
import sqlite3
from pathlib import Path

import pytest

from tokdash import cli, compute, sessions
from tokdash.onboard import engine, paths
from tokdash.sources import coding_tools


# --- rglob walks: a non-PermissionError OSError mid-walk must not blank the source ---


def _fake_rglob_raising_after_first(self, pattern):
    yield self / "a.jsonl"
    raise OSError(5, "I/O error")


def test_iter_file_signatures_keeps_partial_walk_on_mid_walk_oserror(tmp_path, monkeypatch):
    (tmp_path / "a.jsonl").write_text("{}\n")
    monkeypatch.setattr(Path, "rglob", _fake_rglob_raising_after_first)
    sigs = sessions._iter_file_signatures(tmp_path)
    assert [s[0] for s in sigs] == [str(tmp_path / "a.jsonl")]


def test_rglob_sigs_keeps_partial_walk_on_mid_walk_oserror(tmp_path, monkeypatch):
    (tmp_path / "a.jsonl").write_text("{}\n")
    monkeypatch.setattr(Path, "rglob", _fake_rglob_raising_after_first)
    sigs = coding_tools._rglob_sigs(tmp_path)
    assert [s[0] for s in sigs] == [str(tmp_path / "a.jsonl")]


# --- session parsers: an open() without share-read must drop the file, not the view ---


def _test_each_session_parser_drops_file_when_open_raises(tmp_path, monkeypatch, parser, cache):
    session_file = tmp_path / "session.jsonl"
    session_file.write_text("{}\n")
    mtime = session_file.stat().st_mtime_ns
    size = session_file.stat().st_size

    def _open_held(self, *args, **kwargs):
        raise PermissionError(13, "Access is denied", str(self))

    cache.cache_clear()
    try:
        monkeypatch.setattr(Path, "open", _open_held)
        assert parser(str(session_file), mtime, size) is None
    finally:
        cache.cache_clear()


def test_codex_parser_drops_file_when_open_raises(tmp_path, monkeypatch):
    _test_each_session_parser_drops_file_when_open_raises(
        tmp_path, monkeypatch, sessions._parse_codex_session_file, sessions._parse_codex_session_file
    )


def test_claude_parser_drops_file_when_open_raises(tmp_path, monkeypatch):
    _test_each_session_parser_drops_file_when_open_raises(
        tmp_path, monkeypatch, sessions._parse_claude_session_file, sessions._parse_claude_session_file
    )


def test_pi_parser_drops_file_when_open_raises(tmp_path, monkeypatch):
    _test_each_session_parser_drops_file_when_open_raises(
        tmp_path, monkeypatch, sessions._parse_pi_session_file, sessions._parse_pi_session_file
    )


def test_kimi_parser_drops_file_when_open_raises(tmp_path, monkeypatch):
    _test_each_session_parser_drops_file_when_open_raises(
        tmp_path, monkeypatch, sessions._parse_kimi_session_file, sessions._parse_kimi_session_file
    )


# --- Codex title map: the SQLite URI must survive # and % in the path (any platform) ---


def test_codex_title_map_opens_db_with_percent_and_hash_in_path(tmp_path, monkeypatch):
    home = tmp_path / "pct%41#dir"
    home.mkdir()
    db = home / "state_5.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT)")
    conn.execute("INSERT INTO threads (id, title) VALUES ('sess-1', 'My Title')")
    conn.commit()
    conn.close()

    monkeypatch.setenv("CODEX_HOME", str(home))
    sessions._load_codex_title_map.cache_clear()
    try:
        titles = sessions._load_codex_title_map(("win-compat-test",))
    finally:
        sessions._load_codex_title_map.cache_clear()
    assert titles == {"sess-1": "My Title"}


def test_codex_title_map_treats_uri_construction_failure_as_no_titles(tmp_path, monkeypatch):
    # resolve() can raise OSError and as_uri() ValueError (a Windows UNC path with
    # no drive letter) before any connection exists — both must read as "no
    # titles", not a 500.
    (tmp_path / "state_5.sqlite").write_bytes(b"")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    def _no_uri(self):
        raise ValueError("path has no drive letter")

    sessions._load_codex_title_map.cache_clear()
    try:
        monkeypatch.setattr(Path, "as_uri", _no_uri)
        assert sessions._load_codex_title_map(("win-compat-test",)) == {}
    finally:
        sessions._load_codex_title_map.cache_clear()


# --- RO-first connects of live third-party databases ---


def test_connect_sqlite_readonly_refuses_writes(tmp_path):
    db = tmp_path / "live.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()

    for connect in (coding_tools.connect_sqlite_readonly, sessions.connect_sqlite_readonly):
        c = connect(db)
        try:
            assert c.execute("SELECT SUM(x) FROM t").fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError):
                c.execute("CREATE TABLE nope (x INTEGER)")
        finally:
            c.close()


def test_connect_sqlite_readonly_falls_back_to_rw_for_missing_db(tmp_path):
    db = tmp_path / "missing.db"
    c = coding_tools.connect_sqlite_readonly(db)
    try:
        c.execute("CREATE TABLE t (x INTEGER)")  # only possible if the RW fallback fired
    finally:
        c.close()
    assert db.exists()


# --- one broken usage source must not blank the whole usage view ---


def test_usage_tracker_collect_skips_failing_source():
    class _Dummy:
        def __init__(self, result=None, error=None):
            self._result, self._error = result, error

        def collect(self, since_date, until_date):
            if self._error is not None:
                raise self._error
            return self._result

    tracker = coding_tools.CodingToolsUsageTracker.__new__(coding_tools.CodingToolsUsageTracker)
    tracker.entries = []
    tracker.parsers = {
        "good": _Dummy(result=[{"entry_id": "g"}]),
        "bad": _Dummy(error=OSError("held")),
    }
    tracker.collect(None, None)
    assert tracker.entries == [{"entry_id": "g"}]
    assert tracker.to_json()["source_errors"] == [{"source": "bad", "error": "held"}]
    tracker.parsers = {"good": tracker.parsers["good"]}
    tracker.collect(None, None)
    assert tracker.to_json()["source_errors"] == []


def test_compute_usage_payload_carries_source_errors(monkeypatch):
    # /api/usage is the reader of the tracker's source_errors: a source that failed
    # to read must reach the payload so the UI can show "unavailable" instead of a
    # zero that reads as "no usage in range".
    monkeypatch.setattr(
        compute, "get_openclaw_data",
        lambda period: {
            "total_tokens": 0, "total_cost": 0.0, "total_messages": 0,
            "total_tokens_in": 0, "total_tokens_cache": 0, "models": {},
        },
    )
    monkeypatch.setattr(
        compute, "get_tools_data",
        lambda period: {"apps": {}, "all_models": [], "source_errors": ["codex"]},
    )
    assert compute.compute_usage("today")["source_errors"] == ["codex"]

    monkeypatch.setattr(
        compute, "get_tools_data",
        lambda period: {"apps": {}, "all_models": []},  # no key -> no false failures
    )
    assert compute.compute_usage("today")["source_errors"] == []


# --- one failing session source must degrade to an empty view, not a 500 ---


def test_raw_sessions_for_tool_returns_empty_view_on_source_failure(monkeypatch):
    monkeypatch.setattr(sessions, "persistent_usage_db_enabled", lambda: False)

    def _boom():
        raise OSError("locked")

    monkeypatch.setattr(sessions, "_codex_sessions", _boom)
    assert sessions._raw_sessions_for_tool("codex") == {}


def test_raw_sessions_for_tool_propagates_loader_logic_errors(monkeypatch):
    # An I/O or SQLite failure degrades to an empty view; a TypeError is a bug in
    # the loader and must surface, not masquerade as "you have no sessions".
    monkeypatch.setattr(sessions, "persistent_usage_db_enabled", lambda: False)

    def _boom():
        raise TypeError("loader bug")

    monkeypatch.setattr(sessions, "_codex_sessions", _boom)
    with pytest.raises(TypeError):
        sessions._raw_sessions_for_tool("codex")


def test_raw_sessions_for_tool_still_rejects_unknown_tool():
    with pytest.raises(ValueError):
        sessions._raw_sessions_for_tool("nope")


# --- db resync: a held usage DB must fail with a hint, not a traceback ---


def test_db_resync_reports_friendly_error_when_db_held(tmp_path, monkeypatch):
    db = tmp_path / "usage.sqlite3"
    sqlite3.connect(str(db)).close()  # plain file; the store will schema it on demand
    monkeypatch.setenv("TOKDASH_USAGE_DB_PATH", str(db))
    monkeypatch.setattr(cli, "_sync_usage_database", lambda: {"usage_entries": 0})

    real_replace = Path.replace

    def _held_replace(self, target):
        if str(self) == str(db) or str(target) == str(db):
            raise PermissionError(13, "Access is denied")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", _held_replace)
    status = cli._resync_usage_database()

    assert status["ok"] is False
    assert "tokdash serve" in status["error"]
    assert db.exists()  # the live DB survived
    assert not list(tmp_path.glob("*.bak.*"))  # no half-done backup
    assert not list(tmp_path.glob("*.tmp.*"))  # no orphaned temp DB
    assert status["backups"] == []  # the first rename already failed
    assert status["restored_backups"] == []
    assert status["resync_mode"] == "temp-db-atomic-replace"


def test_db_resync_rollback_restores_each_backup_to_its_own_path(tmp_path, monkeypatch):
    # SQLite removes -wal and -shm together on a clean close, so a gap (here: -shm
    # present, -wal absent) is hard to reach in practice — but the rollback must be
    # correct for any gap: zipping the backups against the full candidate tuple
    # would land the -shm backup on the -wal path.
    db = tmp_path / "usage.sqlite3"
    wal = Path(str(db) + "-wal")
    shm = Path(str(db) + "-shm")
    # Not a valid database: UsageEntryStore(path).status() cannot open it, so
    # SQLite's stale-sidecar cleanup never runs and the hand-placed -shm
    # survives until the rename loop.
    db.write_bytes(b"not a sqlite database")
    shm.write_bytes(b"shm-bytes")
    monkeypatch.setenv("TOKDASH_USAGE_DB_PATH", str(db))
    monkeypatch.setattr(cli, "_sync_usage_database", lambda: {"usage_entries": 0})

    real_replace = Path.replace

    def _held_replace(self, target):
        # Both candidate renames succeed; the failure lands when the temp DB is
        # moved into place — the rollback then has two backups and a gap.
        if str(target) == str(db) and ".tmp." in str(self):
            raise PermissionError(13, "Access is denied")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", _held_replace)
    status = cli._resync_usage_database()

    assert status["ok"] is False
    assert "tokdash serve" in status["error"]
    assert db.read_bytes() == b"not a sqlite database"  # the -db backup went back to -db
    assert shm.read_bytes() == b"shm-bytes"  # the -shm backup went back to -shm
    assert not wal.exists()  # the mispairing would create it, holding the -shm backup
    assert not list(tmp_path.glob("*.bak.*"))
    assert not list(tmp_path.glob("*.tmp.*"))
    # The rollback renamed every backup back, so no .bak file survives and the
    # success-path `backups` list is empty; `restored_backups` names what was touched.
    assert status["backups"] == []
    assert len(status["restored_backups"]) == 2  # the db and -shm were backed up before the failure
    assert all(".bak." in p for p in status["restored_backups"])
    assert status["resync_mode"] == "temp-db-atomic-replace"


# --- uninstall --purge: a held usage DB must point at the running server ---


def test_purge_data_points_at_running_server_when_db_held(tmp_path, monkeypatch):
    db = tmp_path / "usage.sqlite3"
    db.write_bytes(b"x")
    monkeypatch.setattr(paths, "usage_db_path", lambda: db)
    monkeypatch.setattr(paths, "pricing_db_override_path", lambda: tmp_path / "pricing_db.json")
    monkeypatch.setattr(paths, "config_path", lambda: tmp_path / "config.json")

    def _no_unlink(self, *args, **kwargs):
        raise OSError("held")

    monkeypatch.setattr(Path, "unlink", _no_unlink)
    with pytest.raises(OSError, match="tokdash serve"):
        engine._purge_data()


# --- a transient lock must not be cached as a permanent miss ---


_LOCKABLE_PARSERS = (
    "_parse_codex_session_file",
    "_parse_claude_session_file",
    "_parse_pi_session_file",
    "_parse_kimi_session_file",
)


@pytest.mark.parametrize("parser_name", _LOCKABLE_PARSERS)
def test_locked_session_file_is_retried_on_the_next_request(parser_name, tmp_path, monkeypatch):
    # The parser key is (path, mtime_ns, size), which never changes again once a
    # session ends. Caching the None from a one-request file lock would hide that
    # session until the process restarts, so the failure must not be memoized.
    parser = getattr(sessions, parser_name)
    session_file = tmp_path / "session.jsonl"
    session_file.write_text("{}\n")
    st = session_file.stat()
    key = (str(session_file), st.st_mtime_ns, st.st_size)

    real_open = Path.open
    opens = []

    def _locked_once(self, *args, **kwargs):
        if str(self) == str(session_file):
            opens.append(str(self))
            if len(opens) == 1:
                raise PermissionError(13, "Access is denied", str(self))
        return real_open(self, *args, **kwargs)

    parser.cache_clear()
    try:
        monkeypatch.setattr(Path, "open", _locked_once)
        assert parser(*key) is None  # locked
        parser(*key)  # lock released, signature unchanged, no cache_clear()
        assert len(opens) == 2, "the failure was cached: the file was never re-opened"
        assert parser.cache_info().hits == 0
    finally:
        parser.cache_clear()


def test_retried_session_file_parses_after_the_lock_clears(tmp_path, monkeypatch):
    session_file = tmp_path / "rollout-2026-08-20T10-00-00-abc.jsonl"
    session_file.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "sess-abc", "cwd": "/proj"}}) + "\n"
    )
    st = session_file.stat()
    key = (str(session_file), st.st_mtime_ns, st.st_size)

    real_open = Path.open
    failed = []

    def _locked_once(self, *args, **kwargs):
        if str(self) == str(session_file) and not failed:
            failed.append(True)
            raise PermissionError(13, "Access is denied", str(self))
        return real_open(self, *args, **kwargs)

    sessions._parse_codex_session_file.cache_clear()
    try:
        monkeypatch.setattr(Path, "open", _locked_once)
        assert sessions._parse_codex_session_file(*key) is None
        recovered = sessions._parse_codex_session_file(*key)
        assert recovered is not None and recovered["session_id"] == "sess-abc"
    finally:
        sessions._parse_codex_session_file.cache_clear()


def test_unreadable_session_file_still_caches_a_real_parse_result(tmp_path):
    # Only the transient open failure bypasses the cache; a file that reads fine
    # but yields nothing must still be memoized, or every request re-parses it.
    session_file = tmp_path / "empty.jsonl"
    session_file.write_text("")
    st = session_file.stat()
    key = (str(session_file), st.st_mtime_ns, st.st_size)

    sessions._parse_codex_session_file.cache_clear()
    try:
        sessions._parse_codex_session_file(*key)
        sessions._parse_codex_session_file(*key)
        assert sessions._parse_codex_session_file.cache_info().hits == 1
    finally:
        sessions._parse_codex_session_file.cache_clear()


# --- a rollback that could not finish must say so, and name the surviving .bak ---


def test_db_resync_reports_an_unfinished_rollback(tmp_path, monkeypatch):
    db = tmp_path / "usage.sqlite3"
    db.write_bytes(b"not a sqlite database")  # see the rollback test above
    monkeypatch.setenv("TOKDASH_USAGE_DB_PATH", str(db))
    monkeypatch.setattr(cli, "_sync_usage_database", lambda: {"usage_entries": 0})

    real_replace = Path.replace

    def _held_replace(self, target):
        # The temp DB cannot be moved into place, and the backup cannot be moved
        # back either — the .bak is then the only intact copy of the old DB.
        if str(target) == str(db):
            raise PermissionError(13, "Access is denied")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", _held_replace)
    status = cli._resync_usage_database()

    assert status["ok"] is False
    assert status["rollback_ok"] is False
    assert status["restored_backups"] == []
    assert len(status["backups"]) == 1  # the .bak that is still on disk
    surviving = Path(status["backups"][0])
    assert surviving.exists() and surviving.read_bytes() == b"not a sqlite database"
    assert surviving.name in status["error"]
    assert "restore it by hand" in status["error"]
    assert "rolling the resync back did not finish" in status["error"]


def test_db_resync_rolls_back_on_a_non_permission_oserror(tmp_path, monkeypatch):
    # The handler exists to undo a half-applied replace. A cross-device rename or a
    # volume I/O error leaves exactly the same half-applied tree as the Windows lock,
    # so catching only PermissionError would let it escape with the old DB already
    # renamed to .bak and no rollback attempted.
    db = tmp_path / "usage.sqlite3"
    db.write_bytes(b"not a sqlite database")
    monkeypatch.setenv("TOKDASH_USAGE_DB_PATH", str(db))
    monkeypatch.setattr(cli, "_sync_usage_database", lambda: {"usage_entries": 0})

    real_replace = Path.replace

    def _cross_device(self, target):
        if str(target) == str(db) and ".tmp." in str(self):
            raise OSError(18, "Invalid cross-device link")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", _cross_device)
    status = cli._resync_usage_database()

    assert status["ok"] is False
    assert status["rollback_ok"] is True
    assert db.read_bytes() == b"not a sqlite database"  # rolled back
    assert not list(tmp_path.glob("*.bak.*"))
    assert not list(tmp_path.glob("*.tmp.*"))
    assert status["backups"] == []
    assert len(status["restored_backups"]) == 1
    # The real cause is named, and the user is not sent to stop a server that is
    # not holding anything.
    assert "Invalid cross-device link" in status["error"]
    assert "tokdash serve" not in status["error"]


def test_db_resync_reports_an_unfinished_rollback_after_a_non_permission_oserror(
    tmp_path, monkeypatch
):
    db = tmp_path / "usage.sqlite3"
    db.write_bytes(b"not a sqlite database")
    monkeypatch.setenv("TOKDASH_USAGE_DB_PATH", str(db))
    monkeypatch.setattr(cli, "_sync_usage_database", lambda: {"usage_entries": 0})

    real_replace = Path.replace

    def _cross_device(self, target):
        # Neither the temp DB nor the backup can be moved onto the live path.
        if str(target) == str(db):
            raise OSError(18, "Invalid cross-device link")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", _cross_device)
    status = cli._resync_usage_database()

    assert status["ok"] is False
    assert status["rollback_ok"] is False
    assert status["restored_backups"] == []
    assert len(status["backups"]) == 1
    surviving = Path(status["backups"][0])
    assert surviving.exists() and surviving.read_bytes() == b"not a sqlite database"
    assert surviving.name in status["error"]
    assert "restore it by hand" in status["error"]
    assert "rolling the resync back did not finish" in status["error"]
    assert "tokdash serve" not in status["error"]


# --- the aggregate loaders must not cache a view built during a lock ---

_AGGREGATE_PAIRS = (
    ("_load_codex_sessions", "_parse_codex_session_file"),
    ("_load_claude_sessions", "_parse_claude_session_file"),
    ("_load_pi_sessions", "_parse_pi_session_file"),
    ("_load_kimi_sessions", "_parse_kimi_session_file"),
)


def _lock_once(session_file, opens):
    """Path.open that raises PermissionError the first time it sees session_file."""
    real_open = Path.open

    def _opener(self, *args, **kwargs):
        if str(self) == str(session_file):
            opens.append(str(self))
            if len(opens) == 1:
                raise PermissionError(13, "Access is denied", str(self))
        return real_open(self, *args, **kwargs)

    return _opener


@pytest.mark.parametrize("loader_name,parser_name", _AGGREGATE_PAIRS)
def test_aggregate_loader_retries_a_transiently_locked_file(
    loader_name, parser_name, tmp_path, monkeypatch
):
    # Not caching the *parser's* transient failure is not enough on its own: these
    # loaders are themselves keyed on the file signature, so a view assembled while
    # one file was locked would be memoized with that session missing and the
    # parser-level retry would never be reached.
    loader = getattr(sessions, loader_name)
    parser = getattr(sessions, parser_name)
    session_file = tmp_path / "session.jsonl"
    session_file.write_text("{}\n")
    signature = (sessions._file_signature(session_file),)
    opens: list[str] = []

    loader.cache_clear()
    parser.cache_clear()
    try:
        monkeypatch.setattr(Path, "open", _lock_once(session_file, opens))
        loader(signature, ())  # locked
        loader(signature, ())  # same signature, no cache_clear()
        assert len(opens) == 2, "the loader cached the partial view; the file was never retried"
        assert loader.cache_info().hits == 0
    finally:
        loader.cache_clear()
        parser.cache_clear()


def test_codex_aggregate_recovers_the_session_after_the_lock_clears(tmp_path, monkeypatch):
    # The end-to-end shape of the bug: one locked file during one request must not
    # hide that session for the life of the process.
    ts = "2026-05-19T12:00:00Z"
    rows = [
        {
            "type": "session_meta",
            "payload": {"id": "sess-lock", "cwd": "/work/proj", "timestamp": ts,
                        "model_provider": "openai"},
        },
        {"type": "turn_context", "payload": {"model": "gpt-5.5"}},
        {
            "type": "event_msg",
            "timestamp": ts,
            "payload": {"type": "token_count", "info": {"last_token_usage": {
                "input_tokens": 3_000, "cached_input_tokens": 2_000,
                "output_tokens": 100, "reasoning_output_tokens": 50}}},
        },
    ]
    session_file = tmp_path / "rollout-2026-05-19T12-00-00-lock.jsonl"
    session_file.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    signature = (sessions._file_signature(session_file),)
    opens: list[str] = []

    sessions._load_codex_sessions.cache_clear()
    sessions._parse_codex_session_file.cache_clear()
    try:
        monkeypatch.setattr(Path, "open", _lock_once(session_file, opens))
        first = sessions._load_codex_sessions(signature, ())
        assert "sess-lock" not in first  # locked on this request
        second = sessions._load_codex_sessions(signature, ())
        assert "sess-lock" in second, "the locked-out session never came back"
        assert second["sess-lock"]["turns"]
    finally:
        sessions._load_codex_sessions.cache_clear()
        sessions._parse_codex_session_file.cache_clear()


def test_codex_activity_records_retry_a_transiently_locked_file(tmp_path, monkeypatch):
    # Same outer-cache trap on the activity path, which get_codex_activity_insights
    # uses whenever the persistent store is off.
    session_file = tmp_path / "rollout-2026-05-19T12-00-00-act.jsonl"
    session_file.write_text("{}\n")
    signature = (sessions._file_signature(session_file),)
    opens: list[str] = []

    sessions._load_codex_activity_records.cache_clear()
    sessions._parse_codex_session_file.cache_clear()
    try:
        monkeypatch.setattr(Path, "open", _lock_once(session_file, opens))
        sessions._load_codex_activity_records(signature, ())
        sessions._load_codex_activity_records(signature, ())
        assert len(opens) == 2
        assert sessions._load_codex_activity_records.cache_info().hits == 0
    finally:
        sessions._load_codex_activity_records.cache_clear()
        sessions._parse_codex_session_file.cache_clear()


def test_aggregate_loader_still_caches_a_clean_read(tmp_path, monkeypatch):
    # Only a transient miss skips the outer cache. A clean pass must still be
    # memoized, or every request repeats the merge fold these loaders exist to avoid.
    session_file = tmp_path / "session.jsonl"
    session_file.write_text("{}\n")
    signature = (sessions._file_signature(session_file),)

    sessions._load_codex_sessions.cache_clear()
    sessions._parse_codex_session_file.cache_clear()
    try:
        sessions._load_codex_sessions(signature, ())
        sessions._load_codex_sessions(signature, ())
        assert sessions._load_codex_sessions.cache_info().hits == 1
    finally:
        sessions._load_codex_sessions.cache_clear()
        sessions._parse_codex_session_file.cache_clear()


# --- the loaders must not require a private attribute on the parser they call ---


@pytest.mark.parametrize("loader_name,parser_name", _AGGREGATE_PAIRS)
def test_aggregate_loader_tolerates_a_parser_without_the_raising_variant(
    loader_name, parser_name, tmp_path, monkeypatch
):
    # The raising variant is hung off the wrapper _cached_session_parser returns, but
    # the module attribute reaching it can be replaced: test instrumentation wraps
    # these parsers in a plain counting function to assert they are not re-entered.
    # Requiring the attribute turned that into an AttributeError that crashed the
    # whole view, so the loaders must fall back to the ordinary call.
    loader = getattr(sessions, loader_name)
    original = getattr(sessions, parser_name)
    calls = {"count": 0}

    def plain_parser(*args, **kwargs):  # exactly what the instrumentation installs
        calls["count"] += 1
        return original(*args, **kwargs)

    assert not hasattr(plain_parser, "raising")
    session_file = tmp_path / "session.jsonl"
    session_file.write_text("{}\n")
    signature = (sessions._file_signature(session_file),)

    loader.cache_clear()
    original.cache_clear()
    try:
        monkeypatch.setattr(sessions, parser_name, plain_parser)
        loader(signature, ())  # must not raise AttributeError
        assert calls["count"] == 1
    finally:
        loader.cache_clear()
        original.cache_clear()


def test_codex_activity_records_tolerate_a_parser_without_the_raising_variant(
    tmp_path, monkeypatch
):
    original = sessions._parse_codex_session_file
    calls = {"count": 0}

    def plain_parser(*args, **kwargs):
        calls["count"] += 1
        return original(*args, **kwargs)

    session_file = tmp_path / "rollout-2026-05-19T12-00-00-plain.jsonl"
    session_file.write_text("{}\n")
    signature = (sessions._file_signature(session_file),)

    sessions._load_codex_activity_records.cache_clear()
    original.cache_clear()
    try:
        monkeypatch.setattr(sessions, "_parse_codex_session_file", plain_parser)
        sessions._load_codex_activity_records(signature, ())
        assert calls["count"] == 1
    finally:
        sessions._load_codex_activity_records.cache_clear()
        original.cache_clear()
