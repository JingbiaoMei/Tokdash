"""#151: a persistent-store failure is reported once, and never mislabelled.

The store is a cache. When it cannot be read the request fails open to the live
parsers and the answer is complete, so two things follow, and they pull in
opposite directions:

* **The failure must be visible.** Before this, the failure was swallowed by a
  bare ``except Exception: pass`` and a fallback that ran on every request was
  indistinguishable in the log from one that never ran (#151).
* **The response must not claim to be incomplete.** ``reconcileUsageRows`` in
  ``static/index.html`` treats a non-empty ``source_errors`` as "this server's
  answer is unusable" and falls back to the last snapshot for that server. With
  a corrupt ``usage.db`` that is a persistent condition, so the dashboard would
  sit on stale numbers until somebody repaired the database — worse than the
  silence this issue was filed about. A response served by the fallback looks
  exactly like a healthy one.

The report is once per kind of failure per process: a corrupt or locked database
stays that way, and one Overview refresh makes several requests that touch it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tokdash import api, compute, sessions, store_logging
from tokdash.api import app
from tokdash.sources import coding_tools, openclaw
from tokdash.usage_store import SCHEMA_VERSION, UsageDatabaseSchemaTooNewError, UsageEntryStore

WARNING_TEXT = "tokdash persistent usage cache failed"
OPENCLAW_WARNING_TEXT = "tokdash persistent openclaw cache failed"
SESSION_WARNING_TEXT = "tokdash persistent session cache failed"


@pytest.fixture(autouse=True)
def _no_local_usage_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Nothing here may depend on whether the machine running the suite has
    usage history of its own.

    The sources that read the home need nothing here: conftest's autouse
    ``hermetic_claude_installs`` already redirects ``Path.home()``, ``HOME`` and
    ``USERPROFILE`` to an empty directory, so a developer machine and a runner
    see the same empty home. A test that needs data plants its own log under
    ``Path.home()`` with ``_write_claude_session``.

    Hermes is the exception, and it is load-bearing: ``hermes_search_dirs()``
    falls back to ``%LOCALAPPDATA%\\hermes`` on Windows rather than the home, so
    the conftest redirect does not cover it, and an unpinned run reads the
    real hermes database off this machine into the response, which makes the
    two payloads stop matching.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    coding_tools._sig_cache.clear()
    coding_tools.BaseParser._entry_cache.clear()
    yield
    coding_tools._sig_cache.clear()
    coding_tools.BaseParser._entry_cache.clear()


def _write_claude_session(home: Path, stem: str = "s1") -> Path:
    """One real Claude session log, so "the fallback produced the full answer"
    is something the test establishes rather than something the machine decides."""
    rows = [
        {
            "sessionId": stem,
            "cwd": "/w",
            "timestamp": "2026-05-19T12:00:00Z",
            "message": {
                "role": "assistant",
                "id": message_id,
                "model": "claude-sonnet-4-5",
                "usage": {
                    "input_tokens": 1_000,
                    "output_tokens": 100,
                    "cache_read_input_tokens": 2_000,
                    "cache_creation_input_tokens": 500,
                },
            },
        }
        for message_id in ("m1", "m2")
    ]
    path = home / ".claude" / "projects" / "proj" / f"{stem}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _no_store_failure_reported_yet():
    """Each test starts as if the process had just started.

    The report is once per process by design, so without this a test would see
    whichever call happened to come first in the session.
    """
    store_logging._REPORTED_STORE_FAILURES.clear()
    yield
    store_logging._REPORTED_STORE_FAILURES.clear()


@pytest.fixture
def corrupt_db_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "tokdash_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_file = data_dir / "usage.sqlite3"
    db_file.write_bytes(b"not a valid sqlite database header\n" * 20)

    monkeypatch.setenv("TOKDASH_DATA_DIR", str(data_dir))
    monkeypatch.setenv("TOKDASH_USAGE_DB_PATH", str(db_file))
    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")
    return db_file


@pytest.fixture
def healthy_db_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "tokdash_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_file = data_dir / "usage.sqlite3"

    monkeypatch.setenv("TOKDASH_DATA_DIR", str(data_dir))
    monkeypatch.setenv("TOKDASH_USAGE_DB_PATH", str(db_file))
    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")

    store = UsageEntryStore(db_file)
    store.status()
    assert db_file.exists()
    return db_file


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


# --- the response ---------------------------------------------------------


def test_a_fallback_response_is_shaped_like_a_healthy_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The contract as an equality: same keys, no source errors.

    Both responses are produced in one test because the comparison is the point
    -- "no usage-db in source_errors" on its own would still pass if the
    fallback had quietly dropped every other field too.
    """
    data_dir = tmp_path / "tokdash_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    corrupt = data_dir / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a valid sqlite database header\n" * 20)
    healthy = data_dir / "healthy.sqlite3"
    UsageEntryStore(healthy).status()

    # The live parsers have something to find, so "the fallback still produced
    # the full answer" is a fact about the corrupt store rather than about
    # whether the machine running the suite has any usage history.
    _write_claude_session(Path.home())

    monkeypatch.setenv("TOKDASH_DATA_DIR", str(data_dir))
    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")

    monkeypatch.setenv("TOKDASH_USAGE_DB_PATH", str(corrupt))
    with caplog.at_level(logging.WARNING, logger="tokdash.compute"):
        fallback = compute.get_tools_data_for_range(None, None)

    monkeypatch.setenv("TOKDASH_USAGE_DB_PATH", str(healthy))
    healthy_payload = compute.get_tools_data_for_range(None, None)

    assert set(fallback) == set(healthy_payload)
    assert fallback["source_errors"] == healthy_payload["source_errors"] == []
    # Fail-open behaviour: the live parsers still produced the full answer, so
    # the corrupt-store response carries the same numbers as the healthy one --
    # equal to each other, not merely present. The per-source counters are
    # compared rather than the whole nested dict: a row that came back through
    # the store also carries a `tokens_reasoning` key that a live parse does not
    # emit, so the two dicts are not comparable field for field.
    assert fallback["apps"], "the planted session log should have been parsed"
    assert fallback["apps"].keys() == healthy_payload["apps"].keys()
    for name, stats in fallback["apps"].items():
        assert stats["tokens"] == healthy_payload["apps"][name]["tokens"]
    assert fallback["total_tokens"] == healthy_payload["total_tokens"] > 0


def test_the_fallback_does_not_mark_the_api_response_incomplete(
    corrupt_db_env: Path,
) -> None:
    client = TestClient(app)

    usage = client.get("/api/usage?period=today")
    assert usage.status_code == 200
    assert usage.json()["source_errors"] == []

    tools = client.get("/api/tools?period=today")
    assert tools.status_code == 200
    assert tools.json()["source_errors"] == []


def test_a_live_parser_failure_is_still_reported(corrupt_db_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The source_errors channel keeps its job: a source that really failed.

    Only the store stops using it. A parser that could not read its logs has
    genuinely incomplete data, and that is what reconcileUsageRows is for.
    """
    original_init = coding_tools.CodingToolsUsageTracker.__init__

    class FailingParser:
        def collect(self, since: Any, until: Any) -> list[dict]:
            raise OSError("simulated disk read failure")

    def mock_init(tracker_self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(tracker_self, *args, **kwargs)
        tracker_self.parsers["broken_tool"] = FailingParser()

    monkeypatch.setattr(coding_tools.CodingToolsUsageTracker, "__init__", mock_init)

    result = compute.get_tools_data_for_range(None, None)

    assert result["source_errors"] == ["broken_tool"]


def test_run_local_coding_tools_json_keeps_its_return_shape(healthy_db_env: Path) -> None:
    """The stored path returns what it always returned: entries and nothing else.

    Adding source_errors here changed a shape #151 does not touch, and the
    caller that reads this response is the CLI, not the dashboard.
    """
    data = compute.run_local_coding_tools_json(["--today"])

    assert set(data) == {"entries"}


# --- the report -----------------------------------------------------------


def test_a_broken_store_is_warned_about_once_across_repeated_requests(
    corrupt_db_env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="tokdash.compute"):
        for _ in range(3):
            compute.get_tools_data_for_range(None, None)

    warnings = _warnings(caplog)
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert WARNING_TEXT in warnings[0].getMessage()
    assert isinstance(warnings[0].exc_info[1], sqlite3.DatabaseError)
    # The repeats are still visible when someone goes looking for them.
    assert len(caplog.records) >= 3


def test_each_kind_of_failure_is_reported_separately(
    healthy_db_env: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Keyed on the exception type too, so a second failure mode is not swallowed."""

    def _locked(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(compute, "_sync_usage_store", _locked)
    with caplog.at_level(logging.WARNING, logger="tokdash.compute"):
        compute.get_tools_data_for_range(None, None)

    def _overflow(*args: Any, **kwargs: Any) -> Any:
        raise OverflowError("Python int too large to convert to SQLite INTEGER")

    monkeypatch.setattr(compute, "_sync_usage_store", _overflow)
    with caplog.at_level(logging.WARNING, logger="tokdash.compute"):
        compute.get_tools_data_for_range(None, None)

    messages = [r.getMessage() for r in _warnings(caplog)]
    assert len(messages) == 2
    assert len({r.exc_info[0] for r in _warnings(caplog)}) == 2


def test_a_healthy_store_logs_nothing(healthy_db_env: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="tokdash.compute"):
        compute.get_tools_data_for_range(None, None)
        compute.run_local_coding_tools_json(["--today"])
        compute.get_tools_contributions_for_range(None, None)

    assert WARNING_TEXT not in caplog.text


def test_a_disabled_store_logs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path))

    with caplog.at_level(logging.DEBUG, logger="tokdash.compute"):
        compute.get_tools_data_for_range(None, None)

    assert WARNING_TEXT not in caplog.text


def test_openclaw_logs_the_failure_and_falls_back(
    corrupt_db_env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="tokdash.sources.openclaw"):
        usage = openclaw.get_usage_for_range(None, None)

    assert "tokdash persistent openclaw cache failed; falling back to session logs" in caplog.text
    assert "total_tokens" in usage
    assert "models" in usage


def test_contributions_log_the_failure_and_fall_back(
    corrupt_db_env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="tokdash.compute"):
        contributions = compute.get_tools_contributions_for_range(None, None)

    assert "tokdash persistent usage cache failed for contributions" in caplog.text
    assert isinstance(contributions, list)


# --- what stays terminal --------------------------------------------------


def test_schema_too_new_still_raises_terminal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A newer database is not a sick cache: it never heals, so it must not be
    swallowed into the live fallback -- for the same reason it is not in the
    once-per-process report."""
    data_dir = tmp_path / "tokdash_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_file = data_dir / "usage.sqlite3"

    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION + 1),))
    conn.commit()
    conn.close()

    monkeypatch.setenv("TOKDASH_DATA_DIR", str(data_dir))
    monkeypatch.setenv("TOKDASH_USAGE_DB_PATH", str(db_file))
    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")

    with pytest.raises(UsageDatabaseSchemaTooNewError):
        compute.get_tools_data_for_range(None, None)

    with pytest.raises(UsageDatabaseSchemaTooNewError):
        compute.run_local_coding_tools_json(["--today"])

    assert WARNING_TEXT not in caplog.text

# --- one report per site, not per exception type ---------------------------


def test_the_session_cache_stops_repeating_itself_on_every_request(
    corrupt_db_env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The session cache was the loud site this never reached.

    One Overview refresh asks for sessions across five tools and reads each more
    than once, and every failed read logged a full traceback, so a corrupt
    database put 45 of them in the journal per refresh -- the flood the
    once-per-process rule exists to prevent.
    """
    with caplog.at_level(logging.DEBUG, logger="tokdash.sessions"):
        for _ in range(3):
            sessions._raw_sessions_for_tool("codex")

    messages = [r.getMessage() for r in _warnings(caplog)]
    assert len(messages) == 1, messages
    assert SESSION_WARNING_TEXT in messages[0]
    assert isinstance(_warnings(caplog)[0].exc_info[1], sqlite3.DatabaseError)
    # Still there for anyone who goes looking, just not in the journal.
    assert len(caplog.records) >= 3


def test_each_unreadable_tool_is_reported_separately(
    corrupt_db_env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Five broken stores are five problems, even from one handler.

    Deduplicating on the message is what keeps the per-tool line from collapsing
    into whichever tool happened to fail first; deduplicating on the site alone
    would lose that.
    """
    with caplog.at_level(logging.WARNING, logger="tokdash.sessions"):
        for tool in ("codex", "claude"):
            sessions._raw_sessions_for_tool(tool)

    messages = [r.getMessage() for r in _warnings(caplog)]
    assert len(messages) == 2, messages
    assert any("tool=codex" in m for m in messages)
    assert any("tool=claude" in m for m in messages)


def test_one_site_cannot_silence_another(
    corrupt_db_env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Every site that reads the store reports, not just the first one to fail.

    Keyed on the exception type alone, openclaw claimed the ``DatabaseError``
    during startup and the compute path then reported at debug for the rest of
    the process -- so the compute failure was invisible on a live server while
    the same call logged correctly in a process that reached it first.
    """
    with caplog.at_level(logging.WARNING):
        openclaw.get_usage_for_range(None, None)
        compute.get_tools_data_for_range(None, None)
        sessions._raw_sessions_for_tool("codex")

    warnings = _warnings(caplog)
    messages = [r.getMessage() for r in warnings]
    assert any(OPENCLAW_WARNING_TEXT in m for m in messages), messages
    assert any(WARNING_TEXT in m for m in messages), messages
    assert any(SESSION_WARNING_TEXT in m for m in messages), messages
    # All three carried the traceback, not just the message.
    assert all(r.exc_info for r in warnings)


def test_every_store_site_uses_the_one_policy() -> None:
    """One copy of the rule, so it cannot drift between call sites."""
    assert compute.log_store_failure is store_logging.log_store_failure
    assert sessions.log_store_failure is store_logging.log_store_failure
    assert openclaw.log_store_failure is store_logging.log_store_failure


def test_a_caller_that_varies_its_message_still_logs_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The message must not be part of the key.

    A caller that interpolates a path, a row count or the exception's own text
    would otherwise mint a fresh key per occurrence and re-flood the journal --
    silently, because the per-tool test would still pass. Only ``site`` and the
    exception type decide what has already been reported.
    """
    logger = logging.getLogger("tokdash.probe")
    messages = [
        f"store read failed at /var/lib/tokdash/usage-{n}.sqlite3 after {n} rows"
        for n in range(3)
    ]
    with caplog.at_level(logging.DEBUG, logger="tokdash.probe"):
        for message in messages:
            store_logging.log_store_failure(
                logger, message, sqlite3.DatabaseError(messages[0]), site="probe.read"
            )

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert warnings[0].getMessage() == messages[0]
    # The repeats stay reachable for anyone who turns the level up.
    assert len(caplog.records) == 3


def test_one_site_reporting_two_kinds_of_failure_still_says_both(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Granularity comes from ``site``, so a caller that needs two lines
    passes two sites -- the session cache does this per tool."""
    logger = logging.getLogger("tokdash.probe")
    with caplog.at_level(logging.WARNING, logger="tokdash.probe"):
        store_logging.log_store_failure(
            logger, "read failed", sqlite3.DatabaseError("x"), site="probe:codex"
        )
        store_logging.log_store_failure(
            logger, "read failed", sqlite3.DatabaseError("x"), site="probe:claude"
        )
        # Same site, different exception type: a new failure mode, so a new line.
        store_logging.log_store_failure(
            logger, "locked", sqlite3.OperationalError("locked"), site="probe:codex"
        )

    assert len(_warnings(caplog)) == 3
