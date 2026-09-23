"""Tests for Roo Code as a session source (sessions.py).

Roo stores one task as one directory: the tokens in ui_messages.json, the
model in the sibling api_conversation_history.json, and the title and
workspace in history_item.json. Sessions and Overview therefore have to be fed
by the SAME producer (roo_task_rows), or the panel labels every turn "unknown"
while the dashboard prices a real model. The parity test below asserts that
per turn, not just per total.

Seeds come from test_roo_code_parser.py so both files describe one 3.54.0
store. All prompt text is placeholder prose.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_roo_code_parser import T0, _write_task, env_user, req, roo_home

from tokdash import sessions
from tokdash.pricing import PricingDatabase
from tokdash.sessions import (
    SESSION_TOOLS,
    TOOL_LABELS,
    _roo_code_sessions,
    get_session_detail,
    get_sessions_data,
    reload_pricing_db,
)
from tokdash.sources.coding_tools import (
    BaseParser,
    RooCodeParser,
    _roo_model_cache,
    _sig_cache,
)

RATES = {"model-a": {"input": 2.0, "output": 4.0, "cache_read": 0.2, "cache_write": 2.0},
         "model-b": {"input": 1.0, "output": 1.0, "cache_read": 0.1, "cache_write": 1.0}}

MODEL_A = "model-a"
MODEL_B = "model-b"

TASK = "task-one"
CHILD = "task-child"


@pytest.fixture(autouse=True)
def _clean_caches():
    _sig_cache.clear()
    _roo_model_cache.clear()
    BaseParser._entry_cache.clear()
    sessions._load_roo_code_sessions.cache_clear()
    reload_pricing_db()
    yield
    _sig_cache.clear()
    _roo_model_cache.clear()
    BaseParser._entry_cache.clear()
    sessions._load_roo_code_sessions.cache_clear()
    reload_pricing_db()


def _setup(monkeypatch, tmp_path, storage):
    """Point the Roo corpus at one seeded storage root with known rates."""
    monkeypatch.setenv("TOKDASH_ROO_STORAGE_DIR", str(storage))
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path / "data-home"))
    override = PricingDatabase().override_path()
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(
        json.dumps({"version": "test", "aliases": {}, "models": RATES}),
        encoding="utf-8",
    )
    reload_pricing_db()


def _task(task_id, *, requests, model=MODEL_A, prompt="Count the files",
          history=None, workspace="/tmp/roo-work", parent=None):
    """Messages, the conversation record that carries the model, and history."""
    messages = [{"type": "say", "say": "task", "ts": T0, "text": prompt}]
    messages += requests
    conversation = [env_user(T0 + 30, model, prompt)]
    if history is None:
        history = {
            "id": task_id,
            "ts": T0,
            "task": f"title of {task_id}",
            "tokensIn": sum(r_payload(x)["tokensIn"] for x in requests),
            "tokensOut": sum(r_payload(x)["tokensOut"] for x in requests),
            "totalCost": 0,
            "workspace": workspace,
            "mode": "act",
        }
    if parent:
        history["parentTaskId"] = parent
    return (task_id, messages, conversation, history)


def r_payload(message):
    return json.loads(message["text"])


def _write(storage, spec):
    task_id, messages, conversation, history = spec
    return _write_task(storage, task_id, messages,
                       conversation=conversation, history_item=history)


def _listing():
    return get_sessions_data("roo_code", "all")


def test_a_task_seen_through_two_root_spellings_is_one_session(monkeypatch, tmp_path, roo_home):
    """The panel double counts the same way Overview does, so pin it too.

    The loader groups by task id and EXTENDS, so a task directory reached under
    two spellings appends the same rows twice: two turns and double the tokens
    for one request. Canonical root dedupe is what keeps this at one.
    """
    real = tmp_path / "real"
    (tmp_path / "sub").mkdir()
    _write(real, _task(TASK, requests=[req(T0 + 40, 100, 5)]))
    _setup(monkeypatch, tmp_path, real)
    monkeypatch.setenv("TOKDASH_ROO_STORAGE_DIR", f"{real},{tmp_path / 'sub' / '..' / 'real'}")
    sessions._load_roo_code_sessions.cache_clear()

    raw = _roo_code_sessions()
    turns = [t for session in raw.values() for t in session["turns"]]
    assert len(raw) == 1
    assert len(turns) == 1, [t["tokens_in"] + t["tokens_out"] for t in turns]
    assert turns[0]["tokens_in"] + turns[0]["tokens_out"] == 105

# --- registry ----------------------------------------------------------------


def test_roo_code_is_a_session_tool():
    assert "roo_code" in SESSION_TOOLS


def test_label_is_roo_code_not_the_title_fallback(monkeypatch, tmp_path, roo_home):
    """Without TOOL_LABELS["roo_code"] this renders "Roo_Code" (key.title())."""
    storage = roo_home / "storage"
    _write(storage, _task(TASK, requests=[req(T0 + 40, 100, 5)]))
    _setup(monkeypatch, tmp_path, storage)
    assert TOOL_LABELS["roo_code"] == "Roo Code"
    assert _listing()["tool_label"] == "Roo Code"
    assert _listing()["tool_label"] != "roo_code".title()


# --- the listing -------------------------------------------------------------


def test_one_session_per_task_dir_one_turn_per_request(monkeypatch, tmp_path, roo_home):
    storage = roo_home / "storage"
    _write(storage, _task(TASK, requests=[req(T0 + 40, 100, 5), req(T0 + 9000, 120, 7)]))
    _write(storage, _task("task-two", requests=[req(T0 + 20000, 60, 3)]))
    _setup(monkeypatch, tmp_path, storage)

    data = _listing()
    assert data["summary"]["session_count"] == 2
    by_id = {row["session_id"]: row for row in data["sessions"]}
    assert by_id[TASK]["token_events"] == 2
    assert by_id["task-two"]["token_events"] == 1


def test_title_and_project_come_from_history_item(monkeypatch, tmp_path, roo_home):
    storage = roo_home / "storage"
    _write(storage, _task(TASK, requests=[req(T0 + 40, 100, 5)],
                          workspace="/home/dev/projects/inventory-api"))
    _setup(monkeypatch, tmp_path, storage)
    row = _listing()["sessions"][0]
    assert row["display_name"] == f"title of {TASK}"
    assert row["project"] == "inventory-api"


def test_a_task_with_no_history_item_is_still_listed_and_named(monkeypatch, tmp_path, roo_home):
    """An unfinished task has no history_item.json; its own prompt names it."""
    storage = roo_home / "storage"
    _write_task(
        storage, TASK,
        [
            {"type": "say", "say": "text", "ts": T0, "text": "Rename the settings table"},
            req(T0 + 40, 100, 5),
        ],
        conversation=[env_user(T0 + 30, MODEL_A)],
    )
    _setup(monkeypatch, tmp_path, storage)
    row = _listing()["sessions"][0]
    assert row["display_name"] == "Rename the settings table"


# --- the shared producer, which is the whole point ---------------------------


def test_parity_with_the_parser_turn_for_turn(monkeypatch, tmp_path, roo_home):
    """Same rows, same models.

    Token sums alone would not catch a loader that read the task file directly:
    that gets every token right and calls every turn "unknown", which moves the
    panel's model column and its per-model billing grouping away from the
    numbers the dashboard is showing beside it.
    """
    storage = roo_home / "storage"
    requests = [req(T0 + 40, 1_000, 20, cache_reads=800, cache_writes=40),
                req(T0 + 9_000, 1_200, 7),
                req(T0 + 18_000, 600, 3, cache_reads=250)]
    _write(storage, _task(TASK, requests=requests))
    _setup(monkeypatch, tmp_path, storage)

    entries = RooCodeParser(PricingDatabase())._parse_all()
    data = _listing()
    turns = [t for s in _roo_code_sessions().values() for t in s["turns"]]

    assert len(entries) == len(turns) == data["sessions"][0]["token_events"] == 3

    parser_rows = sorted(
        (e["timestamp"], e["model"], e["input"] + e["cacheWrite"], e["cacheRead"],
         e["output"], round(e["cost"], 9))
        for e in entries
    )
    session_rows = sorted(
        (t["timestamp_ms"], t["model"], t["tokens_in"], t["tokens_cache"],
         t["tokens_out"], round(t["cost"], 9))
        for t in turns
    )
    assert session_rows == parser_rows

    # And no turn is priced as unknown while Overview knows the model.
    assert {t["model"] for t in turns} == {"model-a"}
    assert "unknown" not in {e["model"] for e in entries}


def test_a_mid_task_model_switch_shows_per_turn(monkeypatch, tmp_path, roo_home):
    storage = roo_home / "storage"
    messages = [{"type": "say", "say": "task", "ts": T0, "text": "try both"}]
    messages += [req(T0 + 40, 100, 5), req(T0 + 30_000, 200, 9)]
    _write_task(
        storage, TASK, messages,
        conversation=[env_user(T0 + 30, MODEL_A, "try both"),
                      env_user(T0 + 30_030, MODEL_B, "use the other one")],
        history_item={"id": TASK, "task": "two models", "workspace": "/tmp/x"},
    )
    _setup(monkeypatch, tmp_path, storage)

    turns = [t for s in _roo_code_sessions().values() for t in s["turns"]]
    assert [t["model"] for t in turns] == [MODEL_A, MODEL_B]
    assert _listing()["sessions"][0]["model"] in (MODEL_A, MODEL_B)


def test_turn_event_keys_are_the_parser_entry_ids(monkeypatch, tmp_path, roo_home):
    storage = roo_home / "storage"
    _write(storage, _task(TASK, requests=[req(T0 + 40, 100, 5), req(T0 + 9_000, 120, 7)]))
    _setup(monkeypatch, tmp_path, storage)
    entries = RooCodeParser(PricingDatabase())._parse_all()
    turns = [t for s in _roo_code_sessions().values() for t in s["turns"]]
    assert {t["_event_key"] for t in turns} == {e["entry_id"] for e in entries}


# --- the one documented difference between the surfaces ----------------------


def test_a_delegated_child_is_billed_but_not_listed(monkeypatch, tmp_path, roo_home):
    """Roo charges for a subtask; it is not a session the user started."""
    storage = roo_home / "storage"
    _write(storage, _task(TASK, requests=[req(T0 + 40, 100, 5)]))
    _write(storage, _task(CHILD, requests=[req(T0 + 5_000, 300, 30)], parent=TASK))
    _setup(monkeypatch, tmp_path, storage)

    assert {row["session_id"] for row in _listing()["sessions"]} == {TASK}
    assert len(RooCodeParser(PricingDatabase())._parse_all()) == 2  # both billed


# --- resumes, windows, and the corpus ----------------------------------------


def test_a_resumed_task_stays_one_session(monkeypatch, tmp_path, roo_home):
    """A resume appends into the same file; it is not a second session."""
    storage = roo_home / "storage"
    _write(storage, _task(TASK, requests=[req(T0 + 40, 100, 5)]))
    _setup(monkeypatch, tmp_path, storage)
    assert _listing()["summary"]["session_count"] == 1

    path = storage / "tasks" / TASK / "ui_messages.json"
    messages = json.loads(path.read_text(encoding="utf-8"))
    messages += [
        {"type": "ask", "ask": "resume_task", "ts": T0 + 50_000, "text": ""},
        req(T0 + 60_000, 700, 12),
    ]
    path.write_text(json.dumps(messages), encoding="utf-8")

    # Roo's signature scan is one TTL cache shared by Overview and Sessions, so
    # an append landing inside that window is deliberately not seen yet. A real
    # resume is minutes later; this one is microseconds, so step over the TTL
    # the way the passage of time would.
    _sig_cache.clear()
    data = _listing()
    assert data["summary"]["session_count"] == 1
    assert data["sessions"][0]["token_events"] == 2


def test_a_title_edit_alone_reaches_the_panel(monkeypatch, tmp_path, roo_home):
    """history_item.json rides in the cache key even when no token moved."""
    storage = roo_home / "storage"
    _write(storage, _task(TASK, requests=[req(T0 + 40, 100, 5)]))
    _setup(monkeypatch, tmp_path, storage)
    assert _listing()["sessions"][0]["display_name"] == f"title of {TASK}"

    history = storage / "tasks" / TASK / "history_item.json"
    doc = json.loads(history.read_text(encoding="utf-8"))
    # The invalidation signature is (mtime_ns, size) and two writes in the same
    # clock tick share an mtime_ns, so a test edit has to move the size too.
    # Real edits to a title change the text; an equal-length one is a case no
    # signature-based reader in this codebase can see.
    doc["task"] = "renamed by the user to something clearly longer"
    history.write_text(json.dumps(doc), encoding="utf-8")
    assert _listing()["sessions"][0]["display_name"] == (
        "renamed by the user to something clearly longer"
    )


def test_a_late_conversation_record_reprices_the_model(monkeypatch, tmp_path, roo_home):
    """The model's own file must invalidate the view; it is not signed by the
    parser, and it is the one file Roo rewrites on its own schedule."""
    storage = roo_home / "storage"
    _write(storage, _task(TASK, requests=[req(T0 + 40, 100, 5)], model=MODEL_A))
    _setup(monkeypatch, tmp_path, storage)
    turns = [t for s in _roo_code_sessions().values() for t in s["turns"]]
    assert turns[0]["model"] == MODEL_A

    conversation = storage / "tasks" / TASK / "api_conversation_history.json"
    # A different byte length, because (mtime_ns, size) is the signature and a
    # rewrite inside one clock tick keeps the mtime. Roo appends to this file,
    # so a late record always moves it.
    conversation.write_text(
        json.dumps([env_user(T0 + 30, MODEL_B, "Count the files in this repository")]),
        encoding="utf-8",
    )
    turns = [t for s in _roo_code_sessions().values() for t in s["turns"]]
    assert turns[0]["model"] == MODEL_B


def test_a_deleted_task_removes_its_session(monkeypatch, tmp_path, roo_home):
    """The corpus IS the set of task files, so a removed task leaves the panel."""
    import shutil
    storage = roo_home / "storage"
    _write(storage, _task(TASK, requests=[req(T0 + 40, 100, 5)]))
    _write(storage, _task("task-two", requests=[req(T0 + 50, 100, 5)]))
    _setup(monkeypatch, tmp_path, storage)
    assert _listing()["summary"]["session_count"] == 2

    shutil.rmtree(storage / "tasks" / TASK)
    assert {row["session_id"] for row in _listing()["sessions"]} == {"task-two"}


def test_the_window_filters_turns_within_a_task(monkeypatch, tmp_path, roo_home):
    """The loader is unwindowed (the corpus is a file set, not a query), so the
    window is applied per turn by _summarize_session. A task that straddles two
    days must show only the in-range turn, and must not vanish from the other
    day."""
    storage = roo_home / "storage"
    day_two = T0 + 2 * 86_400_000
    _write(storage, _task(TASK, requests=[req(T0 + 40, 100, 5), req(day_two, 200, 9)]))
    _setup(monkeypatch, tmp_path, storage)

    day_one = sessions._ms_to_iso(T0)[:10]
    day_two_label = sessions._ms_to_iso(day_two)[:10]

    first = get_sessions_data("roo_code", "range", date_from=day_one, date_to=day_one)
    assert first["summary"]["session_count"] == 1
    assert first["sessions"][0]["token_events"] == 1
    assert first["sessions"][0]["tokens"] == 105

    second = get_sessions_data("roo_code", "range",
                               date_from=day_two_label, date_to=day_two_label)
    assert second["sessions"][0]["token_events"] == 1
    assert second["sessions"][0]["tokens"] == 209

    both = get_sessions_data("roo_code", "range",
                             date_from=day_one, date_to=day_two_label)
    assert both["sessions"][0]["token_events"] == 2


def test_an_empty_storage_root_is_an_empty_success(monkeypatch, tmp_path, roo_home):
    storage = roo_home / "storage"
    (storage / "tasks").mkdir(parents=True, exist_ok=True)
    _setup(monkeypatch, tmp_path, storage)
    assert _roo_code_sessions() == {}
    assert _listing()["sessions"] == []


def test_no_roo_anywhere_is_an_empty_success(monkeypatch, tmp_path, roo_home):
    _setup(monkeypatch, tmp_path, roo_home / "nothing-here")
    assert _roo_code_sessions() == {}


# --- detail and frontend -----------------------------------------------------


def test_session_detail_lists_the_turns(monkeypatch, tmp_path, roo_home):
    storage = roo_home / "storage"
    _write(storage, _task(TASK, requests=[req(T0 + 40, 100, 5), req(T0 + 9_000, 120, 7)]))
    _setup(monkeypatch, tmp_path, storage)
    detail = get_session_detail("roo_code", TASK)
    assert [t["turn_index"] for t in detail["turns"]] == [1, 2]
    assert detail["session"]["tool"] == "roo_code"
    assert detail["session"]["display_name"] == f"title of {TASK}"
    assert "_event_key" not in detail["turns"][0]
    with pytest.raises(FileNotFoundError):
        get_session_detail("roo_code", "not-a-task")


def test_frontend_session_registry_includes_roo_code():
    """The ids are built from the RAW tool key, so roo_code and not rooCode.

    updateSessionPanel does document.getElementById(`${prefix}SessionsTable`)
    and then `if (!tbody) return;` with prefix = "roo_code". A camelCase id
    therefore produces exactly the empty panel with no error that these
    assertions exist to catch. Only the data-i18n heading key is camelCase.
    """
    index = Path(sessions.__file__).parent / "static" / "index.html"
    source = index.read_text(encoding="utf-8")
    assert "'qoder_cli', 'goose', 'roo_code']" in source
    assert "goose: null, roo_code: null, combined: null" in source
    assert 'updateSessionPanel("roo_code", lastSessionsResponses.roo_code);' in source
    assert 'initSortHeaders("roo_code", renderSessionsTab);' in source
    assert "roo_code: { ...DEFAULT_SORT }," in source
    assert "roo_code: 'Roo Code'," in source
    # heading key: a label, camelCase
    assert "rooCodeSessions: 'Roo Code Sessions'," in source
    assert "rooCodeSessions: 'Roo Code 会话'," in source
    assert "rooCodeSessions: 'Roo Code セッション'," in source
    assert "rooCodeSessions: 'Roo Code 세션'," in source
    assert "rooCodeSessions: 'Sesiones de Roo Code'," in source
    assert "rooCodeSessions: 'Sessões do Roo Code'," in source
    # ids: raw key
    assert 'id="roo_codeSessionsTable"' in source
    assert 'data-panel-details="roo_code"' in source
    assert 'data-panel="roo_code"' in source
    assert 'id="roo_codePanelCount"' in source
    assert 'id="roo_codeLatestSession"' in source
    assert 'id="roo_codeActiveAgent"' in source
    assert "rooCodeSessionsTable" not in source
    assert 'id="rooCodeSessionsTable"' not in source
    assert (index.parent / "icons" / "agents" / "roo_code.svg").is_file()
