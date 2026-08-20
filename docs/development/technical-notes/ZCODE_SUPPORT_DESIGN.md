# ZCode support design

ZCode (Z.ai's GLM coding app) persists every model request in a local SQLite
database. Tokdash reads it read-only, one entry per `model_usage` row, and a
phase 2 extension reads `turn_usage` for the Session Explorer. This note
records the storage layout, the token-accounting rules the parser must
follow, the session-support decisions, and what is deliberately out of
scope.

## Storage

- Database: `$ZCODE_HOME/cli/db/db.sqlite`, default `~/.zcode/cli/db/db.sqlite`
  (`%USERPROFILE%\.zcode\cli\db\db.sqlite` on Windows). `ZCODE_HOME` is
  honored by ZCode itself, so the reader follows an overridden home.
- WAL mode: live rows accumulate in `db.sqlite-wal` between checkpoints, with
  `db.sqlite-shm` alongside. A read-only WAL reader is safe while the app runs.
- Timestamps are epoch milliseconds.

Key tables:

| Table | Role |
| --- | --- |
| `model_usage` | One row per model request; the cost and token source |
| `turn_usage` | Per-(session, turn) aggregate; read by the session
  loader (phase 2) for timing, status and turn-level counts |
| `session` | id, title, directory, parent_id (subagents); read by the
  session loader (phase 2) |
| `tool_usage` | Per tool call; not read (turn-level `tool_call_count` covers the display need) |
| `message` / `part` | Conversation text; not read by Tokdash |

`model_usage` columns the parser uses:

```
id                            text primary key
session_id / turn_id / trace  identifiers
provider_id                   e.g. builtin:zai-start-plan (label only)
model_id                      e.g. GLM-5-Turbo (the pricing key)
status                        running / completed / error / cancelled
started_at                    epoch ms
input_tokens                  TOTAL prompt tokens, inclusive of cache
output_tokens                 already includes reasoning_tokens
reasoning_tokens
cache_read_input_tokens       subset of input_tokens
cache_creation_input_tokens
```

Retries are distinct rows sharing a `logical_request_id` with different
`attempt_index` values, and the provider bills each attempt, so Tokdash keeps
every row and keys entries on the row's own `id`.

## Token accounting

The three rules that keep ZCode totals and costs from drifting from the
source's own numbers:

1. **The cached slice is subtracted once.** `input_tokens` is inclusive of
   `cache_read_input_tokens` (observed live: 16026 input + 24 output =
   16050 computed total, with 11776 cache-read not added). `get_cost` bills
   its buckets additively, so the entry carries
   `input = max(0, input_tokens - cache_read_input_tokens)` and
   `cacheRead = cache_read_input_tokens`. This matches the Codex and Gemini
   parsers, which face the same inclusive-input shape.
2. **Reasoning is displayed disjoint but billed in full.** `output_tokens`
   already includes `reasoning_tokens`, while compute.py's displayed total
   adds `input + output + cacheRead + reasoning`. The entry therefore splits
   `output = max(0, output_tokens - reasoning_tokens)` and
   `reasoning = reasoning_tokens` for display, but cost is computed from the
   **full** `output_tokens`: `get_cost` never sees the entry's reasoning
   bucket, and z.ai bills reasoning at the output rate. If a row reports
   more reasoning than output, the subset assumption is broken for that row
   and both display and billing treat the two as disjoint, so the displayed
   total and the billed tokens stay equal. Passing the split
   output to the price lookup would under-charge reasoning, which is most of
   a reasoning model's output. (Gemini's parser currently has this exact bug
   for `thoughts`; do not copy it.) The `max(0, ...)` guard covers the case
   where the inclusion assumption turns out to be wrong: such a row is
   billed and displayed with the two buckets disjoint (see rule 2), so no
   negative split and no billed/displayed mismatch can reach the totals.
3. **Cancelled work that burned tokens still bills.** The keep-guard is
   token presence, not status: a row is kept if any of
   `input_tokens`, `output_tokens`, `cache_read_input_tokens`,
   `cache_creation_input_tokens`, `reasoning_tokens` is non-zero.

Unverified assumptions, each pinned to a first-row check in
`tests/test_zcode_parser.py` or the field notes below:

- `reasoning_tokens ⊆ output_tokens` is documented by third-party adapters but
  the evidence row has `reasoning_tokens = 0`. If a row with
  `reasoning_tokens > output_tokens` appears, the row falls back to disjoint
  accounting for both display and billing (rule 2).
- `cache_creation_input_tokens` is treated as a separate bucket (as in the
  reference adapter). If a row shows creation is also nested inside
  `input_tokens`, the subtraction becomes
  `max(0, input_tokens - cache_read - cache_creation)`.

## Parser design

`ZCodeParser` in `src/tokdash/sources/coding_tools.py`, structured on
`MimoParser` (SQLite, class-level query cache, `SourceSyncCapability`):

- Path resolution: `clientpaths.zcode_db_path()`, honoring `ZCODE_HOME`.
- Connection: the module-level `zcode_snapshot(db_path)` context manager
  copies `db.sqlite` + `db.sqlite-wal` (the live rows sit in the WAL)
  into a private temp dir, opens the copy normally, and deletes the dir
  on exit - close runs even when the body raises, and a close error sets
  `close_failed` instead of escaping. Both the usage parser and the
  session loader consume it. Reading the source
  directly is not side-effect free: even a `?mode=ro` open makes SQLite
  CREATE a missing `db.sqlite-shm` in the source directory (reproduced
  with a valid DB+WAL), so the reader never opens the source file at all.
  The `-shm` is NOT copied - it is live coordination state, and SQLite
  rebuilds the WAL index from the copied `-wal` inside the temp dir. (The
  earlier `resolve().as_uri() + "?mode=ro"` plan is superseded; that URI
  form also truncates on a `#` in the path.)
- Snapshot coherence: the copies are sequential while ZCode may append
  to the WAL or checkpoint between them. The db/-wal signatures - exactly
  the copied set; the live `-shm` is excluded because reader traffic in
  it must not force retries - are taken before and after copying, and any
  failure during the copy is re-checked against them: a signature change
  (a checkpoint deleting the `-wal` between the `exists()` check and its
  copy is the normal one) drops the attempt and retries, bounded by
  `_ZCODE_SNAPSHOT_MAX_ATTEMPTS`. A failure with unchanged signatures, or
  exhausted attempts, skips the source - no fallback open in another
  mode. A `close()` error marks the snapshot `close_failed`; both consumers
  then return the read result but never cache it, without leaking the
  temp dir.
- Failed reads (connect, probe, or query errors) are **not** cached: a restored
  permission or cleared transient SQLite error may not change the file
  signatures, so caching an empty result would keep it stale until a file
  happens to change. The next collect retries.
- Query: half-open window `WHERE started_at >= ? AND started_at < ?`, one
  SELECT by column name so a future schema change degrades to a skipped
  source rather than a crash. Presence of `model_usage` is probed against
  `sqlite_master` inline rather than via `_sqlite_table_exists` (which
  swallows `sqlite3.Error` and returns False): an absent table is a
  legitimate empty success, but a probe error must surface as a failed read
  rather than be mistaken for an absent table and cached as one.
- Cache freshness: the file signature covers `db.sqlite`, `db.sqlite-wal`
  **and** `db.sqlite-shm` (Mimo's `_file_signatures` shape). A signature on
  the main file alone goes stale while ZCode is running, because new rows sit
  in the WAL until checkpoint.
- Cache concurrency: the class-level cache and signature are guarded by a
  class lock. The signature check/clear, the cache lookup, and the
  store-time recheck all run under the same lock: a concurrent collect
  cannot clear and repopulate between this request's validation and
  lookup, and a result fetched under an older signature (its query in
  flight while a concurrent collect advanced the signature) is returned
  for that request but never stored under the new signature.
- Entry ids: `f"zcode:{model_usage.id}"` — flat, stable, retry-distinct.
- Cost is always emitted by the parser for resolvable models; the
  `cost == 0` fallback in compute.py would re-price from the entry's split
  `output` and under-charge reasoning.
- Message count: one entry per `model_usage` row, so the displayed message
  count equals model requests including retries (the usage store defaults
  `messageCount` to 1 per entry).

## Scope

In (phase 1): Overview/Stats tokens, cost, models, daily activity for ZCode;
dashboard brand mark; brand-wiring test coverage.

In (phase 2, this change's session support):

- **Session Explorer, live native-DB group.** The loader in `sessions.py`
  (`_zcode_load_sessions`) reads `turn_usage` + `model_usage` through the same
  `zcode_snapshot` context manager the parser uses, and `zcode` joins
  `SESSION_TOOLS` so `/api/sessions`, the startup warmer, and the active-time
  paths all pick it up. `session_store` stays **False** (D1): ZCode remains a
  live-queried source like OpenCode and Mimo; the earlier plan to flip the flag
  is withdrawn - the flag gates persistent-store sync, not the Sessions tab.
  No `cli.py` change (D2: the warm path is `SESSION_TOOLS`-driven) and no
  `api.py` change (D3).
- **Top-level sessions only** (D4). `session.parent_id` set marks a subagent;
  its model rows are excluded from Sessions entirely, so Sessions totals are a
  subset of Overview totals whenever subagents ran.
- **Token turns vs activity events** (D5). One candidate per
  (session, turn) row. If at least one of its `model_usage` rows survives the
  token-presence guard, it is a token turn. Otherwise its measured `duration_ms`
  (when > 0) is kept as an `_activity_events` entry `(E, duration)` so
  tool-only and error work still credits active time. The field is not named
  `_activity`: Codex already uses that key on raw sessions for unrelated
  per-file metadata.
- **One event timestamp** (D6). `E = COALESCE(completed_at, started_at)` is the
  single instant used for the half-open query window, ordering, boundary
  lookups, and the emitted `timestamp_ms`. Query 2 fetches every `model_usage`
  row by the selected turns' (session_id, turn_id) rather than by an
  independent date window, so a turn's retries and model calls can never be
  split across a boundary by their own `started_at`. The two boundary
  lookups stay bounded the same way: the next-boundary query selects the
  nearest qualifying turn per session in SQL (a correlated MIN; the schema
  has no `completed_at` index, so one row per session is the only
  containment), and the prior-boundary query is restricted to the true set-A
  ids (chunked `IN` list) snapshotted before the next-boundary query adds
  set-B sessions, so neither request materializes whole histories.
- **Composite billing records** (D7). A turn can span several models, and
  `_build_turn` accepted exactly one `_bill`, so turns carry `_bills`
  (one record per (turn, model) group, `split-cache-write` rule) and the
  displayed `tokens_in` is `Σ(input_t + cache_write)` - cache writes show up in
  the input column while billing input stays `Σinput_t` with `cache_write`
  passed to `get_cost` separately. Repricing and private-field stripping handle
  `_bills` as well as `_bill`.
- **Tri-state reads** (D8). A missing database and an absent `turn_usage`
  or `model_usage` table (a partially migrated schema) are legitimate empty
  successes and are cached (the phase-1 signature cache, success-only). A transient snapshot or query failure raises
  `ZCodeReadError` and is never cached, so the next collection retries.
  Verified consumers: the startup warmer swallows the exception;
  `/api/sessions` returns an error instead of false data; `/api/session`
  returns 500 rather than a false 404 for an id that exists on disk;
  `_active_time_window` isolates the failure per tool.
- **Active time.** Measured work of top-level sessions is credited for its
  overlap with the window even when the session has no in-window token event:
  token turns via `_work_ms`, in-window zero-token turns via
  `_activity_events`, and a boundary-only turn via `_next_event_ms` +
  `_next_work_ms` on an activity-only session (the set-B rule: a boundary turn
  earns a raw session iff its measured work overlaps the window). Unmeasured
  work keeps the existing capped-gap contract: a lone unmeasured boundary event
  measures nothing. The two consumers that count activity-only sessions are
  `get_sessions_data`'s summary and `_active_time_window`'s per-tool path,
  both reading the precomputed `_activity_intervals` because
  `_summarize_session` returns None for a session without in-window token
  turns. Caveat: `duration_ms` is turn wall-time, so on an approval-gated
  setup user pauses inside a turn count as active (see Verification status);
  and the response's shared `active_time_method` string still says
  "capped-inter-event-gap" even where ZCode's intervals are fully measured -
  acceptable for v1.

Out, deliberately:

- **Coding Plan quota.** The 5-hour/weekly/monthly pools are remote data. The
  desktop app polls `https://zcode.z.ai/api/v1/zcode-plan/billing/balance`
  (OAuth token in `~/.zcode/v2/credentials.json`), and the local
  `coding-plan-cache.json` holds plan *entitlement* status only, no amounts.
  A quota adapter is a separate provider and must not share the local
  parser's data path.

Reconciliation guarantee (phase 2): per-turn parity - every `model_usage` row
that belongs to a top-level session, has a non-null `turn_id`, and survives the
token-presence guard is billed exactly once in Sessions and once in Overview,
at the same cost and the same displayed buckets, for windows that do not cross
a boundary. Excluded by design (one test each): subagent rows, rows with
`turn_id IS NULL`, all-zero-token turns (their measured work still counts in
active time), and period-edge attribution (Sessions treats a turn atomically;
Overview windows individual requests).

## Verification status (2026-08-19)

- macOS (ZCode 3.7.7): schema, sample rows, timestamps, and the balance
  endpoint observed directly.
- Phase 2 (2026-08-20), live macOS database, single turn:
  `turn_usage.duration_ms == completed_at - started_at` exactly
  (8213 ms both ways), the model request started 25 ms after the turn
  start, and the turn's permission mode was `"build"` (autonomous, no
  mid-turn approval waits). So `duration_ms` is a sound proxy for agent
  work on this setup; on an approval-gated setup user pauses inside a
  turn would count as active. Reproducing a multi-tool turn headlessly
  was not possible: `node .../ZCode.app/Contents/Resources/glm/zcode.cjs
  -p ...` fails with "Model config is missing" (needs
  `~/.zcode/cli/config.json`, which was not created for the check).
- Windows: the current GUI was not installed on the review host; the expected
  layout follows the cross-platform home-dir convention and is unverified.
- Linux: ZCode ships deb/AppImage builds (officially supported under WSLg),
  so the schema can be diffed locally without a Windows machine.
