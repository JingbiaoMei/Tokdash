"""Repairing or purging the usage database must fail safely when it cannot finish.

``tokdash db resync`` rebuilds the usage database into a temp file and swaps it
in; ``tokdash uninstall --purge`` deletes it. On Windows a running server holds
the file open, and any platform can fail the rename part-way. These tests
simulate that with monkeypatched ``Path`` methods and check that the command
leaves the live database (or a named backup) in place, says what to do next, and
does not leak the connection that would keep the file held.
"""
import sqlite3
from pathlib import Path

import pytest

from tokdash import cli, usage_store
from tokdash.onboard import engine, paths


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


# --- a failed connect must not leak the handle it opened ---


def test_usage_store_connect_closes_the_handle_when_schema_setup_fails(tmp_path, monkeypatch):
    # _connect() runs schema setup before handing the connection out, so a failure
    # there escapes past every `with closing(self._connect())` and leaks the handle.
    # A corrupt database is the common path into that branch. On Windows the leaked
    # handle then blocks renaming the file, which made `tokdash db resync` fail on
    # exactly the broken database it exists to repair — and blame a running server.
    db = tmp_path / "usage.sqlite3"
    db.write_bytes(b"not a sqlite database")

    real_connect = sqlite3.connect
    opened: list[sqlite3.Connection] = []

    def _tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", _tracking_connect)
    store = usage_store.UsageEntryStore(db)
    with pytest.raises(sqlite3.DatabaseError):
        store._connect()

    assert opened, "no connection was opened, so the test proves nothing"
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")  # a closed connection raises; an open one leaks


def test_db_resync_repairs_a_corrupt_database(tmp_path, monkeypatch):
    # The end-to-end shape: resync is what you run when the usage DB is broken, so
    # a corrupt DB must not come back as "held open by another process".
    db = tmp_path / "usage.sqlite3"
    db.write_bytes(b"not a sqlite database")
    monkeypatch.setenv("TOKDASH_USAGE_DB_PATH", str(db))
    monkeypatch.setattr(cli, "_sync_usage_database", lambda: {"usage_entries": 3})

    status = cli._resync_usage_database()

    assert status.get("ok") is True, status.get("error")
    assert "tokdash serve" not in str(status.get("error", ""))
    assert len(status["backups"]) == 1  # the corrupt file was kept as a .bak
