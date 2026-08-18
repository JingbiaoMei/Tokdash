# Codex usage counting: replay de-duplication

How Tokdash avoids double-counting Codex usage when rollout files copy an existing
thread's history. Observed against Codex CLI **0.144.1** (subagent replay),
**0.146.0** (ordinary resumed-thread replay), and **0.147.0** (single-meta fork
replay).

## The problem

Codex writes JSONL rollout files under `~/.codex/sessions/YYYY/MM/DD/` and moves
completed rollouts to `~/.codex/archived_sessions/`. Tokdash scans both roots; an
archived copy that still exists under `sessions/` too collapses via the stable
event key, so overlap cannot double-count. A later rollout can also replay token
events already present in an older file:

- A fork file replays its ancestor thread before recording its own work. Forks are
  recognized from any ancestry declaration in the first `session_meta`:
  `source.subagent.thread_spawn.parent_thread_id` (MultiAgent V2), or the
  top-level `forked_from_id` / `parent_thread_id` fields (user `/fork`, exec
  forks). Only `thread_spawn` marks a session as non-primary for activity
  purposes. The bare top-level `session_id` field is deliberately not treated as
  ancestry: no observed log (0/1009 files) carries ancestry there, and its generic
  name would risk rekeying ordinary sessions into a shared scope. Note the
  non-`thread_spawn` fork path is designed-for but not yet observed replaying in
  real logs — all 274 plain-fork files examined had zero pre-`turn_context`
  `token_count` rows — so real-data confidence sits with `thread_spawn`. Two
  replay shapes exist:
  - **0.144 shape**: the file replays the parent's `session_meta`, so the active
    session id flips to the parent's — and stays there for the subagent's own
    turns too (no second child `session_meta` is ever written).
  - **0.146+ single-meta fork shape**: the file has exactly one `session_meta`
    carrying the child's own id, and the replayed parent prefix precedes the
    child's first `turn_context`.
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

For `thread_spawn` files the scoping id is chosen so replayed segments collide
with the parent's originals:

- events recorded under the parent's id (0.144 shape) are already parent-scoped;
- events before the child's first `turn_context` in a single-meta fork file are
  keyed to the declared parent id, because that prefix is the replayed parent
  history. The child's own turns always follow its first `turn_context` and keep
  the child scope.

If the parent file was never indexed (archived or deleted), the rekeyed rows
survive under the parent scope and are counted once — replay handling never
silently drops usage. The parent scope also means a parent file indexed later
still collapses the earlier copy.

If either usage snapshot is absent, Tokdash deliberately falls back to the
file/line identity. That can visibly over-count an old or unrecognized replay,
but it cannot silently merge two genuine calls that happened to use the same
number of tokens.

Known limitation: the parent scope is the *immediate* parent. A nested
`thread_spawn` (a subagent spawning its own subagent) replays the grandparent's
prefix under the parent scope, which does not collide with the grandparent's
keys; that segment can still double-count at depth ≥ 2.

## Parser and store behavior

Both Codex parsers use the same key:

- `CodexParser._parse_all` keeps the earliest timestamp for each event key,
  independent of rollout discovery order. The Overview parser therefore retains
  the original timestamp and ignores restamped copies.
- `_parse_codex_session_file` stores the key on each internal turn. Codex session
  records with the same logical id are merged, and duplicate turns are removed by
  event key instead of allowing the later file to overwrite the earlier one.
  Single-meta fork sessions keep their own id and never merge, so after loading,
  child sessions carrying `_subagent_parent_id` drop the turns whose event key
  the parent session already owns. Windowed stored reads only load sessions that
  touch the window, so the parent is additionally looked up in the store
  unbounded by time — including durable rows kept after the parent file was
  deleted. The child drops the prefix whenever the parent is indexed anywhere.
  When the parent is genuinely absent, sibling forks would each retain an
  identical copy of the prefix, so the earliest sibling keeps it and later
  siblings drop it: exactly one survivor either way.
- The API removes the internal key before returning public turn data.

The `thread_spawn` parent-id gate remains as a fallback for subagent rows that
lack cumulative snapshots (no stable key). Rows with a stable key are never
hard-skipped: in the 0.144 shape the subagent's own turns also run under the
parent's session id, and gating them would lose genuine subagent work.

## Model attribution

`turn_context` owns per-turn model attribution. Newer Codex also emits
`thread_settings_applied` before the first `turn_context`, and issue #23 reported
builds nesting the selection under `session_meta.base_instructions
.provenance.model` (unconfirmed — no real log through 0.147.0 carries that key,
but the lookup is harmless); Tokdash accepts both as early model sources. Rows
written before a file's first model signal — a fork's replayed parent prefix that
outlives an unindexed parent, or old formats — are backfilled with the file's own
first observed model instead of the placeholder default, so surviving replay rows
are never billed under a model that never ran. Files with no model signal at all
label their rows explicitly `unknown` (issue #23): token counts are kept, cost is
$0, and no usage is attributed to a model that never ran.

The placeholder (`CODEX_DEFAULT_MODEL`) is a real, selectable model, so its value
is never used as the backfill sentinel: rows carry an internal placeholder marker
from creation until the file's first model signal, and only marked rows are
backfilled. An explicit mid-file switch to the default model keeps its own
attribution (real logs contain files whose dominant model is exactly that name;
a value-equality sentinel mislabeled thousands of their rows).

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
  repeated unchanged snapshots are removed; they can also rise slightly where a
  subagent's own turns were previously skipped together with the replay.
- Usage returns to the original event date instead of the resume date.
- A resumed multi-file thread appears as one logical session. Period filters keep
  only genuine turns whose original timestamps fall inside the requested window.
- Genuine resumed work is retained even when Codex continues writing it under
  the older logical session id.
- A single-meta fork subagent session shows only its own turns when the parent
  session is indexed, and the full replayed prefix when it is not.

## Guardrails and tests

Regression coverage must keep these cases distinct:

- ordinary resumed-thread history with restamped timestamps;
- genuine new work following the replay;
- `thread_spawn` parent history and real subagent work, in both the 0.144
  replayed-`session_meta` shape and the 0.146+ single-meta fork shape;
- forks declared only via top-level `forked_from_id` / `parent_thread_id` (no
  `thread_spawn` marker) — same replay dedup, still primary;
- a mid-file switch into an explicit selection of the placeholder default model
  (explicit rows keep their attribution; only pre-signal placeholder rows are
  backfilled);
- a single-meta fork whose parent was never indexed (replay survives once,
  backfilled to the file's own model), and sibling orphans of the same parent
  (exactly one keeps the prefix);
- a windowed stored read whose window hides the parent session (replay dedup
  looks the parent up in the store, unbounded, including durable rows whose
  files were deleted);
- primary session-id changes/compaction;
- identical token amounts in different logical sessions;
- persistent-store append/resync, in-place canonical rewrites, and file removal;
- partial logs without `total_token_usage` (loud over-count fallback);
- archived-only sessions (files under `archived_sessions/` are scanned) and
  archived/live overlap (identical copies collapse by event key).

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
