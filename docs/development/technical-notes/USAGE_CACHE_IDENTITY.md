# Usage-cache identity: parse, billing, pricing

How Tokdash decides whether a cached usage row is still valid, and what a
pricing change does instead of invalidating it.

Applies to `usage_entries` in `~/.tokdash/usage.sqlite3` — the persistent cache
behind Overview, Stats, `/api/usage`, `/api/tools` and `tokdash export`. Session
records are a separate cache with its own contract (`sessions.py`).

## The problem this replaced

Through 2.0.0 a cached row was validated against one signature built from:

- the source files (paths, mtimes, sizes), plus
- `parser_code_signature(parser)` — a SHA-1 of the parser's whole module, and
- the complete pricing identity (content + `PricingDatabase` implementation).

Every coding-tool parser lives in one file, `sources/coding_tools.py`, so the
module hash was shared by all of them. Two consequences followed:

- Changing or adding **one** parser invalidated **every** persistently stored
  coding-tool source.
- Adding an unrelated model to `pricing_db.json` reparsed every cached source
  log, even though nothing about token extraction had changed.

2.0.0 shipped both at once, so upgrading rebuilt the entire usage cache.

## Three identities

### 1. Source identity — did the log change?

The file signatures a parser reports: `(path, mtime_ns, size)`, plus the safe
offset a tail append stopped at. Unchanged since before this note. A changed
file is reparsed and its rows replaced by `(source, file_path)`; unchanged files
stay indexed.

### 2. Parse identity — did what we extract change?

Each persistently stored parser declares an integer
`persistent_parser_version` and builds:

```python
{
    "object": "tokdash.sources.coding_tools.CodexParser",
    "version": 1,
    "entry_format": 1,
}
```

- `object` names the class. It is path-free, so a reinstall at a different
  location — or `pipx upgrade` restamping every file's mtime — changes nothing.
- `version` is hand-written, per parser. **Bump it whenever that parser's
  stored output changes**: extraction, deduplication, entry keys, timestamps,
  token buckets, or the billing inputs it records. Do not bump it for a
  refactor that leaves the rows identical.
- `entry_format` is `USAGE_ENTRY_FORMAT_VERSION` in `usage_store.py`, shared by
  every parser. It identifies *what a row must carry to be priceable*, not what
  any one parser extracts. Bumping it rebuilds every stored source, on purpose.

Explicit integers rather than `inspect.getsource(parser.__class__)`: a source
hash of the class alone would miss changes in the shared helpers these parsers
call (`_rglob_sigs`, `codex_token_event_key`, `fold_dsh_usage_samples`, …),
which do change what gets stored.

Two shapes deviate:

- **DSH** folds in `DSH_DECODER_VERSION` and `DSH_ACCOUNTING_VERSION` from
  `sources/dsh_log.py` under a `decoder` key, because its framing and usage fold
  live there. Bumping either invalidates DSH and nothing else.
- **Native-database sources** (OpenCode, MiMo, ZCode) declare
  `persistent_parser_version = None`. They are queried live from the client's own
  SQLite database and never copied into `usage_entries`, so there is nothing
  stored for a version to identify. `test_usage_cache_identity.py` asserts the
  split, so a new stored parser cannot ship without a version and a live one
  cannot acquire a meaningless version.

OpenClaw is stored but has no parser class; it uses `OPENCLAW_PARSER_VERSION` in
`sources/openclaw.py` with the same shape.

### 3. Pricing identity — did rates change?

`persistent_pricing_signature()` — effective pricing content plus the
`PricingDatabase` implementation, both content-based. **It is not part of any
parse signature.** It is stored in one `meta` row and applied by repricing.

The content half comes from `PricingDatabase.content_signature()`, which reports
the identity `load()` computed **from the very bytes it parsed** — not a fresh
read of the file. That distinction is load-bearing. A `PricingDatabase` holds its
rates in memory from construction; if the identity were re-derived by reading the
file later, a write in between would pair the new file's identity with the old
in-memory rates. The cache would then record costs that were never computed under
the identity it stamped, and every later request holding the genuinely-new
pricing would match that identity and skip repricing — permanently.

So `load()` selects the effective file, parses its rates and computes their
identity in one `read_bytes()`, and publishes all of it as one immutable
snapshot. `signature()` is the separate, stat-and-hash based drift detector that
*does* read current files; it answers "should I reload?", which is a different
question from "what am I holding?".

## Billing provenance

A stored row carries the inputs it was billed on, in its own `billing_json`
column. Two kinds:

```python
# Tokdash priced it. Candidates are tried in order; first non-zero wins.
{"kind": "pricing", "models": ["acme/m1", "m1"],
 "input": 1000, "output": 100, "cache_read": 2000, "cache_write": 0}

# The source reported it. Never recomputed.
{"kind": "fixed", "cost": 0.25}
```

Rules that keep this faithful:

- **The counts are the arguments passed to `get_cost`, not the displayed token
  buckets.** Sources differ: Codex and Gemini subtract an inclusive cache read
  out of `input`; Grok folds reasoning into `output`; ZCode bills the full
  output including reasoning; Claude and Kimi bill cache writes at the input
  rate. Store what was billed, display what was parsed.
- **Ordered candidates reproduce provider-qualified fallback.** Hermes prices
  `provider/model` first and only then the bare name; the list preserves that,
  so a later provider-specific rate still shadows the bare key on a reprice.
- **A provider-reported cost is `fixed`.** Pi's `usage.cost.total`, Hermes'
  `actual_cost_usd` / `estimated_cost_usd`. A pricing edit must never move a
  number the provider itself reported.
- **An empty candidate list stays at zero.** A Codex file with no model signal
  anywhere labels its rows `unknown` and hard-codes cost 0; storing no
  candidates reproduces that under any future pricing file, exactly as a
  reparse would.
- **`fallback` covers "our price, else theirs".** OpenClaw prefers Tokdash
  pricing and falls back to its own recorded cost when the model resolves to
  nothing.

The record is private. It never appears in `query_entries`, `/api/usage`,
`/api/tools` or export output: `raw_json` is written through
`public_usage_entry()`, and the live-parser fallback path in `compute.py`
strips it too so both paths hand callers the same shape.

Entry keys are price-free for the same reason. A row the source did not name
itself is keyed on a hash of its fields; including cost made that key move when
the row was repriced, so it would stop colliding with the same logical entry
reparsed out of another file and the duplicate would be counted twice.

## The repricing transaction

`UsageEntryStore.apply_pricing(identity, pricing_db)` runs at the top of
`compute._sync_usage_store()` and `openclaw._sync_openclaw_store()`. If the
stored identity matches, it returns immediately. Otherwise, in one
`BEGIN IMMEDIATE` transaction under the usual process lock:

1. scan `usage_entries` in id-ordered chunks, skipping rows with no provenance;
2. recompute each row's cost with `usage_entry_cost()`; skip rows whose cost did
   not move (which is every `fixed` row);
3. write the SQL `cost` column and `raw_json`'s public `cost` field together;
4. write the new pricing identity **inside the same transaction**;
5. commit.

That ordering is the design. The identity becomes visible only when the rows it
describes are already committed, so it can never advance ahead of them; a
failure anywhere rolls back both and the last good cache stays servable. No
parser is called — not `collect`, not `_parse_all`, not a per-file parser, and
no source log is opened.

### A sync that lands after someone else repriced

Parsing runs *outside* the store lock — deliberately, so a slow corpus parse
does not block every other reader. That opens one interleaving: a sync begins
under pricing P1, another process reprices the whole database to P2 while it
parses, and only then does it commit its P1-priced rows. Leaving the stored
identity at P2 would be silently permanent: every later P2 request matches the
identity, returns early, and never revisits those rows.

So every row-writing transaction declares the pricing it parsed under, and
`_drop_stale_pricing_identity()` deletes the stored identity **in that same
transaction** when the two disagree. The identity then means what it always
meant — "every row here was priced under this" — and the next `apply_pricing`
rebuilds the costs from `billing_json`. Nothing is reparsed to recover; the
provenance on the row is what makes that possible.

`_sync_usage_store()` and `_sync_openclaw_store()` also call `apply_pricing()`
once more after their syncs, so a request that hit this heals before it returns
instead of leaving a dropped identity for the next one. That trailing call is a
latency optimization; the in-transaction drop is the correctness fix, and it is
what survives a process dying between the row commit and the trailing call.

Rejecting the write and retrying under a fresh pricing database would also be
sound, but it throws away a completed parse — the exact cost this design exists
to avoid — and is not a fixpoint either, since the pricing file can change again
during the retry.

### ...and the readers in that window

Dropping the identity stops the staleness becoming permanent, but the repair
lands in a *later* transaction. Between the two, the table genuinely holds two
pricing generations, and a reader must not report their sum. Checking the
identity and then reading would not help: a superseded write can commit in the
gap between the check and the read.

So `_read_priced()` puts both in one deferred transaction, which in WAL mode
pins a single snapshot — seeing an identity there proves every row *in that same
snapshot* was priced under it. An absent identity means a racing write got in,
so the reader repairs and retries instead of returning a mixed table. Retries
are bounded; the final attempt repairs and reads while holding the process lock,
which every writer needs, so nothing can interleave and the loop terminates.
`query_entries`, `aggregate_entries` and `contribution_days` all route through
it, and OpenClaw takes its model totals and its contribution grid from ONE such
snapshot rather than opening two that could straddle a write.

Cost on the hot path is one extra indexed `meta` lookup per read. The repair
branch only runs when a race actually happened.

Note `usage_db_process_lock` is not reentrant — POSIX `flock` is per open file
description, so a second acquisition from the same process blocks against the
first forever. That is why the repricing body is split into
`_reprice_holding_lock()`, which the lock-holding read branch calls directly
instead of re-entering `apply_pricing()`.

A global pass over `usage_entries` is accepted. The goal is to avoid rereading
source logs, not to avoid touching cached SQL rows. Measured on 400,000 rows:
0.4 ms when the identity is unchanged (a single `meta` lookup — the hot path on
every request), 1 s for a pass where no cost moves, 3.3 s for a pass where every
cost does. The pricing file has to actually change for the latter two.

There is one identity for the whole database, so two processes reading
*different* pricing files but writing the *same* usage database would reprice
each other's rows back and forth. In practice they cannot: the database path
defaults to `<data dir>/usage.sqlite3` and the pricing override lives in the
same data dir, so different pricing means different databases. The session cache
solves the same problem differently — it prices on read, because its costs are
not aggregated in SQL.

## The one-time legacy migration

Schema 8 adds `billing_json`. Rows written before it have no trustworthy
provenance — their cost came from pricing this build cannot identify — so the
migration stamps each as `{"kind": "fixed", "cost": <stored cost>}`. Then:

- A row whose source file still exists is rebuilt by the next sync anyway,
  because the parse signature changed shape at the same time, and comes back
  with real provenance.
- A **durable row whose source file is gone** keeps exactly the cost it already
  reported, forever. It is preserved, not discarded and not guessed at.

`session_records`, `quota_snapshots` and every other durable table are
untouched. After this one rebuild, ordinary parser additions and pricing updates
never trigger a global parse again.

## What each kind of change costs

| Change | Reparse | Reprice |
| --- | --- | --- |
| Package version, install path, file restamping | no | no |
| Edit to an unrelated parser or shared helper | no | no |
| A source log is edited or appended to | that file only | no |
| One parser's `persistent_parser_version` bump | that source only | no |
| `USAGE_ENTRY_FORMAT_VERSION` bump | all stored sources | no |
| `DSH_DECODER_VERSION` / `DSH_ACCOUNTING_VERSION` bump | DSH only | no |
| A sync landing after another process repriced | no | yes (identity dropped, then rebuilt) |
| A read landing in that window | no | yes (repaired before the read returns) |
| A pricing rate, alias or model added/changed/removed | no | yes |
| `PricingDatabase` implementation change | no | yes |
| The pricing file changing after a database loaded it | no | yes, once that database reloads |

## Tests

`tests/test_usage_cache_identity.py` holds this contract, including mutation
checks that install the forbidden defect and assert the symptom it causes:
pricing back in the parse signature, a skipped repricing pass, a repriced fixed
cost, a module-hash parser identity, and a pricing identity committed before its
rows. `tests/test_session_cache_pricing.py` is the equivalent for the session
cache, which reached the same place from the other direction — it prices on read
rather than rewriting rows, because its costs are not aggregated in SQL.
