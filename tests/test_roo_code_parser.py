"""Tests for RooCodeParser: one ui_messages.json per task, only completed
api_req_started rows billed, the model paired from the sibling conversation
file, and the totals that must never be read.

Every seed below is an inline literal shaped from the captured 3.54.0 corpus
(four real tasks: a resumed task, an orchestrator parent, a finished child and
an aborted child). The prompt text is a placeholder: the capture carries real
user prompts, and committed fixtures are not where those belong. Scratchpad is
gitignored and no test may reach into it.
"""
import json
from pathlib import Path

import pytest

from tokdash import clientpaths
from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import (
    BaseParser,
    RooCodeParser,
    _roo_model_cache,
    _roo_model_for,
    _sig_cache,
    parse_roo_task_file,
    roo_task_rows,
)
from tokdash.usage_store import UsageFileVanished

T0 = 1_789_932_971_243  # epoch ms, the unit Roo stamps


def req(ts, tokens_in, tokens_out, cache_reads=0, cache_writes=0, cost=0):
    """One completed api_req_started message, as Roo writes it."""
    return {
        "type": "say",
        "say": "api_req_started",
        "ts": ts,
        "text": json.dumps({
            "apiProtocol": "openai",
            "tokensIn": tokens_in,
            "tokensOut": tokens_out,
            "cacheWrites": cache_writes,
            "cacheReads": cache_reads,
            "cost": cost,
        }),
    }


def env_user(ts, model, prompt="do the thing"):
    """A user conversation record carrying Roo's own <model> header."""
    return {
        "role": "user",
        "ts": ts,
        "content": [
            {"type": "text", "text": prompt},
            {
                "type": "text",
                "text": (
                    "<environment_details>\n# VS Code Version\n1.104.3\n"
                    f"<model>{model}</model>\n<mode>act</mode>\n"
                    "</environment_details>"
                ),
            },
        ],
    }


def _write_task(storage, task_id, messages, conversation=None, history_item=None):
    task = storage / "tasks" / task_id
    task.mkdir(parents=True, exist_ok=True)
    (task / "ui_messages.json").write_text(
        json.dumps(messages), encoding="utf-8"
    )
    if conversation is not None:
        (task / "api_conversation_history.json").write_text(
            json.dumps(conversation), encoding="utf-8"
        )
    if history_item is not None:
        (task / "history_item.json").write_text(
            json.dumps(history_item), encoding="utf-8"
        )
    return task


@pytest.fixture
def roo_home(monkeypatch, tmp_path):
    """A home with no VS Code tree anywhere, plus one seeded storage root.

    The root list is a union of real candidates, so every base is pointed at
    this tmp tree: otherwise a parser built on a developer's machine could pick
    up a real install and an entry count would mean two different things.
    """
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setattr(clientpaths, "_wsl_windows_root", lambda: tmp_path / "mnt-c")
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    _roo_model_cache.clear()
    return home


def _parser(monkeypatch, storage):
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    _roo_model_cache.clear()
    monkeypatch.setenv("TOKDASH_ROO_STORAGE_DIR", str(storage))
    return RooCodeParser(PricingDatabase())


def _entries(parser):
    return parser._parse_all()


# --- what counts as a billable request ---------------------------------------


def test_roo_bills_completed_api_req_rows_only(monkeypatch, roo_home, tmp_path):
    storage = tmp_path / "storage"
    _write_task(
        storage,
        "task-a",
        [
            {"type": "say", "say": "text", "ts": T0, "text": "do the thing"},
            req(T0 + 15, 8780, 140),
            {"type": "ask", "ask": "tool", "ts": T0 + 25_609, "text": "{}", "partial": False},
            req(T0 + 27_264, 9041, 178),
            {"type": "say", "say": "completion_result", "ts": T0 + 35_000, "text": "done"},
            {"type": "ask", "ask": "resume_task", "ts": T0 + 366_707},
            {"type": "say", "say": "user_feedback", "ts": T0 + 366_815, "text": "again"},
            req(T0 + 366_826, 9378, 64),
        ],
        [env_user(T0 + 24, "gpt-5.2")],
    )
    entries = _entries(_parser(monkeypatch, storage))

    assert [e["entry_id"] for e in entries] == [
        f"roo_code:task-a:{T0 + 15}",
        f"roo_code:task-a:{T0 + 27_264}",
        f"roo_code:task-a:{T0 + 366_826}",
    ]
    # ts is already epoch ms, so it reaches the entry unchanged.
    assert entries[0]["timestamp"] == T0 + 15
    assert (entries[0]["input"], entries[0]["output"]) == (8780, 140)
    assert entries[0]["source"] == "roo_code"
    assert entries[0]["provider"] == "openai"
    assert entries[0]["reasoning"] == 0


def test_roo_skips_the_preflight_marker_of_an_unfinished_request(monkeypatch, roo_home, tmp_path):
    """The aborted child's lone row is still the bare {"apiProtocol":...}."""
    storage = tmp_path / "storage"
    _write_task(
        storage,
        "task-aborted",
        [
            {"type": "say", "say": "text", "ts": T0, "text": "do the thing"},
            {
                "type": "say",
                "say": "api_req_started",
                "ts": T0 + 15,
                "text": json.dumps({"apiProtocol": "openai"}),
            },
        ],
        [env_user(T0 + 50, "gpt-5.2")],
    )

    assert _entries(_parser(monkeypatch, storage)) == []


def test_roo_skips_zero_partial_retry_and_rewind_rows(monkeypatch, roo_home, tmp_path):
    storage = tmp_path / "storage"
    _write_task(
        storage,
        "task-b",
        [
            req(T0, 0, 0),  # a request that failed before billing
            dict(req(T0 + 1, 500, 50), partial=True),  # still streaming
            {
                "type": "say",
                "say": "api_req_retry_delayed",
                "ts": T0 + 2,
                # A retry rewrites the ORIGINAL api_req_started row, so this
                # marker must never become a second charge.
                "text": json.dumps({"tokensIn": 400, "tokensOut": 10}),
            },
            {
                "type": "say",
                "say": "subtask_result",
                "ts": T0 + 3,
                "text": json.dumps({"tokensIn": 300, "tokensOut": 20}),
            },
            {
                "type": "say",
                "say": "api_req_deleted",
                "ts": T0 + 4,
                # Rewind marker: the rows it describes are already truncated out
                # of the file, so subtracting it would double-count them.
                "text": json.dumps({"tokensIn": 200, "tokensOut": 10, "cost": 0.5}),
            },
            req(T0 + 5, 900, 60),
        ],
        [env_user(T0 + 20, "gpt-5.2")],
    )
    entries = _entries(_parser(monkeypatch, storage))

    assert [(e["input"], e["output"]) for e in entries] == [(900, 60)]


def test_roo_splits_cache_inclusive_tokens_in(monkeypatch, roo_home, tmp_path):
    """tokensIn already contains both cache slices; each part bills once."""
    storage = tmp_path / "storage"
    _write_task(
        storage,
        "task-c",
        [req(T0, 1000, 200, cache_reads=600, cache_writes=100)],
        [env_user(T0 + 20, "claude-sonnet-4-5")],
    )
    entry = _entries(_parser(monkeypatch, storage))[0]

    assert (entry["input"], entry["cacheRead"], entry["cacheWrite"], entry["output"]) == (
        300,
        600,
        100,
        200,
    )
    # Displayed and billed totals stay equal to Roo's gross tokensIn + tokensOut.
    assert entry["input"] + entry["cacheRead"] + entry["cacheWrite"] + entry["output"] == 1200
    assert entry["cost"] == PricingDatabase().get_cost(
        "claude-sonnet-4-5", 300, 200, 600, 100
    )
    assert entry["_billing"]["cache_read"] == 600


def test_roo_priced_cost_is_tokdash_not_roos(monkeypatch, roo_home, tmp_path):
    """The payload's cost field is ignored; the pricing DB decides."""
    storage = tmp_path / "storage"
    _write_task(
        storage,
        "task-d",
        [req(T0, 1000, 50, cost=9.99)],
        [env_user(T0 + 20, "gpt-5.2")],
    )
    entry = _entries(_parser(monkeypatch, storage))[0]

    assert entry["cost"] == PricingDatabase().get_cost("gpt-5.2", 1000, 50, 0, 0)
    assert entry["cost"] != 9.99


# --- the model, which is never a field ---------------------------------------


def test_roo_recovers_the_model_from_its_own_header(monkeypatch, roo_home, tmp_path):
    storage = tmp_path / "storage"
    _write_task(
        storage,
        "task-e",
        [req(T0, 100, 10), req(T0 + 4_000, 120, 12)],
        [env_user(T0 + 24, "qwen3.8-flash-next"), env_user(T0 + 4_007, "qwen3.8-flash-next")],
    )
    entries = _entries(_parser(monkeypatch, storage))

    assert [e["model"] for e in entries] == ["qwen3.8-flash-next"] * 2
    assert all(e["cost"] > 0 or e["model"] == "unknown" for e in entries)


def test_roo_first_request_of_a_task_is_not_unknown(monkeypatch, roo_home, tmp_path):
    """The rule the fixture forced.

    Roo writes the api_req_started marker before the conversation record that
    carries the tag, so "newest tag at or before the row" leaves the request
    that opens a task unresolved: 4 of the 12 captured rows, every one of them
    24-50 ms from its own tag.
    """
    storage = tmp_path / "storage"
    _write_task(
        storage,
        "task-first",
        [req(T0, 100, 10)],
        [env_user(T0 + 24, "gpt-5.2")],
    )
    entry = _entries(_parser(monkeypatch, storage))[0]

    assert entry["model"] == "gpt-5.2"


def test_roo_uses_the_model_in_force_when_no_tag_is_in_the_window(monkeypatch, roo_home, tmp_path):
    """A request whose own record never reached the file still resolves.

    One captured request's tool-result record only got appended after a resume,
    333 s later. The previous tag is the model Roo was actually running, which
    is a persisted string rather than a guess.
    """
    storage = tmp_path / "storage"
    _write_task(
        storage,
        "task-orphan",
        [req(T0, 100, 10), req(T0 + 6_275, 120, 12)],
        [env_user(T0 + 24, "gpt-5.2")],
    )
    entries = _entries(_parser(monkeypatch, storage))

    assert [e["model"] for e in entries] == ["gpt-5.2", "gpt-5.2"]


def test_a_distant_tag_borrows_no_model(monkeypatch, roo_home, tmp_path):
    """The window bounds misattribution, not recovery.

    One second is twenty times the widest own-tag distance measured in the
    capture, so a tag that far away is somebody else's request. The row falls
    back to the newest tag at or before it -- Roo's own persisted string --
    rather than to the neighbour's model.
    """
    storage = tmp_path / "storage"
    _write_task(
        storage,
        "task-distant",
        [req(T0, 100, 10), req(T0 + 10_000, 120, 12)],
        [env_user(T0 + 24, "gpt-5.2"), env_user(T0 + 11_000, "claude-sonnet-4-5")],
    )
    entries = _entries(_parser(monkeypatch, storage))

    # Row 1 owns the 24 ms tag. Row 2 is 1 s from the 11 s tag, outside the
    # window, so it keeps the model in force (gpt-5.2) instead of borrowing.
    assert [e["model"] for e in entries] == ["gpt-5.2", "gpt-5.2"]


def test_roo_attributes_a_mid_task_model_switch_per_request(monkeypatch, roo_home, tmp_path):
    storage = tmp_path / "storage"
    _write_task(
        storage,
        "task-switch",
        [req(T0, 100, 10), req(T0 + 50_000, 120, 12), req(T0 + 90_000, 140, 14)],
        [env_user(T0 + 24, "gpt-5.2"), env_user(T0 + 50_007, "claude-sonnet-4-5")],
    )
    entries = _entries(_parser(monkeypatch, storage))

    assert [e["model"] for e in entries] == [
        "gpt-5.2",
        "claude-sonnet-4-5",
        "claude-sonnet-4-5",
    ]


def test_roo_task_with_no_tag_at_all_is_unknown_and_free(monkeypatch, roo_home, tmp_path):
    storage = tmp_path / "storage"
    _write_task(
        storage,
        "task-untagged",
        [req(T0, 5000, 300)],
        [{"role": "user", "ts": T0 + 24, "content": [{"type": "text", "text": "no header"}]}],
    )
    entry = _entries(_parser(monkeypatch, storage))[0]

    assert entry["model"] == "unknown"
    assert entry["cost"] == 0.0
    assert (entry["input"], entry["output"]) == (5000, 300)  # tokens are not lost


def test_roo_model_pairing_rules_directly():
    tags = [(1_000, "a"), (10_000, "b")]

    assert _roo_model_for([], 1_000) == "unknown"
    assert _roo_model_for(tags, 976) == "a"  # own record lands a few ms later
    assert _roo_model_for(tags, 10_043) == "b"
    assert _roo_model_for(tags, 6_275) == "a"  # in force, nothing in the window
    assert _roo_model_for(tags, 600) == "a"  # a tag inside the window ahead
    # Past the window with nothing at or before the row, nothing is invented.
    assert _roo_model_for([(5_000, "a")], 1_000) == "unknown"


def test_roo_reads_both_conversation_content_shapes(monkeypatch, roo_home, tmp_path):
    """content is a plain string on a text turn, a typed-part list otherwise."""
    storage = tmp_path / "storage"
    conv = [
        {
            "role": "user",
            "ts": T0 + 24,
            "content": "<environment_details><model>gpt-4.1</model></environment_details>",
        },
        {
            "role": "user",
            "ts": T0 + 30_000,
            "content": [
                {"type": "tool_result", "content": "ok", "tool_use_id": "t1"},
                {
                    "type": "text",
                    "text": "<environment_details><model>gpt-5.2</model></environment_details>",
                },
            ],
        },
    ]
    _write_task(storage, "task-shapes", [req(T0, 100, 10), req(T0 + 30_007, 120, 12)], conv)
    entries = _entries(_parser(monkeypatch, storage))

    assert [e["model"] for e in entries] == ["gpt-4.1", "gpt-5.2"]


# --- one producer for both surfaces ------------------------------------------


def test_roo_task_rows_is_the_producer_that_carries_the_model(monkeypatch, roo_home, tmp_path):
    """parse_roo_task_file() alone cannot, and no caller may use it directly."""
    storage = tmp_path / "storage"
    task = _write_task(
        storage,
        "task-producer",
        [req(T0, 100, 10)],
        [env_user(T0 + 24, "gpt-5.2")],
    )
    messages = task / "ui_messages.json"

    raw = parse_roo_task_file(str(messages))
    assert len(raw) == 1
    assert "model" not in raw[0]  # the message file has no model anywhere

    rows = roo_task_rows(messages)
    assert len(rows) == 1
    assert rows[0]["model"] == "gpt-5.2"
    assert rows[0]["entry_id"] == raw[0]["entry_id"]
    assert rows[0]["ts"] == raw[0]["ts"]


def test_roo_parser_consumes_the_shared_producer(monkeypatch, roo_home, tmp_path):
    """Overview's rows and the shared producer agree per turn, model included."""
    storage = tmp_path / "storage"
    task = _write_task(
        storage,
        "task-parity",
        [req(T0, 100, 10), req(T0 + 5_000, 120, 20)],
        [env_user(T0 + 24, "gpt-5.2"), env_user(T0 + 5_006, "gpt-5.2")],
    )
    parser = _parser(monkeypatch, storage)
    entries = _entries(parser)
    rows = roo_task_rows(task / "ui_messages.json")

    assert len(entries) == len(rows)
    for entry, row in zip(entries, rows):
        assert entry["entry_id"] == row["entry_id"]
        assert entry["model"] == row["model"]
        assert entry["timestamp"] == row["ts"]
        assert entry["input"] == row["input"]
        assert entry["output"] == row["output"]
        assert entry["cacheRead"] == row["cacheRead"]
        assert entry["cacheWrite"] == row["cacheWrite"]


# --- totals that must stay unread --------------------------------------------


def test_roo_ignores_the_lagging_index_and_the_history_item(monkeypatch, roo_home, tmp_path):
    """Both copies of the totals exist; neither is the source of a number.

    Seeded from the live capture: the task's own rows and its history_item agree
    at 56126/670 while _index.json sits four requests behind at 46181/590.
    """
    storage = tmp_path / "storage"
    _write_task(
        storage,
        "task-lag",
        [req(T0, 8780, 140), req(T0 + 27_264, 9041, 178), req(T0 + 33_546, 9339, 82)],
        [env_user(T0 + 24, "gpt-5.2")],
        history_item={
            "id": "task-lag",
            "task": "placeholder prompt",
            "workspace": "/tmp/placeholder",
            "tokensIn": 27_160,
            "tokensOut": 400,
            "totalCost": 4.4,
            "ts": T0,
        },
    )
    (storage / "tasks" / "_index.json").write_text(
        json.dumps({"version": 1, "tasks": {}}), encoding="utf-8"
    )
    entries = _entries(_parser(monkeypatch, storage))

    assert sum(e["input"] + e["cacheRead"] + e["cacheWrite"] for e in entries) == 27_160
    assert sum(e["output"] for e in entries) == 400
    assert sum(e["cost"] for e in entries) != 4.4


def test_roo_history_item_may_omit_the_cache_keys(monkeypatch, roo_home, tmp_path):
    """The aborted child omits cacheWrites/cacheReads rather than zeroing them."""
    storage = tmp_path / "storage"
    _write_task(
        storage,
        "task-sparse",
        [req(T0, 700, 40)],
        [env_user(T0 + 24, "gpt-5.2")],
        history_item={"id": "task-sparse", "tokensIn": 700, "tokensOut": 40, "totalCost": 0},
    )
    entry = _entries(_parser(monkeypatch, storage))[0]

    assert (entry["cacheRead"], entry["cacheWrite"]) == (0, 0)


# --- discovery ---------------------------------------------------------------


def test_roo_discovers_tasks_by_glob_not_from_the_index(monkeypatch, roo_home, tmp_path):
    """The live run had a task dir, a history_item and a row with no index entry."""
    storage = tmp_path / "storage"
    _write_task(
        storage,
        "task-unlisted",
        [req(T0, 500, 30)],
        [env_user(T0 + 24, "gpt-5.2")],
        history_item={"id": "task-unlisted", "tokensIn": 500, "tokensOut": 30},
    )
    (storage / "tasks" / "_index.json").write_text(
        json.dumps({"version": 1, "tasks": {}}), encoding="utf-8"
    )
    entries = _entries(_parser(monkeypatch, storage))

    assert len(entries) == 1


def test_roo_index_json_is_not_treated_as_a_task(monkeypatch, roo_home, tmp_path):
    storage = tmp_path / "storage"
    _write_task(storage, "task-ok", [req(T0, 10, 1)], [env_user(T0 + 24, "m")])
    # _index.json is a file, not a task directory, and has no ui_messages.json.
    (storage / "tasks" / "_index.json").write_text(json.dumps({"version": 1}), encoding="utf-8")
    files = _parser(monkeypatch, storage)._file_signatures()

    assert [Path(p).parent.name for p, _, _ in files] == ["task-ok"]


def test_roo_nested_extension_layout_is_found(monkeypatch, roo_home):
    """The VS Code form nests tasks under the extension id."""
    ext = roo_home / ".config" / "Code" / "User" / "globalStorage" / "rooveterinaryinc.roo-cline"
    monkeypatch.delenv("TOKDASH_ROO_STORAGE_DIR", raising=False)
    _write_task(ext, "task-ext", [req(T0, 400, 20)], [env_user(T0 + 24, "gpt-5.2")])

    entries = _entries(RooCodeParser(PricingDatabase()))
    assert len(entries) == 1


def test_roo_cli_storage_root_is_a_candidate(monkeypatch, roo_home):
    """@roo-code/cli writes to ~/.vscode-mock/global-storage, one level shallower."""
    monkeypatch.delenv("TOKDASH_ROO_STORAGE_DIR", raising=False)
    cli = roo_home / ".vscode-mock" / "global-storage"
    _write_task(cli, "task-cli", [req(T0, 300, 15)], [env_user(T0 + 24, "gpt-5.2")])

    assert clientpaths.roo_storage_roots() == [cli]
    assert len(_entries(RooCodeParser(PricingDatabase()))) == 1


def test_roo_wsl_union_finds_server_and_windows_trees_together(monkeypatch, roo_home, tmp_path):
    """The dual-tree case: a remote-server install AND a Windows desktop install."""
    monkeypatch.delenv("TOKDASH_ROO_STORAGE_DIR", raising=False)
    monkeypatch.setattr(clientpaths.osinfo, "os_kind", lambda: "wsl")
    server = roo_home / ".vscode-server" / "data" / "User" / "globalStorage" / "rooveterinaryinc.roo-cline"
    _write_task(server, "task-server", [req(T0, 100, 10)], [env_user(T0 + 24, "m")])
    win = tmp_path / "mnt-c" / "Users" / "someone" / "AppData" / "Roaming" / "Code" / "User" / "globalStorage" / "rooveterinaryinc.roo-cline"
    _write_task(win, "task-windows", [req(T0 + 1, 200, 20)], [env_user(T0 + 25, "m")])

    roots = clientpaths.roo_storage_roots()
    assert server in roots and win in roots
    task_ids = sorted(Path(p).parent.name for p in clientpaths.roo_task_message_files())
    assert task_ids == ["task-server", "task-windows"]

    entries = _entries(RooCodeParser(PricingDatabase()))
    assert len(entries) == 2  # a single-winner rule would have found one


def test_roo_windows_tree_is_never_enumerated_off_wsl(monkeypatch, roo_home, tmp_path):
    """A Linux or macOS process must not walk /mnt/c looking for a Windows tree."""
    monkeypatch.delenv("TOKDASH_ROO_STORAGE_DIR", raising=False)
    monkeypatch.setattr(clientpaths.osinfo, "os_kind", lambda: "linux")
    win = tmp_path / "mnt-c" / "Users" / "someone" / "AppData" / "Roaming" / "Code" / "User" / "globalStorage" / "rooveterinaryinc.roo-cline"
    _write_task(win, "task-foreign", [req(T0, 100, 10)], [env_user(T0 + 24, "m")])

    assert clientpaths.roo_storage_roots() == []


def test_roo_storage_override_adds_to_the_defaults(monkeypatch, roo_home, tmp_path):
    """Relocating storage does not delete the tasks Roo wrote to the old root."""
    default = roo_home / ".config" / "Code" / "User" / "globalStorage" / "rooveterinaryinc.roo-cline"
    _write_task(default, "task-default", [req(T0, 100, 10)], [env_user(T0 + 24, "m")])
    moved = tmp_path / "moved-storage"
    _write_task(moved, "task-moved", [req(T0 + 1, 200, 20)], [env_user(T0 + 25, "m")])

    entries = _entries(_parser(monkeypatch, moved))
    assert sorted(e["entry_id"].split(":")[1] for e in entries) == [
        "task-default",
        "task-moved",
    ]


# --- failure behaviour -------------------------------------------------------


def test_roo_one_broken_task_does_not_blank_the_source(monkeypatch, roo_home, tmp_path):
    storage = tmp_path / "storage"
    _write_task(storage, "task-good", [req(T0, 100, 10)], [env_user(T0 + 24, "gpt-5.2")])
    torn = _write_task(storage, "task-torn", [req(T0 + 1, 200, 20)], [env_user(T0 + 25, "gpt-5.2")])
    (torn / "ui_messages.json").write_text("[{not json", encoding="utf-8")

    entries = _entries(_parser(monkeypatch, storage))
    assert [e["entry_id"].split(":")[1] for e in entries] == ["task-good"]


def test_roo_strict_entry_point_raises_when_the_task_vanishes(monkeypatch, roo_home, tmp_path):
    """file_replace reads [] as "zero entries now" and deletes the stored rows.

    So the stored sync's entry point must raise the typed signal instead, and
    the store keeps the rows for a task that went missing mid-sync.
    """
    storage = tmp_path / "storage"
    task = _write_task(
        storage,
        "task-gone",
        [req(T0, 100, 10)],
        [env_user(T0 + 24, "gpt-5.2")],
    )
    parser = _parser(monkeypatch, storage)
    sigs = parser._file_signatures()
    assert len(sigs) == 1

    (task / "ui_messages.json").unlink()

    with pytest.raises(UsageFileVanished):
        parser._parse_file_strict(sigs[0])


def test_roo_live_path_survives_a_vanished_task(monkeypatch, roo_home, tmp_path):
    """One deleted task must not cost every other task its entries."""
    storage = tmp_path / "storage"
    gone = _write_task(storage, "task-gone", [req(T0, 100, 10)], [env_user(T0 + 24, "m")])
    _write_task(storage, "task-stays", [req(T0 + 1, 200, 20)], [env_user(T0 + 25, "m")])
    parser = _parser(monkeypatch, storage)
    sigs = {Path(path).parent.name for path, _mtime, _size in parser._file_signatures()}
    (gone / "ui_messages.json").unlink()

    entries = parser._parse_all()
    assert [e["entry_id"].split(":")[1] for e in entries] == ["task-stays"]
    assert "task-gone" in sigs


# --- cache bound -------------------------------------------------------------


def test_roo_model_cache_is_bounded(monkeypatch, roo_home, tmp_path):
    """The corpus grows one task directory forever, so the map cache cannot."""
    from tokdash.sources.coding_tools import _ROO_MODEL_CACHE_MAX

    storage = tmp_path / "storage"
    for i in range(_ROO_MODEL_CACHE_MAX + 8):
        _write_task(
            storage,
            f"task-{i:03d}",
            [req(T0 + i, 10, 1)],
            [env_user(T0 + i + 24, "gpt-5.2")],
        )
    parser = _parser(monkeypatch, storage)
    parser._parse_all()

    assert len(_roo_model_cache) <= _ROO_MODEL_CACHE_MAX


def test_roo_registered_as_file_replace():
    from tokdash.sources.coding_tools import CodingToolsUsageTracker

    parser = CodingToolsUsageTracker().parsers["roo_code"]

    assert isinstance(parser, RooCodeParser)
    assert parser.sync_capability.mode == "file_replace"
    assert parser.sync_capability.cross_file_stable_keys is False
    assert parser.persistent_parser_version == 1
    assert parser.runtime_config_signature() is None
