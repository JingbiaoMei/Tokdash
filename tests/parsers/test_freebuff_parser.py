"""Tests for FreebuffParser: Freebuff Desktop's per-project ``desktop-v2.db``
stores.

Pins the accounting conventions verified against four live stores and the
shipped orchestrator bundle:

* one entry per ``role='assistant'`` row that carries a non-zero ``usage``;
* ``cachedInputTokens`` is a SUBSET of ``inputTokens`` (cache-inclusive
  prompt) and is split into its own bucket;
* ``reasoningOutputTokens`` is a SUBSET of ``outputTokens`` and is split out;
* ``cacheWrite`` is zero by construction;
* ``costUsd`` is never read — cost is pricing-DB only;
* ``threads.model`` splits once on ``/`` into (provider, model), and an opaque
  ``m-<hex>`` handle keeps provider "" and prices at 0.00;
* milliseconds timestamps;
* the store is read through the WAL snapshot helper (live rows sit in -wal);
* every project DB is unioned, and ``entry_id`` is source-global.
"""
import json
import sqlite3
from pathlib import Path

import pytest

from tokdash import clientpaths
from tokdash.pricing import PricingDatabase
from tokdash.sources import coding_tools as ct
from tokdash.sources.coding_tools import (
    BaseParser,
    CodingToolsUsageTracker,
    FreebuffParser,
    _sig_cache,
)
from tokdash.usage_store import (
    UsageEntryStore,
    UsageFileUnreadable,
    UsageFileVanished,
)

# 2026-10-02T13:04:12.181Z — a live row's timestamp, milliseconds.
TS_MS = 1_790_924_652_181
MODEL_QUALIFIED = "z-ai/glm-5.3-flash"
MODEL_HANDLE = "m-22ff70c712"

# The real store's schema, trimmed to the columns the parser reads. The
# message table's ``seq`` is AUTOINCREMENT in the shipped schema, which is what
# makes the dedup key stable; keep it that way here.
_THREADS_DDL = """
CREATE TABLE threads (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  project_path TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT 'New thread',
  model TEXT,
  harness_id TEXT,
  sponsored INTEGER DEFAULT 0,
  byok_connection TEXT,
  agent_mode TEXT NOT NULL DEFAULT 'build',
  created_at INTEGER NOT NULL
)
"""

_MESSAGES_DDL = """
CREATE TABLE messages (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id TEXT NOT NULL,
  request_id TEXT,
  input_id TEXT,
  role TEXT NOT NULL,
  parts_json TEXT NOT NULL DEFAULT '[]',
  attachments_json TEXT NOT NULL DEFAULT '[]',
  metrics_json TEXT NOT NULL DEFAULT '{}',
  ts INTEGER NOT NULL
)
"""

# The autonomous (mission / auto-run) loop's per-decision ledger. Only
# manager_json is read by the parser; outcome_json is deliberately not.
_RECEIPTS_DDL = """
CREATE TABLE auto_run_decision_receipts (
  id                  TEXT PRIMARY KEY,
  thread_id           TEXT NOT NULL,
  campaign_started_at INTEGER NOT NULL,
  ordinal             INTEGER NOT NULL,
  effort              INTEGER NOT NULL,
  status              TEXT NOT NULL,
  queue_item_id       TEXT,
  decision_json       TEXT,
  manager_json        TEXT NOT NULL,
  outcome_json        TEXT,
  created_at          INTEGER NOT NULL,
  updated_at          INTEGER NOT NULL
)
"""


def _metrics(
    *,
    input_tokens=0,
    cached_input=0,
    output_tokens=0,
    reasoning=0,
    cost_usd=0,
):
    """A ``metrics_json`` payload in the store's own shape.

    ``totalTokens`` is written as ``inputTokens + outputTokens`` because that
    identity held on every live row — it is what establishes that the cached
    share sits INSIDE input.
    """
    return json.dumps({
        "context": {"usedTokens": input_tokens + output_tokens},
        "compactions": [],
        "usage": {
            "inputTokens": input_tokens,
            "cachedInputTokens": cached_input,
            "outputTokens": output_tokens,
            "reasoningOutputTokens": reasoning,
            "totalTokens": input_tokens + output_tokens,
        },
        "costUsd": cost_usd,
    })


def _manager(
    *,
    harness_id="codebuff",
    model=MODEL_HANDLE,
    input_tokens=0,
    cached_input=0,
    output_tokens=0,
    reasoning=0,
    duration_ms=20000,
    finished_at=None,
    usage=True,
    cost_usd=0,
):
    """A ``manager_json`` payload: the mission manager's own model call.

    Shape taken from a live row. ``usage=False`` reproduces a cancelled or
    failed decision, which persists ``usage: {}``.
    """
    payload = {
        "model": model,
        "startedAt": TS_MS,
        "finishedAt": finished_at if finished_at is not None else (TS_MS + duration_ms),
        "durationMs": duration_ms,
        "costUsd": cost_usd,
    }
    if harness_id is not None:
        payload["harnessId"] = harness_id
    if usage:
        payload["usage"] = {
            "inputTokens": input_tokens,
            "cachedInputTokens": cached_input,
            "outputTokens": output_tokens,
            "reasoningOutputTokens": reasoning,
            "totalTokens": input_tokens + output_tokens,
        }
    else:
        payload["usage"] = {}
        payload["usageIncomplete"] = True
    return json.dumps(payload)


def _outcome(
    *,
    input_tokens=0,
    cached_input=0,
    output_tokens=0,
    reasoning=0,
):
    """An ``outcome_json`` payload.

    Its ``usage`` restates the turn's final assistant message, so the parser
    must NOT read it — tests use it to prove no double-count.
    """
    return json.dumps({
        "queuedAt": TS_MS,
        "startedAt": TS_MS,
        "finishedAt": TS_MS + 1000,
        "turnOutcome": "completed",
        "usage": {
            "inputTokens": input_tokens,
            "cachedInputTokens": cached_input,
            "outputTokens": output_tokens,
            "reasoningOutputTokens": reasoning,
            "totalTokens": input_tokens + output_tokens,
        },
    })


def _make_db(
    project_dir: Path,
    *,
    thread_id="t1",
    model=MODEL_QUALIFIED,
    harness_id="codebuff",
    sponsored=0,
    byok_connection=None,
    messages=(),
    receipts=(),
    checkpoint=True,
) -> Path:
    """Create one project's ``desktop-v2.db``.

    ``messages`` is a sequence of ``(role, metrics_json, ts)``; ``None`` for
    metrics writes the column default ``'{}'`` (the shipped column is
    ``TEXT NOT NULL DEFAULT '{}'``, so a message without usage still holds an
    object, never SQL NULL).

    ``receipts`` is a sequence of ``(receipt_id, manager_json, outcome_json,
    updated_at)`` for the auto-run ledger; ``None`` manager/outcome writes SQL
    NULL for that column.

    ``checkpoint=False`` leaves the rows in the WAL (never truncating it) so
    the snapshot path is exercised for real.
    """
    project_dir.mkdir(parents=True, exist_ok=True)
    db = project_dir / "desktop-v2.db"
    conn = sqlite3.connect(db)
    try:
        # The shipped store is WAL-mode; match it so the -wal semantics the
        # parser relies on are real.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_THREADS_DDL)
        conn.execute(_MESSAGES_DDL)
        conn.execute(_RECEIPTS_DDL)
        conn.execute(
            "INSERT INTO threads (id, project_id, project_path, title, model, "
            "harness_id, sponsored, byok_connection, agent_mode, created_at) "
            "VALUES (?, 'p1', ?, 'T', ?, ?, ?, ?, 'build', ?)",
            (thread_id, str(project_dir), model, harness_id, sponsored, byok_connection, TS_MS),
        )
        for role, metrics_json, ts in messages:
            conn.execute(
                "INSERT INTO messages (thread_id, role, metrics_json, ts) "
                "VALUES (?, ?, ?, ?)",
                (thread_id, role, metrics_json if metrics_json is not None else "{}", ts),
            )
        for receipt_id, manager_json, outcome_json, updated_at in receipts:
            conn.execute(
                "INSERT INTO auto_run_decision_receipts (id, thread_id, "
                "campaign_started_at, ordinal, effort, status, manager_json, "
                "outcome_json, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, 3, 'completed', ?, ?, ?, ?)",
                (receipt_id, thread_id, TS_MS, manager_json, outcome_json,
                 TS_MS, updated_at),
            )
        conn.commit()
        if checkpoint:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return db


def _fresh(monkeypatch, tmp_path) -> FreebuffParser:
    """A parser rooted at a fake home, with both caches cleared."""
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return FreebuffParser(PricingDatabase())


def _projects(tmp_path) -> Path:
    return tmp_path / ".config" / "freebuff-desktop" / "projects"


# --- path resolution --------------------------------------------------------


def test_home_and_db_paths_are_derived_from_home(monkeypatch, tmp_path):
    _sig_cache.clear()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert clientpaths.freebuff_desktop_home() == (
        tmp_path / ".config" / "freebuff-desktop"
    )
    _make_db(_projects(tmp_path) / "proj-a-11111111")
    _make_db(_projects(tmp_path) / "proj-b-22222222")
    # Sorted, and only real desktop-v2.db files.
    (_projects(tmp_path) / "proj-c-33333333").mkdir(parents=True)
    paths = clientpaths.freebuff_desktop_db_paths()
    assert [p.parent.name for p in paths] == ["proj-a-11111111", "proj-b-22222222"]


def test_no_projects_dir_yields_nothing(monkeypatch, tmp_path):
    parser = _fresh(monkeypatch, tmp_path)
    assert parser._file_signatures() == ()
    assert parser._parse_all() == []


# --- core accounting --------------------------------------------------------


def test_cache_is_split_out_of_input(monkeypatch, tmp_path):
    """``cachedInputTokens`` sits inside ``inputTokens``; split, never add."""
    _make_db(
        _projects(tmp_path) / "p1",
        messages=[
            ("user", None, TS_MS),
            ("assistant", _metrics(input_tokens=1000, cached_input=900,
                                   output_tokens=50), TS_MS + 1),
        ],
    )
    entries = _fresh(monkeypatch, tmp_path)._parse_all()

    assert len(entries) == 1
    e = entries[0]
    # 1000 total input, 900 of it cached -> 100 fresh, 900 cacheRead.
    assert (e["input"], e["cacheRead"]) == (100, 900)
    assert e["input"] + e["cacheRead"] == 1000  # conserved, not doubled
    assert (e["output"], e["cacheWrite"], e["reasoning"]) == (50, 0, 0)


def test_reasoning_is_split_out_of_output(monkeypatch, tmp_path):
    """``reasoningOutputTokens`` is a subset of output; compute.py adds it on
    top for display, so it must be removed from output here."""
    _make_db(
        _projects(tmp_path) / "p1",
        messages=[
            ("assistant", _metrics(output_tokens=1000, reasoning=400), TS_MS),
        ],
    )
    entries = _fresh(monkeypatch, tmp_path)._parse_all()

    e = entries[0]
    assert (e["output"], e["reasoning"]) == (600, 400)
    assert e["output"] + e["reasoning"] == 1000


def test_model_splits_once_into_provider_and_model(monkeypatch, tmp_path):
    _make_db(
        _projects(tmp_path) / "p1",
        model=MODEL_QUALIFIED,
        messages=[("assistant", _metrics(input_tokens=10, output_tokens=5), TS_MS)],
    )
    e = _fresh(monkeypatch, tmp_path)._parse_all()[0]
    assert (e["provider"], e["model"]) == ("z-ai", "glm-5.3-flash")
    # The priced model is what the pricing DB can resolve.
    assert e["cost"] == PricingDatabase().get_cost("glm-5.3-flash", 10, 5, 0, 0)
    assert e["cost"] > 0


def test_second_slash_stays_in_the_model(monkeypatch, tmp_path):
    _make_db(
        _projects(tmp_path) / "p1",
        model="vendor/family/model-x",
        messages=[("assistant", _metrics(input_tokens=1, output_tokens=1), TS_MS)],
    )
    e = _fresh(monkeypatch, tmp_path)._parse_all()[0]
    assert (e["provider"], e["model"]) == ("vendor", "family/model-x")


def test_opaque_handle_keeps_no_provider_and_prices_at_zero(monkeypatch, tmp_path):
    """The catalog handle form ``m-<hex>`` is opaque and rotates per account;
    its mapping is server-side and never written to disk, so it cannot be
    resolved offline. The row still counts its tokens."""
    _make_db(
        _projects(tmp_path) / "p1",
        model=MODEL_HANDLE,
        messages=[
            ("assistant", _metrics(input_tokens=5000, cached_input=4000,
                                   output_tokens=200), TS_MS),
        ],
    )
    e = _fresh(monkeypatch, tmp_path)._parse_all()[0]

    assert e["provider"] == ""
    assert e["model"] == MODEL_HANDLE
    assert e["cost"] == 0.0
    # Tokens are still fully counted despite the unpriced model.
    assert (e["input"], e["cacheRead"], e["output"]) == (1000, 4000, 200)


def test_cost_usd_is_never_read(monkeypatch, tmp_path):
    """The store writes ``costUsd`` (always 0 on the free tier); a non-zero
    value there must not become the row's cost."""
    _make_db(
        _projects(tmp_path) / "p1",
        model=MODEL_QUALIFIED,
        messages=[
            ("assistant", _metrics(input_tokens=1_000_000, output_tokens=0,
                                   cost_usd=999.0), TS_MS),
        ],
    )
    e = _fresh(monkeypatch, tmp_path)._parse_all()[0]
    expected = PricingDatabase().get_cost("glm-5.3-flash", 1_000_000, 0, 0, 0)
    assert e["cost"] == expected
    assert e["cost"] != 999.0


# --- row selection ----------------------------------------------------------


def test_only_assistant_rows_with_usage_count(monkeypatch, tmp_path):
    _make_db(
        _projects(tmp_path) / "p1",
        messages=[
            ("user", None, TS_MS),
            ("assistant", _metrics(input_tokens=10, output_tokens=5), TS_MS + 1),
            ("user", None, TS_MS + 2),
            # An assistant row with no usage object at all.
            ("assistant", json.dumps({"context": {}}), TS_MS + 3),
            # An all-zero usage object: a cancelled/errored turn.
            ("assistant", _metrics(), TS_MS + 4),
            ("assistant", _metrics(input_tokens=7, output_tokens=3), TS_MS + 5),
        ],
    )
    entries = _fresh(monkeypatch, tmp_path)._parse_all()
    assert len(entries) == 2
    assert [e["timestamp"] for e in entries] == [TS_MS + 1, TS_MS + 5]


def test_unparsable_metrics_is_skipped(monkeypatch, tmp_path):
    _make_db(
        _projects(tmp_path) / "p1",
        messages=[
            ("assistant", "{not json", TS_MS),
            ("assistant", "[]", TS_MS + 1),
            ("assistant", _metrics(input_tokens=1, output_tokens=1), TS_MS + 2),
        ],
    )
    entries = _fresh(monkeypatch, tmp_path)._parse_all()
    assert len(entries) == 1
    assert entries[0]["timestamp"] == TS_MS + 2


def test_non_positive_timestamp_is_skipped(monkeypatch, tmp_path):
    _make_db(
        _projects(tmp_path) / "p1",
        messages=[
            ("assistant", _metrics(input_tokens=1, output_tokens=1), 0),
            ("assistant", _metrics(input_tokens=2, output_tokens=2), TS_MS),
        ],
    )
    entries = _fresh(monkeypatch, tmp_path)._parse_all()
    assert [e["timestamp"] for e in entries] == [TS_MS]


# --- identity ---------------------------------------------------------------


def test_entry_id_is_thread_scoped_and_stable(monkeypatch, tmp_path):
    _make_db(
        _projects(tmp_path) / "p1",
        thread_id="thread-abc",
        messages=[
            ("assistant", _metrics(input_tokens=1, output_tokens=1), TS_MS),
            ("assistant", _metrics(input_tokens=2, output_tokens=2), TS_MS + 1),
        ],
    )
    parser = _fresh(monkeypatch, tmp_path)
    first = parser._parse_all()
    assert [e["entry_id"] for e in first] == [
        "freebuff:thread-abc:1",
        "freebuff:thread-abc:2",
    ]
    # Re-reading the same store yields the same keys, so nothing re-bills.
    assert [e["entry_id"] for e in parser._parse_all()] == [
        "freebuff:thread-abc:1",
        "freebuff:thread-abc:2",
    ]


def test_projects_are_unioned(monkeypatch, tmp_path):
    """Each project is a disjoint store; all of them are real usage."""
    _make_db(
        _projects(tmp_path) / "p1",
        thread_id="t-a",
        messages=[("assistant", _metrics(input_tokens=10, output_tokens=1), TS_MS)],
    )
    _make_db(
        _projects(tmp_path) / "p2",
        thread_id="t-b",
        messages=[("assistant", _metrics(input_tokens=20, output_tokens=2), TS_MS + 1)],
    )
    entries = _fresh(monkeypatch, tmp_path)._parse_all()

    assert len(entries) == 2
    assert sum(e["input"] for e in entries) == 30
    # Entry keys are source-global, so the two projects cannot collide.
    assert len({e["entry_id"] for e in entries}) == 2


# --- WAL --------------------------------------------------------------------


def test_rows_left_in_the_wal_are_read(monkeypatch, tmp_path):
    """The desktop app holds the store open; live rows sit in the -wal. A
    read-only open in place would miss them, so the snapshot path is used.

    SQLite checkpoints and removes the -wal when its last connection closes,
    so the writer connection is deliberately held open here — that is exactly
    the state the running app leaves the store in.
    """
    project = _projects(tmp_path) / "p1"
    project.mkdir(parents=True, exist_ok=True)
    db = project / "desktop-v2.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_THREADS_DDL)
        conn.execute(_MESSAGES_DDL)
        conn.execute(_RECEIPTS_DDL)
        conn.execute(
            "INSERT INTO threads (id, project_id, project_path, title, model, "
            "harness_id, agent_mode, created_at) VALUES ('t1', 'p1', ?, 'T', ?, "
            "'codebuff', 'build', ?)",
            (str(project), MODEL_QUALIFIED, TS_MS),
        )
        conn.execute(
            "INSERT INTO messages (thread_id, role, metrics_json, ts) "
            "VALUES ('t1', 'assistant', ?, ?)",
            (_metrics(input_tokens=42, output_tokens=8), TS_MS),
        )
        conn.commit()

        # The writer is still open: the rows are in the -wal, not the main db.
        assert (project / "desktop-v2.db-wal").exists()

        entries = _fresh(monkeypatch, tmp_path)._parse_all()
        assert len(entries) == 1
        assert entries[0]["input"] == 42
    finally:
        conn.close()


def test_read_never_creates_sidecars_in_the_source_tree(monkeypatch, tmp_path):
    """The reader must not write into the user's store directory: the -shm is
    the WAL coordination file, and a read-only open in place would create it."""
    project = _projects(tmp_path) / "p1"
    project.mkdir(parents=True, exist_ok=True)
    db = project / "desktop-v2.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_THREADS_DDL)
        conn.execute(_MESSAGES_DDL)
        conn.execute(_RECEIPTS_DDL)
        conn.execute(
            "INSERT INTO threads (id, project_id, project_path, title, model, "
            "harness_id, agent_mode, created_at) VALUES ('t1', 'p1', ?, 'T', ?, "
            "'codebuff', 'build', ?)",
            (str(project), MODEL_QUALIFIED, TS_MS),
        )
        conn.execute(
            "INSERT INTO messages (thread_id, role, metrics_json, ts) "
            "VALUES ('t1', 'assistant', ?, ?)",
            (_metrics(input_tokens=9, output_tokens=1), TS_MS),
        )
        conn.commit()

        before = sorted(p.name for p in project.iterdir())
        entries = _fresh(monkeypatch, tmp_path)._parse_all()
        after = sorted(p.name for p in project.iterdir())

        assert len(entries) == 1
        assert before == after
    finally:
        conn.close()


def test_a_corrupt_db_does_not_blank_the_others(monkeypatch, tmp_path):
    good = _make_db(
        _projects(tmp_path) / "p-good",
        messages=[("assistant", _metrics(input_tokens=5, output_tokens=5), TS_MS)],
    )
    bad = _projects(tmp_path) / "p-bad" / "desktop-v2.db"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"this is not a sqlite database at all")

    entries = _fresh(monkeypatch, tmp_path)._parse_all()
    assert len(entries) == 1
    assert good.exists()


# --- registry ---------------------------------------------------------------


def test_registered_and_declares_capabilities():
    tracker = CodingToolsUsageTracker()
    assert isinstance(tracker.parsers["freebuff"], FreebuffParser)
    parser = tracker.parsers["freebuff"]
    assert parser.sync_capability.mode == "file_replace"
    assert parser.sync_capability.append_jsonl is False
    assert parser.sync_capability.session_store is False
    # Stored persistently, so it must declare an identity (the registry test
    # in test_usage_cache_identity enforces the same rule globally).
    assert parser.persistent_parser_version == 4
    assert parser.persistent_parser_signature()["object"].endswith("FreebuffParser")


def test_billing_record_is_priceable(monkeypatch, tmp_path):
    _make_db(
        _projects(tmp_path) / "p1",
        model=MODEL_QUALIFIED,
        messages=[
            ("assistant", _metrics(input_tokens=1000, cached_input=400,
                                   output_tokens=100), TS_MS),
        ],
    )
    e = _fresh(monkeypatch, tmp_path)._parse_all()[0]
    billing = e["_billing"]
    assert billing["kind"] == "pricing"
    # Both the bare and the qualified id are candidates, with the qualified
    # candidate first (Hermes/Muse rule).
    assert billing["models"] == ["z-ai/glm-5.3-flash", "glm-5.3-flash"]
    # Billing uses the FULL output (reasoning stays billed at the output rate)
    # and the disjoint fresh-input/cache split.
    assert billing["input"] == 600
    assert billing["output"] == 100
    assert billing["cache_read"] == 400
    assert billing["cache_write"] == 0
    assert ct.usage_entry_cost(billing, PricingDatabase()) == e["cost"]


# --- auto-run manager usage -------------------------------------------------


def test_manager_usage_is_counted(monkeypatch, tmp_path):
    """The mission manager's decision call is billed but is NOT an assistant
    message, so it is its own entry."""
    _make_db(
        _projects(tmp_path) / "p1",
        model=MODEL_QUALIFIED,
        messages=[("assistant", _metrics(input_tokens=10, output_tokens=5), TS_MS)],
        receipts=[
            ("rec-1", _manager(model="m-aaaa1111", input_tokens=1000,
                               cached_input=400, output_tokens=50, reasoning=10,
                               finished_at=TS_MS + 500),
             None, TS_MS + 500),
        ],
    )
    entries = _fresh(monkeypatch, tmp_path)._parse_all()

    assert len(entries) == 2
    receipt = [e for e in entries if ":receipt:" in e["entry_id"]]
    assert len(receipt) == 1
    e = receipt[0]
    # Same cache/reasoning split as a message row.
    assert (e["input"], e["cacheRead"]) == (600, 400)
    assert (e["output"], e["reasoning"]) == (40, 10)
    # The manager's OWN model wins over the thread's.
    assert e["model"] == "m-aaaa1111"
    assert e["timestamp"] == TS_MS + 500
    assert e["entry_id"] == "freebuff:t1:receipt:rec-1"


def test_manager_usage_falls_back_to_the_thread_model(monkeypatch, tmp_path):
    """A manager payload without its own model prices at the thread's."""
    _make_db(
        _projects(tmp_path) / "p1",
        model=MODEL_QUALIFIED,
        messages=[],
        receipts=[
            ("rec-1", _manager(model=None, input_tokens=10, output_tokens=5),
             None, TS_MS),
        ],
    )
    e = _fresh(monkeypatch, tmp_path)._parse_all()[0]
    assert e["model"] == "glm-5.3-flash"
    assert e["provider"] == "z-ai"


def test_outcome_json_is_never_read(monkeypatch, tmp_path):
    """``outcome_json.usage`` restates the turn's final assistant message.

    It is a verified duplicate in every live store (37/37 matched an existing
    message row), so reading it would double-count every completed auto-run
    turn. This is the guard for that.
    """
    _make_db(
        _projects(tmp_path) / "p1",
        model=MODEL_QUALIFIED,
        messages=[
            ("assistant", _metrics(input_tokens=1000, cached_input=900,
                                   output_tokens=50), TS_MS),
        ],
        receipts=[
            # The outcome restates the message row above, exactly.
            ("rec-1", _manager(usage=False), 
             _outcome(input_tokens=1000, cached_input=900, output_tokens=50),
             TS_MS + 10),
        ],
    )
    entries = _fresh(monkeypatch, tmp_path)._parse_all()

    # Only the message row: the manager had no usage and the outcome is ignored.
    assert len(entries) == 1
    assert entries[0]["entry_id"] == "freebuff:t1:1"
    assert entries[0]["input"] == 100


def test_incomplete_manager_usage_is_skipped(monkeypatch, tmp_path):
    """A cancelled or failed decision persists ``usage: {}`` — not usage."""
    _make_db(
        _projects(tmp_path) / "p1",
        messages=[],
        receipts=[
            ("rec-1", _manager(usage=False), None, TS_MS),
            ("rec-2", _manager(input_tokens=7, output_tokens=3), None, TS_MS + 1),
        ],
    )
    entries = _fresh(monkeypatch, tmp_path)._parse_all()
    assert [e["entry_id"] for e in entries] == ["freebuff:t1:receipt:rec-2"]


def test_manager_and_message_entry_ids_never_collide(monkeypatch, tmp_path):
    """A receipt key must be distinct from every message key, or the store's
    (source, entry_key) upsert would silently drop one of them."""
    _make_db(
        _projects(tmp_path) / "p1",
        messages=[
            ("assistant", _metrics(input_tokens=1, output_tokens=1), TS_MS),
            ("assistant", _metrics(input_tokens=2, output_tokens=2), TS_MS + 1),
        ],
        receipts=[
            ("1", _manager(input_tokens=3, output_tokens=3), None, TS_MS + 2),
        ],
    )
    entries = _fresh(monkeypatch, tmp_path)._parse_all()
    ids = [e["entry_id"] for e in entries]
    assert len(ids) == len(set(ids)) == 3
    # The receipt id "1" must not masquerade as message seq 1.
    assert "freebuff:t1:receipt:1" in ids
    assert "freebuff:t1:1" in ids


def test_manager_entries_are_stable_across_reads(monkeypatch, tmp_path):
    _make_db(
        _projects(tmp_path) / "p1",
        messages=[],
        receipts=[("rec-1", _manager(input_tokens=5, output_tokens=5), None, TS_MS)],
    )
    parser = _fresh(monkeypatch, tmp_path)
    assert [e["entry_id"] for e in parser._parse_all()] == ["freebuff:t1:receipt:rec-1"]
    assert [e["entry_id"] for e in parser._parse_all()] == ["freebuff:t1:receipt:rec-1"]


def test_manager_usage_survives_the_live_wal(monkeypatch, tmp_path):
    """Manager rows live in the -wal too while the app is open."""
    project = _projects(tmp_path) / "p1"
    project.mkdir(parents=True, exist_ok=True)
    db = project / "desktop-v2.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_THREADS_DDL)
        conn.execute(_MESSAGES_DDL)
        conn.execute(_RECEIPTS_DDL)
        conn.execute(
            "INSERT INTO threads (id, project_id, project_path, title, model, "
            "harness_id, agent_mode, created_at) VALUES ('t1', 'p1', ?, 'T', ?, "
            "'codebuff', 'build', ?)",
            (str(project), MODEL_QUALIFIED, TS_MS),
        )
        conn.execute(
            "INSERT INTO auto_run_decision_receipts (id, thread_id, "
            "campaign_started_at, ordinal, effort, status, manager_json, "
            "created_at, updated_at) VALUES ('rec-1', 't1', ?, 1, 3, 'completed', ?, ?, ?)",
            (TS_MS, _manager(input_tokens=42, output_tokens=8), TS_MS, TS_MS + 9),
        )
        conn.commit()
        assert (project / "desktop-v2.db-wal").exists()

        entries = _fresh(monkeypatch, tmp_path)._parse_all()
        assert len(entries) == 1
        assert entries[0]["input"] == 42
    finally:
        conn.close()


def test_manager_billing_record_is_priceable(monkeypatch, tmp_path):
    _make_db(
        _projects(tmp_path) / "p1",
        model=MODEL_QUALIFIED,
        messages=[],
        receipts=[
            ("rec-1", _manager(model=MODEL_QUALIFIED, input_tokens=1000,
                               cached_input=400, output_tokens=100, reasoning=25),
             None, TS_MS),
        ],
    )
    e = _fresh(monkeypatch, tmp_path)._parse_all()[0]
    b = e["_billing"]
    assert b["kind"] == "pricing"
    assert b["models"] == ["z-ai/glm-5.3-flash", "glm-5.3-flash"]
    # Billing keeps the FULL completion (reasoning billed at the output rate).
    assert b["input"] == 600
    assert b["output"] == 100
    assert b["cache_read"] == 400
    assert ct.usage_entry_cost(b, PricingDatabase()) == e["cost"]
    assert e["cost"] > 0


def test_old_store_without_the_receipts_table_still_reads_messages(monkeypatch, tmp_path):
    """A store written before the auto-run ledger existed must still yield its
    message usage.

    The desktop app migrates its schema additively, so an older build's store
    legitimately lacks ``auto_run_decision_receipts``. An unguarded query would
    raise, and ``_parse_all`` discards the WHOLE database on an error — so the
    missing table would silently erase every message entry too. Regression test
    for exactly that.
    """
    project = _projects(tmp_path) / "old"
    project.mkdir(parents=True, exist_ok=True)
    db = project / "desktop-v2.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(_THREADS_DDL)
        conn.execute(_MESSAGES_DDL)
        # Deliberately NO _RECEIPTS_DDL.
        conn.execute(
            "INSERT INTO threads (id, project_id, project_path, title, model, "
            "harness_id, agent_mode, created_at) VALUES ('t1', 'p1', ?, 'T', ?, "
            "'codebuff', 'build', ?)",
            (str(project), MODEL_QUALIFIED, TS_MS),
        )
        conn.execute(
            "INSERT INTO messages (thread_id, role, metrics_json, ts) "
            "VALUES ('t1', 'assistant', ?, ?)",
            (_metrics(input_tokens=1000, cached_input=900, output_tokens=50,
                      reasoning=10), TS_MS),
        )
        conn.commit()
    finally:
        conn.close()

    entries = _fresh(monkeypatch, tmp_path)._parse_all()

    assert len(entries) == 1, "message usage must survive a missing receipts table"
    e = entries[0]
    assert (e["input"], e["cacheRead"], e["output"], e["reasoning"]) == (100, 900, 40, 10)


def test_has_table_detects_presence_and_absence(monkeypatch, tmp_path):
    _make_db(_projects(tmp_path) / "p1")
    conn = sqlite3.connect(_projects(tmp_path) / "p1" / "desktop-v2.db")
    try:
        assert FreebuffParser._has_table(conn, "messages") is True
        assert FreebuffParser._has_table(conn, "auto_run_decision_receipts") is True
        assert FreebuffParser._has_table(conn, "no_such_table") is False
    finally:
        conn.close()


# --- harness filtering & double-counting prevention ------------------------


def test_claude_code_and_codex_harnesses_are_skipped(monkeypatch, tmp_path):
    """Turns running under claude-code or codex harnesses spawn external CLIs
    that log to ~/.claude and ~/.codex, which Tokdash parses separately.
    Counting them in Freebuff would double-count."""
    _make_db(
        _projects(tmp_path) / "p1",
        thread_id="t-claude",
        harness_id="claude-code",
        messages=[("assistant", _metrics(input_tokens=10, output_tokens=10), TS_MS)],
    )
    _make_db(
        _projects(tmp_path) / "p2",
        thread_id="t-codex",
        harness_id="codex",
        messages=[("assistant", _metrics(input_tokens=20, output_tokens=20), TS_MS + 1)],
    )
    _make_db(
        _projects(tmp_path) / "p3",
        thread_id="t-codebuff",
        harness_id="codebuff",
        messages=[("assistant", _metrics(input_tokens=30, output_tokens=30), TS_MS + 2)],
    )
    entries = _fresh(monkeypatch, tmp_path)._parse_all()
    assert len(entries) == 1
    assert entries[0]["entry_id"] == "freebuff:t-codebuff:1"
    assert entries[0]["input"] == 30


def test_null_harness_falls_back_to_state_json_agent_harness(monkeypatch, tmp_path):
    """When threads.harness_id is NULL, effective harness follows state.json.agentHarness
    (orchestrator.js L199580 & L176831)."""
    # 1. state.json specifies "claude-code": NULL harness thread must be skipped.
    state_file = tmp_path / ".config" / "freebuff-desktop" / "state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"agentHarness": "claude-code"}), encoding="utf-8")

    _make_db(
        _projects(tmp_path) / "p1",
        thread_id="t-null",
        harness_id=None,
        messages=[("assistant", _metrics(input_tokens=10, output_tokens=10), TS_MS)],
    )
    parser = _fresh(monkeypatch, tmp_path)
    assert parser._parse_all() == []

    # 2. state.json specifies "codebuff": NULL harness thread is counted.
    state_file.write_text(json.dumps({"agentHarness": "codebuff"}), encoding="utf-8")
    entries = _fresh(monkeypatch, tmp_path)._parse_all()
    assert len(entries) == 1
    assert entries[0]["entry_id"] == "freebuff:t-null:1"

    # 3. state.json missing: defaults to "codebuff" fallback and is counted.
    state_file.unlink()
    entries = _fresh(monkeypatch, tmp_path)._parse_all()
    assert len(entries) == 1
    assert entries[0]["entry_id"] == "freebuff:t-null:1"


def test_sponsored_and_byok_turns_always_route_to_codebuff(monkeypatch, tmp_path):
    """Sponsored tasks and BYOK tasks always route to codebuff even if thread
    harness_id is set to claude-code or codex (orchestrator.js L199580)."""
    _make_db(
        _projects(tmp_path) / "p-sponsored",
        thread_id="t-spons",
        harness_id="claude-code",
        sponsored=1,
        messages=[("assistant", _metrics(input_tokens=50, output_tokens=5), TS_MS)],
    )
    _make_db(
        _projects(tmp_path) / "p-byok",
        thread_id="t-byok",
        harness_id="codex",
        byok_connection=json.dumps({"connectionId": "byok-1", "revision": 1}),
        messages=[("assistant", _metrics(input_tokens=70, output_tokens=7), TS_MS + 1)],
    )
    entries = _fresh(monkeypatch, tmp_path)._parse_all()
    assert len(entries) == 2
    ids = {e["entry_id"] for e in entries}
    assert ids == {"freebuff:t-spons:1", "freebuff:t-byok:1"}


def test_auto_run_receipts_filter_on_manager_harness(monkeypatch, tmp_path):
    """auto_run_decision_receipts filter on manager_json.harnessId (codebuff only)."""
    _make_db(
        _projects(tmp_path) / "p1",
        messages=[],
        receipts=[
            ("rec-claude", _manager(harness_id="claude-code", input_tokens=10, output_tokens=1), None, TS_MS),
            ("rec-codex", _manager(harness_id="codex", input_tokens=20, output_tokens=2), None, TS_MS + 1),
            ("rec-codebuff", _manager(harness_id="codebuff", input_tokens=30, output_tokens=3), None, TS_MS + 2),
            ("rec-default", _manager(harness_id=None, input_tokens=40, output_tokens=4), None, TS_MS + 3),
        ],
    )
    entries = _fresh(monkeypatch, tmp_path)._parse_all()
    assert len(entries) == 2
    entry_ids = [e["entry_id"] for e in entries]
    assert entry_ids == ["freebuff:t1:receipt:rec-codebuff", "freebuff:t1:receipt:rec-default"]


# --- state path relocation & strict file parsing ---------------------------


def test_freebuff_desktop_state_path_relocates_home_and_projects(monkeypatch, tmp_path):
    """FREEBUFF_DESKTOP_STATE_PATH relocates state.json and projects directory
    (orchestrator.js L216763 & L182493)."""
    custom_state = tmp_path / "relocated" / "custom-state.json"
    custom_projects = tmp_path / "relocated" / "projects" / "p1"
    monkeypatch.setenv("FREEBUFF_DESKTOP_STATE_PATH", str(custom_state))

    assert clientpaths.freebuff_desktop_state_path() == custom_state
    assert clientpaths.freebuff_desktop_home() == custom_state.parent

    _make_db(custom_projects, messages=[("assistant", _metrics(input_tokens=15, output_tokens=5), TS_MS)])
    db_paths = clientpaths.freebuff_desktop_db_paths()
    assert len(db_paths) == 1
    assert db_paths[0].resolve() == (custom_projects / "desktop-v2.db").resolve()

    entries = _fresh(monkeypatch, tmp_path)._parse_all()
    assert len(entries) == 1
    assert entries[0]["input"] == 15


def test_parse_file_strict_raises_usage_file_vanished_on_missing_db(monkeypatch, tmp_path):
    parser = _fresh(monkeypatch, tmp_path)
    missing = tmp_path / "does-not-exist" / "desktop-v2.db"
    sig = (str(missing), 0, 0)
    with pytest.raises(ct.UsageFileVanished):
        parser._parse_file_strict(sig)


def test_parse_file_strict_raises_on_corrupt_db(monkeypatch, tmp_path):
    """A corrupt database raises UsageFileUnreadable rather than returning [] so sync_files
    isolates the failure and keeps existing stored rows."""
    bad = _projects(tmp_path) / "p-bad" / "desktop-v2.db"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"not a sqlite db")
    parser = _fresh(monkeypatch, tmp_path)
    sig = (str(bad), 100, 100)
    with pytest.raises(UsageFileUnreadable) as exc_info:
        parser._parse_file_strict(sig)
    assert exc_info.value.filename == str(bad)


def test_provider_qualified_candidate_precedes_bare_model(monkeypatch, tmp_path):
    """Provider-qualified candidate comes first and prices at the provider-specific
    rate rather than the bare model fallback (Hermes/Muse rule)."""
    custom_pricing_file = tmp_path / "pricing.json"
    custom_pricing_file.write_text(
        json.dumps({
            "version": "test",
            "aliases": {},
            "models": {
                "z-ai/glm-5.3-flash": {"input": 10.0, "output": 20.0},
                "glm-5.3-flash": {"input": 1.0, "output": 2.0},
            },
        }),
        encoding="utf-8",
    )
    pricing = PricingDatabase(db_path=custom_pricing_file, override_path=tmp_path / "absent.json")

    _make_db(
        _projects(tmp_path) / "p1",
        model="z-ai/glm-5.3-flash",
        messages=[("assistant", _metrics(input_tokens=1_000_000, output_tokens=0), TS_MS)],
    )

    parser = FreebuffParser(pricing)
    monkeypatch.setattr(parser, "_db_paths", lambda: [_projects(tmp_path) / "p1" / "desktop-v2.db"])
    entries = parser._parse_all()
    assert len(entries) == 1
    # Qualified candidate comes first
    assert entries[0]["_billing"]["models"] == ["z-ai/glm-5.3-flash", "glm-5.3-flash"]
    # Prices at the qualified rate ($10/M) rather than bare ($1/M)
    assert entries[0]["cost"] == pytest.approx(10.0)


def _stored_usage_rows(db_path: Path, source: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM usage_entries WHERE source = ?", (source,)).fetchall()
    finally:
        conn.close()


def test_corrupt_project_db_unreadable_and_other_projects_still_sync(monkeypatch, tmp_path):
    """A corrupt project DB raises UsageFileUnreadable and the other projects still sync."""
    good_db = _make_db(_projects(tmp_path) / "p-good", messages=[("assistant", _metrics(input_tokens=50), TS_MS)])
    bad_db = _projects(tmp_path) / "p-bad" / "desktop-v2.db"
    bad_db.parent.mkdir(parents=True, exist_ok=True)
    bad_db.write_bytes(b"not a valid sqlite file")

    parser = _fresh(monkeypatch, tmp_path)
    sigs = parser._file_signatures()
    assert len(sigs) == 2

    store_db = tmp_path / "usage.sqlite3"
    store = UsageEntryStore(store_db)
    file_context = parser.prepare_file_context(sigs)

    # sync_files succeeds: the corrupt DB is skipped with UsageFileUnreadable, and good DB commits
    synced = store.sync_files(
        "freebuff",
        sigs,
        parser=parser.persistent_parser_signature(),
        pricing_identity=parser._pricing_signature(),
        parse_file_entries=lambda file_sig: parser._parse_file_strict(file_sig),
        file_context=file_context,
    )
    assert synced is True
    rows = _stored_usage_rows(store_db, "freebuff")
    assert len(rows) == 1
    assert rows[0]["file_path"] == str(good_db)
    assert rows[0]["input"] == 50


def test_deleted_db_before_snapshot_raises_usage_file_vanished(monkeypatch, tmp_path):
    """A DB deleted before or during snapshot copy raises UsageFileVanished."""
    db = _make_db(_projects(tmp_path) / "p-del", messages=[("assistant", _metrics(input_tokens=10), TS_MS)])
    parser = _fresh(monkeypatch, tmp_path)
    sig = (str(db), 100, 100)

    # 1. Deleted before parse_file_strict opens it
    db.unlink()
    with pytest.raises(UsageFileVanished) as exc_info:
        parser._parse_file_strict(sig)
    assert exc_info.value.filename == str(db)

    # 2. Deleted between is_file() check and snapshot copy (surfaces as ZCodeSnapshotError when db is gone)
    db2 = _make_db(_projects(tmp_path) / "p-del2", messages=[("assistant", _metrics(input_tokens=10), TS_MS)])
    sig2 = (str(db2), 100, 100)

    from contextlib import contextmanager
    @contextmanager
    def remove_and_fail_snapshot(path):
        if db2.exists():
            db2.unlink()
        raise ct.ZCodeSnapshotError("snapshot copy failed")
        yield None

    monkeypatch.setattr(ct, "zcode_snapshot", remove_and_fail_snapshot)
    with pytest.raises(UsageFileVanished) as exc_info2:
        parser._parse_file_strict(sig2)
    assert exc_info2.value.filename == str(db2)


def test_agent_harness_unknown_counts_as_codebuff_while_external_skipped(monkeypatch, tmp_path):
    """agentHarness: 'freebuff' (and any unknown value) counts turns as codebuff;
    only 'claude-code' and 'codex' are skipped."""
    state_file = tmp_path / ".config" / "freebuff-desktop" / "state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)

    _make_db(_projects(tmp_path) / "p1", harness_id=None, messages=[("assistant", _metrics(input_tokens=10), TS_MS)])
    parser = _fresh(monkeypatch, tmp_path)

    # 'freebuff' harness -> counted
    state_file.write_text(json.dumps({"agentHarness": "freebuff"}), encoding="utf-8")
    assert len(parser._parse_all()) == 1

    # unknown harness 'something-new' -> counted
    state_file.write_text(json.dumps({"agentHarness": "something-new"}), encoding="utf-8")
    assert len(parser._parse_all()) == 1

    # 'claude-code' harness -> skipped
    state_file.write_text(json.dumps({"agentHarness": "claude-code"}), encoding="utf-8")
    assert len(parser._parse_all()) == 0

    # 'codex' harness -> skipped
    state_file.write_text(json.dumps({"agentHarness": "codex"}), encoding="utf-8")
    assert len(parser._parse_all()) == 0

    # absent harness -> counted as codebuff
    state_file.write_text(json.dumps({}), encoding="utf-8")
    assert len(parser._parse_all()) == 1


def test_threads_harness_id_unknown_counts_as_codebuff_while_external_skipped(monkeypatch, tmp_path):
    """threads.harness_id with 'freebuff' or other unknown harness counts as codebuff;
    only 'claude-code' and 'codex' are skipped."""
    p_dir = _projects(tmp_path) / "p-threads"
    db = p_dir / "desktop-v2.db"
    p_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    try:
        conn.executescript(_THREADS_DDL + ";\n" + _MESSAGES_DDL)
        # 4 threads: freebuff, unknown, claude-code, codex
        conn.execute("INSERT INTO threads (id, project_id, project_path, model, harness_id, created_at) VALUES ('t1', 'p', 'path', 'm1', 'freebuff', ?)", (TS_MS,))
        conn.execute("INSERT INTO threads (id, project_id, project_path, model, harness_id, created_at) VALUES ('t2', 'p', 'path', 'm1', 'future-build-harness', ?)", (TS_MS,))
        conn.execute("INSERT INTO threads (id, project_id, project_path, model, harness_id, created_at) VALUES ('t3', 'p', 'path', 'm1', 'claude-code', ?)", (TS_MS,))
        conn.execute("INSERT INTO threads (id, project_id, project_path, model, harness_id, created_at) VALUES ('t4', 'p', 'path', 'm1', 'codex', ?)", (TS_MS,))
        conn.execute("INSERT INTO messages (thread_id, role, metrics_json, ts) VALUES ('t1', 'assistant', ?, ?)", (_metrics(input_tokens=10), TS_MS))
        conn.execute("INSERT INTO messages (thread_id, role, metrics_json, ts) VALUES ('t2', 'assistant', ?, ?)", (_metrics(input_tokens=20), TS_MS))
        conn.execute("INSERT INTO messages (thread_id, role, metrics_json, ts) VALUES ('t3', 'assistant', ?, ?)", (_metrics(input_tokens=30), TS_MS))
        conn.execute("INSERT INTO messages (thread_id, role, metrics_json, ts) VALUES ('t4', 'assistant', ?, ?)", (_metrics(input_tokens=40), TS_MS))
        conn.commit()
    finally:
        conn.close()

    parser = _fresh(monkeypatch, tmp_path)
    entries = parser._parse_all()
    # Only t1 ('freebuff') and t2 ('future-build-harness') are parsed; t3 ('claude-code') and t4 ('codex') are skipped
    assert sorted(e["input"] for e in entries) == [10, 20]


def test_state_json_context_invalidation_and_unrelated_write(monkeypatch, tmp_path):
    """A state.json change re-syncs; an unrelated state.json write does not invalidate stores."""
    state_file = tmp_path / ".config" / "freebuff-desktop" / "state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"agentHarness": "codebuff", "ui": "normal"}), encoding="utf-8")

    _make_db(_projects(tmp_path) / "p1", harness_id=None, messages=[("assistant", _metrics(input_tokens=10), TS_MS)])
    parser = _fresh(monkeypatch, tmp_path)
    sigs = parser._file_signatures()

    store_db = tmp_path / "usage.sqlite3"
    store = UsageEntryStore(store_db)

    # Initial sync
    parse_count = 0
    def counting_parser(sig):
        nonlocal parse_count
        parse_count += 1
        return parser._parse_file_strict(sig)

    ctx1 = parser.prepare_file_context(sigs)
    store.sync_files("freebuff", sigs, parser=parser.persistent_parser_signature(),
                     pricing_identity=parser._pricing_signature(),
                     parse_file_entries=counting_parser, file_context=ctx1)
    assert parse_count == 1

    # Unrelated state.json write (window resize / UI state)
    state_file.write_text(json.dumps({"agentHarness": "codebuff", "ui": "window_resized", "width": 1024}), encoding="utf-8")
    ctx2 = parser.prepare_file_context(sigs)
    assert ctx2 == ctx1  # Context unchanged

    store.sync_files("freebuff", sigs, parser=parser.persistent_parser_signature(),
                     pricing_identity=parser._pricing_signature(),
                     parse_file_entries=counting_parser, file_context=ctx2)
    # Did NOT reparse! parse_count still 1
    assert parse_count == 1

    # Relevant state.json change (agentHarness changed to codex)
    state_file.write_text(json.dumps({"agentHarness": "codex"}), encoding="utf-8")
    ctx3 = parser.prepare_file_context(sigs)
    assert ctx3 != ctx1  # Context changed!

    store.sync_files("freebuff", sigs, parser=parser.persistent_parser_signature(),
                     pricing_identity=parser._pricing_signature(),
                     parse_file_entries=counting_parser, file_context=ctx3)
    # Did reparse! parse_count incremented
    assert parse_count == 2
    # Turns are now skipped under codex harness
    rows = _stored_usage_rows(store_db, "freebuff")
    assert len(rows) == 0


@pytest.mark.parametrize("captured_harness", ["codebuff", "codex"])
def test_stored_sync_uses_captured_harness_when_state_changes(monkeypatch, tmp_path, captured_harness):
    from tokdash.compute import _collect_parser_file

    state_file = tmp_path / ".config" / "freebuff-desktop" / "state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(json.dumps({"agentHarness": captured_harness}), encoding="utf-8")
    _make_db(
        _projects(tmp_path) / "p1", harness_id=None,
        messages=[("assistant", _metrics(input_tokens=10), TS_MS)],
    )
    parser = _fresh(monkeypatch, tmp_path)
    signatures = parser._file_signatures()
    context = parser.prepare_file_context(signatures)
    previous_context = {"sentinel": {"default_harness": "codebuff"}}
    parser._file_context = previous_context
    parse_count = 0

    def parse_after_state_change(signature):
        nonlocal parse_count
        parse_count += 1
        changed_harness = "codex" if captured_harness == "codebuff" else "codebuff"
        state_file.write_text(json.dumps({"agentHarness": changed_harness}), encoding="utf-8")
        return _collect_parser_file(parser, signature, file_context=context)

    store_path = tmp_path / "usage.sqlite3"
    store = UsageEntryStore(store_path)
    store.sync_files(
        "freebuff", signatures, parser=parser.persistent_parser_signature(),
        pricing_identity=parser._pricing_signature(),
        parse_file_entries=parse_after_state_change, file_context=context,
    )
    assert parser._file_context is previous_context
    expected_rows = 1 if captured_harness == "codebuff" else 0
    assert len(_stored_usage_rows(store_path, "freebuff")) == expected_rows

    # Restoring the captured value must leave correctly parsed rows cached.
    state_file.write_text(json.dumps({"agentHarness": captured_harness}), encoding="utf-8")
    assert store.sync_files(
        "freebuff", signatures, parser=parser.persistent_parser_signature(),
        pricing_identity=parser._pricing_signature(),
        parse_file_entries=parse_after_state_change,
        file_context=parser.prepare_file_context(signatures),
    ) is False
    assert parse_count == 1
    assert len(_stored_usage_rows(store_path, "freebuff")) == expected_rows


def test_symlinked_state_json_finds_projects(monkeypatch, tmp_path):
    """A symlinked state.json still finds the projects directory in the symlink parent."""
    dotfiles_dir = tmp_path / "dotfiles"
    dotfiles_dir.mkdir(parents=True, exist_ok=True)
    real_state = dotfiles_dir / "state.json"
    real_state.write_text(json.dumps({"agentHarness": "codebuff"}), encoding="utf-8")

    app_dir = tmp_path / "app_config"
    app_dir.mkdir(parents=True, exist_ok=True)
    symlink_state = app_dir / "state.json"
    symlink_state.symlink_to(real_state)

    # Project resides under app_dir/projects, NOT dotfiles/projects
    proj_db = _make_db(app_dir / "projects" / "p1", messages=[("assistant", _metrics(input_tokens=10), TS_MS)])

    monkeypatch.setenv("FREEBUFF_DESKTOP_STATE_PATH", str(symlink_state))
    assert clientpaths.freebuff_desktop_state_path() == symlink_state
    assert clientpaths.freebuff_desktop_home() == app_dir

    paths = clientpaths.freebuff_desktop_db_paths()
    assert proj_db in paths


def test_failed_live_read_is_not_cached(monkeypatch, tmp_path):
    """A failed live read is not cached, so the next collect retries rather than serving zero."""
    _make_db(_projects(tmp_path) / "p-good", messages=[("assistant", _metrics(input_tokens=10), TS_MS)])
    bad_db = _projects(tmp_path) / "p-bad" / "desktop-v2.db"
    bad_db.parent.mkdir(parents=True, exist_ok=True)
    bad_db.write_bytes(b"corrupt db")

    parser = _fresh(monkeypatch, tmp_path)
    # Clear entry cache
    BaseParser._entry_cache.clear()

    # First collect: p-bad fails, p-good succeeds
    entries1 = parser.collect()
    assert len(entries1) == 1
    # Cache must NOT be populated because read had a failure
    assert "freebuff" not in BaseParser._entry_cache

    # Now fix p-bad so it is valid
    bad_db.unlink()
    _make_db(bad_db.parent, messages=[("assistant", _metrics(input_tokens=20), TS_MS)])

    # Second collect: without advancing signatures or clearing cache, it should retry and read both
    entries2 = parser.collect()
    assert len(entries2) == 2
    # Now that both succeeded, it is cached
    assert "freebuff" in BaseParser._entry_cache


def test_manager_receipt_prefers_finished_at(monkeypatch, tmp_path):
    """Manager receipts prefer manager_json.finishedAt over updated_at/created_at."""
    _make_db(
        _projects(tmp_path) / "p1",
        messages=[],
        receipts=[
            ("rec-1", _manager(model="m-aaaa1111", input_tokens=100, duration_ms=5000,
                              finished_at=TS_MS + 1234),
             None, TS_MS + 99999),
        ],
    )
    parser = _fresh(monkeypatch, tmp_path)
    entries = parser._parse_all()
    assert len(entries) == 1
    assert entries[0]["timestamp"] == TS_MS + 1234


def test_split_buckets_negative_tokens():
    """Negative input tokens are clamped and cannot produce negative cacheRead (Item O2)."""
    # Negative inputTokens with positive cachedInputTokens
    buckets = FreebuffParser._split_buckets({
        "inputTokens": -10,
        "cachedInputTokens": 5,
        "outputTokens": 20,
        "reasoningOutputTokens": 0,
    })
    assert buckets is not None
    fresh_input, cache_read, fresh_output, reasoning = buckets
    assert fresh_input >= 0
    assert cache_read >= 0
    assert (fresh_input, cache_read, fresh_output, reasoning) == (0, 0, 20, 0)
