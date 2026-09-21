# Goose + Roo Code support design

Goose (AAIF/Block's open-source agent CLI) and Roo Code (the Cline-family VS
Code extension) both persist every model request locally, but in opposite
shapes: Goose keeps one append-only SQLite ledger next to denormalised session
totals, and Roo Code keeps one JSON document per task whose
`api_req_started` rows are the request records. Tokdash reads one corpus per
tool, never the aggregate beside it, and both tools get a Sessions tab because
the same corpus serves it.

This note records the storage layout, the token-accounting rules, the session
decisions, the blind spots, and what is out of scope. Field-level evidence:
  `docs/local/20260920_goose_roo_support/evidence/` (Goose fixture probe,
  `goose info` captures, Roo bundle analysis, and a live Roo task and resume)
  and the fixtures `scratchpad/fixtures/goose/gsnap.db` and
  `scratchpad/fixtures/roo/`.

## Goose

### Storage

- Database: `<data>/goose/sessions/sessions.db`, WAL mode (`-wal`/`-shm`
  beside it while a session runs). Verified with `goose info` on goose
  v1.51.0:

  | Trigger | Sessions DB |
  | --- | --- |
  | default (Linux) | `~/.local/share/goose/sessions/sessions.db` |
  | `XDG_DATA_HOME=/tmp/xdgdata` | `/tmp/xdgdata/goose/sessions/sessions.db` |
  | `GOOSE_PATH_ROOT=/tmp/gpr` | `/tmp/gpr/data/sessions/sessions.db` |

  `clientpaths.goose_sessions_db()` resolves exactly that chain:
  `GOOSE_PATH_ROOT` -> `XDG_DATA_HOME` -> `~/.local/share`, then joins
  `goose/sessions/sessions.db`. macOS (`~/Library/Application Support`) and
  Windows (`%APPDATA%`) were expected to follow the same
  `data_dir/sessions/sessions.db` shape. **macOS does not, and needs no branch
  at all**: on macOS 26.5.2 arm64 with v1.51.0, `goose info` reports
  `~/.local/share/goose/sessions/sessions.db` - the same XDG default as Linux -
  and a real session created it there with `-wal`/`-shm` sidecars and an
  identical schema. `~/Library/Application Support/goose` is never created;
  config goes to `~/.config/goose` and logs to `~/.local/state/goose/logs`.
  Windows remains unobserved (V7).
- No per-project database and no profile dirs: one global DB. `GOOSE_*`
  environment variables control the model and behaviour, not the data root
  (`GOOSE_PATH_ROOT` is the only root knob).
- `schema_version` carries the migration number (16 in the fixture). Queries
  select by column name, so a future schema change degrades to a skipped
  source instead of a crash.
- Timestamps: `usage_ledger.created_timestamp` is **epoch seconds** (proven:
  1789914781 -> 2026-09-20 14:33:01 UTC). `sessions.created_at`/`updated_at`
  are `TEXT` UTC stamps (`CURRENT_TIMESTAMP`), parsed separately and used only
  as session metadata.

Tables:

| Table | Role |
| --- | --- |
| `usage_ledger` | One row per model request; the only usage corpus |
| `sessions` | Session metadata + denormalised totals (never summed) |
| `messages` | Conversation text, `metadata_json.usage` mirror, timing; read by the Sessions loader for labels and measured duration only |
| `provider_inventory_*` | Cached provider/model catalogues; not read |
| `schema_version` | Migration number; probed, not parsed |

`usage_ledger` columns:

```
id                    INTEGER PRIMARY KEY AUTOINCREMENT (never reused in a live DB)
session_id            text, not null
created_timestamp     integer, epoch seconds
model                 e.g. qwen3.8-flash-next (the pricing key)
input_tokens          TOTAL prompt tokens, inclusive of cache read
output_tokens         completion tokens, reasoning included (no split column)
total_tokens          input_tokens + output_tokens (exact, every fixture row)
cache_read_tokens     subset of input_tokens (0 allowed, NULL not observed)
cache_write_tokens    NULL on the OpenAI-compatible path (Anthropic-only)
cost, cost_source     NULL on self-hosted (goose prices only known providers)
is_compaction         0 / 1 - context-compaction request flag
```

### Token accounting

1. **The ledger is the corpus; `sessions` is never summed.** Every fixture
   session shows the same numbers twice: `accumulated_*` equals
   `SUM(usage_ledger)` exactly (session `20260920_7`:
   16854 / 304 / 17158 / 8320 both ways), while `sessions.total_tokens`,
   `input_tokens`, `output_tokens` and `cache_read_tokens` hold only the
   **last request's** snapshot (8659 / 8527 / 132 / 8320 for that same
   session). That is the Crush trap in a new costume: summing session rows
   instead of ledger rows undercounts every multi-request session, and summing
   both double-counts the last one. Tokdash reads ledger rows only.
2. **The cached slice is subtracted once.** `cache_read_tokens` is a subset of
   `input_tokens` (fixture: 8320 cache-read inside 8527 input), so the entry
   carries `input = max(0, input_tokens - cache_read_tokens)` and
   `cacheRead = cache_read_tokens`, matching the ZCode, WorkBuddy and Qwen Code
   parsers. `cache_write_tokens` is a separate bucket, added only when non-null.
3. **Reasoning is not persisted, so it is not invented.** Goose has no
   reasoning column and no reasoning field in `messages.metadata_json.usage`.
   `output_tokens` is gross, so it is billed and displayed whole with
   `reasoning = 0`. The thinking content is in `content_json` but never counted.
4. **Keep-guard is token presence, not status.** A ledger row is kept when any
   of `input_tokens`, `output_tokens`, `cache_read_tokens`,
   `cache_write_tokens` is non-null and non-zero. Failed requests (the
   fixture's 403 session wrote an assistant error message and no ledger row)
   simply have no row, so no status filter is needed - and none is added,
   because there is no status column here.
5. **`cost`/`cost_source` are ignored.** Pricing comes from
   `pricing_db.json`; `qwen3.8-flash-next` is absent from the database, so a
   self-hosted endpoint costs 0.00, which is the project convention.
6. **Entry id.** `f"goose:{session_id}:{id}:{created_timestamp}"`. The id is
   stable within one database: both fixtures declare
   `id INTEGER PRIMARY KEY AUTOINCREMENT` and carry
   `sqlite_sequence ('usage_ledger', 3)`, so SQLite never reissues an id inside
   a live DB and the "rowid reuse" worry that first motivated the composite key
   does not apply. The key is composite anyway for the case AUTOINCREMENT does
   not cover: `ON DELETE CASCADE` plus a recreated `sessions.db` restarts the
   sequence from 1, and a new install's row 1 would otherwise overwrite the old
   install's row 1 in the store, where the unique index is on
   `(source, entry_key)`. Session id and the row's own timestamp make that
   impossible without inventing anything.

### Parser design

`GooseParser` in `src/tokdash/sources/coding_tools.py`, modelled on
`HermesParser` (SQLite) with `CrushParser`'s read path:

- Path: `clientpaths.goose_sessions_db()`; absent file -> empty success.
- Read path: the shared `zcode_snapshot()` copy-then-open context manager
  (first used by ZCode, reused by Crush). A live WAL database needs the `-wal`
  copy, and opening the source file at all can create a `-shm` in the Goose
  data dir, so the source is never opened. Signatures are **one entry per DB**,
  produced by `_sqlite_db_signature()`, which folds the `-wal`/`-shm` sidecars
  into the `.db` entry on purpose: under `file_replace` a separate entry per
  sidecar would re-parse the whole database two or three times per sync. The
  two-entry `(db, db-wal)` tuple comes from `zcode_snapshot_signatures()`, and
  that is the snapshot's own coherence check, not a signature source.
- Sync: `mode="source_replace"`, `persistent_parser_version = 1`,
  `session_store=False` (the Sessions loader queries the same snapshot).
  `source_replace`, not `file_replace`, because Goose owns one whole database
  and deletes rows when a session is deleted; whole-source replacement is what
  lets a deleted session's rows leave the Tokdash store. Same reasoning as
  Hermes.
- Query: one SELECT by column name, no date predicate. `_parse_all()` is
  contractually unwindowed ("Parse all entries without date filtering"), and
  `source_replace` calls `collect(None, None)`, so a windowed `_parse_all()`
  would persist a partial corpus and leave later days permanently missing.
  `sqlite_master` is probed inline, with the empty case split in two. No
  database file, or an empty one, is a legitimate empty success. A **non-empty**
  database that has `sessions` but no `usage_ledger` raises like a failed read
  (ZCode's rule; failed reads are never cached). The distinction matters because
  `sync_source()` returns False without recording the signature when a parse
  yields nothing: on a present-but-table-less database, every collect would
  re-open a snapshot, re-probe, get nothing, and the store would keep serving
  whatever rows it already had with no error surfaced anywhere. For a fresh
  install that is right and cheap; for a table that disappeared under a Goose
  upgrade it is a silently stale reading, which is the one failure mode a
  dashboard must not have. The half-open
  `created_timestamp >= ? AND < ?` window belongs to `_goose_load_sessions()`,
  which is the only caller that has a window.
- Timestamps convert seconds -> ms at the boundary (`* 1000`), so a day bucket
  is the request's own UTC second.

### Sessions tab

Feasible and in scope: `usage_ledger` rows carry `session_id`, `model`,
per-request tokens and a timestamp, and `sessions` supplies the title, working
directory and parent link.

- `_goose_load_sessions()` in `sessions.py`; `"goose"` joins `SESSION_TOOLS`
  and `SESSION_TOOL_KEYS` in `static/index.html`, and both surfaces read the
  same snapshot helper, so Overview and Sessions cannot disagree about which
  rows exist.
- One turn per ledger row, attributed by its own `created_timestamp`. Turn text
  is cosmetic: the nearest preceding `messages` row with `role='user'` and
  `metadata_json.userVisible = true` (Goose writes a second, hidden
  `turnContext` user message per turn, which must not become the label).
- Session label = `sessions.name` (Goose renames sessions from the first
  prompt; the fixture shows both `"CLI Session"` and `"hello.txt file test"`),
  project = `working_dir`, both read directly.
- Active time uses the measured `messages.metadata_json.usage.elapsedMs` where
  a ledger row can be matched to a usage-bearing assistant message by ordinal
  position inside its session (both are append-only, and in the fixture the
  counts match 1:1). The fallback is **per session, not per row**: one billed
  row whose message carries no usage block makes the two sequences different
  lengths, and from that point position k is no longer row k, so the whole
  session keeps the existing capped inter-event-gap contract rather than
  charging its turns a neighbour's duration; `timeToFirstTokenMs` is metadata
  only.
  Seconds-granularity message stamps make window-based attribution lossy,
  which is why ordinal matching (or no match) is used instead.
  **Observed, not assumed.** Every session carrying ledger rows matches exactly
  in both fixtures - Linux 1:1 and 2:2, macOS 3:3 - counting messages whose
  `metadata_json` has a `usage` object. Sessions with no ledger rows trivially
  match at zero. That is every session in both captures, so the ordinal
  alignment holds across the whole corpus rather than in one lucky example.
  It says nothing about compaction turns, which is what V6 still asks.
- Top-level, non-hidden sessions only: rows with `parent_session_id` set or
  `session_type != 'user'` stay out of the panel, exactly as ZCode's subagent
  rule works, so Sessions is a subset of Overview whenever subagents or hidden
  sessions ran. Ledger rows for those sessions still count in Overview: they
  are real billed requests, and dropping them would understate cost.
  **Status of this rule, stated honestly:** the `session_type != 'user'` half is
  observed, the `parent_session_id` half is not. Both fixtures contain only
  `session_type in {user, hidden}` and `parent_session_id IS NULL` on every
  row, so the parent filter selects a shape nobody has yet produced. It is
  harmless to keep - it cannot match an unobserved row and it costs nothing when
  null - but it should not be described as verified, and no test should claim to
  cover it beyond "a null parent still lists".
- `sessions` also carries `archived_at` and `schedule_id`, both unaddressed by
  this design and both NULL throughout the fixtures. `archived_at` is the
  smaller question - an archived session arguably should still count, since its
  tokens were billed. `schedule_id` is the real one: a scheduled recipe run
  would appear as an ordinary interactive session, attributed to whatever
  working directory the schedule happened to use. Listing them apart needs an
  observed example of a scheduled session first, so the design's position is
  that V9 below settles it rather than guessing at a UI treatment now.
- `is_compaction = 1` rows are billed and shown, and carried as
  `is_compaction: true` on the Sessions turn so the panel's API row can tell a
  compaction request from an ordinary one (the v1 modal renders the standard
  token fields, so the flag is in the response rather than on screen).
  They carry tokens, so ZCode's zero-token-turn treatment does not apply.

Implementation notes (2026-09-21), where the build differed from the draft:

- The half-open window is converted to the column's own unit rather than
  compared: `created_timestamp` is epoch seconds, so the ms bounds become
  `ceil(since/1000) <= t < ceil(until/1000)`, which selects exactly the rows
  whose `t * 1000` falls in the ms window. The predicate lives in the loader
  and only there; `GooseParser._parse_all()` stays unwindowed because
  `source_replace` would otherwise persist a partial corpus.
- The ordinal for the `elapsedMs` match is `ROW_NUMBER() OVER (PARTITION BY
  session_id ORDER BY id)` computed over the **whole** ledger, not the window,
  so a window that opens mid-session still pairs each row with its own
  duration. Both fixtures match 1:1 (Linux 1 and 2 rows, macOS 3). It buys
  active time and nothing else: no token or cost is ever read from `messages`.
  Two rules keep the rank honest, both learned from a repro rather than from
  the fixture. It ranks the **guarded** rows, so the keep-filter for an
  all-zero row that both surfaces skip does not hand its slot to the next
  request. And `COUNT(*) OVER (PARTITION BY session_id)` ships alongside it:
  when that count and the duration list differ in length, the loader writes no
  `_work_ms` at all for the session, because after a gap every position is
  off-by-one and a wrong active time is worse than an estimated one.
- The label rule is `sessions.name` **unless** the name is one of Goose's own
  generic defaults (`CLI Session` and friends), in which case the first
  user-visible prompt names the session. Measured reason: six of the seven
  fixture sessions carry the literal string `CLI Session`, so a column of
  nothing but that string identifies nothing.
- Two failures rather than one empty. A database with no tables at all is an
  empty success; a database carrying Goose's `sessions` table without
  `usage_ledger` raises `GooseReadError`, mirroring the parser's
  `GooseSchemaError`, because an empty result here would be cached and would
  read as "no Goose sessions" for the life of the signature.

## Roo Code

### Storage

Extension plus CLI, and both write the same task tree. The extension's store
lives in VS Code's per-extension `globalStorage`:

```
<globalStorage>/rooveterinaryinc.roo-cline/
  tasks/_index.json                          task-history index (debounced)
  tasks/<taskId>/history_item.json           per-task totals, title, workspace
  tasks/<taskId>/ui_messages.json            UI transcript  <- usage corpus
  tasks/<taskId>/api_conversation_history.json  raw model messages, no usage
  tasks/<taskId>/task_metadata.json          file-context tracker, no usage
  settings/, cache/                           MCP settings, model-list caches
```

`@roo-code/cli` does exist and is published (verified on `cli-v0.1.17`, which
is not in the 3.54.0 vsix but installs on its own and drives the same extension
bundle through a bundled VS Code shim). Its root is
`$HOME/.vscode-mock/global-storage`, hardcoded in the CLI as
`DEFAULT_CLI_TASK_STORAGE_PATH`, and `--ephemeral` swaps it for a temp dir. The
directory name is a shim implementation detail, so it is a scan candidate and
not a contract; `TOKDASH_ROO_STORAGE_DIR` covers it if the CLI renames it.

**The two roots differ by one path segment.** The extension nests the tree
under the extension id; the CLI puts `tasks/` directly at the storage root. The
relative layout below `tasks/<taskId>/` is byte-identical, so the scanner globs
both depths rather than assuming one.

`<globalStorage>` candidates, all scanned and deduped by resolved path:

- Linux/WSL remote server (this machine's setup, verified live):
  `~/.vscode-server/data/User/globalStorage/...` and
  `~/.vscode-server-insiders/...`.
- Desktop, per OS: `%APPDATA%\Code\User\globalStorage\...` (Windows),
  `~/.config/Code/User/globalStorage/...` (Linux),
  `~/Library/Application Support/Code/User/globalStorage/...` (macOS), each
  also under `Code - Insiders` and `VSCodium`.
- CLI: `$HOME/.vscode-mock/global-storage/tasks/...` on every OS, observed
  live. It sits in the home directory, so the WSL and Windows home trees each
  carry their own.
- WSL reaches the Windows side through `/mnt/c/...`, the same trick
  `clientpaths.qoder_ide_db_path()` already uses for Windows-only Qoder data,
  so a Windows VS Code install is visible to a WSL Tokdash.
- VS Code **profiles** relocate the root to
  `.../User/profiles/<id>/globalStorage/...`, so those are glob-scanned.
  `roo-cline.customStoragePath` replaces the root wholesale (bundle:
  `getTasksDir()` is `join(customStoragePath ?? globalStoragePath, "tasks")`),
  and it lives in the VS Code settings database, which Tokdash does not read.
  `TOKDASH_ROO_STORAGE_DIR` (comma-separated) is the escape hatch. It needs no
  cache hook at all, and must not be wired into
  `runtime_config_signature()`: a Roo parser is `file_replace`, whose store
  identity is the set of per-file signatures, so pointing the override at a
  different root changes the file paths themselves. `sync_files()` then re-parses
  the newly-visible task files and drops the rows belonging to paths that are no
  longer enumerated, with no runtime signature involved. That is the correct
  behaviour and it is free; the Qoder comparison that used to be cited here does
  not apply, because Qoder's knob is a value that does not change the file set,
  and Qoder is `source_replace` anyway.

### Token accounting

Roo's own aggregator (`e1()` in the bundle) defines the semantics, so the
parser mirrors it instead of inventing a model:

1. **One `api_req_started` message is one billable request.** On send Roo
   appends `{type:"say", say:"api_req_started", text:'{"apiProtocol":...}', ts}`;
   on completion it rewrites that same message's `text` with `tokensIn`,
   `tokensOut`, `cacheWrites`, `cacheReads` and `cost`. Those messages are the
   corpus, with a token-presence guard: a row whose JSON carries no numeric
   `tokensIn`/`tokensOut` is in flight or failed and is skipped, which is
   exactly the shape a mid-flight read sees.
2. **`tokensIn` is cache-inclusive.** Both cost helpers return
   `totalInputTokens` as the gross prompt: `H1()` (openai and
   openai-compatible) takes gross input and bills
   `max(0, input - cacheWrite - cacheRead)`; `BU()` (anthropic) builds
   `input + cacheWrite + cacheRead` as the total. The OpenAI-compatible usage
   mapping is `inputTokens = {total: prompt_tokens, noCache: prompt_tokens -
   cached_tokens, cacheRead: cached_tokens}`, so `cached_tokens` is inside
   `prompt_tokens`. Tokdash emits `input = max(0, tokensIn - cacheReads -
   cacheWrites)` with `cacheRead` and `cacheWrite` as their own buckets,
   `cacheWrite` passed to `get_cost` separately.
   `_split_cline_cache_inclusive_input()` is the existing helper for this
   shape.
3. **`tokensOut` is gross of reasoning.** Roo maps
   `completion_tokens_details.reasoning_tokens` into
   `outputTokens: {total, text: total - reasoning, reasoning}` and persists
   only the total. Reasoning is therefore displayed as 0 and billed inside
   output - the correct billing shape, and the same no-inference rule ZCode
   states for its `reasoning_tokens` guard.
4. **Never sum the aggregates beside the corpus.** `history_item.json` and
   `tasks/_index.json` carry `tokensIn`, `tokensOut`, `cacheWrites`,
   `cacheReads` and `totalCost` as per-task sums (`e1()` output). Tokdash uses
   them for titles, `workspace`, `mode`, `status` and the parent/child links
   only, never for tokens. The Sessions tab sums the same `api_req_started`
   rows Overview parses, so no aggregate is ever a second billing surface.
   Live data shows why the aggregates cannot even be trusted as labels of the
   final state: after a 6-request task `history_item.json` read 56126/670,
   which is the exact SUM of the six rows, while `tasks/_index.json` and the
   legacy `global-state.json` `taskHistory` copy both still read 46181/590,
   i.e. four requests deep. The index is written on a debounce and can lag a
   running task indefinitely, and its `ts` is rewritten on resume
   (1789933001664 -> 1789933351962), so `ts` is not a stable identity key
   either. Three copies of one number, only the row sum is authoritative.
5. **`api_req_deleted` is ignored, not subtracted.** Checkpoint restore calls
   `rewindToTimestamp()`, which removes the rewound messages (their
   `api_req_started` rows with them) and appends one `api_req_deleted` marker
   holding the **positive** sums of what was removed. Subtracting it would
   drop those tokens twice. Roo's own aggregator ignores the marker too, so
   ignoring it is what keeps Tokdash's totals equal to Roo's.
6. **`condense_context` cost is a known gap.** Roo folds
   `message.contextCondense.cost` into its task total with no token figures
   attached, so compaction cost is in Roo's number and not in Tokdash's.
   Tokens stay complete; cost can read slightly low on a session that
   compacted. Documented, not estimated.
7. **Roo's `cost` is ignored.** Pricing comes from `pricing_db.json`. Roo
   prices from its own model table, whose lookup is
   `(price || 0) / 1e6 * tokens`, so a self-hosted id costs 0.00 there as well.
8. **Entry id.** `f"roo_code:{taskId}:{message ts}"`, the `source_name` prefix
   every other parser uses. Roo 3.54 persists no message
   ids, and unlike Cline there is no fork path that copies messages into
   another task: delegation (`delegateParentAndOpenChild`) opens an empty
   child dir, and resume re-opens and appends to the same one, which the live
   resume confirmed - same task dir, same `_index.json` entry with `number`
   still 1, `ui_messages.json` 8 -> 15 rows, and no replayed or duplicated
   `api_req_started` row. Keys are therefore task-scoped,
   `cross_file_stable_keys` stays false, and the collision domain is one
   millisecond inside one task.
9. **The model id is structured nowhere but is recoverable.** The live task
   confirms the bundle reading: `api_req_started` carries only `apiProtocol`
   plus the five numbers, `history_item.json` carries no model field (its
   schema is `id, rootTaskId?, parentTaskId?, number, ts, task, tokensIn,
   tokensOut, cacheWrites?, cacheReads?, totalCost, size?, workspace?, mode?,
   apiConfigName?, status?, delegatedToId?, childIds?, awaitingChildId?,
   completedByChildId?, completionResultSummary?`), and
   `api_conversation_history.json` records are `{role, content, ts}`. What the
   bundle reading missed is that Roo writes the model into its own prompt
   header: every user record embeds
   `<environment_details>...<model>qwen3.8-flash-next</model>`. In the live
   task that is 5 of 5 user records, and 0 of 5 assistant records - the tag
   rides only on the role Roo builds the header for. It is Roo's own string,
   not an inference, and Roo rewrites it per request, so a mid-task model
   switch shows up as a change in the tag. The rule:

   - **a "latest tag at or before the row" rule is wrong, and the fixture says
     so.** Roo writes the `api_req_started` marker *before* the conversation
     record it belongs to, so the first request of a task has no earlier tag at
     all. Across all twelve billable rows in the four captured tasks, the row
     that opens each task - one of six, one of two, one of three, and the only
     row of the fourth - would have priced as `unknown` at 0.00 while its own
     tag sat 24 to 50 ms later in the same file. That is a systematic miss on
     the request that carries the prompt, not an edge case.
   - so pair on proximity instead: the tag nearest the row's `ts` wins when it
     is within `_ROO_MODEL_TAG_WINDOW_MS` (2 s), which prices a mixed-model task
     per request the way Goose does. The captured own-record offsets are 4, 6, 6,
     7, 7, 7, 24, 31, 43, 50 and 59 ms - two orders of magnitude inside the
     window - while the nearest *different* request's tag is 3.2 s away at the
     tightest, so the window separates them without guessing.
   - when nothing is in the window, use the newest tag at or before the row: the
     model in force. Roo rewrites the tag per request, so a row whose own record
     never reached the file (one captured request's tool-result record only got
     appended after a resume, 333 s later) still resolves to the string Roo was
     actually running, rather than to a fabricated one.
   - fall back to `model = "unknown"`, `provider = apiProtocol` (persisted) and
     0.00 only when the task has no tag at all, which is the treatment Qwen Code
     already gives a record with no model.

   There is deliberately **no `TOKDASH_ROO_MODEL` override**, and the reason is
   a framework constraint rather than a preference. `runtime_config_signature()`
   reaches the persistent store on one path only: `compute.py` passes it as
   `extra=` when building a `source_replace` source signature. `sync_files()`,
   which is what a `file_replace` source uses, builds its per-file signatures
   with a hardcoded `extra={"mode": "file"}` and no environment hook, so an
   override on a `file_replace` parser would bust only the in-process
   `_entry_cache`. Overview reads stored rows for every non-`source_native_db`
   source, so after someone set the override the stored `unknown` rows would
   keep pricing at 0.00 until each task file's mtime happened to move, and the
   store-unavailable fallback path would price the same rows differently -
   two answers for one corpus. The repo's only parser with a runtime knob,
   `QoderCliParser`, is `source_replace` for exactly this reason. Rather than
   pick between making Roo `source_replace` (correct, but re-parses the whole
   Roo corpus whenever any task file changes, and the corpus grows one
   directory per task forever) and shipping a silently-stale number, the knob
   goes: the model is now read per request, and an unpriced self-hosted id
   already has a supported answer, the authoritative pricing override under
   `TOKDASH_DATA_DIR` that the dashboard's pricing editor writes. Editing that
   changes `_pricing_signature()`, which both the in-process caches and the
   stored rows already key on, so it reprices correctly on every path. Roo's
   `runtime_config_signature()` therefore stays at the inherited `None`, which
   also means the Sessions cache needs no override key.

   The tag lives inside prompt text, so a header-format change or a config that
   suppresses `environment_details` silently drops rows back to `unknown`. That
   is a documented blind spot, not a silent failure, and the Sessions label
   should say so rather than imply Roo publishes a model field.
10. **Rows that are never billable.** Four `say`/`ask` shapes observed live look
    like transcript content and carry no tokens, and all are skipped:
    `partial: true` streaming placeholders; `resume_task`, appended once at the
    resume boundary with no `text` at all; `subtask_result`, which carries a
    delegated child's completion text onto the parent; and
    `api_req_retry_delayed`, whose text ends in `<retry_timer>160</retry_timer>`
    and which the failed run showed on every retry attempt with `partial: true`
    on the last one. A retry is not a new billable request in Roo's own total,
    and Roo rewrites the *same* `api_req_started` row when the retry finally
    lands, so counting a delayed row would invent a request.

    The token-presence guard is not defensive theory, it is load-bearing. An
    aborted delegated child left behind exactly one row, whose text is still
    the pre-flight `{"apiProtocol":"openai"}` with no token fields at all, and
    a failed request leaves all-zero numbers. Both are skipped by the guard,
    observed rather than inferred.
11. **Delegation does not double count, verified.** An orchestrator run wrote
    one parent dir and one dir per child, each with its own `api_req_started`
    rows. The parent's stored `tokensIn` is the sum of *its own* rows only and
    never folds in its children - Roo's recursive `aggregateTaskCostsRecursive()`
    is a display-time fold over `childIds`, not something persisted. So summing
    every task dir equals the real request total, and keeping children out of
    the Sessions panel loses nothing from Overview. The link fields are real:
    the child carries `parentTaskId`, the parent carries `childIds`,
    `delegatedToId`, `awaitingChildId`, `completedByChildId` and
    `completionResultSummary`. Child dirs are plain UUIDv7 like any other; the
    `01a0c07e-....eeda7f9f` ids in Roo's own error output are in-memory instance
    ids and never appear as directory names.
12. **The index cannot be the discovery path.** A live aborted child task had a
    directory, a `history_item.json` and a `ui_messages.json`, and no
    `_index.json` entry at all - the debounce never fired before the process
    exited. A parser that listed tasks by reading the index would silently drop
    that usage. Discovery must glob `tasks/*/` and treat the index as an
    optional label source. Two more label caveats from the same run: `status`
    reads `"active"` on a child that finished successfully, so it is not a
    completion signal, and `number` was `1` on all four tasks in the
    workspace, so it is not unique and must not be part of an identity key.
    `history_item.json` also omits `cacheWrites`/`cacheReads` entirely on the
    task that never completed, so absent cache fields are normal, not missing
    data.

### Parser design

`RooCodeParser` in `coding_tools.py`, structured on `ClineParser`:

- Paths: `clientpaths.roo_task_message_files()` returns the per-task
  `ui_messages.json` files, deduped by resolved path the way
  `qwen_chat_files()` dedupes its two trees.
- Discovery globs `tasks/*/ui_messages.json`; it does **not** list `_index.json`
  entries. Rule 12 is the reason: a task dir existed with real rows and had no
  index entry at all, so index-driven discovery would under-count.
- Candidate roots come from `clientpaths.roo_storage_roots()` and are computed
  **once** per scan: `tasks/` is anchored in one place and both files a task
  dir holds are found from it, so nothing can disagree about which install is
  being read. The roots are then memoized in `coding_tools._roo_roots()` for
  the same `_SIG_TTL` as the signature scan, because on WSL each `/mnt/c`
  candidate is a Windows round trip and a full fan-out measures 44-51 ms on
  this machine - paid once per TTL now rather than once per read, which
  includes the Sessions loader. Gating is by existence, and `osinfo.os_kind()`
  decides which roots are even candidates. Two cautions on copying the Qoder precedent for this.
  **First, `os_kind()` returns `wsl`, never `linux`, on this machine** - the
  `is_wsl()` check runs before the `linux` fallback - so a
  `if kind == "linux"` branch silently never fires on the primary supported
  platform. Enumerate all four kinds explicitly. **Second, take the per-platform
  candidate *list* from `qoder_ide_db_path()` but not its single-winner
  `if/elif` shape.** That function returns the first existing path on purpose,
  because Qoder snapshots exactly one DB and a copied row would collide on
  `entry_id`. Roo is the opposite case: a WSL user with both a remote-server
  install and a Windows desktop install has two real task trees, and
  mutual exclusion would hide one. Return a **union**, scanned and deduped by
  resolved path. Spelled out:
  - every kind: `$HOME/.vscode-mock/global-storage` (the CLI root);
  - `wsl`: `~/.vscode-server*` server roots **and** the
    `/mnt/c/Users/*/AppData/Roaming/Code*` desktop fan-out, both, at once;
  - `linux`: `~/.vscode-server*` plus the native
    `~/.config/Code*` desktop roots;
  - `windows`: `%APPDATA%\\Code*`;
  - `macos`: `~/Library/Application Support/Code*`.
  A Linux process must not enumerate `/mnt/c` hunting a macOS tree, and a macOS
  process must not walk `/mnt/c/Users` at all; on `/mnt/c` every stat is a
  Windows round trip, and widening the trees a local-first tool reads is a
  privacy cost, not just a latency one. The WSL case therefore needs a test that
  a home holding **both** trees yields one deduplicated task set, not a test that
  either tree works alone.
- Model map: each task's sibling `api_conversation_history.json` is opened by
  path from the task dir that produced the row (there is no separate discovery
  pass for it, and there was never a reason for one: `ui_messages.json` and the
  conversation file are siblings of a directory the scan is already standing
  in). Recovering the model means reading them too, which is a real cost: they
  are the largest file in the task dir
  (5.2 KB against `ui_messages.json`'s 3.2 KB here, and they carry the whole
  prompt). The parser does not tokenise them, only regex-extracts
  `<model>...</model>` plus each record's `ts`, and it caches per file
  signature (bounded by `_ROO_MODEL_CACHE_MAX`), so `file_replace` still
  re-parses only the task that is running.

  They stay **out of `_file_signatures()`**, and this is a correctness rule, not
  an optimisation. `usage_entries` is unique on `(source, entry_key)` with an
  upsert, and Roo's keys are task-scoped. If a signature entry for
  `api_conversation_history.json` emitted rows carrying the same
  `roo_code:<taskId>:<ts>` keys as its sibling `ui_messages.json`, two paths
  would fight over one stored row, and deleting either file would erase the
  other's usage. Conversation files are an input to pricing, read through a
  per-signature cache, never a source of entries.
  That cache must be bounded, not a plain dict. Roo's corpus grows one
  directory per task forever, so an unbounded map keyed on file signature is a
  quiet memory leak with a slow clock. Two precedents set the shape and the
  number: `_OPENCODE_QUERY_CACHE_MAX = 32` (`coding_tools.py:65`) and
  `_ZCODE_SESSIONS_CACHE_MAX = 32` (`sessions.py:5175`). A cap of 32 costs
  nothing semantically - a miss is just a re-read.
- Whole-file JSON rewritten in place: `mode="file_replace"`,
  `persistent_parser_version = 1`, `_file_signatures()` over the message files
  behind `_timed_sigs()`, so only the running task's file re-parses. A torn or
  half-written document skips one task, not the source.
- Two entry points, mirroring `MuseParser`. `_parse_file_strict(file_sig)` is
  what the stored sync uses, and it raises `UsageFileVanished` rather than
  returning `[]` when a task dir disappears between enumeration and open -
  under `file_replace`, `[]` means "this file now has zero entries" and the
  commit deletes every stored row for that path, so a task deleted mid-sync
  would silently lose all of its usage. `sync_files()` catches the typed signal
  and keeps the rows. `_parse_all()` stays the source-wide path for the
  DB-off live route, where one vanished file must not cost every other file its
  entries.
- `_file_signatures()` delegates to one module-level
  `roo_task_file_signatures(root)`, the way `ClineParser._file_signatures()`
  delegates to `cline_message_file_signatures()`, so the Sessions loader and the
  parser cannot drift onto different invalidation clocks.
- **One producer feeds both surfaces**, and it has to be one function rather
  than two callers of the same parser. `parse_roo_task_file(path)` on its own
  cannot supply the model: the token figures live in `ui_messages.json`, the
  model arrives from the sibling conversation file. A Sessions loader built on
  the raw task-file parse would show `unknown` on every turn while Overview
  priced the real model, which changes both the panel's model column and the
  per-model `_bills` grouping. So one module-level `roo_task_rows(task_dir)`
  owns the pairing - read the task file, read the conversation map, emit rows
  with the model resolved - and both `RooCodeParser._parse_file_strict()` and
  `_roo_code_load_sessions()` call it, the way `cline_message_file_signatures()`
  shares discovery. This is why the Overview-versus-Sessions parity test asserts
  **per-turn model equality** and not merely equal token sums; token sums agree
  whether or not the model resolved.
- `partial: true`, `resume_task`, `subtask_result` and `api_req_retry_delayed`
  messages are skipped outright; see rule 10. `user_feedback` rows are not
  billable either but are kept as session text, since they are the follow-up
  prompts a resumed task would otherwise show as an empty turn.
- `history_item.json` cache fields may be absent rather than zero, so the label
  read must default them to 0 instead of assuming the key exists. `status`
  reads `"active"` on a child that completed, so it never gates anything.
- No `api_req_finished` handling: 3.54 does not write it, and the completion
  rewrite already carries the numbers. Confirmed against the live task, whose
  15 rows contain none.

### Sessions tab

Feasible: the per-task file is one session's whole transcript, and
`history_item.json` supplies the label and workspace.

- `_roo_code_load_sessions()`, one turn per `api_req_started` row, timestamp
  from the message's own `ts` (epoch ms, no unit conversion - confirmed, values
  such as 1789932971243 are millisecond wall clock).
- Session title = `history_item.task` (the prompt text), project =
  `history_item.workspace`, which a live task settled as a plain filesystem
  path (`/tmp/roo-smoke`), not a `file://` URI, so it needs no URI decoding.
  When the history item is missing, fall back to the first user-facing prompt
  row and the workspace root: the extension writes that row as `say:"task"`,
  but the CLI's first row is `say:"text"`, so the fallback accepts both rather
  than only the extension's spelling.
- A resumed task stays one session. The live resume appended a `resume_task`
  marker and a `user_feedback` prompt inside the same task dir, so the panel
  shows the whole multi-turn history as a single row rather than one row per
  turn, matching Roo's own history list.
- Delegated children (`parentTaskId` set) stay out of the panel while their
  rows count in Overview, mirroring Goose and ZCode. Roo's history UI folds a
  parent's cost with `aggregateTaskCostsRecursive()` over `childIds`; Tokdash
  lists the rows separately and sums nothing on the user's behalf.
- Active time keeps the existing capped inter-event-gap contract: Roo persists
  no per-request duration.

Implementation notes (2026-09-21):

- The loader is `_roo_code_sessions()` and it consumes `roo_task_rows()`, the
  same producer `RooCodeParser` uses. That is the parity contract, asserted per
  turn (timestamp, model, input, cache, output, cost) rather than as a sum: a
  loader fed `parse_roo_task_file()` alone gets every token right and calls
  every turn `unknown`, which moves the model column and the per-model billing
  grouping away from the numbers shown beside it.
- **The model's own file belongs in the Sessions cache key.**
  `api_conversation_history.json` is not in
  `RooCodeParser._file_signatures`, deliberately, because the store is unique on
  `(source, entry_key)` and Roo's keys are task-scoped. Both Sessions caches
  inherit that file's freshness instead: the aggregate key, and - less
  obviously - the per-task parse cache, whose key is the task file's signature
  while its cached value carries the model. Without the sibling's signature a
  late or edited conversation record keeps serving the previous model. The
  parser's sync signature is unchanged, and the code says why.
- Turns arrive unwindowed, like Cline and every other file corpus;
  `_summarize_session()` applies the window per turn.
- `(mtime_ns, size)` is the signature, and two writes inside one clock tick
  share an `mtime_ns` (measured). An equal-length rewrite of the conversation
  file is therefore invisible until the size moves too. Roo appends, so a real
  record moves it; this is the general limit of the store's invalidation model,
  not a Roo bug.

## Double-count audit

| Risk | Control |
| --- | --- |
| Goose session totals plus ledger | ledger only; `accumulated_*` and the last-request snapshot columns are never read for tokens |
| Goose id reuse after a recreate | entry id carries `session_id` and `created_timestamp`; `AUTOINCREMENT` protects a live DB, a recreated one restarts the sequence |
| Goose WAL double read | one snapshot per collect, shared by parser and session loader |
| Roo per-task totals plus messages | `ui_messages.json` only; `history_item.json`, `_index.json` and the legacy `global-state.json` `taskHistory` copy are label-only, and the last two are known to lag |
| Roo rewind markers | `api_req_deleted` ignored, matching Roo's own aggregator |
| Roo retry markers | `api_req_retry_delayed` skipped; Roo rewrites the original `api_req_started` row when a retry lands, so a retry is never a second row |
| Roo resume and delegation | resume appends in place (live: 8 -> 15 rows, no replayed request); children get their own dirs and the parent's stored total never folds them in, so task-scoped keys can neither replay a request nor double-count a child |
| Roo two paths, one key | only `ui_messages.json` is ever a signature entry or an entry source; `api_conversation_history.json` is read for pricing input only, because the store is unique on `(source, entry_key)` and task-scoped keys from two files would fight over one stored row |
| Both, Overview against Sessions | same corpus, same guard, same timestamps; Sessions only filters by session class |

## Blind spots (documented, not worked around)

- **Goose**: reasoning tokens are not persisted; `cache_write_tokens` is NULL
  on OpenAI-compatible providers; `cost`/`cost_source` are NULL for
  self-hosted endpoints; deleting a session in Goose removes its usage from
  Tokdash at the next sync - whole-source replacement has no missing-file state
  to preserve, so that holds even with the durable usage store on, which is the
  opposite of Roo Code below; pre-SQLite Goose installs (JSON session files) are
  not read at all; the Windows data dir is unobserved, though macOS was
  verified to use the Linux XDG default rather than an Apple-convention path,
  which makes a Windows surprise the residual risk rather than a known gap.
- **Roo Code**: the model id is only ever a string inside Roo's own prompt
  header, so it is absent for any task whose `environment_details` block was
  suppressed or reformatted, and those rows price as `unknown` at 0.00 with no
  source-specific override. Repricing a *recognised* id that the pricing
  database simply does not list, such as `qwen3.8-flash-next`, means adding that
  id to the pricing override the dashboard already edits. Deliberately **not**
  recommended: adding a literal `unknown` entry. `apply_pricing()` selects every
  stored row that has billing inputs, with no source filter, and the models
  table aggregates `unknown` across tools, so an `unknown` price would reprice
  every source's unknown rows at once - a global knob wearing a Roo-shaped
  label. Tag-less Roo rows stay at 0.00, and that is the honest number; no
  cross-tool side effect is worth covering one tool's blind spot. No persisted
  reasoning split;
  `condense_context` cost is in Roo's total and not in Tokdash's; a
  `customStoragePath` relocation or any VS Code data root Tokdash does not
  scan is invisible; switching VS Code profiles leaves two task trees and both
  are read, which is correct but can surprise; the CLI writes nothing under the
  extension id at all, so a scanner that only walks
  `.../globalStorage/rooveterinaryinc.roo-cline/` sees no CLI sessions.
  Deleting a task directory in Roo does **not** remove its usage here, and that
  asymmetry with Goose is structural rather than a setting anyone can change.
  Roo is read per file, and the default durable usage store
  (`TOKDASH_USAGE_DB_DURABLE`) only flags a vanished file - `UPDATE file_state
  SET missing = 1` - and keeps its rows, exactly as it does for every other
  file-based source. So the dashboard keeps accounting for a task Roo has thrown
  away, which is the intended history-retention behaviour and reads as a
  surplus rather than a loss. No sync mode fixes or worsens this:
  `source_replace` has no missing-file concept, and `file_replace` over the
  database path, which is what Crush does, would not help either - a shrinking
  database is still a present file whose rows are replaced wholesale.
- **Both**: neither tool persists a per-request cost Tokdash would trust, and
  neither is scanned for remote quota data. Goose's provider catalogues and
  Roo's `cache/*_models.json` are context windows and price sheets for the
  tool's own UI, not usage; reading them would put a vendor catalogue in the
  dashboard's price path.
  Goose also writes full request payloads, prompts included, to
  `~/.local/state/goose/logs/llm_request.N.jsonl` (observed on macOS; each
  record is `{model_config, input}` with no usage figures). Tokdash does not
  read that tree: it duplicates prompts the ledger already accounts for, and
  pulling token figures out of request logs would be inference. Worth naming
  because it is a large, prompt-bearing directory a user might reasonably
  expect the dashboard to touch.

## Test plan

Mirror the existing per-tool pairs (`test_crush_parser.py`,
`test_zcode_sessions.py`):

- `tests/test_goose_parser.py`, fixture `scratchpad/fixtures/goose/gsnap.db`
  rebuilt the way `tests/test_crush_parser.py` builds its database, from
  inline `CREATE TABLE` DDL seeded with the verified fixture rows (no binary
  `.db` in `tests/fixtures/`): one entry per ledger row; `total == in + out`;
  the cache-read share split out and never added on top; NULL
  `cache_write`/`cost` create no bucket; the session aggregates provably unused
  (feed a database where `accumulated_*` disagrees with the ledger and assert
  the ledger wins); two ledger rows with equal `id` but different `session_id`
  producing two stored entries, which is the recreated-database case that
  actually exists rather than a live rowid reuse that `AUTOINCREMENT` rules out;
  an absent database is an empty success while a non-empty database missing
  `usage_ledger` raises rather than serving stale rows; seconds-to-ms
  conversion.
- `tests/test_goose_sessions.py`: hidden and child sessions excluded from the
  panel and present in Overview; the ordinal `elapsedMs` match and its
  fallback; compaction turns labelled.
- `tests/test_roo_code_parser.py`, seeded inline from
  `scratchpad/fixtures/roo/` (a real two-turn task: 3 requests, then a resume
  adding `resume_task` + `user_feedback` + 3 more): one entry per completed
  `api_req_started`; the six rows sum to 56126/670 and to nothing else;
  in-flight (`apiProtocol` only), all-zero failed, `partial`, `resume_task` and
  `api_req_retry_delayed` rows all skipped; `api_req_deleted` not subtracted;
  the cache-inclusive split; `tokensOut` billed whole; the `<model>` tag
  recovered and attributed to the right request, including a two-model task
  where the tag changes mid-task; `unknown` priced at 0.00, with a test that no
  environment variable can reprice a stored Roo row without its file changing -
  the property that made the override knob unacceptable; a corrupt
  `ui_messages.json` degrades one task, not the source. The fixture already
  proves the aggregate trap, so add a test that reads the fixture's
  `_index.json`-style lagging numbers and asserts they never reach a total.
- `tests/test_roo_code_sessions.py`: title and workspace from
  `history_item.json`; a resumed task listed once, not per turn; child tasks
  excluded; message-sum parity with the parser.
- `tests/test_tool_brand_frontend.py`, `tests/test_i18n_languages.py` and the
  `clientpaths` tests gain `goose` and `roo_code` entries, including the Goose
  root chain and the WSL `/mnt/c` Roo case.

## Rollout

Phase 1 (Overview and Stats): `GooseParser` and `RooCodeParser`, brand wiring
(`static/icons/agents/`, the icon and label maps in `static/index.html`),
`SUPPORTED_CLIENTS.md` and the five translated READMEs in the same PR.

Phase 2 (Sessions): `goose` and `roo_code` join `SESSION_TOOLS` and
`SESSION_TOOL_KEYS`, with `_goose_load_sessions()` and
`_roo_code_load_sessions()`. Both keep `session_store=False` and stay
live-queried, like ZCode and Qoder IDE.

Out of scope: Goose provider catalogues, Roo model-list caches, Roo Cloud and
synced settings, and any quota card for either tool.

## Live-verification items

Goose's facts are verified against `scratchpad/fixtures/goose/gsnap.db` (Linux)
and `scratchpad/fixtures/goose/gmac-sessions.db` (macOS), and V7's macOS half,
except for V6. V1-V3 were settled on 2026-09-20 by a real Roo task and a real
resume driven through `@roo-code/cli` against this machine's vLLM endpoint; the
fixture is `scratchpad/fixtures/roo/` and the transcript of both runs plus the
before/after counters is
`docs/local/20260920_goose_roo_support/evidence/roo_live_run.md`. Everything
else about Roo is still read off the shipped bundle
(`evidence/roo_static_analysis.txt`).

| # | Item | Settles | Status |
| --- | --- | --- | --- |
| V1 | One live Roo task on the self-hosted OpenAI-compatible provider | the real `api_req_started` field set, `cost` value, `ts` unit, `taskId` shape | **Done.** Fields are exactly `apiProtocol, tokensIn, tokensOut, cacheWrites, cacheReads, cost`; `cost` is `0`; `ts` is epoch ms; `taskId` is UUIDv7 (`01a0c051-a4d8-...`), so a task id is time-ordered and usable as a sort key |
| V2 | One resumed task | append-in-place, so no replayed rows | **Done.** Same dir, `ui_messages.json` 8 -> 15 rows, row sum and `history_item.json` both 56126/670, `_index.json` lagging at 46181/590 |
| V3 | `history_item.workspace` on a live task | path vs `file://` URI for the Sessions project label | **Done.** Plain path `/tmp/roo-smoke` |
| V4 | One delegated (orchestrator) task | the child task dir starts empty, so task-scoped keys are replay-free | **Done**, via `--mode orchestrator`. Children start with a single pre-flight `api_req_started` row and get their own dirs; parent totals exclude children (rule 11). It also settled two things the original question did not ask: the index can omit a task dir entirely (rule 12), and `subtask_result` is a row type the skip list needs |
| V5 | One checkpoint restore / rewind | the `api_req_deleted` marker coexists with removed `api_req_started` rows | **Settled from the code**, which beats a screenshot here. `rewindToTimestamp()` -> `performRewind()` -> `truncateClineMessages(index)` removes the rewound rows from `ui_messages.json`, and only then does the caller `say("api_req_deleted", {tokensIn, tokensOut, cacheWrites, cacheReads, cost})` with the sums of what went. The marker is therefore additive over a **shortened** file, which is why ignoring it is correct and why the parser must be whole-file `file_replace` rather than an incremental tail. The physical artifact was still never photographed: the CLI forces `enableCheckpoints: false` |
| V6 | A Goose session that compacts | `is_compaction = 1` ledger rows, and whether a matching message exists | Open |
| V9 | A Goose **subagent** session and a **scheduled** recipe run | whether `parent_session_id` is ever set and what shape Goose uses for it; whether `schedule_id` should split a session out of the panel | Open, and the only open item that could change a user-visible grouping. Neither fixture shows either shape, so today's parent filter is an untested assumption rather than an observation |
| V7 | Goose on macOS or Windows | data-dir layout for `clientpaths` | **Done for macOS** (v1.51.0, macOS 26.5.2 arm64). `goose info` reports `~/.local/share/goose/sessions/sessions.db`, the same XDG default as Linux, and a real session created it there with WAL sidecars and an identical schema. `~/Library/Application Support/goose` is never created, so **no Apple-convention branch belongs in `clientpaths`**. Windows is still unobserved |
| V8 | Roo fork (`number: 2`) | whether a fork copies `api_req_started` rows into the new task dir, which would break task-scoped entry ids | **Closed: no fork feature exists in 3.54.0.** Zero case-sensitive `Fork` occurrences in the 14.8 MB bundle, no fork handler, and no fork string anywhere in the webview bundle, `package.nls.json` or the release changelog. The Cline fork-copy risk this item was written against has no counterpart here, so `cross_file_stable_keys = False` is safe. 3.54.0 is the current marketplace release as at 2026-09-20, so this is not a stale-version reading; a future version that adds fork would need a re-check |

After V4, V5 or V8 run `python3 scratchpad/probe_roo_storage.py <root>`; after
V6 run `python3 scratchpad/probe_goose_fixture.py <sessions.db>`. Both print
redacted, paste-ready evidence. V4, V5 and V8 all need the VS Code extension,
not the CLI.

## SUPPORTED_CLIENTS.md paragraphs (landed 2026-09-21)

- **Goose**: `sessions/sessions.db` under Goose's data dir -
  `$GOOSE_PATH_ROOT/data`, then `$XDG_DATA_HOME`, then `~/.local/share`
  (confirmed with `goose info` on v1.51.0). WAL mode, read through the same
  copy-and-snapshot path as ZCode and Crush. One `usage_ledger` row per model
  request with the model id on the row, so a mixed-model session prices per
  request; the cached share inside `input_tokens` is split into `cacheRead` and
  billed at the cache rate, and reasoning is not persisted, so output is billed
  whole. `sessions.accumulated_*` duplicates the ledger while
  `sessions.total_tokens` holds only the last request's snapshot, so neither is
  ever summed; `cost` is ignored and pricing comes from the pricing database,
  which leaves a self-hosted model at 0.00. The Sessions tab reads the same
  ledger rows, top-level non-hidden sessions only, with titles and working
  directories from `sessions` and measured per-request duration from `messages`
  where it can be matched. Deleting a session in Goose deletes its usage here
  at the next sync.
- **Roo Code**: `tasks/<taskId>/ui_messages.json` inside the extension's VS
  Code `globalStorage` (`Code`, `Code - Insiders` and `VSCodium` on each OS,
  plus the WSL `~/.vscode-server` server root, and the same tree written by
  `@roo-code/cli` under `~/.vscode-mock/global-storage`; VS Code profiles are
  scanned and `TOKDASH_ROO_STORAGE_DIR` covers a `customStoragePath`
  relocation). One entry per completed `api_req_started` message - that is
  exactly what Roo's own task total sums, verified against a live task whose six
  rows and its per-task total both read 56126 input / 670 output - so in-flight
  requests, `partial` placeholders, `api_req_retry_delayed` retry markers,
  `resume_task` boundary markers and `api_req_deleted` rewind markers are
  skipped and nothing is subtracted twice.
  `tokensIn` is cache-inclusive and splits into `input`, `cacheRead` and
  `cacheWrite` before pricing; `tokensOut` already includes reasoning, which Roo
  computes but never persists. Roo records no model id as a field, but it does
  write one into the prompt header of every user message, so that is what rows
  are attributed to; where the header is absent, rows are `unknown` at 0.00
  and are repriced through the pricing database like any other unrecognised
  model id, not through a source-specific flag. Neither Roo's
  `cost` nor the per-task totals in `history_item.json`, `tasks/_index.json` or
  the legacy `taskHistory` blob are used for billing - the index copies are
  written on a debounce and can lag a running task by several requests. The
  Sessions tab reads the same messages, one turn each, titled and grouped by
  `history_item.json`, with a resumed task kept as one session and delegated
  child tasks listed apart from their parent.
  Deleting a task in Roo Code does not remove its usage here: Tokdash's usage
  index is durable by default, so a task directory that disappears keeps the
  rows it had already contributed. `docs/reference/HISTORY_RETENTION.md` covers
  the case that is genuinely unrecoverable, which is data deleted before
  Tokdash ever indexed it.
