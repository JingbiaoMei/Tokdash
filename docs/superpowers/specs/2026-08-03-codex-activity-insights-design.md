# Codex Activity Insights Design

**Date:** 2026-08-03

**Issue:** [#14 — Add accurate local Codex Activity Insights](https://github.com/JingbiaoMei/Tokdash/issues/14)

**Status:** Approved for implementation planning

## Summary

Add local-first Codex Activity Insights to the existing Profile experience. The feature reports only values that can be derived from explicit structured Codex records:

- locally recorded primary chats;
- most-used reasoning effort;
- total structured tool calls; and
- most-used structured tools.

The complete insight section appears below the token-activity heatmap in the Profile view. A quiet four-value ribbon appears below the compact heatmap and legend in the Overview Profile band. Both surfaces use one shared API response.

The parser extends the existing per-file session sync. It does not add a second log scanner, read message content, retain tool arguments, or infer skills/plugins from text. Unchanged files are not reparsed. A parser-signature change may trigger one rebuild after upgrade; subsequent warm requests over an unchanged corpus invoke the session-file parser zero times.

## Goals

1. Show accurate local activity metrics based only on explicit structured fields.
2. Exclude subagent activity from every v1 metric.
3. Deduplicate resumed/replayed Codex history across files without relying on timestamps.
4. Preserve current Profile, Overview, Sessions, and `/api/stats` behavior.
5. Keep warm Profile and Overview loads cache-backed and low latency.
6. Communicate local-history scope and incomplete coverage honestly.

## Non-goals

- Fast Mode percentage. Its source and denominator remain deferred.
- Skill, plugin, or app usage inferred from `$name`, `@name`, prompts, or generic tools.
- Account-lifetime or cloud analytics.
- Prompt, response, tool-argument, credential, or secret storage.
- Activity insights for non-Codex clients in v1.
- A new filter, date-range selector, drill-down page, or tool-call history viewer.

## Confirmed product decisions

- Count all locally recorded primary/root chats, including interrupted chats with no token event.
- Count no subagent files in v1 metrics.
- Use the existing durable-store semantics after an activity record has been indexed: retained activity remains visible when its source file later disappears. A legacy file already missing during the upgrade rebuild is the explicit exception and appears only in unavailable coverage.
- Place complete insights in Profile and a quiet summary ribbon in Overview.
- Use the approved “Quiet ribbon” layout rather than a split card or tag-cloud layout.
- Render the top five tools in Profile. Other tools remain included in totals and percentages.

## Metric contract

### Shared scope and identity

A file is a subagent file only when its first `session_meta.payload.source` contains the structured `subagent.thread_spawn` marker already used by the Codex session parser. Every activity entry from such a file is excluded.

Primary records use the parser's normalized, last-seen `session_id`. Multiple cached files with the same normalized session ID form one logical chat and are merged before aggregation. Opaque `turn_id` and `call_id`/`id` values are retained only inside the compact cache to deduplicate replayed records; they are never returned by the API.

### Locally recorded chats

**Source:** primary rollout records with an explicit `session_meta.payload.id`.

**Formula:** count distinct normalized session IDs across activity-ready primary records.

A primary record with no token turns still participates in Activity Insights. It remains excluded from the existing Sessions list so the Sessions UI does not gain empty rows.

**Coverage:** report activity-ready primary files, primary files with an explicit session ID, and legacy durable records that lack activity metadata because their source file disappeared before the upgrade rebuild.

### Most-used reasoning effort

**Source:** `turn_context.payload.turn_id` and non-empty `turn_context.payload.effort` from primary files.

The stable key is `(normalized_session_id, turn_id)`. Duplicate copies of the same turn collapse across resumed files. If duplicates disagree on effort, mark that turn ambiguous and exclude it from the distribution rather than choosing a value.

**Formula:**

1. Count each unambiguous effort value once per stable turn key.
2. `known_effort_turns = sum(effort_counts)`.
3. The most-used effort is the highest count; ties use raw-value ascending order.
4. `most_used_share = winning_count / known_effort_turns`.

The API keeps raw effort values. Display mapping such as `xhigh` to “Extra High” belongs to the localized frontend.

**Coverage:** return identified turn contexts, known-effort turns, ambiguous turns, and records excluded because they lacked a stable turn ID or effort.

### Total structured tool calls

Explicit call sources are:

- `response_item` with `payload.type` equal to `function_call`, `custom_tool_call`, `tool_search_call`, or `web_search_call`; and
- `event_msg` with `payload.type == "mcp_tool_call_end"`.

The stable key is `(normalized_session_id, payload.call_id || payload.id)`. Repeated status/update records and resumed-history copies collapse to one call. A top-level call and MCP completion sharing a key count once. Attempts count regardless of success or failure. Explicit call records without a stable ID are excluded from totals and reported in coverage.

**Formula:** count distinct stable call keys after merging all primary records for each normalized session.

### Most-used structured tools

Each deduplicated call receives one canonical name:

1. Prefer explicit `invocation.server/invocation.tool` from an MCP completion.
2. Otherwise use explicit `payload.name` from a function or custom-tool call.
3. Use `tool_search` and `web_search` for those explicit call types.
4. A fully qualified MCP name wins when the same call key also has a top-level name.
5. Conflicting names at equal specificity make the name ambiguous. The call remains in the total but is excluded from the tool distribution.

**Formula:** group named unique calls by canonical name, sort by count descending then name ascending, and compute each share over all uniquely identified calls with an unambiguous canonical name.

**Coverage:** return the named-call denominator, ambiguous-name calls, and explicit call records excluded for missing stable IDs.

## Architecture

### Parser and in-memory activity record

Extend the existing Codex per-file parser in `src/tokdash/sessions.py`. During the same line-by-line pass that builds token turns, collect a private activity record:

```json
{
  "version": 1,
  "is_primary": true,
  "has_explicit_session_id": true,
  "reasoning_by_turn_id": {
    "opaque-turn-id": "xhigh"
  },
  "tool_by_call_id": {
    "opaque-call-id": {
      "name": "node_repl/js",
      "specificity": "mcp"
    }
  },
  "turn_records_missing_id": 0,
  "turn_records_missing_effort": 0,
  "tool_records_missing_id": 0
}
```

This record contains no prompt, response, tool arguments, MCP results, credentials, or secret-bearing data. Stable IDs and canonical names are the minimum information required to merge exact activity across resumed files in the v1.5.5 session model.

The parser returns a primary record even when it has no token turns so interrupted chats can be counted. Subagent files with no own token turns may retain the current `None` behavior because they are outside the metric scope. Session loaders explicitly filter zero-turn records before producing Sessions responses, preserving existing behavior.

### Persistent storage

Add a nullable `activity_json` column to `session_records` and bump the usage database schema version. `sync_session_files()` copies the private activity record into `activity_json` without mutating the parser's cached object. Existing `raw_json` remains the session payload used by Sessions.

Add a narrow query that selects only `session_id`, `file_path`, `missing`, and `activity_json` for Codex activity aggregation. Profile loads must not deserialize stored turns.

The parser signature includes the activity parsing/canonicalization helpers. On upgrade, present files reparse once. A durable row whose file was already missing cannot be reconstructed and keeps `activity_json = NULL`; the aggregator excludes it and reports it in coverage instead of guessing.

### Store-disabled fallback

Respect `TOKDASH_USAGE_DB=0`. In this mode, do not create or update the database. Aggregate activity from the same per-file parser through an LRU loader keyed by the full file-signature tuple. The first request may parse present files; an unchanged warm request returns from the loader cache and performs zero session-file parser calls.

### Merge and aggregation module

Keep activity merge, conflict resolution, distribution sorting, and public-response construction in a focused module, `src/tokdash/activity_insights.py`. `sessions.py` owns extraction because it already performs the single file scan; `usage_store.py` owns persistence; the new module owns activity semantics.

Merging occurs in two levels:

1. merge activity records by normalized session ID using stable turn and call IDs; then
2. aggregate the merged primary sessions into counts, distributions, and coverage.

This matches the v1.5.5 Codex session behavior, where multiple records with the same logical session are merged instead of overwritten.

## API

Add `GET /api/activity-insights`. V1 always returns Codex-local activity and identifies that scope explicitly. The route uses the normal API cache/backpressure handling but does not modify `/api/stats`.

Example response:

```json
{
  "scope": {
    "tool": "codex",
    "local": true,
    "primary_only": true
  },
  "recorded_chats": {
    "value": 1046,
    "coverage": {
      "primary_files": 1050,
      "files_with_session_id": 1048,
      "legacy_unavailable_records": 2
    }
  },
  "reasoning": {
    "most_used": {
      "effort": "xhigh",
      "count": 1149,
      "share": 0.48
    },
    "distribution": [
      {"effort": "xhigh", "count": 1149, "share": 0.48},
      {"effort": "high", "count": 718, "share": 0.30},
      {"effort": "medium", "count": 400, "share": 0.17},
      {"effort": "low", "count": 126, "share": 0.05}
    ],
    "coverage": {
      "identified_turns": 2402,
      "known_effort_turns": 2393,
      "ambiguous_turns": 0,
      "excluded_records": 9
    }
  },
  "tools": {
    "total_calls": 8921,
    "most_used": {
      "name": "exec",
      "count": 3301,
      "share": 0.37
    },
    "distribution": [
      {"name": "exec", "count": 3301, "share": 0.37},
      {"name": "exec_command", "count": 2006, "share": 0.23},
      {"name": "apply_patch", "count": 1800, "share": 0.20},
      {"name": "web_search", "count": 1795, "share": 0.20}
    ],
    "coverage": {
      "named_calls": 8902,
      "ambiguous_name_calls": 19,
      "excluded_records": 0
    }
  },
  "timestamp": "2026-08-03T12:00:00+00:00"
}
```

Distributions contain the complete ordered result. The Profile frontend renders the first five tools. No opaque IDs leave the backend.

## Frontend design

### Profile

Place a new “Activity insights” section below the existing token-activity heatmap and legend.

- First row: Recorded chats, Most-used reasoning, Structured tool calls, and Most-used tool.
- Second row: a compact coverage panel and a horizontal top-five tool ranking.
- Reuse existing theme tokens, typography, localization, focus treatment, and responsive breakpoints.
- At narrow widths, KPI cells wrap to two columns and the coverage/ranking row stacks.

### Overview

Add a quiet four-value ribbon below the existing compact heatmap and legend inside the Overview Profile band:

- Chats;
- Reasoning;
- Tool calls; and
- Top tool.

The ribbon uses separators and typography rather than a new competing card. It does not resize or narrow the heatmap. Values come from the same in-memory response used by Profile.

### Loading, empty, partial, and error states

- **Loading:** reserve section/ribbon height with existing skeleton conventions to avoid layout shift during the initial or upgrade rebuild.
- **No activity:** replace numeric peers with one muted “No local Codex activity yet” state; do not show fabricated zero percentages.
- **Partial coverage:** show reliable values, use an em dash for an unavailable top value, and retain the coverage caption.
- **API error:** leave all existing Profile/Overview content functional. Profile shows a quiet “Activity insights are temporarily unavailable” state; Overview omits numeric peers and shows one muted unavailable line.

Add English and Chinese strings for headings, labels, coverage text, effort display names, and states. Semantic lists/definitions and existing focus styles provide keyboard and screen-reader support.

## Performance contract

1. Do not add a second JSONL scan.
2. Parse only new or changed files through existing file signatures.
3. Do not deserialize session `raw_json` when serving Activity Insights.
4. Allow one parser-signature rebuild after upgrade.
5. An unchanged warm request must invoke the per-file session parser zero times.
6. Keep opaque identity maps in the private cache only; the API receives aggregated values.
7. Keep `/api/stats` and existing callers unchanged.

## Compatibility and behavioral impact

- The database change is additive, with a schema-version bump and nullable column.
- Existing databases rebuild activity metadata only for files still available.
- Existing durable rows remain intact; missing legacy rows are reported as unavailable coverage.
- Empty primary records become cacheable for chat counting but remain absent from Sessions responses.
- Existing token counts, session merge identities, display names, heatmaps, modes, milestones, and API fields do not change.
- `TOKDASH_USAGE_DB=0` remains write-free.
- Fast Mode, skills, and inferred plugin attribution remain absent.

## Verification plan

### Parser and semantic tests

- primary empty/interrupted file counts as a chat but not a Sessions row;
- subagent records contribute to none of the four metrics;
- resumed files with the same session, turn, and call IDs deduplicate;
- repeated status records and overlapping top-level/MCP events count once;
- conflicting reasoning effort becomes ambiguous coverage;
- canonical-name specificity and equal-specificity conflicts behave deterministically;
- missing IDs and names affect coverage exactly as documented;
- failed calls still count as attempts;
- no prompt, response, arguments, or results enter `activity_json`.

### Store and performance tests

- schema migration adds nullable `activity_json` without losing rows;
- only changed files invoke the parser;
- an unchanged warm persistent-store request invokes the parser zero times;
- an unchanged store-disabled request hits the signature-keyed LRU and invokes the parser zero times;
- durable missing legacy rows remain stored and appear only in unavailable coverage;
- activity queries do not select or deserialize `raw_json`.

### API and compatibility tests

- complete, empty, partial, and no-store responses match the contract;
- distributions and tie-breaking are deterministic;
- `/api/stats` remains byte-shape compatible apart from its existing timestamp behavior;
- Activity Insights failure does not fail the Stats route.

### Frontend tests

- Profile renders four KPIs, coverage, and at most five ranked tools;
- Overview renders the approved quiet ribbon without shrinking the heatmap;
- loading, no-data, partial, and failure states remain isolated;
- English/Chinese labels and raw-to-display effort mappings are safe;
- desktop and narrow layouts do not clip or overflow;
- one fetched response is reused by Overview and Profile.

### Commands during implementation

Run focused tests while iterating, then the repository suite:

```bash
pytest -q tests/test_usage_store.py tests/test_api_smoke.py tests/test_profile_stats_frontend.py
pytest -q
```

The repository currently has no configured static type-check command. Implementation verification will report that explicitly and will additionally run Python compilation and the existing Node-backed frontend checks exercised by pytest.

## Rollout

1. Land parser/storage/API behavior with deterministic fixtures and warm-cache regression tests.
2. Land Profile and Overview rendering against the stable API contract in the same PR.
3. Document the feature and local-history/coverage scope in the changelog or user-facing docs requested by the maintainer.
4. Keep the issue open until the maintainer validates scope and presentation in the PR.
