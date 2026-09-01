"""Tests for Kilo Code as a session source (sessions.py).

Kilo Code is field-for-field the OpenCode schema, so the harness reuses the
shared opencode loader with tool="kilocode", recorded cost ignored, and the
split-cache-write billing rule (the KiloCodeParser pricing shape). The
load-bearing property is bucket-for-bucket parity with KiloCodeParser,
including the orphan message whose session row was deleted — the parser bills
it (no join) and the LEFT-join fix in the shared loaders (Integration 1b) must
let the harness bill it too.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tokdash import sessions
from tokdash.pricing import PricingDatabase
from tokdash.sessions import (
    SESSION_TOOLS,
    _kilocode_db_signature,
    _kilocode_sessions,
    _load_opencode_sessions_scalar,
    get_sessions_data,
    reload_pricing_db,
)
from tokdash.sources.coding_tools import KiloCodeParser

# Whole-second base so the parser's datetime window converts back to the same
# millisecond boundary without float drift.
BASE = 1_787_000_000_000

PROJECT_DDL = "CREATE TABLE project (id text primary key, worktree text);"
SESSION_DDL = """
CREATE TABLE session (
    id text primary key,
    project_id text,
    slug text not null,
    directory text not null,
    title text not null
);
"""
MESSAGE_DDL = """
CREATE TABLE message (
    id text primary key,
    session_id text not null,
    time_created integer not null,
    time_updated integer not null,
    data text not null
);
"""


@pytest.fixture(autouse=True)
def _clean_caches():
    sessions._load_kilocode_sessions.cache_clear()
    KiloCodeParser._query_cache.clear()
    KiloCodeParser._query_cache_sig = ()
    reload_pricing_db()
    yield
    sessions._load_kilocode_sessions.cache_clear()
    KiloCodeParser._query_cache.clear()
    KiloCodeParser._query_cache_sig = ()
    reload_pricing_db()


def _kilo_root(tmp_path: Path) -> Path:
    root = tmp_path / "xdg" / "kilo"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _make_db(path: Path, projects=(), sess=(), messages=()) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(PROJECT_DDL + SESSION_DDL + MESSAGE_DDL)
    conn.executemany("INSERT INTO project (id, worktree) VALUES (?,?)", projects)
    conn.executemany(
        "INSERT INTO session (id, project_id, slug, directory, title) "
        "VALUES (?,?,?,?,?)",
        sess,
    )
    conn.executemany(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) "
        "VALUES (?,?,?,?,?)",
        messages,
    )
    conn.commit()
    conn.close()
    return path


def _advance_mtime(db: Path) -> None:
    """Give *db* a strictly later mtime, as a real second write would have.

    _load_kilocode_sessions is an lru_cache keyed on _kilocode_db_signature(),
    i.e. exactly (path, mtime_ns, size). A SQLite INSERT changes NEITHER when
    the row fits an already-allocated free page and the write lands in the
    same clock tick as the previous one — measured 18/40 appends on this
    filesystem with the fixtures below. When that happens the cache key is
    unchanged, the loader returns the pre-write result, and a test that
    appends and re-reads fails through no fault of the code under test (this
    flaked ~1 run in 8). Production writes are separated in time, so advancing
    mtime models the real case; the assertion still tests what it claims, that
    a moved signature invalidates the cache.
    """
    st = db.stat()
    os.utime(db, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def _append_message(db: Path, mid: str, sid: str, ts: int, data: dict) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) "
        "VALUES (?,?,?,?,?)",
        (mid, sid, ts, ts, json.dumps(data)),
    )
    conn.commit()
    conn.close()
    # An append alone need not move the signature; see _advance_mtime.
    _advance_mtime(db)


def _proj(pid="p1", worktree="/"):
    return (pid, worktree)


def _sess(sid="ses_1", pid="p1", slug="proj-slug", directory="/tmp/proj",
          title="proj session"):
    return (sid, pid, slug, directory, title)


def _msg(mid, sid, ts, data):
    return (mid, sid, ts, ts, json.dumps(data))


def _assistant(model="deepseek-chat", provider="deepseek", cwd="", root="/",
               cost=None, **tokens):
    data = {
        "role": "assistant",
        "modelID": model,
        "providerID": provider,
        "path": {"cwd": cwd, "root": root},
        "tokens": tokens,
    }
    if cost is not None:
        data["cost"] = cost
    return data


def _user():
    return {"role": "user", "modelID": "deepseek-chat", "providerID": "deepseek"}


def _patch_env(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))


def _dt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _turn_sums(raw):
    turns = [t for s in raw.values() for t in s["turns"]]
    return {
        "in": sum(t["tokens_in"] for t in turns),
        "cache": sum(t["tokens_cache"] for t in turns),
        "out": sum(t["tokens_out"] for t in turns),
        "reasoning": sum(t["tokens_reasoning"] for t in turns),
        "cost": sum(t["cost"] for t in turns),
    }


def test_kilocode_registered():
    assert "kilocode" in SESSION_TOOLS
    assert sessions.TOOL_LABELS["kilocode"] == "Kilo Code"


def test_mapping(monkeypatch, tmp_path):
    _patch_env(monkeypatch, tmp_path)
    _make_db(
        _kilo_root(tmp_path) / "kilo.db",
        projects=[_proj()],
        sess=[_sess("ses_a", title="proj session"), _sess("ses_b", title="")],
        messages=[
            _msg("m1", "ses_a", BASE,
                 _assistant(input=1000, output=100, cache={"read": 400, "write": 250})),
            _msg("m2", "ses_a", BASE + 10_000,
                 _assistant(input=467, output=58,
                            cache={"read": 14112, "write": 0}, reasoning=30)),
            _msg("m3", "ses_b", BASE + 20_000,
                 _assistant(input=10, output=1, cache={"read": 0, "write": 0})),
        ],
    )
    raw = _kilocode_sessions()

    a = raw["ses_a"]
    assert a["tool"] == "kilocode"
    assert a["session_id"] == "ses_a"
    assert a["display_name"] == "proj session"
    # worktree "/" falls through to session.directory.
    assert a["project"] == "proj"
    cold, warm = a["turns"]
    assert cold["turn_index"] == 1
    assert cold["timestamp_ms"] == BASE
    # tokens_in folds cache.write into billable input.
    assert cold["tokens_in"] == 1000 + 250
    assert cold["tokens_cache"] == 400
    assert cold["tokens_out"] == 100
    assert cold["tokens_reasoning"] == 0
    assert cold["tokens"] == 1000 + 250 + 400 + 100
    # Model is the bare modelID; billing is provider/model (rule 6).
    assert cold["model"] == "deepseek-chat"
    bill = cold["_bill"]
    assert bill["rule"] == "split-cache-write"
    assert bill["model"] == "deepseek/deepseek-chat"
    assert "fixed" not in bill
    assert cold["cost"] == pytest.approx(
        PricingDatabase().get_cost("deepseek-chat", 1000, 100, 400, 250)
    )
    assert warm["tokens_in"] == 467
    assert warm["tokens_cache"] == 14112
    assert warm["tokens_out"] == 58
    assert warm["tokens_reasoning"] == 30

    # Empty title falls back to slug.
    assert raw["ses_b"]["display_name"] == "proj-slug"


def test_user_rows_ignored(monkeypatch, tmp_path):
    _patch_env(monkeypatch, tmp_path)
    db = _kilo_root(tmp_path) / "kilo.db"
    _make_db(
        db,
        projects=[_proj()],
        sess=[_sess("ses_a"), _sess("ses_zero")],
        messages=[
            # User rows carry no tokens field at all.
            _msg("m1", "ses_a", BASE, _user()),
            # An all-zero assistant row is dropped by the turn builder.
            _msg("m2", "ses_zero", BASE + 1000,
                 _assistant(input=0, output=0, cache={"read": 0, "write": 0})),
        ],
    )
    assert _kilocode_sessions() == {}

    _append_message(db, "m3", "ses_a", BASE + 2000,
                    _assistant(input=5, output=1, cache={"read": 0, "write": 0}))
    raw = _kilocode_sessions()
    assert set(raw) == {"ses_a"}
    assert len(raw["ses_a"]["turns"]) == 1
    assert raw["ses_a"]["turns"][0]["tokens_out"] == 1


def test_parity_with_usage_parser(monkeypatch, tmp_path):
    """The load-bearing property: over the full window and partial windows,
    the harness turn sums equal KiloCodeParser entries bucket for bucket.
    The fixture includes an orphan message (session row deleted), which the
    no-join parser bills and the LEFT-join harness must bill too."""
    _patch_env(monkeypatch, tmp_path)
    db = _kilo_root(tmp_path) / "kilo.db"
    _make_db(
        db,
        projects=[_proj()],
        sess=[_sess("ses_a")],  # no session row for ses_orphan
        messages=[
            _msg("m1", "ses_a", BASE,
                 _assistant(input=14579, output=37, cache={"read": 0, "write": 0})),
            _msg("m2", "ses_a", BASE + 10_000,
                 _assistant(input=467, output=58, cache={"read": 14112, "write": 0})),
            _msg("m5", "ses_orphan", BASE + 20_000,
                 _assistant(input=100, output=50, cache={"read": 0, "write": 250})),
            _msg("m3", "ses_a", BASE + 30_000, _user()),
            _msg("m4", "ses_a", BASE + 500_000,
                 _assistant(input=700, output=60, cache={"read": 200, "write": 100},
                            reasoning=30)),
        ],
    )

    windows = [
        (None, None, 4),           # all four assistant rows
        (BASE + 5_000, None, 3),   # m1 (at BASE) excluded
        (None, BASE + 15_000, 2),  # m5, m4 excluded (spanning session ses_a)
        (BASE + 5_000, BASE + 15_000, 1),  # only m2; ses_a spans until_ms
    ]
    for since_ms, until_ms, expected_entries in windows:
        parser = KiloCodeParser(PricingDatabase())
        entries = parser.collect(_dt(since_ms) if since_ms else None,
                                 _dt(until_ms) if until_ms else None)
        assert len(entries) == expected_entries
        assert all(e["source"] == "kilocode" for e in entries)
        raw = _kilocode_sessions(since_ms=since_ms, until_ms=until_ms)
        h = _turn_sums(raw)
        assert h["in"] == sum(e["input"] + e["cacheWrite"] for e in entries)
        assert h["cache"] == sum(e["cacheRead"] for e in entries)
        assert h["out"] == sum(e["output"] for e in entries)
        assert h["reasoning"] == sum(e["reasoning"] for e in entries)
        assert h["cost"] == pytest.approx(sum(e["cost"] for e in entries))
        if since_ms is None and until_ms is None:
            # The orphan is billed by the harness, not just the parser.
            assert "ses_orphan" in raw
            assert raw["ses_orphan"]["turns"][0]["tokens_in"] == 100 + 250
            assert raw["ses_orphan"]["project"] == "unknown"
        if since_ms is None and until_ms == BASE + 15_000:
            assert "ses_orphan" not in raw

    # Integration 1b: the same fix must hold through the shared loader's
    # default tool="opencode" call — there is no dedicated opencode/mimo
    # session test file, so this is their orphan coverage.
    oc = _load_opencode_sessions_scalar(db)
    assert "ses_orphan" in oc
    assert oc["ses_orphan"]["tool"] == "opencode"
    assert oc["ses_a"]["tool"] == "opencode"
    assert len(oc["ses_orphan"]["turns"]) == 1


def test_window_half_open(monkeypatch, tmp_path):
    S, U = BASE, BASE + 1_000_000
    _patch_env(monkeypatch, tmp_path)
    _make_db(
        _kilo_root(tmp_path) / "kilo.db",
        projects=[_proj()],
        sess=[_sess("s_at_since"), _sess("s_at_until"), _sess("s_before")],
        messages=[
            _msg("m1", "s_at_since", S,
                 _assistant(input=1, output=1, cache={"read": 0, "write": 0})),
            _msg("m2", "s_at_until", U,
                 _assistant(input=1, output=1, cache={"read": 0, "write": 0})),
            _msg("m3", "s_before", S - 1000,
                 _assistant(input=1, output=1, cache={"read": 0, "write": 0})),
        ],
    )
    raw = _kilocode_sessions(since_ms=S, until_ms=U)
    assert set(raw) == {"s_at_since"}
    assert raw["s_at_since"]["turns"][0]["timestamp_ms"] == S


def test_multi_db_union(monkeypatch, tmp_path):
    _patch_env(monkeypatch, tmp_path)
    root = _kilo_root(tmp_path)
    _make_db(root / "kilo.db", projects=[_proj()], sess=[_sess("ses_main")],
             messages=[_msg("m1", "ses_main", BASE,
                            _assistant(input=1, output=1, cache={"read": 0, "write": 0}))])
    _make_db(root / "kilo-dev.db", projects=[_proj()], sess=[_sess("ses_dev")],
             messages=[_msg("m2", "ses_dev", BASE + 1000,
                            _assistant(input=2, output=2, cache={"read": 0, "write": 0}))])
    raw = _kilocode_sessions()
    assert set(raw) == {"ses_main", "ses_dev"}
    assert raw["ses_main"]["turns"][0]["tokens_out"] == 1
    assert raw["ses_dev"]["turns"][0]["tokens_out"] == 2
    # The harness signature pins the same file set (DB + WAL + SHM) as the
    # Overview parser's _file_signatures.
    assert _kilocode_db_signature() == KiloCodeParser(PricingDatabase())._file_signatures()


def test_legacy_fallback_never_double_read(monkeypatch, tmp_path):
    _patch_env(monkeypatch, tmp_path)
    root = _kilo_root(tmp_path)
    _make_db(root / "opencode-legacy.db", projects=[_proj()],
             sess=[_sess("ses_legacy")],
             messages=[_msg("m1", "ses_legacy", BASE,
                            _assistant(input=1, output=1, cache={"read": 0, "write": 0}))])
    # Pre-rename install: the legacy name is read while no kilo-named file
    # exists.
    assert set(_kilocode_sessions()) == {"ses_legacy"}

    # A kilo-named DB appearing shadows the legacy file (migrated install).
    _make_db(root / "kilo.db", projects=[_proj()], sess=[_sess("ses_kilo")],
             messages=[_msg("m2", "ses_kilo", BASE + 1000,
                            _assistant(input=1, output=1, cache={"read": 0, "write": 0}))])
    assert set(_kilocode_sessions()) == {"ses_kilo"}


def test_missing_dir_empty(monkeypatch, tmp_path):
    # XDG root exists but no kilo dir at all.
    _patch_env(monkeypatch, tmp_path)
    assert _kilocode_sessions() == {}


def test_corrupt_db_skips_siblings(monkeypatch, tmp_path):
    _patch_env(monkeypatch, tmp_path)
    root = _kilo_root(tmp_path)
    (root / "kilo.db").write_bytes(
        b"garbage, not a sqlite database at all. padding padding padding."
    )
    _make_db(root / "kilo-dev.db", projects=[_proj()], sess=[_sess("ses_dev")],
             messages=[_msg("m1", "ses_dev", BASE,
                            _assistant(input=1, output=1, cache={"read": 0, "write": 0}))])
    listing = get_sessions_data("kilocode", "all")
    assert [s["session_id"] for s in listing["sessions"]] == ["ses_dev"]
    assert listing["tool_label"] == "Kilo Code"


def test_cost_from_pricing_db_not_recorded(monkeypatch, tmp_path):
    _patch_env(monkeypatch, tmp_path)
    _make_db(_kilo_root(tmp_path) / "kilo.db", projects=[_proj()],
             sess=[_sess("ses_a")],
             messages=[_msg("m1", "ses_a", BASE,
                            _assistant(input=1000, output=100,
                                       cache={"read": 400, "write": 250}, cost=999))])
    raw = _kilocode_sessions()
    turn = raw["ses_a"]["turns"][0]
    expected = PricingDatabase().get_cost("deepseek-chat", 1000, 100, 400, 250)
    assert turn["cost"] == pytest.approx(expected)
    assert turn["cost"] != 999
    # deepseek-chat has a distinct cache_write rate, so the split rule is
    # observable: folding the write into input would cost differently.
    folded = PricingDatabase().get_cost("deepseek-chat", 1000 + 250, 100, 400, 0)
    assert expected != folded


def test_cache_invalidates_on_write(monkeypatch, tmp_path):
    _patch_env(monkeypatch, tmp_path)
    db = _kilo_root(tmp_path) / "kilo.db"
    _make_db(db, projects=[_proj()], sess=[_sess("ses_a")],
             messages=[_msg("m1", "ses_a", BASE,
                            _assistant(input=1, output=1, cache={"read": 0, "write": 0}))])
    first = _kilocode_sessions()
    assert len(first["ses_a"]["turns"]) == 1
    # Warm cache: same signature, same window -> cache hit, same data.
    assert _kilocode_sessions() == first

    _append_message(db, "m2", "ses_a", BASE + 1000,
                    _assistant(input=2, output=2, cache={"read": 0, "write": 0}))
    second = _kilocode_sessions()
    assert len(second["ses_a"]["turns"]) == 2
    assert [t["timestamp_ms"] for t in second["ses_a"]["turns"]] == [BASE, BASE + 1000]

    # A pricing reload also clears the session cache (turns price at load).
    reload_pricing_db()
    assert _kilocode_sessions() == second
