# Codex Activity Insights Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add accurate, local-only Codex chat, reasoning-effort, and structured-tool-call insights to Profile and Overview without changing existing token/session behavior.

**Architecture:** Extend the existing Codex JSONL parser to emit a compact private activity record during its current single pass. Persist that record in an additive SQLite column or reuse it from a signature-keyed in-memory cache when persistence is disabled, then merge and aggregate stable turn/call identities in a focused module. Expose one cached endpoint and reuse its response in the complete Profile section and quiet Overview ribbon.

**Tech Stack:** Python 3.10+, FastAPI, SQLite, `functools.lru_cache`, vanilla JavaScript, HTML/CSS, pytest, Node-backed frontend harnesses already used by the repository.

## Global Constraints

- Implement the approved contract in `docs/superpowers/specs/2026-08-03-codex-activity-insights-design.md`; do not add Fast Mode, inferred skills/plugins, date filters, or cloud/account analytics.
- Count primary/root Codex files only. Determine subagent status exclusively from the first `session_meta.payload.source.subagent.thread_spawn` marker.
- Scan each JSONL file once. The activity extraction must run inside `_parse_codex_session_file()`'s existing loop.
- Keep prompts, responses, tool arguments, tool results, credentials, and opaque IDs out of public API responses. Store only opaque turn/call IDs plus canonical activity values in `activity_json`.
- Preserve `/api/stats`, current session merging, empty-session visibility, heatmaps, milestones, and Overview layout dimensions.
- Respect `TOKDASH_USAGE_DB=0`: no database creation or writes in that mode.
- Warm requests over unchanged signatures must call `_parse_codex_session_file()` zero times in both persistent and store-disabled modes.
- Do not remove files. If a file must leave active use, archive it with a recorded reason under a dedicated archive directory.
- Run relevant tests first, then `pytest -q`, an explicit mypy check over the four touched Python modules, and `python -m compileall -q src`. The repository does not configure or declare mypy, but this workspace's `.venv` has mypy 2.3.0; compare the final result with the recorded 14-error baseline and report every remaining diagnostic exactly.
- Keep `.superpowers/` untracked and out of commits.

## File Map

**Create**

- `src/tokdash/activity_insights.py` — compact record mutation, canonical tool naming, stable-ID conflict handling, cross-file merge, deterministic distributions, and public response construction.
- `tests/test_activity_insights.py` — pure semantics plus parser/privacy/store-disabled regression tests.

**Modify**

- `src/tokdash/sessions.py` — collect activity in the existing parser pass, preserve empty-session behavior, load persistent or store-disabled activity, and expose `get_codex_activity_insights()`.
- `src/tokdash/usage_store.py` — schema version 6, nullable `activity_json`, non-mutating split during sync, and a narrow activity-only query.
- `src/tokdash/api.py` — cached `GET /api/activity-insights` route with existing backpressure/error behavior.
- `src/tokdash/static/index.html` — Profile detail section, Overview quiet ribbon, shared fetch state, loading/empty/partial/error rendering, EN/CN labels, and responsive styling.
- `tests/test_usage_store.py` — migration, persistence, durable missing-row, changed-file, and narrow-query tests.
- `tests/test_api_smoke.py` — endpoint shape, cache/error isolation, and `/api/stats` compatibility tests.
- `tests/test_profile_stats_frontend.py` — markup, shared-fetch, rendering, i18n, state, top-five, and responsive-layout contracts.
- `docs/development/CHANGELOG.md` — user-visible feature and local/primary-history scope.

---

## Task 1: Build deterministic activity semantics

**Interfaces introduced in this task**

```python
ACTIVITY_SCHEMA_VERSION = 1

def new_activity_record(*, is_primary: bool, has_explicit_session_id: bool) -> dict[str, Any]: ...
def record_reasoning_turn(record: dict[str, Any], *, turn_id: Any, effort: Any) -> None: ...
def record_structured_tool_call(
    record: dict[str, Any], *, call_id: Any, name: Any, specificity: str
) -> None: ...
def canonical_mcp_tool_name(invocation: Any) -> str | None: ...
def build_activity_insights(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]: ...
```

The `records` iterable accepted by `build_activity_insights()` contains dictionaries shaped as:

```python
{
    "session_id": "normalized-session-id",
    "file_path": "/opaque/local/path.jsonl",
    "missing": False,
    "activity": { ... } | None,
}
```

The design document's compact reasoning string is the normal stored case. The implementation uses a tagged `{effort, ambiguous}` entry only when necessary to preserve a same-file conflict without choosing a winner. Tool specificity remains the documented string (`top_level` or `mcp`) in stored JSON; numeric ranks are temporary comparison values only.

- [ ] **Step 1: Add failing pure-semantic tests**

Create `tests/test_activity_insights.py` with fixtures that use no filesystem content beyond the activity records:

```python
from tokdash.activity_insights import (
    build_activity_insights,
    new_activity_record,
    record_reasoning_turn,
    record_structured_tool_call,
)


def _wrapped(session_id, activity, *, missing=False):
    return {
        "session_id": session_id,
        "file_path": f"/{session_id}.jsonl",
        "missing": missing,
        "activity": activity,
    }


def test_activity_insights_merge_resumes_and_resolve_specificity():
    first = new_activity_record(is_primary=True, has_explicit_session_id=True)
    record_reasoning_turn(first, turn_id="turn-1", effort="xhigh")
    record_structured_tool_call(
        first, call_id="call-1", name="mcp_tool_call", specificity="top_level"
    )

    resumed = new_activity_record(is_primary=True, has_explicit_session_id=True)
    record_reasoning_turn(resumed, turn_id="turn-1", effort="xhigh")
    record_structured_tool_call(
        resumed, call_id="call-1", name="browser/click", specificity="mcp"
    )

    result = build_activity_insights([
        _wrapped("chat-1", first),
        _wrapped("chat-1", resumed),
    ])

    assert result["recorded_chats"]["value"] == 1
    assert result["reasoning"]["distribution"] == [
        {"effort": "xhigh", "count": 1, "share": 1.0}
    ]
    assert result["tools"]["total_calls"] == 1
    assert result["tools"]["distribution"] == [
        {"name": "browser/click", "count": 1, "share": 1.0}
    ]


def test_activity_insights_exclude_subagents_and_ambiguous_values():
    primary = new_activity_record(is_primary=True, has_explicit_session_id=True)
    record_reasoning_turn(primary, turn_id="turn-1", effort="high")
    record_reasoning_turn(primary, turn_id="turn-1", effort="xhigh")
    record_structured_tool_call(
        primary, call_id="call-1", name="exec", specificity="top_level"
    )
    record_structured_tool_call(
        primary, call_id="call-1", name="apply_patch", specificity="top_level"
    )

    subagent = new_activity_record(is_primary=False, has_explicit_session_id=True)
    record_reasoning_turn(subagent, turn_id="sub-turn", effort="high")
    record_structured_tool_call(
        subagent, call_id="sub-call", name="exec", specificity="top_level"
    )

    result = build_activity_insights([
        _wrapped("chat-1", primary),
        _wrapped("subagent-1", subagent),
    ])

    assert result["recorded_chats"]["value"] == 1
    assert result["reasoning"]["distribution"] == []
    assert result["reasoning"]["coverage"]["ambiguous_turns"] == 1
    assert result["tools"]["total_calls"] == 1
    assert result["tools"]["distribution"] == []
    assert result["tools"]["coverage"]["ambiguous_name_calls"] == 1


def test_activity_insights_sort_ties_and_report_missing_coverage():
    activity = new_activity_record(is_primary=True, has_explicit_session_id=True)
    record_reasoning_turn(activity, turn_id=None, effort="high")
    record_reasoning_turn(activity, turn_id="turn-2", effort=None)
    record_structured_tool_call(
        activity, call_id=None, name="exec", specificity="top_level"
    )
    for call_id, name in (("b", "zeta"), ("a", "alpha")):
        record_structured_tool_call(
            activity, call_id=call_id, name=name, specificity="top_level"
        )

    result = build_activity_insights([
        _wrapped("chat-1", activity),
        _wrapped("legacy", None, missing=True),
    ])

    assert [row["name"] for row in result["tools"]["distribution"]] == ["alpha", "zeta"]
    assert result["reasoning"]["coverage"]["excluded_records"] == 2
    assert result["tools"]["coverage"]["excluded_records"] == 1
    assert result["recorded_chats"]["coverage"]["legacy_unavailable_records"] == 1
    assert "turn-2" not in str(result)
    assert "call" not in result
```

- [ ] **Step 2: Run the new tests and confirm the import failure**

Run:

```bash
pytest -q tests/test_activity_insights.py
```

Expected: collection fails with `ModuleNotFoundError: No module named 'tokdash.activity_insights'`.

- [ ] **Step 3: Implement the compact record and merge rules**

Create `src/tokdash/activity_insights.py`. Use string-normalization helpers that reject empty IDs/names, specificity ranks `top_level=1` and `mcp=2`, and entry objects that retain ambiguity explicitly:

```python
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


ACTIVITY_SCHEMA_VERSION = 1
_SPECIFICITY_RANK = {"top_level": 1, "mcp": 2}


def _nonempty(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def new_activity_record(*, is_primary: bool, has_explicit_session_id: bool) -> dict[str, Any]:
    return {
        "version": ACTIVITY_SCHEMA_VERSION,
        "is_primary": bool(is_primary),
        "has_explicit_session_id": bool(has_explicit_session_id),
        "reasoning_by_turn_id": {},
        "tool_by_call_id": {},
        "turn_records_missing_id": 0,
        "turn_records_missing_effort": 0,
        "tool_records_missing_id": 0,
    }


def record_reasoning_turn(record: dict[str, Any], *, turn_id: Any, effort: Any) -> None:
    stable_id = _nonempty(turn_id)
    normalized_effort = _nonempty(effort)
    if stable_id is None:
        record["turn_records_missing_id"] += 1
        return
    if normalized_effort is None:
        record["turn_records_missing_effort"] += 1
        return
    turns = record["reasoning_by_turn_id"]
    existing = turns.get(stable_id)
    if existing is None:
        turns[stable_id] = normalized_effort
    elif isinstance(existing, str) and existing != normalized_effort:
        turns[stable_id] = {"effort": None, "ambiguous": True}


def record_structured_tool_call(
    record: dict[str, Any], *, call_id: Any, name: Any, specificity: str
) -> None:
    stable_id = _nonempty(call_id)
    canonical_name = _nonempty(name)
    normalized_specificity = specificity if specificity in _SPECIFICITY_RANK else "top_level"
    rank = _SPECIFICITY_RANK[normalized_specificity]
    if stable_id is None:
        record["tool_records_missing_id"] += 1
        return
    calls = record["tool_by_call_id"]
    incoming = {
        "name": canonical_name,
        "specificity": normalized_specificity,
        "ambiguous": False,
    }
    existing = calls.get(stable_id)
    existing_rank = _SPECIFICITY_RANK.get(str((existing or {}).get("specificity")), 0)
    if existing is None:
        calls[stable_id] = incoming
    elif existing.get("ambiguous") and rank <= existing_rank:
        return
    elif canonical_name is None:
        return
    elif existing.get("name") is None or rank > existing_rank:
        calls[stable_id] = incoming
    elif rank == existing_rank and existing.get("name") != canonical_name:
        calls[stable_id] = {
            "name": None,
            "specificity": normalized_specificity,
            "ambiguous": True,
        }


def canonical_mcp_tool_name(invocation: Any) -> str | None:
    if not isinstance(invocation, Mapping):
        return None
    server = _nonempty(invocation.get("server"))
    tool = _nonempty(invocation.get("tool"))
    return f"{server}/{tool}" if server and tool else tool
```

Implement `build_activity_insights()` in the same module with these exact stages:

1. Count missing `activity` rows only when `missing` is true.
2. Ignore activity records whose `version != 1` or `is_primary` is false.
3. Group remaining records by non-empty normalized `session_id`.
4. Merge non-ambiguous turn/call entries through the two record functions so cross-file conflicts use the same rules. When a source entry already has `ambiguous: true`, copy that ambiguity into the merged stable ID directly; never reinterpret its `None` value as a missing-effort or missing-name record.
5. Sum missing-ID/effort counters once per file record.
6. Count chats only for merged sessions with at least one `has_explicit_session_id` record.
7. Sort distributions by `(-count, raw_name)` and round shares to six decimal places.
8. Return the approved `scope`, metrics, coverage, and a UTC ISO-8601 `timestamp`.

- [ ] **Step 4: Run semantic tests**

Run:

```bash
pytest -q tests/test_activity_insights.py
```

Expected: all Task 1 tests pass.

- [ ] **Step 5: Commit the semantics unit**

```bash
git add src/tokdash/activity_insights.py tests/test_activity_insights.py
git commit -m "feat: add Codex activity insight semantics"
```

---

## Task 2: Extract activity in the existing Codex parser pass

**Interfaces changed in this task**

- `_parse_codex_session_file(...)` may return a primary record with `turns == []` and private `_activity` metadata.
- `_load_codex_sessions(...)` and `_session_records_to_raw_sessions(...)` continue exposing only records with token turns.
- No extra file-open loop is added.

- [ ] **Step 1: Add failing parser and compatibility tests**

Append to `tests/test_activity_insights.py` using the repository's JSONL writer style:

```python
import json

from tokdash import sessions as sessions_module


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _parse(path):
    stat = path.stat()
    return sessions_module._parse_codex_session_file(
        str(path), stat.st_mtime_ns, stat.st_size, ()
    )


def test_codex_parser_collects_activity_without_private_payload_content(tmp_path):
    path = tmp_path / "root.jsonl"
    _write_jsonl(path, [
        {"type": "session_meta", "payload": {"id": "chat-1"}},
        {"type": "turn_context", "payload": {
            "turn_id": "turn-1", "effort": "xhigh", "model": "gpt-5.3-codex"
        }},
        {"type": "response_item", "payload": {
            "type": "function_call", "call_id": "call-1", "name": "exec",
            "arguments": "SECRET-ARGUMENT"
        }},
        {"type": "event_msg", "payload": {
            "type": "mcp_tool_call_end", "call_id": "call-1",
            "invocation": {"server": "browser", "tool": "click"},
            "result": "SECRET-RESULT"
        }},
    ])

    raw = _parse(path)

    assert raw["session_id"] == "chat-1"
    assert raw["turns"] == []
    assert raw["_activity"]["is_primary"] is True
    assert raw["_activity"]["reasoning_by_turn_id"]["turn-1"] == "xhigh"
    assert raw["_activity"]["tool_by_call_id"]["call-1"]["name"] == "browser/click"
    assert "SECRET" not in json.dumps(raw["_activity"])


def test_codex_parser_marks_subagent_from_first_session_meta(tmp_path):
    path = tmp_path / "subagent.jsonl"
    _write_jsonl(path, [
        {"type": "session_meta", "payload": {
            "id": "sub-1",
            "source": {"subagent": {"thread_spawn": {"parent_thread_id": "root-1"}}},
        }},
        {"type": "turn_context", "payload": {"turn_id": "turn-1", "effort": "high"}},
    ])

    assert _parse(path) is None


def test_empty_primary_activity_record_stays_out_of_sessions_conversion():
    raw = {
        "tool": "codex",
        "session_id": "empty",
        "project": "unknown",
        "turns": [],
        "_activity": new_activity_record(is_primary=True, has_explicit_session_id=True),
    }

    assert sessions_module._session_records_to_raw_sessions("codex", [raw]) == {}
```

Also add one fixture with duplicate `response_item` status records, a failed `mcp_tool_call_end`, missing IDs, `tool_search_call`, and `web_search_call`; assert attempts are counted exactly once and failures remain included.

- [ ] **Step 2: Run focused tests and confirm failures**

```bash
pytest -q tests/test_activity_insights.py
```

Expected: parser activity assertions fail because `_activity` is absent and empty primary files currently return `None`.

- [ ] **Step 3: Wire extraction into `_parse_codex_session_file()`**

Import the Task 1 helpers in `src/tokdash/sessions.py`. Initialize activity before opening the file, update `has_explicit_session_id` and `is_primary` when the first `session_meta` is seen, and inspect records before the existing token-event `continue` gates:

```python
activity = new_activity_record(is_primary=True, has_explicit_session_id=False)

# Inside session_meta handling, only for the first session_meta:
activity["has_explicit_session_id"] = bool(str(meta_id).strip()) if meta_id is not None else False
activity["is_primary"] = not is_subagent_file

# Inside turn_context handling:
record_reasoning_turn(
    activity,
    turn_id=payload.get("turn_id"),
    effort=payload.get("effort"),
)

# Before the token_count-only event gate:
if obj_type == "response_item" and payload_type in {
    "function_call", "custom_tool_call", "tool_search_call", "web_search_call"
}:
    fixed_name = {
        "tool_search_call": "tool_search",
        "web_search_call": "web_search",
    }.get(payload_type)
    record_structured_tool_call(
        activity,
        call_id=payload.get("call_id") or payload.get("id"),
        name=fixed_name or payload.get("name"),
        specificity="top_level",
    )
elif obj_type == "event_msg" and payload_type == "mcp_tool_call_end":
    record_structured_tool_call(
        activity,
        call_id=payload.get("call_id") or payload.get("id"),
        name=canonical_mcp_tool_name(payload.get("invocation")),
        specificity="mcp",
    )
```

Do not export `_nonempty`; set the explicit-ID flag directly from `meta_id`. At return time:

```python
if not turns and not activity["is_primary"]:
    return None

return {
    # existing public session fields unchanged
    "turns": turns,
    "_activity": activity,
}
```

Filter `not raw.get("turns")` in `_load_codex_sessions()` and before Codex merge in `_session_records_to_raw_sessions()`. Do not change `_merge_raw_session()`.

- [ ] **Step 4: Run parser plus existing session tests**

```bash
pytest -q tests/test_activity_insights.py tests/test_usage_store.py -k "activity or codex or session"
```

Expected: new parser tests and existing Codex session merge/display tests pass.

- [ ] **Step 5: Commit the parser unit**

```bash
git add src/tokdash/sessions.py tests/test_activity_insights.py
git commit -m "feat: extract Codex activity during session parsing"
```

---

## Task 3: Persist compact activity and support write-free fallback

**Interfaces introduced in this task**

```python
class UsageEntryStore:
    def query_session_activity_records(self, tool: str) -> list[dict[str, Any]]: ...

def get_codex_activity_insights() -> dict[str, Any]: ...
```

- [ ] **Step 1: Add failing schema, migration, privacy, and warm-cache tests**

In `tests/test_usage_store.py`, add tests that:

```python
def test_session_activity_is_stored_separately_from_session_raw_json(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    path = str(tmp_path / "session.jsonl")
    activity = new_activity_record(is_primary=True, has_explicit_session_id=True)
    record_structured_tool_call(
        activity, call_id="opaque", name="exec", specificity="top_level"
    )

    store.sync_session_files(
        "codex",
        ((path, 1, 100),),
        parser={"v": 2},
        parse_file_session=lambda _sig: {
            "tool": "codex",
            "session_id": "chat-1",
            "turns": [],
            "_activity": activity,
        },
    )

    with sqlite3.connect(store.path) as conn:
        raw_json, activity_json = conn.execute(
            "SELECT raw_json, activity_json FROM session_records"
        ).fetchone()
    assert "_activity" not in raw_json
    assert json.loads(activity_json)["tool_by_call_id"]["opaque"]["name"] == "exec"
    assert store.query_session_activity_records("codex") == [{
        "session_id": "chat-1",
        "file_path": path,
        "missing": False,
        "activity": activity,
    }]
```

Add separate tests for:

- opening a hand-built schema-version-5 database adds nullable `activity_json`, preserves `raw_json`, and records schema version `6`;
- a changed file invokes the parser once, while the identical second sync invokes it zero times;
- a durable missing schema-5 row remains present with `activity is None`;
- monkeypatching `json.loads` to fail on a sentinel stored only in `raw_json` does not break `query_session_activity_records()`.

In `tests/test_activity_insights.py`, add two loader tests:

```python
def test_codex_activity_warm_persistent_load_does_not_reparse(monkeypatch, tmp_path): ...
def test_codex_activity_warm_store_disabled_load_does_not_reparse(monkeypatch, tmp_path): ...
```

For the second test, set `TOKDASH_USAGE_DB=0`, point `clientpaths.codex_sessions_dir()` at the fixture directory, wrap `_parse_codex_session_file` with a call counter before the first load, and assert the count is unchanged after the second load. Also assert no SQLite file is created beneath the temporary home.

- [ ] **Step 2: Run focused tests and confirm schema/query failures**

```bash
pytest -q tests/test_usage_store.py tests/test_activity_insights.py -k "activity or session_activity"
```

Expected: failures mention missing `activity_json`, missing `query_session_activity_records`, and missing `get_codex_activity_insights`.

- [ ] **Step 3: Add schema version 6 and the narrow query**

In `src/tokdash/usage_store.py`:

```python
SCHEMA_VERSION = 6
```

Add `activity_json TEXT` to new `session_records` DDL and additive migration:

```python
if "activity_json" not in session_columns:
    conn.execute("ALTER TABLE session_records ADD COLUMN activity_json TEXT")
```

Split each parsed record without mutating the parser's cached dictionary:

```python
activity = raw.get("_activity") if isinstance(raw.get("_activity"), dict) else None
session_raw = {key: value for key, value in raw.items() if key != "_activity"}
```

Extend the `INSERT` with nullable `activity_json`, passing `stable_json(activity)` only when present. Implement the narrow query with no `raw_json` selection:

```python
def query_session_activity_records(self, tool: str) -> list[dict[str, Any]]:
    with closing(self._connect()) as conn:
        rows = conn.execute(
            """
            SELECT session_id, file_path, missing, activity_json
            FROM session_records
            WHERE tool = ?
            ORDER BY file_path ASC, session_id ASC
            """,
            (tool,),
        ).fetchall()
    result = []
    for row in rows:
        activity = None
        if row["activity_json"]:
            try:
                parsed = json.loads(row["activity_json"])
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                activity = parsed
        result.append({
            "session_id": str(row["session_id"]),
            "file_path": str(row["file_path"]),
            "missing": bool(row["missing"]),
            "activity": activity,
        })
    return result
```

- [ ] **Step 4: Add persistent and store-disabled activity loaders**

In `src/tokdash/sessions.py`, add a cache whose key is the full file signature plus parser/pricing signature:

```python
@lru_cache(maxsize=8)
def _load_codex_activity_records(
    signature: tuple[tuple[str, int, int], ...], pricing_sig: tuple = ()
) -> tuple[dict[str, Any], ...]:
    rows = []
    for path_str, mtime_ns, size in signature:
        raw = _parse_codex_session_file(path_str, mtime_ns, size, pricing_sig)
        if not raw:
            continue
        rows.append({
            "session_id": str(raw.get("session_id") or Path(path_str).stem),
            "file_path": path_str,
            "missing": False,
            "activity": raw.get("_activity"),
        })
    return tuple(rows)
```

Implement the public service function:

```python
def get_codex_activity_insights() -> Dict[str, Any]:
    signatures = _iter_file_signatures(clientpaths.codex_sessions_dir())
    pricing_sig = _pricing_signature()
    if not persistent_usage_db_enabled():
        return build_activity_insights(
            _load_codex_activity_records(signatures, pricing_sig)
        )

    store = UsageEntryStore()
    parser_sig = {
        "parser": parser_code_signature(_parse_codex_session_file),
        "activity": parser_code_signature(build_activity_insights),
        "pricing": pricing_sig,
    }
    store.sync_session_files(
        "codex",
        signatures,
        parser=parser_sig,
        parse_file_session=lambda file_sig: _parse_codex_session_file(*file_sig, pricing_sig),
    )
    return build_activity_insights(store.query_session_activity_records("codex"))
```

Include the extraction/canonicalization helpers in `parser_sig` individually if `parser_code_signature(build_activity_insights)` does not transitively change when those helpers change. Clear `_load_codex_activity_records` in the existing parser-cache test helper.

- [ ] **Step 5: Run store and warm-cache tests**

```bash
pytest -q tests/test_usage_store.py tests/test_activity_insights.py
```

Expected: all tests pass, including zero new parser calls on both warm paths and no store-disabled database write.

- [ ] **Step 6: Commit the storage/loader unit**

```bash
git add src/tokdash/usage_store.py src/tokdash/sessions.py tests/test_usage_store.py tests/test_activity_insights.py
git commit -m "feat: cache compact Codex activity records"
```

---

## Task 4: Expose the cached Activity Insights API

**Interface introduced in this task**

```http
GET /api/activity-insights
```

- [ ] **Step 1: Add failing API shape and isolation tests**

Update the `synthetic_api_data` fixture in `tests/test_api_smoke.py` to monkeypatch `api.get_codex_activity_insights`. Add:

```python
def test_activity_insights_endpoint_shape(synthetic_api_data):
    result = api.get_activity_insights()

    assert result["scope"] == {
        "tool": "codex", "local": True, "primary_only": True
    }
    assert result["recorded_chats"]["value"] == 3
    assert result["reasoning"]["most_used"]["effort"] == "xhigh"
    assert result["tools"]["total_calls"] == 8
    assert result["tools"]["distribution"][0]["name"] == "exec"


def test_activity_insights_failure_does_not_change_stats(monkeypatch):
    expected = {"stats": {"total_tokens": 10}, "contributions": []}
    monkeypatch.setattr(api, "compute_stats", lambda _year=None: expected)
    monkeypatch.setattr(
        api,
        "get_codex_activity_insights",
        lambda: (_ for _ in ()).throw(RuntimeError("activity unavailable")),
    )

    with pytest.raises(HTTPException) as exc:
        api.get_activity_insights()
    assert exc.value.status_code == 500
    assert api.get_stats() == expected
```

Patch `_cached_route` in one test to assert route name `/api/activity-insights` and a stable versioned key such as `activity_insights_v1`.

- [ ] **Step 2: Run API tests and confirm the missing route function**

```bash
pytest -q tests/test_api_smoke.py -k "activity_insights"
```

Expected: failures because `api.get_activity_insights` does not exist.

- [ ] **Step 3: Implement the endpoint without touching `/api/stats`**

Import `get_codex_activity_insights` from `sessions.py` and add beside `get_stats`:

```python
@app.get("/api/activity-insights")
def get_activity_insights() -> Dict[str, Any]:
    try:
        return _cached_route(
            "/api/activity-insights",
            "activity_insights_v1",
            get_codex_activity_insights,
        )
    except CacheBackpressureError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
```

Do not add fields to `/api/stats`, do not make one endpoint call the other, and do not wrap activity failure into the Stats response.

- [ ] **Step 4: Run API plus backend regressions**

```bash
pytest -q tests/test_api_smoke.py tests/test_activity_insights.py tests/test_usage_store.py
```

Expected: all pass.

- [ ] **Step 5: Commit the API unit**

```bash
git add src/tokdash/api.py tests/test_api_smoke.py
git commit -m "feat: expose local Codex activity insights"
```

---

## Task 5: Render the shared Profile and Overview experience

**Frontend state introduced in this task**

```javascript
const activityInsightsState = {
  status: 'idle',
  data: null,
  promise: null,
};

function loadActivityInsights(options = {}) { ... }
function renderActivityInsights() { ... }
function renderProfileActivityInsights() { ... }
function renderOverviewActivityInsights() { ... }
```

- [ ] **Step 1: Add failing markup, fetch, rendering, and localization contracts**

Append focused tests to `tests/test_profile_stats_frontend.py`:

```python
def test_activity_insights_profile_and_overview_markup_contract():
    source = INDEX_HTML.read_text(encoding="utf-8")
    for element_id in (
        "profileActivityInsights", "profileActivityInsightsKpis",
        "profileActivityCoverage", "profileActivityToolRanking",
        "overviewActivityInsights", "overviewActivityInsightsValues",
    ):
        assert f'id="{element_id}"' in source
    assert source.index('id="profileActivityLegend"') < source.index(
        'id="profileActivityInsights"'
    )
    assert source.index('id="overviewProfileLegend"') < source.index(
        'id="overviewActivityInsights"'
    )


def test_activity_insights_use_one_shared_fetch_and_limit_profile_to_five_tools():
    source = INDEX_HTML.read_text(encoding="utf-8")
    loader = _extract_js_function(source, "async function loadActivityInsights(options = {}) {")
    profile = _extract_js_function(source, "function renderProfileActivityInsights() {")
    overview = _extract_js_function(source, "function renderOverviewActivityInsights() {")

    assert source.count("fetchJsonWithRetry(appPath('/api/activity-insights')") == 1
    assert "activityInsightsState.promise" in loader
    assert ".slice(0, 5)" in profile
    assert "fetch(" not in profile
    assert "fetch(" not in overview


def test_activity_insights_localization_and_responsive_contract():
    source = INDEX_HTML.read_text(encoding="utf-8")
    compact = re.sub(r"\s+", "", source)
    for key in (
        "activityInsightsTitle", "activityRecordedChats", "activityReasoning",
        "activityToolCalls", "activityTopTool", "activityCoverage",
        "activityNoData", "activityUnavailable", "effortXhigh",
    ):
        assert source.count(f"{key}: '") == 2
    assert "@media(max-width:768px)" in compact
    assert ".profile-activity-insights-kpis{grid-template-columns:repeat(2,minmax(0,1fr))" in compact
```

Add a Node harness test for `formatActivityShare`, raw effort mapping, loading/empty/partial/error state models, and HTML escaping of arbitrary raw tool names. Assert `xhigh` displays as localized “Extra High”/“超高”, but unknown raw effort strings are rendered safely rather than dropped.

- [ ] **Step 2: Run frontend tests and confirm missing-contract failures**

```bash
pytest -q tests/test_profile_stats_frontend.py -k "activity_insights"
```

Expected: failures for missing IDs/functions/translations.

- [ ] **Step 3: Add semantic HTML in the approved positions**

Below `profileActivityLegend`, add a section with a heading, a four-cell `<dl>`, a coverage `<p aria-live="polite">`, and an ordered ranking list. Below `overviewProfileLegend`, add only a quiet four-cell `<dl>` ribbon plus one muted state line. Both shells start with `data-state="loading"` and `aria-busy="true"`.

Use these IDs exactly:

```html
<section id="profileActivityInsights" class="profile-activity-insights" data-state="loading" aria-busy="true">
  <h3 data-i18n="activityInsightsTitle">Activity insights</h3>
  <div id="profileActivityInsightsKpis" class="profile-activity-insights-kpis"></div>
  <div class="profile-activity-insights-detail">
    <p id="profileActivityCoverage" class="profile-activity-insights-coverage" aria-live="polite"></p>
    <ol id="profileActivityToolRanking" class="profile-activity-tool-ranking"></ol>
  </div>
</section>
```

```html
<section id="overviewActivityInsights" class="overview-activity-insights" data-state="loading" aria-busy="true">
  <dl id="overviewActivityInsightsValues" class="overview-activity-insights-values"></dl>
  <p id="overviewActivityInsightsState" class="overview-activity-insights-state" aria-live="polite"></p>
</section>
```

- [ ] **Step 4: Implement one shared request and isolated states**

Use a single in-memory state/promise:

```javascript
async function loadActivityInsights(options = {}) {
  const force = Boolean(options.force);
  if (!force && activityInsightsState.data) {
    renderActivityInsights();
    return activityInsightsState.data;
  }
  if (!force && activityInsightsState.promise) return activityInsightsState.promise;

  activityInsightsState.status = 'loading';
  renderActivityInsights();
  const request = fetchJsonWithRetry(appPath('/api/activity-insights'))
    .then((data) => {
      activityInsightsState.data = data;
      activityInsightsState.status = data?.recorded_chats?.value ? 'ready' : 'empty';
      renderActivityInsights();
      return data;
    })
    .catch((error) => {
      activityInsightsState.status = 'error';
      renderActivityInsights();
      throw error;
    })
    .finally(() => {
      if (activityInsightsState.promise === request) activityInsightsState.promise = null;
    });
  activityInsightsState.promise = request;
  return request;
}
```

Call `loadActivityInsights().catch(() => {})` from the same startup/navigation path that warms or loads Profile stats. Do not block `loadStats()`, `renderProfileView()`, or `renderOverviewProfilePreview()` on it. Call `renderActivityInsights()` from `applyI18n()` so effort labels and state copy update immediately when language changes.

Build DOM using `createElement()` and `textContent`; never concatenate untrusted tool names into `innerHTML`. Profile renders all four KPI peers, coverage, and `distribution.slice(0, 5)`. Overview reads the same `activityInsightsState.data` and renders only four quiet values. When data is partial, use `—` for absent most-used values while preserving known totals and coverage.

- [ ] **Step 5: Style without resizing either heatmap**

Reuse theme variables and existing radii. The Profile section may use a subtle top divider; the Overview ribbon must have no outer competing card/background. Required layout behavior:

```css
.profile-activity-insights-kpis {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
}
.profile-activity-insights-detail {
  display: grid;
  grid-template-columns: minmax(220px, .8fr) minmax(320px, 1.2fr);
}
.overview-activity-insights-values {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  border-top: 1px solid var(--color-border);
}
@media (max-width: 768px) {
  .profile-activity-insights-kpis,
  .overview-activity-insights-values {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .profile-activity-insights-detail {
    grid-template-columns: minmax(0, 1fr);
  }
}
```

Do not modify `.profile-activity-grid-wrap`, `.overview-profile-grid-wrap`, scroller padding, cell dimensions, gap, or heatmap min-width rules.

- [ ] **Step 6: Run all frontend and API tests**

```bash
pytest -q tests/test_profile_stats_frontend.py tests/test_api_smoke.py
```

Expected: all pass, including existing geometry, tooltip, milestone, and single-stats-fetch contracts.

- [ ] **Step 7: Commit the complete UI unit**

```bash
git add src/tokdash/static/index.html tests/test_profile_stats_frontend.py
git commit -m "feat: show Codex activity insights in Profile"
```

---

## Task 6: Document scope and complete regression verification

**Behavioral impact to document**

- New Activity Insights count all locally recorded primary Codex chats, including empty/interrupted primary files.
- Empty primary files remain absent from Sessions responses.
- Reasoning/tool values count only explicit structured records with stable IDs; ambiguous/missing data is coverage, not an estimate.
- Subagents and inferred skills/plugins remain excluded.
- Existing databases reparse present files once after schema/parser-signature change; unchanged warm requests do not reparse.
- Durable legacy records whose files were already missing cannot be reconstructed and appear only as unavailable coverage.
- Store-disabled mode remains write-free.

- [ ] **Step 1: Add the changelog entry**

Under the current unreleased section in `docs/development/CHANGELOG.md`, add one concise feature bullet and one scope note. Use wording equivalent to:

```markdown
- Add local Codex Activity Insights for recorded primary chats, explicit reasoning effort,
  and structured tool calls in Profile and Overview.
  Counts are deduplicated from local structured records; subagents, inferred skills/plugins,
  and unavailable legacy files are excluded and reflected in coverage.
```

- [ ] **Step 2: Run the focused feature suite**

```bash
pytest -q tests/test_activity_insights.py tests/test_usage_store.py tests/test_api_smoke.py tests/test_profile_stats_frontend.py
```

Expected: exit code 0.

- [ ] **Step 3: Run the full repository suite**

```bash
pytest -q
```

Expected: exit code 0. If any pre-existing failure remains, record its exact node ID and diagnostic; do not hide it.

- [ ] **Step 4: Run the relevant type check and compare it with the baseline**

The pre-feature baseline command is:

```bash
.venv/bin/python -m mypy src/tokdash/sessions.py src/tokdash/usage_store.py src/tokdash/api.py
```

It currently reports 14 errors: six `int(object)` `call-overload` errors in `usage_store.py`, one missing annotation plus six optional-dictionary `union-attr` errors in `sessions.py`, and one lambda-inference error in `api.py`. Run the expanded final command:

```bash
.venv/bin/python -m mypy src/tokdash/activity_insights.py src/tokdash/sessions.py src/tokdash/usage_store.py src/tokdash/api.py
```

Expected: `activity_insights.py` adds no error and the implementation adds no new error category or affected expression. If the existing errors remain, preserve the complete final output for the PR verification as required by the project instructions; do not claim the type check passed.

- [ ] **Step 5: Run Python compilation and confirm the lack of a repository-configured type-check command**

```bash
python -m compileall -q src
```

Expected: exit code 0 with no output. Also verify the repository still has no configured static type checker:

```bash
rg -n "mypy|pyright|typecheck|type-check" pyproject.toml requirements-dev.txt .github scripts
```

Expected: no configured command or dependency. Report this exact limitation alongside the explicit local mypy result; the Node-backed frontend checks are already exercised through pytest.

- [ ] **Step 6: Audit compatibility and privacy from the final diff**

Run:

```bash
git diff origin/main...HEAD -- src/tokdash/api.py src/tokdash/sessions.py src/tokdash/usage_store.py src/tokdash/activity_insights.py src/tokdash/static/index.html
git status --short
```

Confirm before continuing:

- `/api/stats` implementation and response construction are unchanged;
- no second JSONL scan or message-content inference exists;
- `query_session_activity_records()` never selects `raw_json`;
- no private `_activity`, opaque turn ID, or call ID reaches the API;
- empty-session filtering protects Sessions;
- no store-disabled database writes occur;
- `.superpowers/` is still untracked and unstaged;
- only intended files are staged.

- [ ] **Step 7: Commit documentation and any test-only final adjustments**

```bash
git add docs/development/CHANGELOG.md
git commit -m "docs: describe Codex activity insight scope"
```

- [ ] **Step 8: Prepare PR verification text, but do not push or open the PR without user confirmation**

Include:

```markdown
## What changed
- Added compact, single-pass Codex activity indexing and exact resumed-history deduplication.
- Added `/api/activity-insights` with explicit coverage and no opaque IDs.
- Added complete Profile insights plus the approved quiet Overview ribbon using one shared response.

## Compatibility
- `/api/stats`, Sessions token rows, heatmaps, modes, and milestones keep their prior behavior.
- Empty primary chats are counted only by Activity Insights and remain hidden from Sessions.
- Existing present files rebuild once; unchanged warm requests perform zero parser calls.

## Verification
- `pytest -q tests/test_activity_insights.py tests/test_usage_store.py tests/test_api_smoke.py tests/test_profile_stats_frontend.py`
- `pytest -q`
- `.venv/bin/python -m mypy src/tokdash/activity_insights.py src/tokdash/sessions.py src/tokdash/usage_store.py src/tokdash/api.py` (report all remaining baseline diagnostics exactly)
- `python -m compileall -q src`
- No mypy/pyright/type-check command is configured or declared by the repository; the explicit workspace mypy check above is supplemental.
```
