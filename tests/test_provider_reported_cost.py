"""A cost the provider itself reported wins over Tokdash's estimate.

OpenCode and Mimo record what a message actually billed. That number is the
provider's own, computed from rates Tokdash may not carry, so it must survive
into the session payload instead of being recomputed from token counts. Both
OpenCode loaders are covered: SQLite without JSON1 falls back to reading the
message blob in Python, and the two have to agree.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tokdash import sessions
from tokdash.pricing import PricingDatabase
from tokdash.sessions import reload_pricing_db

MODEL = "glm-5.2"
PROVIDER = "zai"
REPORTED_COST = 4.25  # nothing like what the rates below produce


@pytest.fixture(autouse=True)
def _priced(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = PricingDatabase().override_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": "test",
                "aliases": {},
                "models": {
                    MODEL: {
                        "provider": PROVIDER,
                        "input": 3.0,
                        "output": 15.0,
                        "cache_read": 0.3,
                        "cache_write": 3.75,
                        "unit": "per_million_tokens",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    reload_pricing_db()
    yield
    reload_pricing_db()


# OpenCode's rule bills cache writes at the input rate, so the model's own
# cache_write rate above is deliberately never reached.
ESTIMATE = ((1_000 + 500) * 3.0 + 100 * 15.0 + 2_000 * 0.3) / 1_000_000


def _message(cost, role: str = "assistant") -> str:
    data = {
        "role": role,
        "modelID": MODEL,
        "providerID": PROVIDER,
        "tokens": {"input": 1_000, "output": 100, "reasoning": 0, "cache": {"write": 500, "read": 2_000}},
    }
    if cost is not None:
        data["cost"] = cost
    return json.dumps(data)


def _session_db(path: Path, cost, *, imports: bool = False, extra_messages=()) -> Path:
    """OpenCode's schema, which Mimo shares.

    imports adds the table Mimo excludes rows through, so its queries are built
    with that clause attached rather than in their simplest form.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE project(id TEXT PRIMARY KEY, worktree TEXT);
            CREATE TABLE session(id TEXT PRIMARY KEY, project_id TEXT, directory TEXT, title TEXT, slug TEXT);
            CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT);
            """
        )
        if imports:
            conn.executescript(
                """
                CREATE TABLE external_import(id TEXT PRIMARY KEY, message_ids TEXT);
                INSERT INTO external_import(id, message_ids) VALUES('i1', '["imported"]');
                """
            )
        conn.execute("INSERT INTO project(id, worktree) VALUES('p1', '/workspace/proj')")
        conn.execute(
            "INSERT INTO session(id, project_id, directory, title, slug)"
            " VALUES('s1', 'p1', '/workspace/proj', 'Session', 'slug')"
        )
        conn.execute(
            "INSERT INTO message(id, session_id, time_created, data) VALUES('m1', 's1', 1000, ?)",
            (_message(cost),),
        )
        for message_id, created_ms in extra_messages:
            conn.execute(
                "INSERT INTO message(id, session_id, time_created, data) VALUES(?, 's1', ?, ?)",
                (message_id, created_ms, _message(cost)),
            )
        conn.commit()
    finally:
        conn.close()
    return path


# Both tools read the same schema, and each falls back to reading the message
# blob in Python when SQLite has no JSON1. All four paths must agree.
PAIRS = {
    "opencode": ("_load_opencode_sessions_scalar", "_load_opencode_sessions_raw_json"),
    "mimo": ("_load_mimo_sessions_scalar", "_load_mimo_sessions_raw_json"),
}
LOADERS = [(tool, name) for tool, names in PAIRS.items() for name in names]


def _db_for(tool: str, path: Path, cost) -> Path:
    return _session_db(path, cost, imports=tool == "mimo")


@pytest.mark.parametrize("tool,loader_name", LOADERS)
def test_loaders_keep_the_reported_cost(tmp_path, tool, loader_name):
    db_path = _db_for(tool, tmp_path / f"{loader_name}.db", REPORTED_COST)

    loaded = getattr(sessions, loader_name)(db_path)["s1"]

    assert [turn["cost"] for turn in loaded["turns"]] == [REPORTED_COST]
    assert loaded["tool"] == tool
    assert REPORTED_COST != pytest.approx(ESTIMATE), "the fixture must not agree by accident"


@pytest.mark.parametrize("tool,loader_name", LOADERS)
@pytest.mark.parametrize("cost", [0, None])
def test_loaders_estimate_when_nothing_was_reported(tmp_path, tool, loader_name, cost):
    """Plan and subscription providers report 0; those turns are still estimated."""
    db_path = _db_for(tool, tmp_path / f"{loader_name}-{cost}.db", cost)

    loaded = getattr(sessions, loader_name)(db_path)["s1"]

    assert loaded["turns"][0]["cost"] == pytest.approx(ESTIMATE)


@pytest.mark.parametrize("tool", list(PAIRS))
def test_both_loaders_agree(tmp_path, tool):
    scalar_name, raw_name = PAIRS[tool]
    db_path = _db_for(tool, tmp_path / f"{tool}-parity.db", REPORTED_COST)

    scalar = getattr(sessions, scalar_name)(db_path)["s1"]
    raw = getattr(sessions, raw_name)(db_path)["s1"]

    assert scalar == raw


@pytest.mark.parametrize("tool,loader_name", LOADERS)
def test_a_reported_cost_is_not_repriced(tmp_path, tool, loader_name):
    """It is the provider's number: a rate edit must not move it."""
    db_path = _db_for(tool, tmp_path / f"{loader_name}-reprice.db", REPORTED_COST)
    turn = getattr(sessions, loader_name)(db_path)["s1"]["turns"][0]

    doubled_path = tmp_path / "doubled.json"
    doubled_path.write_text(
        json.dumps(
            {
                "version": "test",
                "aliases": {},
                "models": {
                    MODEL: {"provider": PROVIDER, "input": 6.0, "output": 30.0, "unit": "per_million_tokens"}
                },
            }
        ),
        encoding="utf-8",
    )
    doubled = PricingDatabase(override_path=doubled_path)

    assert sessions._turn_cost(turn["_bill"], doubled) == REPORTED_COST


# --- the environment the raw loaders exist for ------------------------------


def _deny_json_functions(monkeypatch):
    """Every connection the loaders open refuses SQLite's JSON functions.

    This is the build the raw-JSON loaders are the fallback for. Reaching for
    json_extract, json_valid or json_each anywhere on that path — including the
    boundary reads — raises instead of quietly working in the test.
    """
    real_connect = sqlite3.connect

    def authorizer(action, arg1, arg2, db_name, trigger):
        name = str(arg2 or "") if action == sqlite3.SQLITE_FUNCTION else str(arg1 or "")
        return sqlite3.SQLITE_DENY if name.startswith("json") else sqlite3.SQLITE_OK

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_authorizer(authorizer)
        return conn

    monkeypatch.setattr(sessions.sqlite3, "connect", connect)


def test_mimo_falls_back_when_sqlite_has_no_json_functions(tmp_path, monkeypatch):
    db_path = _session_db(tmp_path / "mimo-nojson.db", REPORTED_COST, imports=True)
    _deny_json_functions(monkeypatch)

    with pytest.raises(sqlite3.Error):
        sessions._load_mimo_sessions_scalar(db_path)
    loaded = sessions._load_mimo_sessions(((str(db_path), 1, 2),))

    assert list(loaded) == ["s1"]
    assert [turn["cost"] for turn in loaded["s1"]["turns"]] == [REPORTED_COST]
    assert loaded["s1"]["tool"] == "mimo"


def test_opencode_falls_back_when_sqlite_has_no_json_functions(tmp_path, monkeypatch):
    """The sibling path, which has no import tables to expand."""
    db_path = _session_db(tmp_path / "opencode-nojson.db", REPORTED_COST)
    _deny_json_functions(monkeypatch)

    with pytest.raises(sqlite3.Error):
        sessions._load_opencode_sessions_scalar(db_path)
    loaded = sessions._load_opencode_sessions_raw_json(db_path)

    assert [turn["cost"] for turn in loaded["s1"]["turns"]] == [REPORTED_COST]


def test_the_mimo_fallback_still_excludes_imported_messages(tmp_path, monkeypatch):
    """The exclusion is the reason that path touched json_each at all."""
    db_path = _session_db(
        tmp_path / "mimo-imported.db",
        REPORTED_COST,
        imports=True,
        extra_messages=(("imported", 2_000),),
    )
    _deny_json_functions(monkeypatch)

    loaded = sessions._load_mimo_sessions(((str(db_path), 1, 2),))

    assert len(loaded["s1"]["turns"]) == 1, "the imported message is not Mimo's usage"


def test_the_mimo_fallback_excludes_imported_boundary_events(tmp_path, monkeypatch):
    """An imported row outside the window must not become the edge event either."""
    db_path = _session_db(
        tmp_path / "mimo-boundary.db",
        REPORTED_COST,
        imports=True,
        # m1 sits in the window at 1000; these two precede it, nearest first.
        extra_messages=(("imported", 800), ("m0", 700)),
    )
    _deny_json_functions(monkeypatch)

    loaded = sessions._load_mimo_sessions(((str(db_path), 1, 2),), (), 900, 1_100)

    assert [turn["timestamp_ms"] for turn in loaded["s1"]["turns"]] == [1_000]
    assert loaded["s1"]["_prior_event_ms"] == 700, "the imported row at 800 is not an event"
