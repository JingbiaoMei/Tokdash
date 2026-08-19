# Reasonix token and session support — implementation plan

**Status:** implemented, uncommitted. Fixes F1-F6 are applied and the assets and docs listed
below have landed; this document is the design reference for the shipped behavior. Recon R1-R5
is still open — none of it blocks v1, but R1/R2 could change the per-session attribution
decision if they surface explicit usage.

**Researched:** 2026-08-15, against Reasonix `1.25.2` (npm `reasonix`, binary
`@reasonix/cli-linux-x64`, repo `esengine/DeepSeek-Reasonix`) on WSL2, using the real
`~/.reasonix` of this machine (live data, including sessions written while researching).

Reasonix is a "cache-first DeepSeek coding agent for the terminal" — a multi-model TUI that
talks to any configured provider (`kind = "anthropic"` or `"openai"` in `config.toml`), so its
model list is user-defined, not a fixed vendor lineup.

## Goal and non-goals

Add Reasonix as a local Tokdash source with:

- per-request provider token usage in Overview and Stats, from the daily stats logs;
- per-session rows and drill-down in Session Explorer, from the per-project session logs;
- model attribution and pricing through the existing pricing database;
- native macOS, Linux, and Windows path discovery with a home override;
- persistent-store behavior consistent with the other file-backed clients.

This design intentionally does not:

- read `~/.reasonix/.env` (API keys) or `~/.reasonix/state/` (keyring state);
- read Reasonix's web / HTTP+SSE / ACP servers — local files only. The native machine-client
  CLI (`reasonix session list --json`) is a *candidate input* for the recon in §Recon, not a
  dependency: shelling out to a user binary from the dashboard process is outside the local-file
  convention and is only acceptable if file-based per-session usage turns out to be impossible;
- add quota tracking;
- infer token counts heuristically. A time-correlation join between stats rows and session
  windows is explicitly rejected (see §Per-session usage attribution) — this machine runs
  multiple concurrent Reasonix sessions, which makes any such join ambiguous;
- decode `.ckpt`, `.recovery.json`, `.inbox`, or `.meta.lock` internals.

The source key is `reasonix`. The display label is `Reasonix`.

## Current state (working tree, uncommitted)

| File | Status |
|---|---|
| `src/tokdash/clientpaths.py` | ✅ `reasonix_home()` (`$REASONIX_HOME` else `~/.reasonix`, blank counts as unset, relative resolved), `reasonix_stats_dir()`, `reasonix_projects_dir()` |
| `src/tokdash/sources/coding_tools.py` | ✅ `ReasonixParser` (stats JSONL) with F1-F4 applied, registered in `CodingToolsUsageTracker.parsers`, `--sources` default updated |
| `src/tokdash/sessions.py` | ✅ `SESSION_TOOLS` / `TOOL_LABELS` / parser version v2 / signatures (sidecar `.meta` included) / `lru_cache` loaders and their `reload_pricing_db` teardown / persistent-store branch. Turns are zero-token by design (F6) |
| `src/tokdash/cli.py` | ✅ `_sync_usage_database()` warm-up |
| `src/tokdash/static/index.html` | ✅ full brand registration plus the known-limitation note (en/zh) under the panel header |
| `tests/test_reasonix_parser.py`, `tests/test_reasonix_sessions.py` | ✅ present, untracked |
| `src/tokdash/static/icons/agents/reasonix.svg` | ✅ present, mirrored to `docs/assets/agents/` with a generated pill |
| README / README_CN / SUPPORTED_CLIENTS / pill / changelog | ✅ landed |
| this design doc | ✅ |

## Data map (observed on this machine)

### Home directory

`~/.reasonix`, containing:

- `config.toml` — `config_version = 6`; `[[providers]]` blocks (`name`, `kind`, `models`,
  `api_key_env`, `context_window`);
- `.env` — provider API keys; **never read by Tokdash**;
- `stats/YYYY-MM-DD.jsonl` — per-request usage log (see below);
- `projects/<project-key>/sessions/…` — session logs (see below);
- `state/` — keyring migration state; never read;
- `sessions.db` — SQLite DB at the home root; purpose not yet established (§Recon R1).

Config resolution is `flag > ./reasonix.toml > <Reasonix home>/config.toml > built-in defaults`.
Whether `<Reasonix home>` itself honors a native `REASONIX_HOME` environment variable is not yet
verified (§Recon R3). Tokdash resolves `$REASONIX_HOME` first and otherwise uses
`Path.home() / ".reasonix"`; the design doc must state accurately which side owns the override
(dsh's `DSH_HOME` is dsh-native; `PI_AGENT_DIR` is Tokdash-side — Reasonix lands in one of
those two camps after R3).

### Stats log — the only explicit usage record

`~/.reasonix/stats/YYYY-MM-DD.jsonl`, one JSON object per provider request, appended during the
day. Observed row:

```json
{"ts":"2026-08-15T12:24:35.556495944+01:00","model":"minimax-cn/MiniMax-M3","source":"cli",
 "prompt":8247,"completion":56,"cache_hit":128,"cache_miss":8119,"total":8303,"requests":1,
 "usage_source":"executor","cost_complete":false,"display_complete":false,
 "display_status":"unavailable","cost_estimated":true,"incomplete_reason":"no_price"}
```

Field inventory:

| Field | Meaning | Notes |
|---|---|---|
| `ts` | request timestamp | ISO-8601, **up to 9 fractional-second digits**, explicit UTC offset |
| `model` | `<provider-name>/<model>` | provider name is the `[[providers]] name` from config; no provider segment means bare model id |
| `source` | origin | observed `cli`; other values (desktop?) expected — confirm in R4 |
| `prompt` | prompt tokens **including cache reads** | observed invariant: `prompt == cache_hit + cache_miss` |
| `completion` | output tokens | |
| `cache_hit` | cached (read) prompt tokens | **optional — absent means zero** |
| `cache_miss` | uncached prompt tokens | |
| `total` | `prompt + completion` | redundant, do not parse for accounting |
| `requests` | request count | always 1 in observed rows |
| `usage_source` | how usage was measured | observed `executor` |
| `cost_*` / `display_*` / `incomplete_reason` | Reasonix's own pricing status | **ignore** — Tokdash prices with its own DB. `incomplete_reason: "no_price"` corroborates models absent from any price table (e.g. self-hosted vLLM models) |

The stats row carries **no session id** and **no turn id**.

### Session directory

`~/.reasonix/projects/<project-key>/sessions/` with one file cluster per session. `<project-key>`
is the absolute workspace path with `/` replaced by `-` (observed:
`-mnt-h-Developing-Agent-Tokdash_Project-tokdash`; underscores preserved). The key is lossy, so
Tokdash must not decode the project from it — the system prompt carries the authoritative cwd.

Per session `<id>` (e.g. `20260815-220524.345805130-qwen3.8-27B-FP8`):

| File | Role | Tokdash use |
|---|---|---|
| `<id>.jsonl` | conversation log | **parse**: roles `system` / `user` / `assistant` / `tool` |
| `<id>.jsonl.meta` | session metadata | **parse**: `id`, `created_at`, `updated_at`, `model` (`provider/model`), `preview`, `turns`, `schema_version: 2`, `writer_id`, `content_digest`, `revision` |
| `<id>.events.jsonl` | snapshot log: full message array repeated in `{"type":"replace","revision":N,…}` rows | ignore (no usage; must be excluded from the session-file glob — already done) |
| `<id>.event-index.json`, `<id>.display-index.json` | byte offsets / counts | ignore |
| `<id>.ckpt`, `<id>.recovery.json`, `<id>.inbox`, `.meta.lock` | runtime state | ignore |

Conversation rows (observed):

- `system` — `content` includes `Current workspace: "<abs path>"` (authoritative cwd) plus the
  injected environment/AGENTS.md summary;
- `user` — `content`, `raw_content` (clean preview), `createdAt` (epoch ms);
- `assistant` — `content`, `reasoning_content`, `tool_calls[]`, `workDurationMs`;
- `tool` — `content`, `tool_call_id`, `tool_execution`.

**No token-usage field exists anywhere in the session file cluster** (conversation, events,
indexes all inspected). `workDurationMs` is wall time, not tokens.

### Native machine client (reference, not a dependency)

`reasonix --help` documents machine-oriented, redacted JSON commands:
`reasonix session list --json [--dir PATH]`, `reasonix session show|status <machine-session-id>
--json`, `reasonix doctor --json`, `reasonix run --events-jsonl`. If any of these surfaces
per-request usage (R2), it changes the per-session attribution decision.

## Usage accounting

### Field mapping (stats → Tokdash entry)

Tokdash's canonical buckets are disjoint: `input` is *uncached* input, `cacheRead` is cached
input. Reasonix's `prompt` already includes `cache_hit`, so the mapping must split it:

```text
source      <- "reasonix"
model       <- model segment after the provider "/"
provider    <- provider segment before it ("" when absent)
input       <- cache_miss when present, else prompt - cache_hit
cacheRead   <- cache_hit (absent = 0)
cacheWrite  <- 0            # stats log has no cache-write bucket
output      <- completion
reasoning   <- 0
cost        <- pricing_db.get_cost(model, input, completion, cache_read=cache_hit, cache_write=0)
timestamp   <- ts parsed to epoch ms
entry_id    <- "reasonix:" + sha256(content fields, no line number — see fix F4)
```

> **Fix F1 (correctness):** the in-progress parser maps `input <- prompt` *and*
> `cacheRead <- cache_hit`, which double-counts the cached portion in every aggregate
> (input + cacheRead both bill it). The observed row proves the overlap: `8247 = 128 + 8119`.
> Apply the split above and add a test asserting `input + cacheRead == prompt`.

Rules:

- skip rows without a parseable `ts`; naive timestamps (no offset) are treated as UTC;
- token fields must be explicit non-negative integers (dsh `dsh_log._to_int` precedent) — a
  missing `cache_hit` is zero, a present-but-invalid one drops the *row*, never the file;
- skip all-zero rows; an absent usage row is not zero usage;
- per-row exception isolation: one bad line in a day file must not discard the rest of that
  file (see fix F3).

### Timestamps

Observed `ts` values carry 9 fractional-second digits and an explicit offset
(`2026-08-15T12:24:35.556495944+01:00`). The local runtime (Python 3.12.3) parses them, but
Tokdash supports Python 3.10+, where `datetime.fromisoformat` rejects fractions longer than six
digits. **Fix F2:** truncate the fractional part to six digits before parsing. The existing
parser test already uses a 9-digit fixture — keep it; it is the regression guard.

### Model attribution and pricing

The provider segment is descriptive (it is a user-chosen config label, e.g. `minimax-cn`,
`vllm-hpc`), and model attribution is the bare model id — normalization and pricing proceed as
for every other source.

Current pricing-DB coverage for observed models:

- `MiniMax-M3` → `minimax-m3` ✅ priced (0.6 / 2.4, cache_read 0.06 / M);
- `glm-5.2`, `deepseek-v4-pro` ✅ present;
- `qwen3.6-27B-FP8`, `qwen3.8-27B-FP8` — self-hosted vLLM models, **not priced, cost 0 is
  correct** (matches Reasonix's own `incomplete_reason: "no_price"`). The pricing-updater repo
  can add commercial entries later; no pricing-DB change is required for v1.

### Entry identity

**Fix F4:** the in-progress `entry_id` hashes the physical line number. Reasonix owns the day
file; if it ever rewrites or compacts a day (no guarantee observed), line numbers shift, entry
ids flip, and the persistent store double-counts. The dsh design settled the same question by
refusing line-number identity. Key the hash on content only
(`ts | raw_model | prompt | completion | cache_hit`) — two identical requests in one day are
indistinguishable by content and would collapse to one entry; if R4 shows Reasonix appends
strictly (most likely, given the log is append-style), optionally keep a per-file sequence
suffix in the id to disambiguate duplicates.

## Per-session usage attribution (design decision)

The conversation log has no token fields, and stats rows have no session id. v1 therefore ships:

- **Overview/Stats:** full per-request usage from the stats logs (the source of truth);
- **Session Explorer:** session rows with id, title (meta `preview` / first user
  `raw_content`), project (from the system-prompt workspace line), turn count and timing —
  **with zero token counts per turn**, exactly as the in-progress stub produces.

This is documented in the UI copy as a known limitation, with the dsh "known undercount"
precedent. The alternative — correlating stats rows to sessions by timestamp — is rejected:
concurrent sessions (observed: three live in one project dir) make the join ambiguous, and the
repo rule forbids heuristic token inference.

If recon R1 (`sessions.db`) or R2 (machine-client JSON) surfaces *explicit* per-session usage,
add it in a follow-up with a `_parse_reasonix_session_file` version bump rather than blocking v1.

## Recon (complete before implementation continues)

- **R1.** `sqlite3 ~/.reasonix/sessions.db ".schema"` + sampled rows: establish what the home
  root DB holds. If it contains per-request usage keyed by session, the attribution decision
  above changes.
- **R2.** From a project directory: `reasonix session list --json` and
  `reasonix session show <machine-session-id> --json` — inspect the redacted payload for token
  or cost fields. Also `reasonix doctor --json` for resolved-path diagnostics.
- **R3.** Native home override: run `REASONIX_HOME=/tmp/rix-home reasonix doctor --json` (or
  `setup` dry-run) and check whether Reasonix itself writes under the override. Record the
  answer so the clientpaths docstring and this doc claim the right thing.
- **R4.** Sweep all existing day files: `jq -s 'map(keys) | unique' stats/*.jsonl`; check for
  non-`cli` `source` values, any session/turn id field, `prompt == cache_hit + cache_miss`
  holding on every row, and whether files are strictly append-only (mtime/offset check across
  two reads while a session runs).
- **R5.** macOS/Windows path confirmation from the upstream source or the platform npm
  packages (mirror of what the dsh doc did for Windows).

## Fix list (all applied)

1. **F1** — `prompt` is split into uncached `input` + `cacheRead` (§Usage accounting). `cache_miss`
   is used when present, `prompt - cache_hit` otherwise, floored at zero. Cost is computed from
   the uncached half, not from `prompt`.
2. **F2** — fractional seconds are truncated to six digits before `fromisoformat`, so the observed
   9-digit stamps parse on Python 3.10 (`requires-python = ">=3.10"`, and CI runs a 3.10 leg)
   instead of dropping every row.
3. **F3** — token fields go through a strict `_token_int` guard (the `dsh_log._to_int` precedent)
   and each row is isolated, so one bad value drops its own row rather than the remainder of the
   day file.
4. **F4** — `entry_id` hashes row content only (`ts | model | prompt | completion | cache_hit |
   input`) with an occurrence counter for byte-identical duplicates. Neither the file path nor the
   physical line number participates, so moving `$REASONIX_HOME` or rewriting a day file cannot
   re-ingest history as new entries.
5. **F5** — `reasonix.svg` is in `src/tokdash/static/icons/agents/` and `docs/assets/agents/`, and
   `scripts/make_agent_pills.py` carries the `("reasonix", "Reasonix", "reasonix.svg")` row.
6. **F6** — the zero-token session contract is stated in the `_parse_reasonix_session_file`
   docstring, surfaced in the dashboard panel copy, and `schema_version` from `.meta` gates the
   parse: a version above `_REASONIX_META_SCHEMA_MAX` is skipped rather than parsed blind.

Beyond the original list, the review that followed also fixed: the sidecar `.meta` now
participates in change detection (a metadata-only edit was previously invisible to both the
`lru_cache` and the persistent store); `reload_pricing_db()` clears the two Reasonix caches;
a conversation with no assistant message yields no session rather than an empty-turn one; and
`_dsh_sessions()`, deleted by the first draft of this work, is restored.

### Turn timing

`_active_intervals` treats a turn's timestamp as the instant its work *finished* — "an event's
own generation time is part of the gap that ends at it" — so each assistant turn is stamped after
its `workDurationMs`, not before it. Stamping the start (as the first draft did) shifts every
event earlier by its own duration and drops the last step's work from the session entirely.

That still leaves the first step with no predecessor to be measured against, which for most tools
is an accepted loss. Reasonix knows better: the user message that prompted the turn is handed back
as `_prior_event_ms`, the same hook the SQLite-backed loaders use for their window edges, so a
turn that logged no duration still gets measured.

Beyond that, Reasonix does not need the heuristic at all. Every assistant turn publishes its own
`workDurationMs` as `_work_ms`, and `_measured_intervals` uses it directly: the interval is exactly
that long and ends at the turn. The capped-gap rule exists because a completion-instant log cannot
tell four minutes of model output from four minutes of a human reading it, so it treats everything
past the cap as idle. A timed duration contains no idle, so it is used uncapped and the pause
between one answer and the next prompt is excluded outright rather than billed up to the cap. A
session with 15s of logged work inside a 10-minute span reports 15s, where the heuristic reported
5m15s.

Turns with no `workDurationMs` keep the capped-gap rule, and a measured turn still anchors the gap
for an unmeasured one after it, so the two mix safely in one session. Every other tool is
unaffected: `_measured_intervals` falls back to exactly `_active_intervals`' output when no turn
carries a duration. A user `createdAt` older than work already timed is ignored rather than
allowed to rewind the clock.

## Remaining work

1. Recon R1-R5 (below). None blocks v1; R1/R2 would reopen the per-session attribution decision.
2. Regenerate `docs/assets/agents/pills/reasonix.png` with `scripts/make_agent_pills.py` in an
   environment that has `cairosvg`. The committed pill was rendered through ImageMagick because
   `cairosvg` was unavailable, so it is correct but not byte-identical to the script's output.
3. Verify on a throwaway run (never repoint the live pipx service at the checkout):
   `PYTHONPATH=src python3 main.py`, then check `/api/usage` for reasonix rows,
   `/api/sessions?tool=reasonix`, and that the icon serves.
4. Commit the Reasonix work as one commit, **separate** from the unrelated `companion/` changes
   already in the tree. Release per the AGENTS.md procedure when picked up.

## Test plan

Stats parser:

1. basic row → correct model split, disjoint token buckets, cost > 0 for a priced model;
2. multi-model / multi-provider in one file;
3. invalid lines (bad JSON, bad ts, all-zero, negative, non-numeric) skipped without dropping
   the rest of the file;
4. `cache_hit` absent → `cacheRead == 0` and `input == prompt`;
5. 9-digit fractional-second ts with non-UTC offset (3.10-safe after F2);
6. naive ts → UTC assumption;
7. multiple day files combined and timestamp-sorted;
8. `REASONIX_HOME` set / blank / relative-path resolution;
9. unknown model → tokens counted, cost 0.

Sessions:

10. meta + jsonl round-trip: id, preview, project from workspace line, turn count;
11. jsonl without meta → stem-id fallback; meta without jsonl → no session;
12. `.events.jsonl` excluded from discovery;
13. `schema_version` gating (unknown version → skipped, not corrupt);
14. persistent-store sync: unchanged files stay indexed, changed file reparsed, pricing edit
    reprices stored rows;
15. `/api/sessions?tool=reasonix` and `/api/session?tool=reasonix` smoke;
16. frontend registry sync (the existing `test_tool_brand_frontend` /
    `test_frontend_normalizer_sync` families cover the new key once reasonix lands).

Run focused tests and the release-safe pricing tests during development; the full suite before
release.

## References

- Project site: <https://reasonix.io/>
- Upstream repository: <https://github.com/esengine/DeepSeek-Reasonix>
- npm package: `reasonix@1.25.2` (`@reasonix/cli-<platform>-<arch>` prebuilt binaries)
- Local observations: this machine's `~/.reasonix` (WSL2), 2026-08-15
- Pattern reference: `docs/development/technical-notes/DSH_SUPPORT_DESIGN.md`
