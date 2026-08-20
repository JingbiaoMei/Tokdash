# ZCode support design

ZCode (Z.ai's GLM coding app) persists every model request in a local SQLite
database. Tokdash reads it read-only, one entry per `model_usage` row. This
note records the storage layout, the token-accounting rules the parser must
follow, and what is deliberately out of scope for the first change.

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
| `turn_usage` | Per-(session, turn) aggregate; phase 2 |
| `session` | id, title, directory, parent_id (subagents); phase 2 |
| `tool_usage` | Per tool call; phase 2 |
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
- Connection: read-only URI `path.resolve().as_uri() + "?mode=ro"` (the
  Antigravity pattern). The plain `f"file:{path}?mode=ro"` form truncates on a
  `#` in the path and silently drops `mode=ro`. A failed read-only open skips
  the source for that window - there is deliberately no read-write fallback,
  because a reader must never be able to modify WAL/SHM state.
- Failed reads (connect or query errors) are **not** cached: a restored
  permission or cleared transient SQLite error may not change the file
  signatures, so caching an empty result would keep it stale until a file
  happens to change. The next collect retries.
- Query: half-open window `WHERE started_at >= ? AND started_at < ?`,
  guarded by `_sqlite_table_exists`, one SELECT by column name so a future
  schema change degrades to a skipped source rather than a crash.
- Cache freshness: the file signature covers `db.sqlite`, `db.sqlite-wal`
  **and** `db.sqlite-shm` (Mimo's `_file_signatures` shape). A signature on
  the main file alone goes stale while ZCode is running, because new rows sit
  in the WAL until checkpoint.
- Entry ids: `f"zcode:{model_usage.id}"` — flat, stable, retry-distinct.
- Cost is always emitted by the parser for resolvable models; the
  `cost == 0` fallback in compute.py would re-price from the entry's split
  `output` and under-charge reasoning.
- Message count: one entry per `model_usage` row, so the displayed message
  count equals model requests including retries (the usage store defaults
  `messageCount` to 1 per entry).

## Scope

In (this change): Overview/Stats tokens, cost, models, daily activity for
ZCode; dashboard brand mark; brand-wiring test coverage.

Out, deliberately:

- **Session Explorer.** `session` + `turn_usage` are ready for a phase 2 that
  adds `sessions.py` support, flips `session_store`, and adds the `cli.py`
  warm call. Declaring `session_store=True` without that wiring does nothing:
  no code gates the Sessions tab on the flag.
- **Coding Plan quota.** The 5-hour/weekly/monthly pools are remote data. The
  desktop app polls `https://zcode.z.ai/api/v1/zcode-plan/billing/balance`
  (OAuth token in `~/.zcode/v2/credentials.json`), and the local
  `coding-plan-cache.json` holds plan *entitlement* status only, no amounts.
  A quota adapter is a separate provider and must not share the local
  parser's data path.

## Verification status (2026-08-19)

- macOS (ZCode 3.7.7): schema, sample rows, timestamps, and the balance
  endpoint observed directly.
- Windows: the current GUI was not installed on the review host; the expected
  layout follows the cross-platform home-dir convention and is unverified.
- Linux: ZCode ships deb/AppImage builds (officially supported under WSLg),
  so the schema can be diffed locally without a Windows machine.
