"""A store failure must not make the dashboard keep showing old data.

`reconcileUsageRows` (`src/tokdash/static/index.html`) treats a non-empty
`source_errors` as "this response is incomplete": it drops the fresh payload and
puts that server's last complete snapshot back in its place, or marks the row
partial when there is no snapshot yet.

That is the right call for a source that genuinely failed to answer. It is the
wrong call for the persistent usage cache: when `usage.db` is corrupt the server
fails *open*, reparses the logs and returns complete, correct numbers. Reporting
that fallback as an incomplete source makes the dashboard sit on a stale snapshot
for as long as the database stays broken — the view freezing is worse than the
silence #151 was filed about, which is why this checks the payload the fallback
really produces rather than a hand-written one.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import tokdash
from tokdash import compute, store_logging

INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")

# The page's other stand-ins for the two functions under test.
HARNESS = """
const SERVER = { id: 'local', label: 'Local' };
function selectedServers() { return [SERVER]; }
const lastUsageRowsByServer = new Map();

__FUNCTIONS__

const fresh = JSON.parse(process.argv[2]);
const stale = JSON.parse(process.argv[3]);
const withSnapshot = process.argv[4] === 'true';
const windowKey = 'test-window';

// A complete snapshot from an earlier refresh of the same window.
if (withSnapshot) {
  lastUsageRowsByServer.set(SERVER.id, { payload: stale, at: 1, windowKey });
}

const result = reconcileUsageRows([{ server: SERVER, payload: fresh }], windowKey);

process.stdout.write(JSON.stringify({
  rows: result.rows.map((row) => ({
    tokens: row.payload.total_tokens,
    retained: Boolean(row._retained),
    partial: Boolean(row._partial),
    sourceErrors: row._source_errors || [],
  })),
  retained: result.retained.length,
  unavailable: result.unavailable.length,
}));
"""


def _extract_js_function(src: str, signature: str) -> str:
    start = src.find(signature)
    assert start >= 0, f"{signature} not found"
    depth = 0
    body_start = start + len(signature) - 1 if signature.endswith("{") else src.find("{", start)
    for index in range(body_start, len(src)):
        if src[index] == "{":
            depth += 1
        elif src[index] == "}":
            depth -= 1
            if depth == 0:
                return src[start : index + 1]
    raise AssertionError(f"unterminated function: {signature}")


def _reconcile(tmp_path: Path, fresh: dict, stale: dict = None, *, snapshot: bool = True) -> dict:
    source = INDEX_HTML.read_text(encoding="utf-8")
    functions = "\n".join(
        _extract_js_function(source, signature)
        for signature in (
            "function usageSourceErrors(payload) {",
            "function reconcileUsageRows(rows, windowKey, servers = selectedServers(), cache = lastUsageRowsByServer) {",
        )
    )
    harness = tmp_path / "reconcile.js"
    harness.write_text(
        HARNESS.replace("__FUNCTIONS__", functions), encoding="utf-8"
    )
    result = subprocess.run(
        ["node", str(harness), json.dumps(fresh), json.dumps(stale or {}), str(snapshot).lower()],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


@pytest.fixture
def corrupt_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "tokdash_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_file = data_dir / "usage.sqlite3"
    db_file.write_bytes(b"not a valid sqlite database header\n" * 20)
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(data_dir))
    monkeypatch.setenv("TOKDASH_USAGE_DB_PATH", str(db_file))
    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")
    return db_file


@pytest.fixture(autouse=True)
def _no_store_failure_reported_yet():
    """Each test starts as if the process had just started: the report about a
    broken store is once per process by design.
    """
    store_logging._REPORTED_STORE_FAILURES.clear()
    yield
    store_logging._REPORTED_STORE_FAILURES.clear()


def _stamped(payload: dict, tokens: int) -> dict:
    """The fallback's payload with a recognisable total, so the discard shows.

    The reconciliation decision is what is under test, not the token arithmetic;
    the shape -- including source_errors -- is the payload the real fallback
    produced.
    """
    return {**payload, "total_tokens": tokens}


def test_a_corrupt_store_does_not_freeze_the_dashboard_on_the_last_snapshot(
    tmp_path: Path, corrupt_db: Path
) -> None:
    fallback = compute.get_tools_data_for_range(None, None)
    assert "apps" in fallback, "the fallback must still return complete data"

    fresh = _stamped(fallback, 20)
    stale = _stamped(fallback, 12)

    out = _reconcile(tmp_path, fresh, stale)

    assert out["rows"][0]["tokens"] == 20, (
        "the fresh payload from the fallback must be shown, not the old snapshot"
    )
    assert out["rows"][0]["retained"] is False
    assert out["rows"][0]["partial"] is False
    assert out["retained"] == 0
    assert out["rows"][0]["sourceErrors"] == []


def test_a_source_that_really_failed_is_still_held_back(tmp_path: Path) -> None:
    """The control: reconcileUsageRows does discard a genuinely incomplete row.

    Without this the test above would also pass if the dashboard stopped
    reconciling at all, and "the fallback is accepted" would prove nothing.
    """
    incomplete = _stamped({"total_tokens": 20, "source_errors": ["broken_tool"]}, 20)
    stale = _stamped({"total_tokens": 12}, 12)

    out = _reconcile(tmp_path, incomplete, stale)

    assert out["rows"][0]["tokens"] == 12
    assert out["rows"][0]["retained"] is True
    assert out["rows"][0]["sourceErrors"] == ["broken_tool"]
    assert out["retained"] == 1


def test_the_first_refresh_with_a_failed_store_is_not_marked_partial(
    tmp_path: Path, corrupt_db: Path
) -> None:
    """With no snapshot to fall back on, an incomplete row is labelled partial.

    Same dashboard code, no previous snapshot: the fallback's complete payload
    has to be taken at face value, or the very first read after a restart shows
    a row flagged as broken.
    """
    fallback = compute.get_tools_data_for_range(None, None)
    fresh = _stamped(fallback, 20)

    out = _reconcile(tmp_path, fresh, snapshot=False)

    assert out["rows"][0]["tokens"] == 20
    assert out["rows"][0]["partial"] is False
