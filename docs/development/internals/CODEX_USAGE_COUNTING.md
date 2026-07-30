# Codex usage counting: replay de-duplication

How Tokdash avoids double-counting Codex usage when rollout files copy an existing
thread's history. Observed against Codex CLI **0.144.1** (subagent replay) and
**0.146.0** (ordinary resumed-thread replay).

## The problem

Codex writes JSONL rollout files under `~/.codex/sessions/YYYY/MM/DD/`. A later
rollout can replay token events already present in an older file:

- A MultiAgent V2 `thread_spawn` file replays its parent thread before recording
  the subagent's own work.
- An ordinary VS Code/CLI resume can open with a new file id, switch to the older
  logical session id, and replay the full history before recording new work.

The copied events are log artifacts, not new API calls. Their timestamps may be
restamped to the new file's creation time, so timestamp/file/line identity both
double-counts the usage and moves old usage into the current day.

This affects both views:

- **Overview**: every copied `token_count` becomes another usage entry.
- **Sessions**: allowing the later file to replace the original session either
  shows the replay as today's work or loses genuine turns from another file.

## Stable event identity

Codex replay preserves three useful pieces of state:

1. The logical session id active at the `token_count` event.
2. `total_token_usage`, the cumulative session snapshot.
3. `last_token_usage`, the per-call snapshot.

Tokdash hashes that state into a `codex-token-v1:*` event key. Known numeric token
fields are normalized so a missing field and an explicit zero compare equally;
Codex 0.146 can add zero-valued fields while copying snapshots written by an older
CLI.

The logical session id scopes the key. Identical counters in two independent
sessions, or on opposite sides of a real session-id/compaction change, remain
distinct.

If either usage snapshot is absent, Tokdash deliberately falls back to the
file/line identity. That can visibly over-count an old or unrecognized replay,
but it cannot silently merge two genuine calls that happened to use the same
number of tokens.

## Parser and store behavior

Both Codex parsers use the same key:

- `CodexParser._parse_all` keeps the earliest timestamp for each event key,
  independent of rollout discovery order. The Overview parser therefore retains
  the original timestamp and ignores restamped copies.
- `_parse_codex_session_file` stores the key on each internal turn. Codex session
  records with the same logical id are merged, and duplicate turns are removed by
  event key instead of allowing the later file to overwrite the earlier one.
- The API removes the internal key before returning public turn data.

The existing `thread_spawn` parent-id gate remains as an additional fallback for
subagent files whose older token events lack cumulative snapshots.

The persistent usage store has a unique `(source, entry_key)` index. Codex file
sync resolves a duplicate in favor of the earliest timestamp, regardless of file
discovery order, so a later replay cannot move the canonical row. Normal
append-only changes still reparse only the changed file. If a full file
replacement removes a key that file previously owned, or non-durable cleanup
removes a canonical Codex file, remaining Codex files are reparsed once so a
surviving occurrence can take ownership instead of losing the usage. Rewrites
that retain all owned keys stay file-local.

## Expected consequences

- Historical Codex totals can decrease after upgrade because copied events and
  repeated unchanged snapshots are removed.
- Usage returns to the original event date instead of the resume date.
- A resumed multi-file thread appears as one logical session. Period filters keep
  only genuine turns whose original timestamps fall inside the requested window.
- Genuine resumed work is retained even when Codex continues writing it under
  the older logical session id.

## Guardrails and tests

Regression coverage must keep these cases distinct:

- ordinary resumed-thread history with restamped timestamps;
- genuine new work following the replay;
- `thread_spawn` parent history and real subagent work;
- primary session-id changes/compaction;
- identical token amounts in different logical sessions;
- persistent-store append/resync, in-place canonical rewrites, and file removal;
- partial logs without `total_token_usage` (loud over-count fallback).

`CodexParser.replay_events_skipped` counts both source-gated replay events and
stable-key duplicates so format changes remain observable.

## Operational note

The usage database is a parse cache, not a source of truth. Parser module content
is part of each stored signature, so upgrading to a parser with this logic
reparses Codex rollout files and removes previously cached copies automatically.
`tokdash db resync` remains available as a manual full rebuild but is not required.

## References

- ccusage [#950](https://github.com/ryoppippi/ccusage/issues/950) /
  [#1218](https://github.com/ccusage/ccusage/pull/1218)
- Codex subagents: <https://developers.openai.com/codex/concepts/subagents>
