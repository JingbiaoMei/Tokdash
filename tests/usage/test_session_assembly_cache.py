"""Assembled sessions must survive between requests, and only where they still hold.

Deserializing a session's rows and merging them is the expensive half of a stored
read, and it repeated in full on every request. The memo that would have been
obvious — one keyed on the window — is worth nothing, because the windows a
reader opens end today and the route's own response cache already absorbs repeats
inside its TTL. Keying on the files behind a session instead makes one entry serve
every window and every period at once.

That only works if the token is exact. These tests pin what has to invalidate
(a file changing, a file leaving, a rate edit) against what must not (a different
window, a wider period, a second request), and pin the Python window filter to the
SQL one it replaced.
"""
import json

from tokdash import sessions as sessions_module
from tokdash.sessions import (
    _assembly_token,
    _row_touches_window,
    _session_records_to_raw_sessions,
    _stored_session_assembly,
    _SESSION_ASSEMBLY,
)
from tokdash.usage_store import UsageEntryStore

DAY = 24 * 60 * 60 * 1000
BASE = 1_800_000_000_000


def _turn(stamp, index=1, tokens=10):
    return {
        "turn_index": index,
        "timestamp_ms": stamp,
        "model": "claude-sonnet-4.5",
        "tokens_in": tokens,
        "tokens_cache": 0,
        "tokens_out": 5,
        "tokens_reasoning": 0,
        "tokens": tokens + 5,
        "cost": 0.0,
    }


def _raw(session_id, stamps, *, name=None, project="proj"):
    return {
        "tool": "claude",
        "session_id": session_id,
        "project": project,
        "display_name": name,
        "turns": [_turn(stamp, index) for index, stamp in enumerate(stamps, start=1)],
    }


def _sync(store, contents, *, tool="claude", stamps=None):
    """Store one row per path; ``stamps`` restamps only the paths it names."""
    stamps = stamps or {}
    files = tuple(
        (path, stamps.get(path, 1), 100 + stamps.get(path, 1)) for path in sorted(contents)
    )
    store.sync_session_files(
        tool,
        files,
        parser={"v": 1},
        parse_file_session=lambda file_sig: contents[file_sig[0]],
    )
    return files


def _store(tmp_path, contents, **kwargs):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    _sync(store, contents, **kwargs)
    return store


def _canon(value):
    return json.dumps(value, sort_keys=True, default=str)


class _CountingStore:
    """Wraps a store and counts the reads that deserialize ``raw_json``."""

    def __init__(self, store):
        self._store = store
        self.decoded_ids = []

    def query_session_signatures(self, tool):
        return self._store.query_session_signatures(tool)

    def query_session_records_by_ids(self, tool, session_ids):
        ids = sorted(session_ids)
        self.decoded_ids.append(ids)
        return self._store.query_session_records_by_ids(tool, ids)

    def query_session_records(self, tool, **kwargs):
        return self._store.query_session_records(tool, **kwargs)


def test_a_second_read_of_an_unchanged_tool_deserializes_nothing(tmp_path):
    contents = {
        str(tmp_path / "a.jsonl"): _raw("s1", [BASE, BASE + 1000]),
        str(tmp_path / "b.jsonl"): _raw("s1", [BASE + 2000], name="Real"),
        str(tmp_path / "c.jsonl"): _raw("s2", [BASE + 3000]),
    }
    store = _CountingStore(_store(tmp_path, contents))

    first, _ = _stored_session_assembly(store, "claude", None, None)
    assert store.decoded_ids == [["s1", "s2"]]

    second, _ = _stored_session_assembly(store, "claude", None, None)
    assert store.decoded_ids == [["s1", "s2"]]
    # Same objects, not merely equal ones: that is what makes the read free.
    assert second["s1"] is first["s1"]
    assert second["s2"] is first["s2"]


def test_a_narrower_window_reuses_what_a_wider_one_assembled(tmp_path):
    contents = {
        str(tmp_path / "a.jsonl"): _raw("s1", [BASE, BASE + DAY]),
        str(tmp_path / "b.jsonl"): _raw("s2", [BASE + 2 * DAY]),
    }
    store = _CountingStore(_store(tmp_path, contents))

    everything, _ = _stored_session_assembly(store, "claude", None, None)
    assert store.decoded_ids == [["s1", "s2"]]

    # A window that only s1 touches. Nothing is rebuilt, and the entry the
    # unbounded read made is the one served.
    windowed, _ = _stored_session_assembly(store, "claude", BASE, BASE + DAY)
    assert set(windowed) == {"s1"}
    assert store.decoded_ids == [["s1", "s2"]]
    assert windowed["s1"] is everything["s1"]


def test_an_appended_file_rebuilds_only_the_session_it_belongs_to(tmp_path):
    paths = [str(tmp_path / name) for name in ("a.jsonl", "b.jsonl", "c.jsonl")]
    contents = {
        paths[0]: _raw("s1", [BASE]),
        paths[1]: _raw("s1", [BASE + 1000]),
        paths[2]: _raw("s2", [BASE + 2000]),
    }
    inner = _store(tmp_path, contents)
    store = _CountingStore(inner)
    first, _ = _stored_session_assembly(store, "claude", None, None)

    contents[paths[1]] = _raw("s1", [BASE + 1000, BASE + 1500])
    _sync(inner, contents, stamps={paths[1]: 2})

    second, _ = _stored_session_assembly(store, "claude", None, None)
    assert store.decoded_ids == [["s1", "s2"], ["s1"]]
    assert len(second["s1"]["turns"]) == 3
    assert second["s2"] is first["s2"]


def test_a_file_leaving_the_session_invalidates_it(tmp_path):
    paths = [str(tmp_path / name) for name in ("a.jsonl", "b.jsonl")]
    contents = {paths[0]: _raw("s1", [BASE]), paths[1]: _raw("s1", [BASE + 1000])}
    inner = _store(tmp_path, contents)
    store = _CountingStore(inner)
    assert len(_stored_session_assembly(store, "claude", None, None)[0]["s1"]["turns"]) == 2

    # A non-durable sync deletes the row outright, leaving every surviving row's
    # signature untouched — only the shape of the set says anything changed.
    del contents[paths[1]]
    inner.sync_session_files(
        "claude",
        ((paths[0], 1, 101),),
        parser={"v": 1},
        parse_file_session=lambda file_sig: contents[file_sig[0]],
        durable=False,
    )

    second, _ = _stored_session_assembly(store, "claude", None, None)
    assert store.decoded_ids == [["s1"], ["s1"]]
    assert len(second["s1"]["turns"]) == 1


def test_new_rates_invalidate_every_assembled_session(tmp_path, monkeypatch):
    contents = {str(tmp_path / "a.jsonl"): _raw("s1", [BASE])}
    store = _CountingStore(_store(tmp_path, contents))
    _stored_session_assembly(store, "claude", None, None)

    monkeypatch.setattr(sessions_module, "_pricing_signature", lambda: ("edited",))
    _stored_session_assembly(store, "claude", None, None)
    assert store.decoded_ids == [["s1"], ["s1"]]


def test_the_cached_read_matches_the_uncached_one(tmp_path, monkeypatch):
    contents = {
        str(tmp_path / "a.jsonl"): _raw("s1", [BASE, BASE + DAY]),
        str(tmp_path / "b.jsonl"): _raw("s1", [BASE + 2 * DAY], name="Titled"),
        str(tmp_path / "c.jsonl"): _raw("s2", [BASE + 3 * DAY], project="other"),
        str(tmp_path / "d.jsonl"): _raw("s3", [BASE - DAY]),
    }
    store = _store(tmp_path, contents)

    windows = [
        (None, None),
        (BASE, BASE + 4 * DAY),
        (BASE + DAY, BASE + 3 * DAY),
        (None, BASE + DAY),
        (BASE + 2 * DAY, None),
        (BASE + 9 * DAY, BASE + 10 * DAY),
    ]
    for since_ms, until_ms in windows:
        expected = _session_records_to_raw_sessions(
            "claude",
            store.query_session_records(
                "claude", since_ms=since_ms, until_ms=until_ms, whole_sessions=True
            ),
        )
        # Twice: the second pass reads the entries the first one left behind.
        for _attempt in range(2):
            assembled, _tokens = _stored_session_assembly(store, "claude", since_ms, until_ms)
            assert list(assembled) == list(expected)
            assert _canon(assembled) == _canon(expected)


def test_the_python_window_filter_agrees_with_the_sql_one(tmp_path):
    contents = {
        str(tmp_path / f"{index}.jsonl"): _raw(f"s{index}", [BASE + index * DAY])
        for index in range(6)
    }
    # One session spanning two far-apart files: its rows straddle windows that
    # neither row alone covers, which is where a per-session predicate diverges
    # from the per-row one SQL applies.
    contents[str(tmp_path / "wide-a.jsonl")] = _raw("wide", [BASE])
    contents[str(tmp_path / "wide-b.jsonl")] = _raw("wide", [BASE + 5 * DAY])
    store = _store(tmp_path, contents)

    edges = [BASE + offset * DAY for offset in range(-1, 8)]
    for since_ms in [None, *edges]:
        for until_ms in [None, *edges]:
            expected = {
                str(record["session_id"])
                for record in store.query_session_records(
                    "claude", since_ms=since_ms, until_ms=until_ms, whole_sessions=True
                )
            }
            assembled, _tokens = _stored_session_assembly(store, "claude", since_ms, until_ms)
            assert set(assembled) == expected, (since_ms, until_ms)


def test_a_null_bound_fails_the_window_the_way_sql_does():
    # SQL compares NULL to anything as NULL, which is not true, so the row is out.
    assert _row_touches_window(None, None, None, None) is True
    assert _row_touches_window(None, 10, 5, None) is True
    assert _row_touches_window(None, 10, None, 20) is False
    assert _row_touches_window(10, None, 5, None) is False


def test_the_token_is_a_set_not_a_sequence():
    pairs = [("b.jsonl", "sig-b"), ("a.jsonl", "sig-a")]
    assert _assembly_token(pairs, ()) == _assembly_token(reversed(pairs), ())
    assert _assembly_token(pairs, ()) != _assembly_token(pairs[:1], ())
    assert _assembly_token(pairs, ()) != _assembly_token(pairs, ("rates",))


def test_a_live_loader_reuses_the_sessions_whose_files_did_not_change(monkeypatch):
    """The live path's own memo is keyed on the whole tool, which is too coarse.

    One appended log — the session being worked in right now — changed the tool's
    signature and re-merged every session it had. Two files per session so the
    merge builds something new, and object identity means something.
    """
    contents = {
        "/fake/a.jsonl": _raw("s1", [BASE]),
        "/fake/b.jsonl": _raw("s1", [BASE + 1000]),
        "/fake/c.jsonl": _raw("s2", [BASE + 2000]),
        "/fake/d.jsonl": _raw("s2", [BASE + 3000]),
    }
    monkeypatch.setattr(
        sessions_module,
        "_parse_claude_session_file",
        lambda path_str, *_args: contents[path_str],
    )
    loader = sessions_module._load_claude_sessions
    loader.cache_clear()
    try:
        stamps = {path: 1 for path in contents}
        first = loader(tuple((path, stamps[path], 100) for path in sorted(contents)), ())

        contents["/fake/b.jsonl"] = _raw("s1", [BASE + 1000, BASE + 1500])
        stamps["/fake/b.jsonl"] = 2
        second = loader(tuple((path, stamps[path], 100) for path in sorted(contents)), ())

        assert second["s2"] is first["s2"]
        assert second["s1"] is not first["s1"]
        assert len(second["s1"]["turns"]) == 3
    finally:
        loader.cache_clear()


def test_the_turn_budget_evicts_and_can_be_switched_off(tmp_path, monkeypatch):
    contents = {
        str(tmp_path / "a.jsonl"): _raw("s1", [BASE + n for n in range(6)]),
        str(tmp_path / "b.jsonl"): _raw("s2", [BASE + 100 + n for n in range(6)]),
    }
    store = _CountingStore(_store(tmp_path, contents))

    monkeypatch.setenv("TOKDASH_SESSION_CACHE_TURNS", "0")
    _stored_session_assembly(store, "claude", None, None)
    _stored_session_assembly(store, "claude", None, None)
    assert store.decoded_ids == [["s1", "s2"], ["s1", "s2"]]
    assert _SESSION_ASSEMBLY.stats() == (0, 0)

    # Room for one session of six turns, so caching both evicts the older.
    monkeypatch.setenv("TOKDASH_SESSION_CACHE_TURNS", "6")
    _SESSION_ASSEMBLY.clear()
    _stored_session_assembly(store, "claude", None, None)
    assert _SESSION_ASSEMBLY.stats() == (1, 6)
    _stored_session_assembly(store, "claude", None, None)
    assert store.decoded_ids[-1] == ["s1"]

    # Switching it off under a running server drops what it was already holding
    # rather than leaving it resident for the life of the process.
    monkeypatch.setenv("TOKDASH_SESSION_CACHE_TURNS", "0")
    _stored_session_assembly(store, "claude", None, None)
    assert _SESSION_ASSEMBLY.stats() == (0, 0)
