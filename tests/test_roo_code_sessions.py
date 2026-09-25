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
import os
import shutil
from contextlib import contextmanager
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


def test_the_panel_reuses_the_model_maps_overview_built(monkeypatch, tmp_path, roo_home):
    """One conversation-file read per task, not one per surface.

    The model map keys on api_conversation_history.json, a file neither surface
    reads for tokens: Overview reads it during a sync, and the panel reads the
    SAME file for the SAME task moments later. Sharing is the cache's whole
    purpose, and a bound under the task count means it never happens. Measured
    on a 300-task corpus on a Windows-mounted drive, the previous 32-entry bound
    re-read all 300 files on the panel's first pass after a sync (2.2 s for the
    all window, 3.5 s for a week window, against 1.2 s and 2.2 s warm), and the
    map held a dozen entries at the end of every pass.
    """
    storage = tmp_path / "storage"
    ids = [f"task-{i:03d}" for i in range(40)]   # more than the old bound
    for i, task_id in enumerate(ids):
        _write(storage, _task(task_id, requests=[req(T0 + 40 + i, 100 + i, 5)]))
    _setup(monkeypatch, tmp_path, storage)

    entries = RooCodeParser(PricingDatabase()).collect(None, None)
    assert len(entries) >= len(ids)
    built = len(_roo_model_cache)
    assert built == len(ids), (
        f"Overview left {built} of {len(ids)} model maps behind; a bound under "
        "the task count shares nothing with the panel")

    reads: list[str] = []
    monkeypatch.setattr(
        "tokdash.sources.coding_tools._roo_conversation_tags",
        lambda path: reads.append(str(path)) or [],
    )
    # A cold panel, one TTL later: the scans run again, the maps do not.
    _sig_cache.clear()
    sessions._parse_roo_task_file_for_sessions.cache_clear()
    sessions._read_roo_first_prompt.cache_clear()
    sessions._load_roo_code_sessions.cache_clear()

    raw = _roo_code_sessions()

    assert len(raw) == len(ids)
    assert reads == [], (
        f"the panel re-read {len(reads)} conversation files Overview had already "
        "parsed; the model map must outlive one surface's caches")

    # And the models still resolve, so the reuse is real and not a silent miss.
    models = {t["model"] for s in raw.values() for t in s["turns"]}
    assert models == {MODEL_A}, models


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

def test_a_copied_task_directory_does_not_double_the_panel(monkeypatch, tmp_path, roo_home):
    """Two directories, one task id: the loader may not extend twice.

    Roo copies a task directory when a workspace moves, and the copy keeps its
    id and its request stamps. Overview folds the duplicate entry ids; the
    loader groups by task id and EXTENDS, so without the same fold the panel
    shows four turns and double the tokens for a two-request task.
    """
    first = tmp_path / "storage-a"
    second = tmp_path / "storage-b"
    spec = _task(TASK, requests=[req(T0 + 40, 100, 5), req(T0 + 90, 60, 4)])
    _write(first, spec)
    _write(second, spec)
    _setup(monkeypatch, tmp_path, first)
    monkeypatch.setenv("TOKDASH_ROO_STORAGE_DIR", f"{first},{second}")
    sessions._load_roo_code_sessions.cache_clear()

    raw = _roo_code_sessions()
    turns = [t for session in raw.values() for t in session["turns"]]
    assert len(raw) == 1
    assert len(turns) == 2, f"two turns, not four: {turns}"
    assert sum(t["tokens_in"] + t["tokens_out"] for t in turns) == 169

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
    # One signature scan for both surfaces, so the panel moves on the same TTL
    # clock Overview does (see test_a_resume_is_not_a_second_session). Stepping
    # over it is what a real edit minutes after the task would already have done.
    _sig_cache.clear()
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
    _sig_cache.clear()  # the shared signature TTL, as above
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
    # A deletion now leaves the panel on the same scan as the Overview, so the
    # two surfaces go stale together and, more to the point, come back together.
    # Before they shared one scan the panel could drop a task while the dashboard
    # still billed it.
    _sig_cache.clear()
    assert {row["session_id"] for row in _listing()["sessions"]} == {"task-two"}


@contextmanager
def _count_corpus_calls(root: str):
    """Count os.scandir / os.stat calls whose path is under *root*.

    Counting the syscalls is the point: the corpus can sit behind a Windows
    round trip per call, so the number of calls per request is the thing that
    decides whether a panel refresh is 20 ms or two seconds.
    """
    counts = {"scandir": 0, "stat": 0}
    real_scandir, real_stat = os.scandir, os.stat

    def scandir(path, *args, **kwargs):
        if str(path).startswith(root):
            counts["scandir"] += 1
        return real_scandir(path, *args, **kwargs)

    def stat(path, *args, **kwargs):
        if str(path).startswith(root):
            counts["stat"] += 1
        return real_stat(path, *args, **kwargs)

    os.scandir, os.stat = scandir, stat
    try:
        yield counts
    finally:
        os.scandir, os.stat = real_scandir, real_stat


def test_a_warm_panel_refresh_walks_the_corpus_no_more(monkeypatch, tmp_path, roo_home):
    """One readdir per task on a cold pass; nothing at all on a warm one.

    Roo's two sidecars have to invalidate the panel's cache even when no token
    moved, and reading that literally cost a stat per sidecar PER REQUEST --
    the signatures the parser uses sit behind a TTL cache, the sidecars did not.
    On a /mnt/c corpus that is two Windows round trips per task per refresh
    (measured ~1.2 ms each), so a 600-task history spent about a second of
    syscalls on every Sessions request and every warmer pass, for a dashboard
    that polls. The stamps now come out of the same scan, which makes this
    assertion, not a comment.
    """
    storage = roo_home / "storage"
    for i in range(6):
        _write(storage, _task(f"task-{i}", requests=[req(T0 + 40 + i * 1000, 100, 5)]))
    _setup(monkeypatch, tmp_path, storage)

    with _count_corpus_calls(str(storage)) as cold:
        assert _listing()["summary"]["session_count"] == 6
    # One listing of tasks/ plus one readdir per task dir. That per-task readdir
    # is both the existence proof and the source of the stamp, which is the
    # point: the signature used to cost a stat per file on top of a stat per
    # directory, and the model map used to stat its own sibling once more per
    # task on top of THAT. The only stats left belong to root discovery -- one
    # existence proof and two canonicalisations -- so the budget is a constant,
    # and a corpus-sized pass would read 3 + n here.
    assert cold["scandir"] == 7, cold
    assert cold["stat"] <= 3, cold

    with _count_corpus_calls(str(storage)) as warm:
        assert _listing()["summary"]["session_count"] == 6
    assert warm == {"scandir": 0, "stat": 0}, warm


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


# ---------------------------------------------------------------------------
# The per-file memo: one entry per task, sized to the corpus
#
# A Roo corpus grows by one task directory forever and Roo never prunes one, so
# a fixed bound cannot be right at both ends. 1,024 was picked against a 600-task
# corpus; at 3,000 tasks it recorded zero hits across a whole rebuild, because
# lru_cache keys on (path, mtime, size) and a changed file ADDS an entry rather
# than replacing its own, so the entries a rebuild needed had been evicted by the
# entries it had already read. Measured on a Windows-mounted drive: a rebuild
# that touched ONE task re-read all 3,000 files, 6.98 s.
# ---------------------------------------------------------------------------


def _memo_cold_reads(monkeypatch, tmp_path, storage, n_tasks):
    """Cold-build the panel over a corpus, then report the re-reads of a rebuild."""
    _setup(monkeypatch, tmp_path, storage)
    ids = [f"task-{i:04d}" for i in range(n_tasks)]
    for i, task_id in enumerate(ids):
        _write(storage, _task(task_id, requests=[req(T0 + 40 + i, 100 + i, 5)]))
    reads: list[str] = []
    real = sessions._parse_roo_task_file_for_sessions._func

    def counting(path_str, *rest):
        reads.append(path_str)
        return real(path_str, *rest)

    monkeypatch.setattr(
        sessions._parse_roo_task_file_for_sessions, "_func", counting)
    return ids, reads


def test_one_changed_task_re_reads_one_task(monkeypatch, tmp_path, roo_home):
    """The corpus-sized memo is what makes a rebuild proportional to the change."""
    storage = tmp_path / "storage"
    ids, reads = _memo_cold_reads(monkeypatch, tmp_path, storage, 40)
    assert len(_listing()["sessions"]) == len(ids)
    assert len(reads) == len(ids), "the first pass did not read every task"

    del reads[:]
    target = storage / "tasks" / ids[7] / "ui_messages.json"
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    _sig_cache.clear()
    sessions._load_roo_code_sessions.cache_clear()
    assert len(_listing()["sessions"]) == len(ids)

    re_read = [path for path in reads if str(target) == path]
    assert len(re_read) == 1, (
        f"a one-task change re-read {len(reads)} of {len(ids)} task files; the "
        "memo must replace a changed file's own entry, not evict its neighbours")


def test_the_memo_holds_one_entry_per_task_not_per_version():
    """A task edited a hundred times is one entry, not a hundred.

    That is the whole difference from the lru_cache this replaced, and it is what
    lets the bound be the task count: under the old key, churn alone pushed the
    working set out of its own cache.
    """
    memo = sessions._parse_roo_task_file_for_sessions
    memo.cache_clear()
    calls: list[str] = []

    def reader(path_str, _mtime_ns, _size, _pricing_sig, *extra):
        calls.append(path_str)
        return [{"n": len(calls)}]

    original = memo._func
    memo._func = reader
    try:
        for version in range(50):
            memo("/tmp/one/ui_messages.json", version, 10, ())
        assert len(calls) == 50, "each version should re-read once"
        assert len(memo._data) == 1, (
            f"50 versions of one file left {len(memo._data)} entries")
        assert memo.cache_info().currsize == 1
    finally:
        memo._func = original
        memo.cache_clear()


def test_the_memo_follows_the_corpus_and_forgets_the_tasks_that_left(
    monkeypatch, tmp_path, roo_home
):
    """A bound under the task count shares nothing; an unbounded one never shrinks."""
    storage = tmp_path / "storage"
    ids, _reads = _memo_cold_reads(monkeypatch, tmp_path, storage, 30)
    assert len(_listing()["sessions"]) == len(ids)
    memo = sessions._parse_roo_task_file_for_sessions
    # Three files per task are live, and the bound covers them all.
    assert memo.cache_info().maxsize >= len(ids)
    assert memo.cache_info().currsize == len(ids)

    # Delete a task and rebuild: its entry must go with it, or the memo is a
    # second corpus that only ever grows.
    shutil.rmtree(storage / "tasks" / ids[3])
    _sig_cache.clear()
    sessions._load_roo_code_sessions.cache_clear()
    assert len(_listing()["sessions"]) == len(ids) - 1
    gone = str(storage / "tasks" / ids[3] / "ui_messages.json")
    assert gone not in memo._data, "a deleted task is still held by the memo"


def test_the_memo_is_bounded_by_requests_as_well_as_by_files():
    """20,000 tasks of four requests and 2,000 of forty are not the same heap."""
    memo = sessions._parse_roo_task_file_for_sessions
    memo.cache_clear()
    width = 200
    original = memo._func
    memo._func = lambda path_str, mtime_ns, _size, _pricing_sig, *extra: [
        {"n": i} for i in range(width)
    ]
    try:
        memo.note_corpus({f"/tmp/{i}" for i in range(5_000)})
        for i in range(5_000):
            memo(f"/tmp/{i}", i, 1, ())
        rows = sum(len(entry[1]) for entry in memo._data.values())
        assert rows <= sessions._ROO_FILE_CACHE_ROW_BUDGET, (
            f"the memo retained {rows} rows against a budget of "
            f"{sessions._ROO_FILE_CACHE_ROW_BUDGET}")
        # And it kept the most RECENT tasks, which is the reuse worth keeping.
        assert "/tmp/4999" in memo._data
    finally:
        memo._func = original
        memo.cache_clear()
