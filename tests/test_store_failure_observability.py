"""Tests for persistent store failure observability (Issue #151).

Verifies that when the persistent usage cache fails (corrupt file, database lock,
disk I/O error, poisoned row, etc.), Tokdash:
1. Logs a warning with exc_info detailing the store failure;
2. Fails open to the live parsers so the request succeeds;
3. Surfaces "usage-db" in ``source_errors`` across compute functions and API routes;
4. Preserves coexistence with live parser errors;
5. Does not report false positives when the store is healthy or disabled via TOKDASH_USAGE_DB=0;
6. Still raises UsageDatabaseSchemaTooNewError as a terminal error.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from tokdash import api, compute
from tokdash.api import app
from tokdash.sources import coding_tools, openclaw
from tokdash.usage_store import (
    SCHEMA_VERSION,
    UsageDatabaseSchemaTooNewError,
    UsageEntryStore,
)


@pytest.fixture
def corrupt_db_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Configures a temporary Tokdash environment with a corrupt SQLite database."""
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
    """Configures a temporary Tokdash environment with a healthy initialized SQLite database."""
    data_dir = tmp_path / "tokdash_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_file = data_dir / "usage.sqlite3"

    monkeypatch.setenv("TOKDASH_DATA_DIR", str(data_dir))
    monkeypatch.setenv("TOKDASH_USAGE_DB_PATH", str(db_file))
    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")

    # Initialize store schema
    store = UsageEntryStore(db_file)
    store.status()
    assert db_file.exists()
    return db_file


def test_corrupt_db_records_source_error_in_get_tools_data_for_range(
    corrupt_db_env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Corrupt store emits a warning with exc_info and includes 'usage-db' in source_errors."""
    with caplog.at_level(logging.WARNING, logger="tokdash.compute"):
        result = compute.get_tools_data_for_range(None, None)

    assert "usage-db" in result["source_errors"]
    assert "tokdash persistent usage cache failed; falling back to live parsers" in caplog.text
    assert any(
        record.exc_info and isinstance(record.exc_info[1], sqlite3.DatabaseError)
        for record in caplog.records
        if record.name == "tokdash.compute"
    )
    # Fail-open behavior: entries and apps structures are still populated
    assert "apps" in result
    assert "all_models" in result


def test_corrupt_db_records_source_error_in_run_local_coding_tools_json(
    corrupt_db_env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Corrupt store in run_local_coding_tools_json emits warning and detailed source_errors entry."""
    with caplog.at_level(logging.WARNING, logger="tokdash.compute"):
        data = compute.run_local_coding_tools_json(["--today"])

    errors = data.get("source_errors", [])
    assert any(e.get("source") == "usage-db" for e in errors)
    db_err = next(e for e in errors if e.get("source") == "usage-db")
    assert "file is not a database" in db_err["error"]
    assert "entries" in data
    assert "tokdash persistent usage cache failed; falling back to live parsers" in caplog.text


def test_corrupt_db_surfaces_in_compute_usage_and_api(
    corrupt_db_env: Path
) -> None:
    """Corrupt store propagates 'usage-db' through compute_usage, /api/usage, and /api/tools."""
    usage = compute.compute_usage("today")
    assert "usage-db" in usage["source_errors"]

    client = TestClient(app)

    # /api/usage
    resp = client.get("/api/usage?period=today")
    assert resp.status_code == 200
    payload = resp.json()
    assert "usage-db" in payload["source_errors"]

    # /api/tools
    resp_tools = client.get("/api/tools?period=today")
    assert resp_tools.status_code == 200
    tools_payload = resp_tools.json()
    assert "usage-db" in tools_payload["source_errors"]


def test_corrupt_db_with_sync_false(corrupt_db_env: Path) -> None:
    """Corrupt store with sync=False (comparison window) still captures usage-db in source_errors."""
    result = compute.get_tools_data_for_range(None, None, sync=False)
    assert "usage-db" in result["source_errors"]


def test_corrupt_db_coexists_with_failing_live_parser(
    corrupt_db_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When both the store and a live parser fail, source_errors contains both without duplicates."""
    original_init = coding_tools.CodingToolsUsageTracker.__init__

    class FailingParser:
        def collect(self, since: Any, until: Any) -> list[dict]:
            raise OSError("simulated disk read failure")

    def mock_init(tracker_self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(tracker_self, *args, **kwargs)
        tracker_self.parsers["broken_tool"] = FailingParser()

    monkeypatch.setattr(coding_tools.CodingToolsUsageTracker, "__init__", mock_init)

    result = compute.get_tools_data_for_range(None, None)
    assert "usage-db" in result["source_errors"]
    assert "broken_tool" in result["source_errors"]
    assert len(result["source_errors"]) == len(set(result["source_errors"]))

    # Also verify run_local_coding_tools_json receives both dicts
    data = compute.run_local_coding_tools_json(["--today"])
    sources = [e["source"] for e in data.get("source_errors", [])]
    assert "usage-db" in sources
    assert "broken_tool" in sources


def test_healthy_db_does_not_contain_usage_db_in_source_errors(
    healthy_db_env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Healthy store does not include 'usage-db' in source_errors and logs no warnings."""
    with caplog.at_level(logging.WARNING, logger="tokdash.compute"):
        result = compute.get_tools_data_for_range(None, None)
        data = compute.run_local_coding_tools_json(["--today"])
        usage = compute.compute_usage("today")

    assert "usage-db" not in result["source_errors"]
    assert "usage-db" not in [e.get("source") for e in data.get("source_errors", [])]
    assert "usage-db" not in usage["source_errors"]
    assert "tokdash persistent usage cache failed" not in caplog.text


def test_disabled_db_does_not_contain_usage_db_in_source_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When persistent DB is intentionally disabled via TOKDASH_USAGE_DB=0, no error is flagged."""
    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path))

    with caplog.at_level(logging.WARNING, logger="tokdash.compute"):
        result = compute.get_tools_data_for_range(None, None)
        data = compute.run_local_coding_tools_json(["--today"])
        usage = compute.compute_usage("today")

    assert "usage-db" not in result["source_errors"]
    assert "usage-db" not in [e.get("source") for e in data.get("source_errors", [])]
    assert "usage-db" not in usage["source_errors"]
    assert "tokdash persistent usage cache failed" not in caplog.text


def test_schema_too_new_still_raises_terminal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UsageDatabaseSchemaTooNewError must not be swallowed into live fallback."""
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


def test_openclaw_corrupt_store_logs_warning_and_falls_back(
    corrupt_db_env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Corrupt store during OpenClaw operations logs warning and succeeds via session logs."""
    with caplog.at_level(logging.WARNING, logger="tokdash.sources.openclaw"):
        usage = openclaw.get_usage_for_range(None, None)

    assert "tokdash persistent openclaw cache failed; falling back to session logs" in caplog.text
    assert "total_tokens" in usage
    assert "models" in usage


def test_contributions_corrupt_store_logs_warning_and_falls_back(
    corrupt_db_env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Corrupt store during get_tools_contributions_for_range logs warning and returns contributions."""
    with caplog.at_level(logging.WARNING, logger="tokdash.compute"):
        contribs = compute.get_tools_contributions_for_range(None, None)

    assert "tokdash persistent usage cache failed for contributions; falling back to live parsers" in caplog.text
    assert isinstance(contribs, list)


def test_poisoned_store_sync_failure_records_source_error_and_logs(
    healthy_db_env: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Poisoned row raising OverflowError during sync records 'usage-db' and logs warning."""
    def _overflow_sync(*args: Any, **kwargs: Any) -> Any:
        raise OverflowError("Python int too large to convert to SQLite INTEGER")

    monkeypatch.setattr(compute, "_sync_usage_store", _overflow_sync)

    with caplog.at_level(logging.WARNING, logger="tokdash.compute"):
        result = compute.get_tools_data_for_range(None, None)
        data = compute.run_local_coding_tools_json(["--today"])
        usage = compute.compute_usage("today")

    assert "usage-db" in result["source_errors"]
    assert any(e.get("source") == "usage-db" for e in data.get("source_errors", []))
    assert "usage-db" in usage["source_errors"]
    assert "tokdash persistent usage cache failed; falling back to live parsers" in caplog.text


def test_store_database_locked_records_source_error(
    healthy_db_env: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """OperationalError (database locked) during sync records 'usage-db' and logs warning."""
    def _locked_sync(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(compute, "_sync_usage_store", _locked_sync)

    with caplog.at_level(logging.WARNING, logger="tokdash.compute"):
        result = compute.get_tools_data_for_range(None, None)
        data = compute.run_local_coding_tools_json(["--today"])

    assert "usage-db" in result["source_errors"]
    assert any(e.get("source") == "usage-db" for e in data.get("source_errors", []))
    assert "tokdash persistent usage cache failed; falling back to live parsers" in caplog.text

