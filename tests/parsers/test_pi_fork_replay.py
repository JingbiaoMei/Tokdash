"""Issue #124: Pi fork files replay history, but their new usage is real.

Wire-format evidence: badlogic/pi-mono commit
2b0a123de98318c2ff8069661721ce0c3794c34e,
packages/coding-agent/src/core/session-manager.ts:
createBranchedSession (label filtering/relinked parents), forkFrom (verbatim
entries and parentSession), assertValidSessionId (custom IDs with underscores).
The initial parent/child token example follows the reporter's redacted fixture:
https://github.com/JingbiaoMei/Tokdash/issues/124#issuecomment-5855921857
"""
import copy
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from tokdash import compute, sessions
from tokdash.compute import _collect_parser_file
from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import BaseParser, PiAgentParser, _sig_cache
from tokdash.usage_store import UsageEntryStore


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("PI_AGENT_DIR", str(tmp_path / "pi"))
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TOKDASH_USAGE_DB_DURABLE", "0")
    _sig_cache.clear()
    BaseParser._entry_cache.clear()


def turn(mid, tokens, day=14):
    return {
        "type": "message", "id": mid, "parentId": "previous-" + mid,
        "timestamp": f"2026-09-{day:02}T12:00:00.000Z",
        "message": {"role": "assistant", "model": "test-model", "provider": "test",
                    "usage": {"input": tokens, "output": tokens // 10,
                              "cacheRead": 0, "cacheWrite": 0,
                              "cost": {"total": tokens / 1000}}},
    }


def write(root, sid, turns, parent=None, name=None):
    path = root / f"2026-09-14_{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {"type": "session", "version": 3, "id": sid, "cwd": "/project"}
    if parent:
        header["parentSession"] = str(parent)
    rows = [header, *turns]
    if name:
        rows.append({"type": "session_info", "name": name})
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def signatures(root):
    return tuple(sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in root.rglob("*.jsonl")))


def collect(root):
    parser = PiAgentParser(PricingDatabase())
    parser._file_signatures = lambda: signatures(root)
    return parser._parse_all()


def sync(store, root, calls=None, version=None):
    parser = PiAgentParser(PricingDatabase())
    sigs = signatures(root)
    context = parser.prepare_file_context(sigs, previous_context=store.file_contexts("pi_agent"))
    def parse(sig):
        if calls is not None:
            calls.append(sig[0])
        return _collect_parser_file(parser, sig, file_context=context)
    return store.sync_files(
        "pi_agent", sigs, parser=version or parser.persistent_parser_signature(),
        file_context=context, parse_file_entries=parse, cross_file_stable_keys=True,
    )


def total(rows):
    return sum(e["input"] + e["output"] + e["cacheRead"] + e["cacheWrite"] for e in rows)


@pytest.mark.parametrize("named", [False, True])
def test_parent_fork_and_views(tmp_path, named):
    root = tmp_path / "pi"
    shared, own = turn("34f479e3", 1000), turn("bbb00001", 500, 17)
    parent = write(root, "parent", [shared])
    child = write(root / "forks", "child", [shared, own], parent,
                  "subagent-worker-run-1" if named else None)
    assert total(collect(root)) == 1650
    store = UsageEntryStore(tmp_path / "usage.db")
    assert sync(store, root)
    assert total(store.query_entries(sources=["pi_agent"])) == 1650
    assert not sync(store, root)
    raw = sessions._load_pi_sessions(signatures(root))
    assert {sid: len(s["turns"]) for sid, s in raw.items()} == {"parent": 1, "child": 1}
    own_date = (
        datetime.fromisoformat(own["timestamp"].replace("Z", "+00:00"))
        .astimezone()
        .strftime("%Y-%m-%d")
    )
    data = sessions.get_sessions_data("pi_agent", "today", own_date, own_date)
    assert [(r["session_id"], r["tokens"]) for r in data["sessions"]] == [("child", 550)]
    detail = sessions.get_session_detail("pi_agent", "child")
    assert detail["session"]["tokens"] == 550
    # Trimming the aggregate must not mutate the cached per-file parse.
    stat = child.stat()
    assert len(sessions._parse_pi_session_file(str(child), stat.st_mtime_ns, stat.st_size)["turns"]) == 2


def test_nested_siblings_order_and_missing_parent(tmp_path):
    root = tmp_path / "pi"
    a, b, c, d = (turn(mid, n) for mid, n in [("a", 1000), ("b", 500), ("c", 200), ("d", 100)])
    parent = write(root / "z", "parent", [a])
    first = write(root / "b", "child", [a, b], parent)
    write(root / "a", "grandchild", [a, b, c], first)
    write(root / "c", "sibling", [a, d], parent)
    expected = 1980
    for absent in (False, True):
        if absent:
            parent.unlink()
        sigs = signatures(root)
        assert total(collect(root)) == expected
        forward = sessions._load_pi_sessions(sigs)
        reverse = sessions._load_pi_sessions(tuple(reversed(sigs)))
        assert forward == reverse
        assert sum(t["tokens"] for s in forward.values() for t in s["turns"]) == expected
        store = UsageEntryStore(tmp_path / f"usage-{absent}.db")
        sync(store, root)
        assert total(store.query_entries(sources=["pi_agent"])) == expected


def test_short_id_collisions_and_distinct_fork_events_survive(tmp_path):
    root = tmp_path / "pi"
    shared = turn("deadbeef", 1000)
    parent = write(root, "parent", [shared])
    write(root, "unrelated", [shared])  # Even all corroborating fields coincide.
    own = turn("deadbeef", 500, 17)
    write(root / "forks", "child", [own], parent)
    assert total(collect(root)) == 2750
    raw = sessions._load_pi_sessions(signatures(root))
    assert set(raw) == {"parent", "unrelated", "child"}
    store = UsageEntryStore(tmp_path / "usage.db")
    sync(store, root)
    assert total(store.query_entries(sources=["pi_agent"])) == 2750


def test_incremental_new_fork_append_and_owner_removal(tmp_path):
    root = tmp_path / "pi"
    shared, own = turn("a", 1000), turn("b", 500, 17)
    parent = write(root, "parent", [shared])
    store = UsageEntryStore(tmp_path / "usage.db")
    sync(store, root)
    child = write(root / "forks", "child", [shared, own], parent)
    calls = []
    sync(store, root, calls)
    assert calls == [str(child)]
    assert total(store.query_entries(sources=["pi_agent"])) == 1650
    with child.open("a") as handle:
        handle.write(json.dumps(turn("c", 100, 18)) + "\n")
    calls.clear()
    sync(store, root, calls)
    assert calls == [str(child)]
    assert total(store.query_entries(sources=["pi_agent"])) == 1760
    parent.unlink()
    sync(store, root)
    assert total(store.query_entries(sources=["pi_agent"])) == 1760


def test_restored_ancestor_invalidates_unchanged_descendant(tmp_path):
    root = tmp_path / "pi"
    shared = turn("a", 1000)
    parent = write(root, "parent", [shared])
    missing = root / "2026-09-14_middle.jsonl"
    child = write(root / "forks", "child", [shared, turn("b", 500)], missing)
    store = UsageEntryStore(tmp_path / "usage.db")
    sync(store, root)
    # Until the link exists there is no proof these two families are related.
    assert total(store.query_entries(sources=["pi_agent"])) == 2750
    write(root, "middle", [shared], parent)
    calls = []
    sync(store, root, calls)
    assert str(child) in calls
    assert total(store.query_entries(sources=["pi_agent"])) == 1650


def test_tool_results_artifacts_and_replay_only_forks(tmp_path):
    root = tmp_path / "pi"
    shared = turn("a", 1000)
    parent = write(root, "parent", [shared, {
        "type": "message", "message": {"role": "toolResult", "toolName": "subagent",
            "details": {"results": [{"usage": {"input": 999999}}]}}
    }])
    write(root / "forks", "child", [shared], parent)
    artifact = root / "subagent-artifacts" / "worker_transcript.jsonl"
    artifact.parent.mkdir()
    artifact.write_text(json.dumps({"recordType": "message", "runId": "run", "usage": {"input": 999999}}))
    assert total(collect(root)) == 1100
    assert set(sessions._load_pi_sessions(signatures(root))) == {"parent"}


def test_recorded_cost_does_not_change_replay_identity(tmp_path):
    root = tmp_path / "pi"
    shared = turn("a", 1000)
    parent = write(root / "z", "parent", [shared])
    other = copy.deepcopy(shared)
    other["message"]["usage"]["cost"]["total"] = 2
    write(root / "a", "child", [other, turn("b", 500)], parent)
    rows = collect(root)
    assert total(rows) == 1650
    assert sum(row["cost"] for row in rows) == 1.5
    store = UsageEntryStore(tmp_path / "usage.db")
    sync(store, root)
    rows = store.query_entries(sources=["pi_agent"])
    assert sum(row["cost"] for row in rows) == 1.5
    raw = sessions._load_pi_sessions(signatures(root))
    assert sum(t["cost"] for session in raw.values() for t in session["turns"]) == 1.5


def test_missing_parents_with_underscore_custom_ids_stay_separate(tmp_path):
    root = tmp_path / "pi"
    shared = turn("a", 1000)
    write(root, "child_a", [shared], "/missing/2026-09-14T00-00-00-000Z_alpha_custom.jsonl")
    write(root, "child_b", [shared], "/missing/2026-09-14T00-00-00-000Z_beta_custom.jsonl")
    assert total(collect(root)) == 2200
    store = UsageEntryStore(tmp_path / "usage.db")
    sync(store, root)
    assert total(store.query_entries(sources=["pi_agent"])) == 2200
    raw = sessions._load_pi_sessions(signatures(root))
    assert set(raw) == {"child_a", "child_b"}


@pytest.mark.parametrize("delete_root", [False, True])
def test_durable_ancestry_survives_deleted_intermediate_and_restart(tmp_path, monkeypatch, delete_root):
    monkeypatch.setenv("TOKDASH_USAGE_DB_DURABLE", "1")
    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")
    root = tmp_path / "pi"
    a, b, c = turn("a", 1000), turn("b", 500), turn("c", 100)
    parent = write(root / "a", "parent", [a])
    middle = write(root / "b", "middle", [a, b], parent)
    write(root / "c", "child", [a, b, c], middle)
    db = tmp_path / "usage.db"
    sync(UsageEntryStore(db), root)
    middle.unlink()
    if delete_root:
        parent.unlink()
    from tokdash.sources import pi_forks
    pi_forks._header.cache_clear()
    store = UsageEntryStore(db)
    sync(store, root)
    assert total(store.query_entries(sources=["pi_agent"])) == 1760
    assert not sync(store, root)
    monkeypatch.setattr(sessions, "UsageEntryStore", lambda: store)
    raw = sessions._pi_sessions()
    assert sum(t["tokens"] for session in raw.values() for t in session["turns"]) == 1760


def test_branch_relinks_copied_message_after_removing_label(tmp_path):
    # Pi createBranchedSession filters LabelEntry records and rebuilds the
    # parentId chain. The assistant id, timestamp and usage remain unchanged.
    root = tmp_path / "pi"
    shared = turn("a", 1000)
    shared["parentId"] = "label-entry"
    parent = write(root, "parent", [
        {"type": "label", "id": "label-entry", "parentId": None,
         "targetId": "earlier-entry", "label": "checkpoint"},
        shared,
    ])
    copied = copy.deepcopy(shared)
    copied["parentId"] = None
    write(root / "forks", "child", [copied, turn("b", 500)], parent)
    assert total(collect(root)) == 1650
    store = UsageEntryStore(tmp_path / "usage.db")
    sync(store, root)
    assert total(store.query_entries(sources=["pi_agent"])) == 1650
    raw = sessions._load_pi_sessions(signatures(root))
    assert {sid: sum(t["tokens"] for t in s["turns"]) for sid, s in raw.items()} == {
        "parent": 1100, "child": 550,
    }


def test_production_sync_context_and_parser_migration(tmp_path, monkeypatch):
    root = tmp_path / "pi"
    shared = turn("a", 1000)
    parent = write(root, "parent", [shared])
    middle = write(root / "forks", "middle", [shared, turn("b", 500)], parent)
    write(root / "forks" / "forks", "child", [shared, turn("b", 500), turn("c", 100)], middle)
    store = UsageEntryStore(tmp_path / "usage.db")
    monkeypatch.setattr(compute, "UsageEntryStore", lambda: store)
    pricing = PricingDatabase()
    parser = PiAgentParser(pricing)
    parser._file_signatures = lambda: signatures(root)
    tracker = SimpleNamespace(parsers={"pi_agent": parser}, pricing_db=pricing)
    # Seed v1 rows with inflated counts; no log signature changes during upgrade.
    def old_parse(sig):
        rows = _collect_parser_file(parser, sig)
        return [{**row, "entry_id": sig[0] + str(i)} for i, row in enumerate(rows)]
    old_signature = {**parser.persistent_parser_signature(), "version": 1}
    store.sync_files("pi_agent", signatures(root), parser=old_signature, parse_file_entries=old_parse)
    assert total(store.query_entries(sources=["pi_agent"])) == 4510
    compute._sync_usage_store(tracker)
    assert total(store.query_entries(sources=["pi_agent"])) == 1760
    assert total(parser._parse_all()) == 1760  # Temporary single-file context was restored.
    monkeypatch.setattr(parser, "_parse_all", lambda: pytest.fail("warm cache reparsed logs"))
    compute._sync_usage_store(tracker)
    assert total(store.query_entries(sources=["pi_agent"])) == 1760


@pytest.mark.parametrize("durable", ["0", "1"])
def test_missing_parent_across_restart(tmp_path, monkeypatch, durable):
    monkeypatch.setenv("TOKDASH_USAGE_DB_DURABLE", durable)
    root = tmp_path / "pi"
    shared = turn("a", 1000)
    parent = write(root, "parent", [shared])
    write(root / "forks", "child", [shared, turn("b", 500)], parent)
    write(root / "forks", "sibling", [shared, turn("c", 100)], parent)
    db_path = tmp_path / "usage.db"
    sync(UsageEntryStore(db_path), root)
    parent.unlink()
    from tokdash.sources import pi_forks
    pi_forks._header.cache_clear()
    store = UsageEntryStore(db_path)
    sync(store, root)
    assert total(store.query_entries(sources=["pi_agent"])) == 1760


def test_relative_parent_path_and_cycle(tmp_path):
    root = tmp_path / "pi"
    shared = turn("a", 1000)
    parent = write(root, "parent", [shared])
    write(root / "forks", "child", [shared, turn("b", 500)], "../" + parent.name)
    assert total(collect(root)) == 1650
    # Corrupt cycles never justify dropping usage from different sessions.
    write(root / "cycle", "x", [shared], "2026-09-14_y.jsonl")
    write(root / "cycle", "y", [shared], "2026-09-14_x.jsonl")
    assert total(collect(root)) == 3850


def test_unkeyed_events_are_preserved(tmp_path):
    root = tmp_path / "pi"
    shared = turn("a", 1000)
    shared.pop("id")
    parent = write(root, "parent", [shared])
    write(root / "forks", "child", [shared], parent)
    assert total(collect(root)) == 2200
    assert set(sessions._load_pi_sessions(signatures(root))) == {"parent", "child"}
