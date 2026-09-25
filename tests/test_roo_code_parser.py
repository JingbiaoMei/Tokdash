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
import os
from pathlib import Path

import pytest

from tokdash import clientpaths
from tokdash.pricing import PricingDatabase
from tokdash.sources import coding_tools as ct
from tokdash.sources.coding_tools import (
    BaseParser,
    RooCodeParser,
    _roo_model_cache,
    _ROO_MODEL_TAG_WINDOW_MS,
    _roo_model_for,
    _roo_roots,
    _roo_roots_cache,
    _sig_cache,
    parse_roo_task_file,
    roo_task_file_signatures,
    roo_task_rows,
)
from tokdash.usage_store import UsageFileVanished

T0 = 1_789_932_971_243  # epoch ms, the unit Roo stamps


def req(ts, tokens_in, tokens_out, cache_reads=0, cache_writes=0, cost=0,
        protocol="openai"):
    """One completed api_req_started message, as Roo writes it.

    protocol=None omits apiProtocol altogether, which is the shape of a row
    from before the field existed.
    """
    payload = {
        "tokensIn": tokens_in,
        "tokensOut": tokens_out,
        "cacheWrites": cache_writes,
        "cacheReads": cache_reads,
        "cost": cost,
    }
    if protocol is not None:
        payload["apiProtocol"] = protocol
    return {
        "type": "say",
        "say": "api_req_started",
        "ts": ts,
        "text": json.dumps(payload),
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
    _roo_roots_cache.clear()
    return home


def _desktop_global_storage(home: Path) -> Path:
    """The desktop globalStorage root the CURRENT OS kind actually scans.

    Built from the same three branches `clientpaths.roo_storage_roots()` uses,
    because the layouts do not overlap: `%APPDATA%` on Windows,
    `$XDG_CONFIG_HOME` (default `~/.config`) on Linux and WSL, and
    `~/Library/Application Support` on macOS. A test that names one of them
    passes on that platform and proves nothing on the other two, which CI
    demonstrated by failing exactly this helper's two callers on Windows and
    macOS while Ubuntu passed.
    """
    kind = clientpaths.osinfo.os_kind()
    if kind == "windows":
        base = Path(os.environ["APPDATA"])
    elif kind == "macos":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "Code" / "User" / "globalStorage" / "rooveterinaryinc.roo-cline"


def _other_platform_storage(home: Path) -> Path:
    """A desktop tree this process must NOT walk, whatever the platform is."""
    if clientpaths.osinfo.os_kind() == "macos":
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
    else:
        base = Path.home() / "Library" / "Application Support"
    return base / "Code" / "User" / "globalStorage" / "rooveterinaryinc.roo-cline"


def _parser(monkeypatch, storage):
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    _roo_model_cache.clear()
    _roo_roots_cache.clear()
    monkeypatch.setenv("TOKDASH_ROO_STORAGE_DIR", str(storage))
    return RooCodeParser(PricingDatabase())


def _entries(parser):
    return parser._parse_all()


# --- what counts as a billable request ---------------------------------------

def test_two_spellings_of_one_root_are_scanned_once(monkeypatch, roo_home, tmp_path):
    """A root reached twice is a double count, never a duplicate row.

    Roo scans a UNION of roots on purpose, so the dedupe has to be on the
    resolved path. Two absolute spellings of one directory -- a
    TOKDASH_ROO_STORAGE_DIR written with a ".." in it, or a symlinked
    ~/.vscode-server beside the real one -- used to become two roots, which fed
    one ui_messages.json to the parser twice. Roo's entry keys are task-scoped,
    so the second copy carries the same id and the live path billed it again.
    """
    real = tmp_path / "real"
    (tmp_path / "sub").mkdir()
    _write_task(real, "t1", [env_user(T0, "model-a"), req(T0 + 1000, 1000, 20)])

    alias = tmp_path / "sub" / ".." / "real"
    assert alias.resolve() == real.resolve()
    monkeypatch.setenv("TOKDASH_ROO_STORAGE_DIR", f"{real},{alias}")
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    _roo_model_cache.clear()
    _roo_roots_cache.clear()

    roots = clientpaths.roo_storage_roots()
    assert len(roots) == 1, f"one directory, one root: {roots}"
    assert len(clientpaths.roo_task_message_files()) == 1

    entries = RooCodeParser(PricingDatabase())._parse_all()
    assert len(entries) == 1, [e["entry_id"] for e in entries]
    assert entries[0]["input"] + entries[0]["output"] == 1020


def test_roo_wsl_also_finds_windows_profile_storage(monkeypatch, roo_home, tmp_path):
    """A Windows-side VS Code PROFILE is a second tree, not a curiosity.

    Switching profiles leaves both task trees on disk, and the native desktop
    branches have always read both. The WSL branch used its own one-glob
    pattern, which saw globalStorage/ and skipped profiles/*/globalStorage/ --
    so the docs promised profile coverage that the WSL scan did not deliver.
    """
    monkeypatch.delenv("TOKDASH_ROO_STORAGE_DIR", raising=False)
    monkeypatch.setattr(clientpaths.osinfo, "os_kind", lambda: "wsl")
    roaming = tmp_path / "mnt-c" / "Users" / "someone" / "AppData" / "Roaming"
    plain = roaming / "Code" / "User" / "globalStorage" / "rooveterinaryinc.roo-cline"
    profile = (
        roaming / "Code" / "User" / "profiles" / "Work" / "globalStorage"
        / "rooveterinaryinc.roo-cline"
    )
    _write_task(plain, "task-plain", [req(T0, 100, 10)], [env_user(T0 + 24, "m")])
    _write_task(profile, "task-profile", [req(T0 + 1, 200, 20)], [env_user(T0 + 25, "m")])

    task_ids = sorted(Path(f).parent.name for f in clientpaths.roo_task_message_files())
    assert task_ids == ["task-plain", "task-profile"]


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0,
                    reason="needs a POSIX EACCES that root cannot create")
def test_one_unreadable_root_costs_only_that_root(monkeypatch, roo_home, tmp_path):
    """pathlib re-raises EACCES out of is_dir(), and Roo scans trees it does not own.

    Another user's AppData under /mnt/c, a 0700 dir on a shared box: the probe
    used to escape roo_storage_roots() and take the WHOLE Roo source with it,
    so one locked directory meant no Roo usage anywhere on the dashboard. An
    unreadable candidate is absent, and the readable neighbours still count.
    """
    locked = tmp_path / "locked"
    healthy = tmp_path / "healthy"
    _write_task(healthy, "task-ok", [req(T0, 1000, 20)], [env_user(T0 + 20, "m")])
    hidden = locked / "storage"
    _write_task(hidden, "task-inaccessible", [req(T0, 5, 5)], [env_user(T0 + 20, "m")])
    locked.chmod(0o000)
    try:
        monkeypatch.setenv("TOKDASH_ROO_STORAGE_DIR", f"{hidden},{healthy}")
        _sig_cache.clear()
        BaseParser._entry_cache.clear()
        _roo_roots_cache.clear()

        files = clientpaths.roo_task_message_files()
        assert [Path(f).parent.name for f in files] == ["task-ok"]
        entries = RooCodeParser(PricingDatabase())._parse_all()
        assert [e["entry_id"] for e in entries] == [f"roo_code:task-ok:{T0}"]
    finally:
        locked.chmod(0o755)


@pytest.mark.skipif(os.name == "nt", reason="symlink semantics are POSIX here")
def test_one_tasks_directory_under_two_spellings_is_counted_once(
    monkeypatch, roo_home, tmp_path
):
    """The roots are canonical; the tasks/ INSIDE them was not.

    A tasks/ symlinked or bind-mounted into a second root yields two directory
    spellings for one task, and Roo's task-scoped entry keys make the second
    copy a double count on the live path. The store's unique index absorbs it,
    which is exactly the disagreement between the two totals this pins.
    """
    real_root = tmp_path / "real"
    other_root = tmp_path / "other"
    _write_task(real_root, "task-1", [req(T0, 1000, 20)], [env_user(T0 + 20, "m")])
    other_root.mkdir()
    (other_root / "tasks").symlink_to(real_root / "tasks", target_is_directory=True)

    monkeypatch.setenv("TOKDASH_ROO_STORAGE_DIR", f"{real_root},{other_root}")
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    _roo_roots_cache.clear()

    assert len(clientpaths.roo_task_message_files()) == 1
    entries = RooCodeParser(PricingDatabase())._parse_all()
    assert len(entries) == 1, [e["entry_id"] for e in entries]
    assert entries[0]["input"] + entries[0]["output"] == 1020


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


def test_roo_anthropic_rows_before_3_29_5_keep_their_fresh_input(
    monkeypatch, roo_home, tmp_path
):
    """Roo changed the MEANING of tokensIn in 3.29.5, and the rows differ.

    Upstream PR #8954 (merged 2025-10-31, released in 3.29.5 on 2025-11-01)
    replaced `tokensIn: inputTokens` with `tokensIn: costResult.totalInputTokens`
    and made that choice per protocol. Before it, an anthropic-protocol row held
    the provider's own number, and Roo's comment on calculateApiCostAnthropic
    says that number "does NOT include the cached tokens". So on a pre-fix row
    the four fields are already disjoint, and subtracting the cache from an
    input that never contained it bills a 49,912-token request as a 412-token
    one -- an undercount of two orders of magnitude, silently.

    The test is arithmetic, not a version guess. Here the inclusive reading
    would have to bill 412 fresh tokens to a prompt that reported 49,500 cached
    ones, which is impossible: the row's own numbers say 412 is the fresh part
    and the cache sits alongside it. Under the old reading this row was 1,023
    tokens; it is 50,523.
    """
    storage = tmp_path / "storage"
    _write_task(
        storage,
        "task-old",
        [req(T0, 412, 611, cache_reads=49_500, protocol="anthropic")],
        [env_user(T0 + 20, "claude-sonnet-4-5")],
    )
    entry = _entries(_parser(monkeypatch, storage))[0]

    assert (entry["input"], entry["cacheRead"], entry["cacheWrite"], entry["output"]) == (
        412,
        49_500,
        0,
        611,
    )
    # The clamp is what makes the old reading silent rather than merely wrong:
    # cacheRead is capped at tokensIn, so the row's own cache mostly vanished.
    assert entry["_billing"]["input"] == 412


def test_roo_modern_anthropic_rows_are_still_cache_inclusive(
    monkeypatch, roo_home, tmp_path
):
    """The other half of the rule: 3.29.5+ never trips the pre-fix reading.

    A modern anthropic row is fresh + reads + writes by construction, so it is
    never below reads + writes and the discriminant cannot misfire on it. Same
    numbers as the openai case above, different protocol.
    """
    storage = tmp_path / "storage"
    _write_task(
        storage,
        "task-new",
        [req(T0, 1000, 200, cache_reads=600, cache_writes=100, protocol="anthropic")],
        [env_user(T0 + 20, "claude-sonnet-4-5")],
    )
    entry = _entries(_parser(monkeypatch, storage))[0]

    assert (entry["input"], entry["cacheRead"], entry["cacheWrite"], entry["output"]) == (
        300,
        600,
        100,
        200,
    )


def test_roo_anthropic_row_that_fits_both_readings_stays_inclusive(
    monkeypatch, roo_home, tmp_path
):
    """The residue, pinned rather than papered over.

    A pre-fix row whose fresh input is NOT below its cache -- 49,912 fresh
    against 49,500 cached, say -- is arithmetically indistinguishable from a
    modern inclusive row, and Roo is archived at 3.54.0 with no version written
    into the task file. Guessing the other way there would ADD tokens to a row
    whose numbers already add up, so the inclusive reading stands and the row
    can still read low. A cutoff date would separate them, but it is evidence
    about the install rather than about the row, and it misfires on a machine
    that simply did not upgrade. Documented blind spot, in SUPPORTED_CLIENTS.
    """
    storage = tmp_path / "storage"
    _write_task(
        storage,
        "task-ambiguous",
        [req(T0, 49_912, 611, cache_reads=49_500, protocol="anthropic")],
        [env_user(T0 + 20, "claude-sonnet-4-5")],
    )
    entry = _entries(_parser(monkeypatch, storage))[0]

    assert (entry["input"], entry["cacheRead"]) == (412, 49_500)


def test_a_row_with_no_protocol_stamp_is_still_read_the_old_way(
    monkeypatch, roo_home, tmp_path
):
    """Rows from before the apiProtocol field exist cannot opt out of the fix.

    The discriminant is about the numbers, not about Roo's own label, so a row
    with no apiProtocol key at all is still recognised as pre-3.29.5 when its
    tokensIn cannot contain its cache. Gating on the stamp would silently clamp
    exactly these rows back to 1,023 tokens.
    """
    storage = tmp_path / "storage"
    _write_task(
        storage,
        "task-noprotocol",
        [req(T0, 412, 611, cache_reads=49_500, protocol=None)],
        [env_user(T0 + 20, "claude-sonnet-4-5")],
    )
    entry = _entries(_parser(monkeypatch, storage))[0]

    assert (entry["input"], entry["cacheRead"], entry["cacheWrite"], entry["output"]) == (
        412, 49_500, 0, 611,
    )


def test_a_bedrock_row_stamped_openai_is_read_the_old_way(
    monkeypatch, roo_home, tmp_path
):
    """Roo's own protocol label is wrong in the direction that matters.

    Bedrock and Vertex-Claude requests were stamped "openai" until upstream PR
    #6019, so a row that says openai can still be an Anthropic-family row whose
    tokensIn excludes the cache. This is the second reason the rule does not
    consult the stamp: trusting it would re-clamp precisely the rows this fix
    exists for.
    """
    storage = tmp_path / "storage"
    _write_task(
        storage,
        "task-bedrock",
        [req(T0, 412, 611, cache_reads=49_500, cache_writes=1_000, protocol="openai")],
        [env_user(T0 + 20, "claude-sonnet-4-5")],
    )
    entry = _entries(_parser(monkeypatch, storage))[0]

    assert (entry["input"], entry["cacheRead"], entry["cacheWrite"], entry["output"]) == (
        412, 49_500, 1_000, 611,
    )


def test_the_task_signature_folds_in_its_model_sibling(monkeypatch, roo_home, tmp_path):
    """A late <model> tag must re-price the row that needed it.

    The model lives in api_conversation_history.json and Roo writes that file on
    its own schedule, so a task can gain its first tag AFTER its last request.
    ui_messages.json never changes again once a task is finished, so a signature
    over the token file alone would leave the stored row at "unknown" and 0.00
    for the life of the index. The sibling is therefore FOLDED into the task's
    one signature entry - max mtime, summed size, the _sqlite_db_signature shape
    for sidecars - rather than becoming an entry of its own, which would fight
    the task's task-scoped entry keys over the same stored row.
    """
    storage = tmp_path / "storage"
    task = _write_task(
        storage,
        "task-late-tag",
        [req(T0, 1000, 200, cache_reads=600)],
        None,
    )
    monkeypatch.setenv("TOKDASH_ROO_STORAGE_DIR", str(storage))
    _sig_cache.clear()
    before = roo_task_file_signatures()
    assert len(before) == 1

    (task / "api_conversation_history.json").write_text(
        json.dumps([env_user(T0 + 20, "claude-sonnet-4-5")]), encoding="utf-8"
    )
    _sig_cache.clear()
    after = roo_task_file_signatures()

    assert len(after) == 1                      # still ONE entry per task
    assert after[0][0] == before[0][0]          # same path: the token file
    assert after[0][1:] != before[0][1:]        # but the stamp moved


@pytest.mark.skipif(os.name == "nt", reason="symlink semantics are POSIX here")
def test_one_task_under_two_spellings_is_discovered_once(monkeypatch, roo_home, tmp_path):
    """Two roots, one tasks/ tree: one task, one entry.

    The second root is a symlinked tasks/, which is how a relocated
    customStoragePath or a bind mount actually looks. Discovery canonicalises the
    tasks/ TREE once rather than resolving every task directory, so this costs
    one realpath call per root and still catches all of it.
    """
    real = tmp_path / "real"
    _write_task(real, "task-x", [req(T0, 1000, 200, cache_reads=600)],
                [env_user(T0 + 20, "m")])
    alias = tmp_path / "alias"
    alias.mkdir()
    (alias / "tasks").symlink_to(real / "tasks", target_is_directory=True)

    parser = _parser(monkeypatch, real)              # one root, for the caches
    # ... then both spellings of the same tree. The override is set after the
    # parser was built because the thing under test is the enumeration, which
    # happens on the first scan, not at construction.
    monkeypatch.setenv("TOKDASH_ROO_STORAGE_DIR", f"{real},{alias}")
    _sig_cache.clear()
    _roo_roots_cache.clear()
    entries = _entries(parser)

    assert len(entries) == 1
    assert entries[0]["input"] == 400


def test_parse_all_folds_two_emissions_of_one_entry_id(monkeypatch, roo_home, tmp_path):
    """The last line of defence, pinned directly.

    Roo's entry keys are roo_code:{taskId}:{ts}, so any path that hands the
    parser one task twice bills the same request twice on the live surface. The
    store absorbs it through its unique index, and that asymmetry is the reason
    this fold exists: live and stored totals have to be one number. Discovery
    already dedupes by task id, so this is defence in depth - hence the doubled
    signature list rather than a contrived filesystem.
    """
    storage = tmp_path / "storage"
    _write_task(storage, "task-fold", [req(T0, 1000, 200, cache_reads=600)],
                [env_user(T0 + 20, "m")])
    monkeypatch.setenv("TOKDASH_ROO_STORAGE_DIR", str(storage))
    parser = _parser(monkeypatch, storage)
    sigs = parser._file_signatures()
    assert len(sigs) == 1
    monkeypatch.setattr(parser, "_file_signatures", lambda: tuple(list(sigs) * 2))

    entries = parser._parse_all()

    assert len(entries) == 1
    assert entries[0]["input"] == 400


def test_the_roots_cache_keys_on_the_variables_the_roots_read(
    monkeypatch, roo_home, tmp_path
):
    """A relocated ~/.config must not inherit the previous root list.

    _roo_roots() memoises the enumeration for the same short TTL the signature
    scan uses, because on WSL the fan-out is a Windows round trip per candidate.
    The key has to name what actually decides the set: the roots read
    XDG_CONFIG_HOME for the desktop trees, and keying on XDG_DATA_HOME instead
    would serve a stale list for the whole TTL after a relocation.
    """
    a = tmp_path / "config-a"
    b = tmp_path / "config-b"
    for base in (a, b):
        storage = base / "Code" / "User" / "globalStorage" / clientpaths._ROO_EXTENSION_ID
        _write_task(storage / "tasks", "task-x", [req(T0, 100, 5)],
                    [env_user(T0 + 20, "m")])

    monkeypatch.delenv("TOKDASH_ROO_STORAGE_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(a))
    _roo_roots_cache.clear()
    first = [str(p) for p in _roo_roots()]
    assert any("/config-a/" in p for p in first)

    monkeypatch.setenv("XDG_CONFIG_HOME", str(b))
    second = [str(p) for p in _roo_roots()]        # same TTL, different key
    assert any("/config-b/" in p for p in second)
    assert not any("/config-a/" in p for p in second)


class _SteppedClock:
    """A monotonic clock the test advances, so a slow scan costs no wall time."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:  # pragma: no cover - not used here
        self.now += seconds


def test_a_scan_that_costs_more_than_the_ttl_is_still_reused(monkeypatch, tmp_path):
    """The memo has to outlive the scan that filled it, or nothing reads it.

    ``_SIG_TTL`` is a flat five seconds, which presumes a cheap scan. On a
    600-task Roo corpus mounted on a Windows filesystem one scan measured 6.3 s,
    so the entry was stale before Overview finished parsing what the scan had
    just found, and the Sessions panel -- the second surface of the same refresh
    -- always walked the tree again. Honouring the cost of one scan is what
    makes a slow corpus warm at all; the cap keeps a corpus that got
    unreasonably slow from pinning the dashboard to a stale view.
    """
    clock = _SteppedClock()
    monkeypatch.setattr(ct, "_time", clock)
    monkeypatch.setattr(ct, "_SIG_TTL", 5.0)
    ct._sig_cache.clear()

    scans = 0

    def slow_scan():
        nonlocal scans
        scans += 1
        clock.now += 6.3  # one Windows-mounted walk
        return (("p", 1, 1),)

    first = ct._timed_sigs("corpus", slow_scan)
    assert scans == 1

    clock.now += 1.0  # Overview parses the corpus the scan just found
    assert ct._timed_sigs("corpus", slow_scan) == first
    assert scans == 1, "a memo worth less than the refresh cannot be used"

    clock.now += 5.0  # the Sessions panel of that same refresh arrives
    assert ct._timed_sigs("corpus", slow_scan) == first
    assert scans == 1, "the second surface of one refresh must reuse the scan"

    clock.now += 7.0  # past two scans' worth of cost
    assert ct._timed_sigs("corpus", slow_scan) == first
    assert scans == 2, "the honoured window is bounded, not indefinite"

    # The cap: a scan far slower than the TTL delays Overview, it does not
    # freeze it.
    scans = 0

    def pathological():
        nonlocal scans
        scans += 1
        clock.now += 60.0
        return (("q", 2, 2),)

    assert ct._timed_sigs("huge", pathological) == (("q", 2, 2),)
    clock.now += 19.0
    assert ct._timed_sigs("huge", pathological) == (("q", 2, 2),)
    assert scans == 1
    clock.now += 2.0
    assert ct._timed_sigs("huge", pathological) == (("q", 2, 2),)
    assert scans == 2

    # And the documented off switch still means "never reuse".
    monkeypatch.setattr(ct, "_SIG_TTL", 0.0)
    before = scans
    ct._timed_sigs("huge", pathological)
    ct._timed_sigs("huge", pathological)
    assert scans == before + 2
    ct._sig_cache.clear()


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


def _roo_model_for_linear(tags, ts):
    """The pre-binary-search scan, kept as the oracle for the tests below.

    The binary search is only worth having if it answers identically, and the
    interesting cases are the ones it could get wrong: a stamp that is an exact
    hit, one exactly at the window edge, a tie between the tag before and the
    tag after, and a conversation file with two tags in the same millisecond.
    """
    if not tags:
        return "unknown"
    best_delta = None
    best_model = ""
    newest_before = ""
    for tag_ts, model in tags:
        delta = abs(tag_ts - ts)
        if best_delta is None or delta < best_delta:
            best_delta, best_model = delta, model
        if tag_ts <= ts:
            newest_before = model
    if best_delta is not None and best_delta <= _ROO_MODEL_TAG_WINDOW_MS:
        return best_model
    return newest_before or "unknown"


@pytest.mark.parametrize("seed", [0, 1, 2, 7, 43])
def test_roo_model_binary_search_matches_the_scan_it_replaced(seed):
    import random

    rng = random.Random(seed)
    window = _ROO_MODEL_TAG_WINDOW_MS
    for _ in range(400):
        n = rng.randint(0, 40)
        stamps = sorted(
            rng.randrange(1, 60_000) for _ in range(n)
        )  # deliberately allows duplicate stamps
        tags = [(t, f"m{t % 5}") for t in stamps]
        # Probe every stamp, both window edges of every stamp, and random gaps.
        probes = {0, 1, 60_000}
        for t in stamps:
            probes.update({t, t - 1, t + 1, t - window, t + window,
                           t - window - 1, t + window + 1})
        for _ in range(20):
            probes.add(rng.randrange(-5_000, 65_000))
        for ts in sorted(probes):
            assert _roo_model_for(tags, ts) == _roo_model_for_linear(tags, ts), (
                f"seed={seed} ts={ts} tags={tags}"
            )


def test_roo_model_pairing_prefers_first_duplicate_nearest_last_in_force():
    """Same millisecond, two models: nearest is the first, in-force the last.

    Not a shape any fixture shows, but the scan had a definite answer for it and
    a bisect landing on the wrong end of the duplicate run is exactly the
    regression a rewrite of this loop would introduce.
    """
    tags = [(1_000, "a"), (5_000, "first"), (5_000, "last"), (20_000, "c")]

    # Nearest is the FIRST tag stamped 5000 (strict < kept the earliest).
    assert _roo_model_for(tags, 5_400) == "first"
    # Past the window, the model in force is the LAST one at or before the row.
    assert _roo_model_for(tags, 12_000) == "last"


def test_roo_model_lookup_scales_logarithmically_not_with_task_length():
    """Wall-clock-free scaling proof: count the tags the lookup may touch.

    A long Roo session rewrites one conversation file for its whole life, so a
    scan of that list once per billed request is quadratic in the length of the
    longest task. The count is asserted rather than timed, because a timing
    assertion on a shared machine is a flake with extra steps.
    """
    class Counting(list):
        def __init__(self, items):
            super().__init__(items)
            self.touched = 0

        def __getitem__(self, index):
            self.touched += 1
            return list.__getitem__(self, index)

    n = 10_000
    tags = Counting([(i * 10, f"m{i}") for i in range(n)])
    # Mid-list is the worst case for the scan and the best case for the bisect.
    assert _roo_model_for(tags, n * 5) == f"m{n // 2}"
    assert tags.touched < 64, f"touched {tags.touched} of {n} tags"


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
    """The VS Code form nests tasks under the extension id.

    The other platform's desktop tree is seeded too, and must stay unread: the
    candidate list is gated on `os_kind()` before it is existence-gated, so a
    Linux process never walks the macOS layout and a macOS process never walks
    the Linux one.
    """
    monkeypatch.delenv("TOKDASH_ROO_STORAGE_DIR", raising=False)
    ext = _desktop_global_storage(roo_home)
    other = _other_platform_storage(roo_home)
    _write_task(ext, "task-ext", [req(T0, 400, 20)], [env_user(T0 + 24, "gpt-5.2")])
    _write_task(other, "task-other-platform", [req(T0, 400, 20)], [env_user(T0 + 24, "gpt-5.2")])

    entries = _entries(RooCodeParser(PricingDatabase()))
    assert [e["entry_id"].split(":")[1] for e in entries] == ["task-ext"]


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
    default = _desktop_global_storage(roo_home)
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
    from tokdash.sources.coding_tools import _ROO_MODEL_CACHE_FLOOR

    _ROO_MODEL_CACHE_MAX = _ROO_MODEL_CACHE_FLOOR
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

    # Overflow evicts the OLDEST entry. A wholesale clear at this bound is what
    # made a corpus over the bound share nothing, and a bare upper bound would
    # pass on either: clearing leaves 8 entries here, evicting leaves the bound.
    assert len(_roo_model_cache) == _ROO_MODEL_CACHE_MAX, (
        f"overflow left {len(_roo_model_cache)} of {_ROO_MODEL_CACHE_MAX} entries: "
        "the map must evict its oldest entry, not itself")


def test_the_model_map_grows_with_the_corpus_it_serves(
    monkeypatch, roo_home, tmp_path
):
    """A bound under the task count buys no sharing, which is the whole point.

    Overview reads a task's tags during a sync and the Sessions panel reads the
    SAME file moments later. Measured on a Windows-mounted corpus at the old
    32-entry bound, the panel re-read every one of 300 conversation files on its
    first pass after a sync -- 2.2 s where a warm map costs 1.2 s -- and the map
    held a dozen entries at the end of every pass. So the bound follows the
    corpus, and a corpus that shrinks takes its map down with it.
    """
    from tokdash.sources.coding_tools import (
        _ROO_MODEL_CACHE_CEILING,
        _ROO_MODEL_CACHE_FLOOR,
        note_roo_corpus_size,
    )

    try:
        note_roo_corpus_size(4_000)
        assert len(_roo_model_cache) <= 4_000
        # Held above the floor by the floor, not by the task count: a corpus of
        # ten tasks still keeps a thousand maps, because a map costs little and
        # the next corpus may be larger.
        note_roo_corpus_size(10)
        assert len(_roo_model_cache) <= _ROO_MODEL_CACHE_FLOOR
        # And the ceiling holds against a corpus that could ask for more than
        # the heap should spend.
        note_roo_corpus_size(_ROO_MODEL_CACHE_CEILING * 4)
        assert len(_roo_model_cache) <= _ROO_MODEL_CACHE_CEILING
    finally:
        note_roo_corpus_size(_ROO_MODEL_CACHE_FLOOR)


def test_a_torn_conversation_read_is_not_cached_as_no_model(
    monkeypatch, roo_home, tmp_path
):
    """A read that failed is not an answer about the task.

    Roo rewrites api_conversation_history.json in place, so a read can land on a
    half-written file. Caching the empty tag list against that file's signature
    would make "unknown" permanent for a finished task, because its signature
    never moves again -- while Overview stored the same unknown in the usage
    rows it priced. The model is still unknown for THIS read; it just is not
    remembered as the task's answer.
    """
    storage = tmp_path / "storage"
    task = _write_task(
        storage,
        "task-torn",
        [req(T0, 1000, 20)],
        [{"role": "user", "ts": T0, "content": "not json at all"},
    ]
    )
    # Not JSON is not enough: the file has to parse as a list and still carry no
    # tag, which is the shape a torn write leaves.
    (task / "api_conversation_history.json").write_text('{"trunca', encoding="utf-8")

    rows = roo_task_rows(task / "ui_messages.json")

    assert [r["model"] for r in rows] == ["unknown"]
    assert not _roo_model_cache, "a failed read must not be remembered"

def test_roo_registered_as_file_replace():
    from tokdash.sources.coding_tools import CodingToolsUsageTracker

    parser = CodingToolsUsageTracker().parsers["roo_code"]

    assert isinstance(parser, RooCodeParser)
    assert parser.sync_capability.mode == "file_replace"
    assert parser.sync_capability.cross_file_stable_keys is False
    assert parser.persistent_parser_version == 2
    assert parser.runtime_config_signature() is None
