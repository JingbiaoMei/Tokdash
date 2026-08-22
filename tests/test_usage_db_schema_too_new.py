"""A usage DB written by a newer Tokdash must fail fast, never reparse.

Regression cover for the incident where a schema-9 database met a build that
only supported schema 7: every cache read raised, every caller treated that as
"the cache is sick" and fell back to the raw session parsers, and the server
reparsed the full on-disk history on *every* request until it was restarted.

The contract these tests pin down:
  - the too-new database is rejected before anything mutates it;
  - no raw Codex/Claude/Kimi/OpenClaw parser is ever reached;
  - the API fails fast with an actionable message (and not with the 503 the
    dashboard retries);
  - `tokdash doctor` reports it;
  - /health is unaffected.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3

from pathlib import Path

import pytest

import tokdash.compute as compute_module
import tokdash.sessions as sessions_module
import tokdash.sources.openclaw as openclaw_module
from tokdash.clientpaths import usage_db_path
from tokdash.usage_store import (
    SCHEMA_VERSION,
    UsageDatabaseSchemaTooNewError,
    UsageEntryStore,
)

FUTURE_SCHEMA = SCHEMA_VERSION + 2


def _make_future_schema_db(path: Path) -> None:
    """A minimal database that claims a schema this build cannot read.

    Only ``meta`` exists, so any DDL or migration this build runs is visible as
    an extra table -- which is what the no-mutation assertions key on.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(FUTURE_SCHEMA),),
        )
        conn.commit()
    finally:
        conn.close()


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        conn.close()


def _journal_mode(path: Path) -> str:
    conn = sqlite3.connect(str(path))
    try:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        conn.close()


@pytest.fixture
def future_db() -> Path:
    path = usage_db_path()
    _make_future_schema_db(path)
    return path


def test_rejected_before_any_schema_mutation(future_db: Path) -> None:
    before_tables = _tables(future_db)
    before_journal = _journal_mode(future_db)
    assert before_tables == {"meta"}

    with pytest.raises(UsageDatabaseSchemaTooNewError) as excinfo:
        UsageEntryStore()._connect()

    assert excinfo.value.found == FUTURE_SCHEMA
    assert excinfo.value.supported == SCHEMA_VERSION
    # No DDL, no WAL flip, no migration: the file is byte-for-byte as we left it.
    assert _tables(future_db) == before_tables
    assert _journal_mode(future_db) == before_journal
    assert str(future_db) in str(excinfo.value)


def test_error_message_is_actionable(future_db: Path) -> None:
    with pytest.raises(UsageDatabaseSchemaTooNewError) as excinfo:
        UsageEntryStore()._connect()
    message = str(excinfo.value)
    assert "tokdash update" in message
    assert str(FUTURE_SCHEMA) in message and str(SCHEMA_VERSION) in message


@pytest.mark.parametrize("tool", ["codex", "claude", "kimi", "dsh", "reasonix"])
def test_session_paths_never_reach_raw_parsers(future_db: Path, monkeypatch, tool: str) -> None:
    for loader in ("_codex_sessions", "_claude_sessions", "_kimi_sessions", "_dsh_sessions", "_reasonix_sessions"):
        monkeypatch.setattr(
            sessions_module,
            loader,
            lambda *a, **k: pytest.fail("raw session parser ran on a too-new usage DB"),
        )
    with pytest.raises(UsageDatabaseSchemaTooNewError):
        sessions_module._raw_sessions_for_tool(tool)


def test_openclaw_paths_never_reach_raw_parsers(future_db: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        openclaw_module,
        "_collect_entries",
        lambda *a, **k: pytest.fail("raw OpenClaw parser ran on a too-new usage DB"),
    )
    with pytest.raises(UsageDatabaseSchemaTooNewError):
        openclaw_module._collect_normalized_entries([], openclaw_module.PricingDatabase())


def test_coding_tools_path_never_falls_back_to_live_collect(future_db: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        compute_module.CodingToolsUsageTracker,
        "collect",
        lambda *a, **k: pytest.fail("live tracker.collect ran on a too-new usage DB"),
    )
    with pytest.raises(UsageDatabaseSchemaTooNewError):
        compute_module.run_local_coding_tools_json([])


def test_mismatch_is_rechecked_not_latched(future_db: Path) -> None:
    """The failure must not be cached for the process's lifetime.

    Marking the store permanently unusable would be cheap but wrong: replacing
    the database (an upgrade, a `tokdash db resync`, a restore) has to start
    working again without a restart. The check is two queries, so it is re-run.
    """
    store = UsageEntryStore()
    for _ in range(3):
        with pytest.raises(UsageDatabaseSchemaTooNewError):
            store._connect()

    future_db.unlink()
    # Same store instance, same path: a readable database must now go through.
    store._connect().close()
    assert "usage_entries" in _tables(future_db)


def test_doctor_reports_the_mismatch(future_db: Path) -> None:
    from tokdash.onboard.engine import _append_usage_db_issues, _doctor_usage_db

    info = _doctor_usage_db()
    assert info["stored_schema"] == FUTURE_SCHEMA
    assert info["supported_schema"] == SCHEMA_VERSION

    issues: list[str] = []
    _append_usage_db_issues(issues, info)
    assert len(issues) == 1
    assert "tokdash update" in issues[0]


def test_doctor_is_read_only(future_db: Path) -> None:
    """Doctor must be able to report a too-new DB without migrating it."""
    from tokdash.onboard.engine import _doctor_usage_db

    before_tables, before_journal = _tables(future_db), _journal_mode(future_db)
    _doctor_usage_db()
    assert _tables(future_db) == before_tables
    assert _journal_mode(future_db) == before_journal


def test_doctor_clean_on_a_current_database() -> None:
    from tokdash.onboard.engine import _append_usage_db_issues, _doctor_usage_db

    UsageEntryStore()._connect().close()
    info = _doctor_usage_db()
    assert info["stored_schema"] == SCHEMA_VERSION
    issues: list[str] = []
    _append_usage_db_issues(issues, info)
    assert issues == []


class TestApiSurface:
    """Handlers are called directly, never through TestClient/ASGITransport.

    The routes below are sync `def` handlers, and this repo documents a
    sync-handler deadlock under TestClient (see test_write_protection.py); the
    compute semaphore this change interacts with makes that pattern especially
    prone to hanging. test_api_smoke.py's convention is followed instead: call
    the handler, catch HTTPException, and asyncio.run the async ones.
    """

    @staticmethod
    def _api():
        pytest.importorskip("fastapi")
        import tokdash.api as api

        api._clear_cache()
        return api

    def test_usage_route_fails_fast_and_actionably(self, future_db: Path) -> None:
        api = self._api()
        with pytest.raises(api.HTTPException) as excinfo:
            api.get_usage(period="today")
        # 503 is the dashboard's retry signal; a too-new DB never recovers on
        # retry, so it must NOT be reported as one.
        assert excinfo.value.status_code == 500
        detail = str(excinfo.value.detail)
        assert "tokdash update" in detail
        assert str(FUTURE_SCHEMA) in detail

    def test_health_is_unaffected(self, future_db: Path) -> None:
        api = self._api()
        payload = asyncio.run(api.health_check())
        assert payload["service"] == "tokdash"

    def test_version_reports_supported_schema(self, future_db: Path) -> None:
        api = self._api()
        payload = asyncio.run(api.get_version())
        assert payload["usage_db_schema_supported"] == SCHEMA_VERSION


# A data dir whose name carries characters that break a naive
# "file:{path}?mode=ro" string: `#` starts the fragment, `%` opens an escape
# sequence, and the space needs escaping too. Deliberately no `?` -- Windows
# forbids it in filenames and CI runs on windows-latest; the POSIX-only test
# below covers that character separately.
SPECIAL_DIR_NAME = "data #% dir"
POSIX_SPECIAL_DIR_NAME = "data ?#% dir"


def test_read_only_uri_is_escaped_and_has_one_query_string(tmp_path: Path) -> None:
    special = tmp_path / SPECIAL_DIR_NAME
    special.mkdir()
    db = special / "usage.sqlite3"
    db.touch()

    uri = db.resolve().as_uri() + "?mode=ro"

    # file:/// covers POSIX and the Windows drive-letter form alike.
    assert uri.startswith("file:///")
    # Reserved characters are percent-escaped, so exactly one `?` survives -- the
    # one that introduces mode=ro.
    assert uri.count("?") == 1
    assert uri.endswith("?mode=ro")
    assert "#" not in uri
    assert " " not in uri


def test_doctor_reads_a_database_under_a_reserved_character_path(
    tmp_path: Path, monkeypatch
) -> None:
    from tokdash.onboard.engine import _doctor_usage_db

    special = tmp_path / SPECIAL_DIR_NAME
    special.mkdir()
    db = special / "usage.sqlite3"
    monkeypatch.setenv("TOKDASH_USAGE_DB_PATH", str(db))
    _make_future_schema_db(db)

    info = _doctor_usage_db()

    assert info.get("error") is None
    assert info["present"] is True
    assert info["stored_schema"] == FUTURE_SCHEMA
    assert info["path"] == str(db)


def test_doctor_ignores_schema_when_the_database_is_disabled(
    future_db: Path, monkeypatch
) -> None:
    """TOKDASH_USAGE_DB=0 means nothing reads the file; it cannot break a request."""
    from tokdash.onboard.engine import _append_usage_db_issues, _doctor_usage_db

    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")
    info = _doctor_usage_db()
    assert info["enabled"] is False
    assert info["stored_schema"] == FUTURE_SCHEMA

    issues: list[str] = []
    _append_usage_db_issues(issues, info)
    assert issues == []


def test_doctor_flags_an_enabled_but_unreadable_database() -> None:
    """Enabled and unreadable must fail doctor, not print a note and exit 0."""
    from tokdash.onboard.engine import _append_usage_db_issues, _doctor_usage_db

    db = usage_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"this is definitely not a sqlite database")

    info = _doctor_usage_db()
    assert info["enabled"] is True
    assert info["present"] is True
    assert info.get("error")

    issues: list[str] = []
    _append_usage_db_issues(issues, info)
    assert len(issues) == 1
    assert str(db) in issues[0]


def test_doctor_command_fails_end_to_end_on_a_too_new_database(
    future_db: Path, capsys
) -> None:
    """The issue must reach cmd_doctor's report, ok flag and exit code."""
    import json as _json

    from tokdash import cli
    from tokdash.onboard.engine import run_lifecycle

    capsys.readouterr()
    args = cli.build_parser("tokdash").parse_args(["doctor", "--json"])
    rc = run_lifecycle(args)
    payload = _json.loads(capsys.readouterr().out)

    assert payload["usage_db"]["stored_schema"] == FUTURE_SCHEMA
    assert payload["usage_db"]["supported_schema"] == SCHEMA_VERSION
    assert any("tokdash update" in issue for issue in payload["issues"])
    assert payload["ok"] is False and rc != 0


@pytest.mark.skipif(os.name == "nt", reason="Windows forbids '?' in filenames")
def test_doctor_reads_a_path_containing_a_question_mark(tmp_path: Path, monkeypatch) -> None:
    """`?` is the character that truncated the naive f-string URI. POSIX only."""
    from tokdash.onboard.engine import _doctor_usage_db

    special = tmp_path / POSIX_SPECIAL_DIR_NAME
    special.mkdir()
    db = special / "usage.sqlite3"
    monkeypatch.setenv("TOKDASH_USAGE_DB_PATH", str(db))
    _make_future_schema_db(db)

    uri = db.resolve().as_uri() + "?mode=ro"
    assert uri.count("?") == 1

    info = _doctor_usage_db()
    assert info.get("error") is None
    assert info["stored_schema"] == FUTURE_SCHEMA


# --- pre-flight: the guard must run BEFORE any source discovery ------------------


def _forbid_discovery(monkeypatch) -> None:
    """Make every signature/discovery scan fail loudly if it is reached."""
    def boom(name):
        def _fail(*_a, **_k):
            pytest.fail(f"{name} ran before the usage-DB compatibility check")
        return _fail

    for fn in (
        "_codex_file_signatures",
        "_kimi_session_signatures",
        "_dsh_session_signatures",
        "_reasonix_session_signatures",
        "_iter_file_signatures",
    ):
        monkeypatch.setattr(sessions_module, fn, boom(f"sessions.{fn}"))
    for fn in ("_session_files", "_signature"):
        monkeypatch.setattr(openclaw_module, fn, boom(f"openclaw.{fn}"))


@pytest.mark.parametrize("tool", ["codex", "claude", "kimi", "dsh", "reasonix"])
def test_stored_sessions_checks_schema_before_discovery(
    future_db: Path, monkeypatch, tool: str
) -> None:
    _forbid_discovery(monkeypatch)
    with pytest.raises(UsageDatabaseSchemaTooNewError):
        sessions_module._stored_sessions_for_tool(tool)


def test_activity_insights_checks_schema_before_discovery(
    future_db: Path, monkeypatch
) -> None:
    _forbid_discovery(monkeypatch)
    with pytest.raises(UsageDatabaseSchemaTooNewError):
        sessions_module.get_codex_activity_insights()


def test_openclaw_sync_checks_schema_before_discovery(future_db: Path, monkeypatch) -> None:
    _forbid_discovery(monkeypatch)
    with pytest.raises(UsageDatabaseSchemaTooNewError):
        openclaw_module._sync_openclaw_store([], openclaw_module.PricingDatabase())


def test_preflight_is_silent_on_a_compatible_database() -> None:
    """The guard must not disturb the normal path, nor create the file."""
    from tokdash.usage_store import raise_if_usage_db_incompatible

    raise_if_usage_db_incompatible()  # absent file: no error, no creation
    assert not usage_db_path().exists()

    UsageEntryStore()._connect().close()
    raise_if_usage_db_incompatible()  # current schema: still silent


def test_preflight_stays_silent_on_a_corrupt_database() -> None:
    """Corruption is the fallback paths' business, not the fail-fast guard's."""
    from tokdash.usage_store import raise_if_usage_db_incompatible

    db = usage_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"not a sqlite database")
    raise_if_usage_db_incompatible()


@pytest.fixture
def clean_quota_cooldown(monkeypatch):
    """Isolate the quota refresh cooldown and make the store read deterministic.

    Two host dependencies have to go, or this only passes on a machine that
    happens to have quota providers configured:

    * ``collect_enabled_snapshots`` reads the store via ``collect_local_snapshots``,
      but with no providers it returns [] -- and ``insert_quota_snapshots`` then
      short-circuits on empty rows *before* opening the database, so nothing ever
      reaches the schema guard and the route returns 200.
    * the real collector is called with ``include_network=True``, so the test would
      do live provider I/O in CI.

    The stub below touches the store exactly as the real collector does, so the
    error still comes from the real guard in ``_ensure_schema`` rather than a
    fabricated raise.
    """
    pytest.importorskip("fastapi")
    import tokdash.api as api
    from tokdash.sources import quota as quota_module

    def _collect_touching_store(*, include_network=True, store=None, network_sources=None):
        if store is not None:
            store._connect()
        return []

    monkeypatch.setattr(quota_module, "collect_enabled_snapshots", _collect_touching_store)

    saved = (api._quota_last_refresh_monotonic, api._quota_prev_refresh_monotonic)
    api._quota_last_refresh_monotonic = 0.0
    api._quota_prev_refresh_monotonic = 0.0
    yield api
    api._quota_last_refresh_monotonic, api._quota_prev_refresh_monotonic = saved


def test_quota_refresh_succeeds_on_a_healthy_database(clean_quota_cooldown) -> None:
    """Guards the fixture: with a readable DB the route must still return 200.

    Without this, a stub that silently stopped touching the store would make the
    two tests below pass for the wrong reason.
    """
    api = clean_quota_cooldown
    assert api.refresh_quota() == {"snapshots": 0, "inserted": 0}


def test_quota_refresh_converts_the_typed_error_to_an_actionable_500(
    future_db: Path, clean_quota_cooldown
) -> None:
    """A bare `raise` here reaches FastAPI as a detail-free 500.

    /api/quota/refresh does not go through _cached_route, so it never had the
    typed handler. The dashboard rendered "HTTP 500" -- the remediation existed
    but never left the process.
    """
    api = clean_quota_cooldown
    with pytest.raises(api.HTTPException) as excinfo:
        api.refresh_quota()

    assert excinfo.value.status_code == 500
    detail = str(excinfo.value.detail)
    assert "tokdash update" in detail
    assert str(FUTURE_SCHEMA) in detail and str(SCHEMA_VERSION) in detail


def test_quota_refresh_failure_still_releases_the_cooldown(
    future_db: Path, clean_quota_cooldown
) -> None:
    """Converting the error must not skip the reservation rollback."""
    api = clean_quota_cooldown
    with pytest.raises(api.HTTPException):
        api.refresh_quota()

    # 0.0 means the slot was free and is now reserved by this probe -- i.e. the
    # failed refresh did not burn the user's 60s window.
    assert api._try_begin_quota_refresh() == 0.0
    api._abort_quota_refresh()


def test_every_store_backed_route_reports_the_remediation() -> None:
    """No route may let the typed error escape as a detail-free 500.

    /api/quota/refresh was missed because it does not use _cached_route; this
    guards the whole surface rather than that one route.
    """
    import re

    source = (Path(__import__("tokdash").__file__).parent / "api.py").read_text(encoding="utf-8")
    routes = re.split(r"\n@app\.(?:get|post)\(", source)[1:]
    offenders = []
    for chunk in routes:
        path = chunk.split('"')[1] if '"' in chunk else "?"
        body = chunk.split("\n@app.")[0]
        touches_store = "UsageEntryStore" in body or "_cached_route" in body
        # Either the typed handler, or a generic handler that preserves str(e).
        reports = "UsageDatabaseSchemaTooNewError" in body or "detail=str(e)" in body
        if touches_store and not reports:
            offenders.append(path)
    assert offenders == [], f"routes that would drop the remediation: {offenders}"
