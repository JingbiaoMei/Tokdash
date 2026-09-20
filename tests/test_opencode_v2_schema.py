"""OpenCode's v2 SQLite schema: ``session_message`` / ``session_v2``.

v2 replaced the legacy ``message`` table's in-data role with a plain ``type``
column, nested the model under ``model.id``/``model.providerID``, and froze the
legacy table — but ``session_message`` already contains the migrated v1 rows,
so every reader must pick exactly one table per database. Kilo Code's DBs may
still be v1, and Mimo's loaders share the same helpers, so those paths must
keep their behavior.

All fixtures are synthetic; no live payloads or account identifiers.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from tokdash import sessions
from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import KiloCodeParser, OpenCodeParser

# Whole-second base so datetime windows convert back to the same millisecond.
BASE = 1_787_000_000_000
TS_COLD = BASE
TS_WARM = BASE + 10_000

V1_MESSAGE_DDL = """
CREATE TABLE message (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    time_created INTEGER,
    time_updated INTEGER,
    data TEXT
);
"""
V2_MESSAGE_DDL = """
CREATE TABLE session_message (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    type TEXT NOT NULL,
    seq INTEGER NOT NULL,
    time_created INTEGER NOT NULL,
    time_updated INTEGER NOT NULL,
    data TEXT NOT NULL
);
"""


def _fresh_caches() -> None:
    OpenCodeParser._query_cache.clear()
    OpenCodeParser._query_cache_sig = ()
    KiloCodeParser._query_cache.clear()
    KiloCodeParser._query_cache_sig = ()


# --- parser fixtures ---------------------------------------------------------


def _make_v2_message_db(path: Path, rows, legacy_rows=()) -> Path:
    """A v2-shaped DB: ``session_message`` rows, optionally plus a frozen
    legacy ``message`` table. rows are (time_created_ms, type, data_dict);
    legacy_rows are (time_created_ms, data_dict)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(V2_MESSAGE_DDL)
    for index, (ts, msg_type, data) in enumerate(rows):
        conn.execute(
            "INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"v2-{index}", "sess-1", msg_type, index, ts, ts, json.dumps(data)),
        )
    if legacy_rows:
        conn.execute(V1_MESSAGE_DDL)
        for index, (ts, data) in enumerate(legacy_rows):
            conn.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                (f"legacy-{index}", "sess-1", ts, ts, json.dumps(data)),
            )
    conn.commit()
    conn.close()
    return path


def _make_v1_message_db(path: Path, rows) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(V1_MESSAGE_DDL)
    for index, (ts, data) in enumerate(rows):
        conn.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
            (f"m-{index}", "sess-1", ts, ts, json.dumps(data)),
        )
    conn.commit()
    conn.close()
    return path


def _v2_assistant(model="synthetic-model", provider="synthetic-provider", **tokens) -> dict:
    return {
        "time": {"created": TS_COLD},
        "agent": "build",
        "model": {"id": model, "providerID": provider, "variant": "max"},
        "tokens": tokens,
    }


def _v2_assistant_without_tokens() -> dict:
    return {
        "time": {"created": TS_COLD},
        "agent": "build",
        "model": {"id": "synthetic-model", "providerID": "synthetic-provider"},
        "content": [],
    }


def _v2_user() -> dict:
    return {
        "time": {"created": TS_COLD},
        "agent": "build",
        "model": {"id": "synthetic-model", "providerID": "synthetic-provider"},
    }


def _v1_assistant(model="legacy-model", provider="legacy-provider", **tokens) -> dict:
    return {"role": "assistant", "modelID": model, "providerID": provider, "tokens": tokens}


def _v1_user() -> dict:
    return {"role": "user", "modelID": "legacy-model", "providerID": "legacy-provider"}


# --- parser ------------------------------------------------------------------


def test_parser_reads_v2_rows_and_skips_user_and_tokenless_rows(monkeypatch, tmp_path):
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    _make_v2_message_db(
        xdg / "opencode" / "opencode.db",
        [
            (TS_COLD, "assistant", _v2_assistant(input=14579, output=37, cache={"read": 0, "write": 0})),
            (TS_COLD + 1, "user", _v2_user()),
            (TS_COLD + 2, "assistant", _v2_assistant_without_tokens()),
            (TS_WARM, "assistant",
             _v2_assistant(input=467, output=58, cache={"read": 14112, "write": 0}, reasoning=30)),
            (TS_WARM + 1, "idle", _v2_user()),
        ],
    )

    _fresh_caches()
    entries = OpenCodeParser(PricingDatabase()).collect(None, None)

    assert [entry["timestamp"] for entry in entries] == [TS_COLD, TS_WARM]
    cold, warm = entries
    assert (cold["model"], cold["provider"]) == ("synthetic-model", "synthetic-provider")
    assert (cold["input"], cold["output"], cold["cacheRead"], cold["cacheWrite"], cold["reasoning"]) == (
        14579, 37, 0, 0, 0,
    )
    assert (warm["input"], warm["cacheRead"], warm["output"], warm["reasoning"]) == (467, 14112, 58, 30)


def test_parser_reads_only_session_message_when_both_tables_exist(monkeypatch, tmp_path):
    """The v2 table already holds the migrated v1 rows; reading the frozen
    legacy table too would count every legacy message twice."""
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    _make_v2_message_db(
        xdg / "opencode" / "opencode.db",
        [(TS_COLD, "assistant", _v2_assistant(input=10, output=1, cache={"read": 0, "write": 0}))],
        legacy_rows=[(TS_WARM, _v1_assistant(model="frozen-model", input=999, output=999, cache={"read": 0, "write": 0}))],
    )

    _fresh_caches()
    entries = OpenCodeParser(PricingDatabase()).collect(None, None)

    assert len(entries) == 1
    assert entries[0]["model"] == "synthetic-model"
    assert entries[0]["timestamp"] == TS_COLD


@pytest.mark.parametrize("control_type", [None, "agent-switched", "user", "idle"])
def test_parser_falls_back_to_legacy_rows_before_v2_assistant(monkeypatch, tmp_path, control_type):
    """The v2 tables can be migrated in long before the app switches writes.

    While ``session_message`` has only control/user events (or no rows),
    legacy ``message`` is still live; selecting v2 at that point would
    report zero usage from an otherwise healthy v1 database.
    """
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    _make_v2_message_db(
        xdg / "opencode" / "opencode.db",
        [] if control_type is None else [(TS_WARM, control_type, {})],
        legacy_rows=[
            (TS_COLD, _v1_assistant(input=1000, output=100, cache={"read": 40, "write": 5})),
            (TS_WARM, _v1_user()),
        ],
    )

    _fresh_caches()
    entries = OpenCodeParser(PricingDatabase()).collect(None, None)

    assert [entry["timestamp"] for entry in entries] == [TS_COLD]
    assert (entries[0]["model"], entries[0]["provider"]) == ("legacy-model", "legacy-provider")
    assert (entries[0]["input"], entries[0]["cacheRead"]) == (1000, 40)


def test_parser_bills_a_flat_only_v2_row(monkeypatch, tmp_path):
    """A ``session_message`` row can carry only the flat v1 fields (a mixed
    install, or a row hand-migrated without the nested model); it must bill."""
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    _make_v2_message_db(
        xdg / "opencode" / "opencode.db",
        [(TS_COLD, "assistant",
          _v1_assistant(model="flat-only-model", provider="flat-only-provider",
                        input=3, output=4, cache={"read": 1, "write": 2}))],
    )

    _fresh_caches()
    entries = OpenCodeParser(PricingDatabase()).collect(None, None)

    assert len(entries) == 1
    assert (entries[0]["model"], entries[0]["provider"]) == ("flat-only-model", "flat-only-provider")
    assert (entries[0]["input"], entries[0]["cacheWrite"], entries[0]["cacheRead"], entries[0]["output"]) == (
        3, 2, 1, 4,
    )


def test_parser_still_reads_v1_flat_rows(monkeypatch, tmp_path):
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    _make_v1_message_db(
        xdg / "opencode" / "opencode.db",
        [
            (TS_COLD, _v1_assistant(input=1000, output=100, cache={"read": 40, "write": 5}, reasoning=7)),
            (TS_WARM, _v1_user()),
        ],
    )

    _fresh_caches()
    entries = OpenCodeParser(PricingDatabase()).collect(None, None)

    assert len(entries) == 1
    assert (entries[0]["model"], entries[0]["provider"]) == ("legacy-model", "legacy-provider")
    assert (entries[0]["input"], entries[0]["cacheRead"], entries[0]["cacheWrite"]) == (1000, 40, 5)
    assert entries[0]["reasoning"] == 7


def test_parser_accepts_a_nested_model_without_flat_fields(monkeypatch, tmp_path):
    """A v1-shaped database can still hold a nested-model row (v1 rows migrated
    into v2 keep their id, and both shapes have been written); the identity
    helper must bill it rather than fall back to "unknown"."""
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    nested = {
        "role": "assistant",
        "model": {"id": "nested-model", "providerID": "nested-provider"},
        "tokens": {"input": 5, "output": 6, "cache": {"read": 0, "write": 0}},
    }
    _make_v1_message_db(xdg / "opencode" / "opencode.db", [(TS_COLD, nested)])

    _fresh_caches()
    entries = OpenCodeParser(PricingDatabase()).collect(None, None)

    assert len(entries) == 1
    assert (entries[0]["model"], entries[0]["provider"]) == ("nested-model", "nested-provider")


def test_kilo_parser_reads_a_v2_shaped_database(monkeypatch, tmp_path):
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    _make_v2_message_db(
        xdg / "kilo" / "kilo.db",
        [(TS_COLD, "assistant",
          _v2_assistant(model="kilo-v2-model", provider="kilo-v2-provider",
                        input=20, output=4, cache={"read": 2, "write": 1}))],
    )

    _fresh_caches()
    entries = KiloCodeParser(PricingDatabase()).collect(None, None)

    assert len(entries) == 1
    assert entries[0]["source"] == "kilocode"
    assert (entries[0]["model"], entries[0]["provider"]) == ("kilo-v2-model", "kilo-v2-provider")
    assert (entries[0]["input"], entries[0]["cacheRead"], entries[0]["cacheWrite"]) == (20, 2, 1)


# --- sessions ----------------------------------------------------------------

PROJECT_DDL = "CREATE TABLE project(id TEXT PRIMARY KEY, worktree TEXT);"
SESSION_V1_DDL = """
CREATE TABLE session (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    slug TEXT NOT NULL,
    directory TEXT NOT NULL,
    title TEXT NOT NULL
);
"""
SESSION_V2_DDL = """
CREATE TABLE session_v2 (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    workspace_id TEXT,
    parent_id TEXT,
    slug TEXT NOT NULL,
    directory TEXT NOT NULL,
    title TEXT,
    version TEXT NOT NULL,
    time_created INTEGER,
    time_updated INTEGER
);
"""

SINCE = BASE + 100_000
UNTIL = BASE + 200_000


def _make_v2_session_db(path: Path, frozen_legacy=(), extra_rows=()) -> Path:
    """project + session_v2 + session_message, plus an empty/frozen legacy
    ``message`` table (v2 freezes it rather than dropping it)."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(PROJECT_DDL + SESSION_V2_DDL + V2_MESSAGE_DDL + V1_MESSAGE_DDL)
        conn.execute("INSERT INTO project(id, worktree) VALUES('p1', '/workspace/tokdash')")
        conn.execute(
            "INSERT INTO session_v2(id, project_id, slug, directory, title, version, time_created, time_updated)"
            " VALUES('s1', 'p1', 'v2-slug', '/tmp/v2-fallback', 'Synthetic v2 title', '1.0.0', 0, 0)"
        )
        rows = [
            ("pre_user", "user", SINCE - 1_000, _v2_user()),
            ("pre", "assistant", SINCE - 30_000,
             _v2_assistant(input=1, output=2, reasoning=5, cache={"write": 3, "read": 4})),
            ("inside1", "assistant", SINCE + 10_000,
             _v2_assistant(input=10, output=20, reasoning=50, cache={"write": 30, "read": 40})),
            ("inside_user", "user", SINCE + 20_000, _v2_user()),
            ("tokenless", "assistant", SINCE + 30_000, _v2_assistant_without_tokens()),
            ("inside2", "assistant", SINCE + 40_000,
             _v2_assistant(input=100, output=200, cache={"write": 0, "read": 0})),
            ("post_user", "user", UNTIL + 1_000, _v2_user()),
            ("post", "assistant", UNTIL + 30_000,
             _v2_assistant(input=7, output=8, cache={"write": 0, "read": 0})),
        ]
        for seq, (message_id, msg_type, ts, data) in enumerate(rows + list(extra_rows)):
            conn.execute(
                "INSERT INTO session_message VALUES (?, 's1', ?, ?, ?, ?, ?)",
                (message_id, msg_type, seq, ts, ts, json.dumps(data)),
            )
        for index, (ts, data) in enumerate(frozen_legacy):
            conn.execute(
                "INSERT INTO message VALUES (?, 's1', ?, ?, ?)",
                (f"frozen-{index}", ts, ts, json.dumps(data)),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def _make_pre_switch_session_db(path: Path) -> Path:
    """The migration state: v2 tables exist but are still empty, legacy live.

    ``session_v2`` holds a deliberately stale row (different project, blank
    title/directory) so a wrong join shows up as a wrong project/name rather
    than passing by coincidence.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            PROJECT_DDL + SESSION_V1_DDL + SESSION_V2_DDL + V1_MESSAGE_DDL + V2_MESSAGE_DDL
        )
        conn.execute("INSERT INTO project(id, worktree) VALUES('p1', '/workspace/tokdash')")
        conn.execute("INSERT INTO project(id, worktree) VALUES('p2', '/workspace/other')")
        conn.execute(
            "INSERT INTO session(id, project_id, slug, directory, title)"
            " VALUES('s1', 'p1', 'legacy-slug', '/tmp/legacy', 'Legacy title')"
        )
        conn.execute(
            "INSERT INTO session_v2(id, project_id, slug, directory, title, version, time_created, time_updated)"
            " VALUES('s1', 'p2', '', '', '', '1.0.0', 0, 0)"
        )
        rows = [
            (TS_COLD, _v1_assistant(input=10, output=1, cache={"read": 0, "write": 0})),
            (TS_WARM, _v1_assistant(input=20, output=2, cache={"read": 0, "write": 0})),
            (TS_WARM + 1, _v1_user()),
        ]
        for index, (ts, data) in enumerate(rows):
            conn.execute(
                "INSERT INTO message VALUES (?, 's1', ?, ?, ?)",
                (f"legacy-{index}", ts, ts, json.dumps(data)),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def test_v2_scalar_and_raw_loaders_agree_and_read_session_v2(tmp_path):
    db_path = _make_v2_session_db(
        tmp_path / "opencode.db",
        frozen_legacy=[
            (SINCE + 5_000, _v1_assistant(model="frozen-model", input=999, output=999, cache={"read": 0, "write": 0})),
        ],
    )

    scalar = sessions._load_opencode_sessions_scalar(db_path)
    raw = sessions._load_opencode_sessions_raw_json(db_path)

    assert scalar == raw
    assert set(scalar) == {"s1"}
    loaded = scalar["s1"]
    assert loaded["project"] == "tokdash"
    assert loaded["display_name"] == "Synthetic v2 title"
    # Tokenless and user rows are dropped; the frozen legacy row must not
    # appear (it would otherwise be a fifth turn under "frozen-model").
    assert [turn["timestamp_ms"] for turn in loaded["turns"]] == [
        SINCE - 30_000, SINCE + 10_000, SINCE + 40_000, UNTIL + 30_000,
    ]
    inside = next(turn for turn in loaded["turns"] if turn["timestamp_ms"] == SINCE + 10_000)
    assert inside["model"] == "synthetic-model"
    assert inside["_bill"]["model"] == "synthetic-provider/synthetic-model"
    assert (inside["tokens_in"], inside["tokens_cache"], inside["tokens_out"], inside["tokens_reasoning"]) == (
        10 + 30, 40, 20, 50,
    )


def test_v2_boundary_events_skip_user_rows_on_both_loaders(tmp_path):
    db_path = _make_v2_session_db(tmp_path / "opencode.db")

    scalar = sessions._load_opencode_sessions_scalar(db_path, since_ms=SINCE, until_ms=UNTIL)["s1"]
    raw = sessions._load_opencode_sessions_raw_json(db_path, since_ms=SINCE, until_ms=UNTIL)["s1"]

    assert [turn["timestamp_ms"] for turn in scalar["turns"]] == [SINCE + 10_000, SINCE + 40_000]
    assert scalar == raw
    # A user row sits just outside each edge; the assistant behind it is the
    # boundary event (a user row is not a token event).
    for loaded in (scalar, raw):
        assert loaded["_prior_event_ms"] == SINCE - 30_000
        assert loaded["_next_event_ms"] == UNTIL + 30_000


@pytest.mark.parametrize("control_type", [None, "agent-switched", "user", "idle"])
def test_loaders_read_legacy_rows_and_join_the_legacy_session_before_the_switch(tmp_path, control_type):
    """V2 tables without assistant rows must not shadow the live legacy schema.

    The join must also come from the legacy ``session`` table: ``session_v2``
    here is stale (a different project, blank title), so using it would change
    the project and the display name.
    """
    db_path = _make_pre_switch_session_db(tmp_path / "opencode.db")
    if control_type is not None:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO session_message VALUES('control', 's1', ?, 0, ?, ?, '{}')",
                (control_type, TS_WARM, TS_WARM),
            )

    scalar = sessions._load_opencode_sessions_scalar(db_path)
    raw = sessions._load_opencode_sessions_raw_json(db_path)

    assert scalar == raw
    assert set(scalar) == {"s1"}
    loaded = scalar["s1"]
    assert loaded["project"] == "tokdash"
    assert loaded["display_name"] == "Legacy title"
    assert [turn["timestamp_ms"] for turn in loaded["turns"]] == [TS_COLD, TS_WARM]
    assert loaded["turns"][0]["model"] == "legacy-model"


def test_v2_loaders_agree_on_a_flat_only_session_message_row(tmp_path):
    """The scalar SQL must use the same flat-first precedence as the Python
    identity helper, or scalar and raw would disagree on this row."""
    flat = {
        "modelID": "flat-only-model",
        "providerID": "flat-only-provider",
        "tokens": {"input": 5, "output": 6, "cache": {"read": 1, "write": 2}},
    }
    db_path = _make_v2_session_db(
        tmp_path / "opencode.db",
        extra_rows=[("flat_only", "assistant", SINCE + 50_000, flat)],
    )

    scalar = sessions._load_opencode_sessions_scalar(db_path)
    raw = sessions._load_opencode_sessions_raw_json(db_path)

    assert scalar == raw
    flat_turn = next(turn for turn in scalar["s1"]["turns"] if turn["model"] == "flat-only-model")
    assert flat_turn["_bill"]["model"] == "flat-only-provider/flat-only-model"
    assert (flat_turn["tokens_in"], flat_turn["tokens_cache"], flat_turn["tokens_out"]) == (5 + 2, 1, 6)


def test_v2_loaders_agree_when_flat_fields_are_empty_strings(tmp_path):
    """The Python helper falls back on falsy values, so an empty-string flat
    field must fall back to the nested model in SQL too — otherwise the scalar
    path bills "unknown" where the raw path bills the nested model."""
    row = {
        "modelID": "",
        "providerID": "",
        "model": {"id": "nested-model", "providerID": "nested-provider"},
        "tokens": {"input": 5, "output": 6, "cache": {"read": 1, "write": 2}},
    }
    db_path = _make_v2_session_db(
        tmp_path / "opencode.db",
        extra_rows=[("empty_flat", "assistant", SINCE + 60_000, row)],
    )

    scalar = sessions._load_opencode_sessions_scalar(db_path)
    raw = sessions._load_opencode_sessions_raw_json(db_path)

    assert scalar == raw
    turn = next(turn for turn in scalar["s1"]["turns"] if turn["timestamp_ms"] == SINCE + 60_000)
    assert turn["model"] == "nested-model"
    assert turn["_bill"]["model"] == "nested-provider/nested-model"


def test_v2_loaders_skip_unusable_data_rows(tmp_path):
    """Neither a malformed payload nor valid JSON that is not an object may
    raise out of a loader: the scalar SELECT guards with json_valid (and
    json_extract yields NULL for the missing paths), the raw fallback skips
    on parse or on the non-object guard."""
    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(PROJECT_DDL + SESSION_V2_DDL + V2_MESSAGE_DDL)
        conn.execute("INSERT INTO project(id, worktree) VALUES('p1', '/workspace/tokdash')")
        conn.execute(
            "INSERT INTO session_v2(id, project_id, slug, directory, title, version, time_created, time_updated)"
            " VALUES('s1', 'p1', 'v2-slug', '/tmp/v2-fallback', 'Malformed fixture', '1.0.0', 0, 0)"
        )
        conn.execute(
            "INSERT INTO session_message VALUES('bad', 's1', 'assistant', 0, ?, ?, ?)",
            (SINCE, SINCE, "this is not json"),
        )
        conn.execute(
            "INSERT INTO session_message VALUES('array', 's1', 'assistant', 1, ?, ?, ?)",
            (SINCE + 500, SINCE + 500, "[]"),
        )
        conn.execute(
            "INSERT INTO session_message VALUES('good', 's1', 'assistant', 2, ?, ?, ?)",
            (
                SINCE + 1_000,
                SINCE + 1_000,
                json.dumps(_v2_assistant(input=3, output=4, cache={"read": 0, "write": 0})),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    scalar = sessions._load_opencode_sessions_scalar(db_path)
    raw = sessions._load_opencode_sessions_raw_json(db_path)

    assert [turn["timestamp_ms"] for turn in scalar["s1"]["turns"]] == [SINCE + 1_000]
    assert scalar == raw


def test_parser_picks_up_the_v2_switch_without_cache_clearing(monkeypatch, tmp_path):
    """Detection is content-dependent and the query cache is keyed on file
    signatures, so a live upgrade must be seen without anyone calling
    ``_fresh_caches()``."""
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    db = _make_v1_message_db(
        xdg / "opencode" / "opencode.db",
        [(TS_COLD, _v1_assistant(model="old-model", input=1, output=1, cache={"read": 0, "write": 0}))],
    )

    _fresh_caches()
    parser = OpenCodeParser(PricingDatabase())
    assert [entry["model"] for entry in parser.collect(None, None)] == ["old-model"]

    # The migration copies the legacy row into session_message and switches writes.
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(V2_MESSAGE_DDL)
        conn.execute(
            "INSERT INTO session_message VALUES('migrated', 'sess-1', 'assistant', 0, ?, ?, ?)",
            (
                TS_COLD,
                TS_COLD,
                json.dumps(_v2_assistant(model="old-model", input=1, output=1, cache={"read": 0, "write": 0})),
            ),
        )
        conn.execute(
            "INSERT INTO session_message VALUES('new', 'sess-1', 'assistant', 1, ?, ?, ?)",
            (
                TS_WARM,
                TS_WARM,
                json.dumps(_v2_assistant(model="new-model", input=2, output=2, cache={"read": 0, "write": 0})),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    models = sorted(entry["model"] for entry in parser.collect(None, None))
    assert models == ["new-model", "old-model"]


def test_session_loader_picks_up_the_v2_switch_without_cache_clearing(tmp_path):
    """The same upgrade, on the sessions side: the lru cache is keyed on the
    file signature, and the live message table is re-detected per load."""
    db_path = _make_pre_switch_session_db(tmp_path / "opencode.db")
    sessions._load_opencode_sessions.cache_clear()

    sig_before = ((str(db_path), db_path.stat().st_mtime_ns, db_path.stat().st_size),)
    before = sessions._load_opencode_sessions(sig_before, ())
    assert before["s1"]["display_name"] == "Legacy title"
    assert [turn["model"] for turn in before["s1"]["turns"]] == ["legacy-model", "legacy-model"]

    # The app migrates: legacy rows are copied into session_message and the
    # session_v2 row becomes the live one (new title and project).
    conn = sqlite3.connect(str(db_path))
    try:
        legacy_rows = conn.execute("SELECT time_created, data FROM message ORDER BY time_created").fetchall()
        for seq, (ts, data) in enumerate(legacy_rows):
            conn.execute(
                "INSERT INTO session_message VALUES(?, 's1', 'assistant', ?, ?, ?, ?)",
                (f"migrated-{seq}", seq, ts, ts, data),
            )
        conn.execute(
            "UPDATE session_v2 SET title='V2 title', directory='/tmp/v2', project_id='p2' WHERE id='s1'"
        )
        conn.commit()
    finally:
        conn.close()

    # Fast writes can share an mtime on coarse-resolution filesystems while
    # these inserts leave the database size unchanged. Make the fixture's
    # signature change deterministic; production still uses actual file stats.
    stat = db_path.stat()
    os.utime(db_path, ns=(stat.st_atime_ns, sig_before[0][1] + 2_000_000_000))
    sig_after = ((str(db_path), db_path.stat().st_mtime_ns, db_path.stat().st_size),)
    assert sig_after != sig_before

    after = sessions._load_opencode_sessions(sig_after, ())
    assert after["s1"]["display_name"] == "V2 title"
    assert after["s1"]["project"] == "other"
    assert [turn["model"] for turn in after["s1"]["turns"]] == ["legacy-model", "legacy-model"]


def test_v2_loaders_work_without_json_functions(tmp_path, monkeypatch):
    """The raw fallback must not reach for JSON functions anywhere on the v2
    path, including the boundary reads, and the scalar loader must still raise
    so ``_load_opencode_sessions`` falls back."""
    db_path = _make_v2_session_db(tmp_path / "opencode.db")
    real_connect = sqlite3.connect

    def authorizer(action, arg1, arg2, db_name, trigger):
        name = str(arg2 or "") if action == sqlite3.SQLITE_FUNCTION else str(arg1 or "")
        return sqlite3.SQLITE_DENY if name.startswith("json") else sqlite3.SQLITE_OK

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_authorizer(authorizer)
        return conn

    monkeypatch.setattr(sessions.sqlite3, "connect", connect)

    with pytest.raises(sqlite3.Error):
        sessions._load_opencode_sessions_scalar(db_path)

    sessions._load_opencode_sessions.cache_clear()
    loaded = sessions._load_opencode_sessions(
        ((str(db_path), db_path.stat().st_mtime_ns, db_path.stat().st_size),)
    )
    assert [turn["timestamp_ms"] for turn in loaded["s1"]["turns"]] == [
        SINCE - 30_000, SINCE + 10_000, SINCE + 40_000, UNTIL + 30_000,
    ]

    windowed = sessions._load_opencode_sessions_raw_json(
        db_path, since_ms=SINCE, until_ms=UNTIL
    )["s1"]
    assert windowed["_prior_event_ms"] == SINCE - 30_000
    assert windowed["_next_event_ms"] == UNTIL + 30_000


@pytest.mark.parametrize("control_type", [None, "agent-switched", "user"])
def test_v2_only_database_without_assistants_is_empty(tmp_path, control_type):
    db = _make_v2_message_db(
        tmp_path / "opencode.db",
        [] if control_type is None else [(TS_COLD, control_type, {})],
    )
    with sqlite3.connect(db) as conn:
        conn.executescript(PROJECT_DDL + SESSION_V2_DDL)
    assert OpenCodeParser(PricingDatabase())._query_db(db, 0, UNTIL) == []
    assert sessions._load_opencode_sessions_scalar(db) == {}
    assert sessions._load_opencode_sessions_raw_json(db) == {}
