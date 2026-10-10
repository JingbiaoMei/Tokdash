# Configuration reference

Environment variables, estimation method, and database semantics for Tokdash.
Tokdash is **localhost-only by default**.

## Server & cache

- `TOKDASH_HOST` (default: `127.0.0.1`)
- `TOKDASH_PORT` (default: `55423`)
- `TOKDASH_CACHE_TTL` (default: `600` seconds)
- `TOKDASH_CACHE_MAX_ENTRIES` (default: `256`) — bound cached API responses and their idle per-key locks
- `TOKDASH_COMPUTE_CONCURRENCY` (default: `2`) — cap on simultaneous heavy history reparses; excess cold requests return a fast `503` instead of saturating the server under load
- `TOKDASH_STARTUP_WARM_JOIN_SECONDS` (default: `30` seconds) — how long the first request for a key still being warmed at startup waits for that fill instead of returning `503`; at most one request per key waits
- `TOKDASH_LIMIT_CONCURRENCY` (default: `64`) — uvicorn connection cap (backpressure)
- `TOKDASH_KEEPALIVE` (default: `5` seconds) — uvicorn keep-alive timeout
- `TOKDASH_ALLOW_ORIGINS` (comma-separated, default: empty)
- `TOKDASH_ALLOW_ORIGIN_REGEX` (default CORS policy allows localhost/127.0.0.1 and same-tailnet Tailscale Serve reads; setting either CORS option replaces that default policy)
- `TOKDASH_UPDATE_ORIGIN` (unset) — exact HTTPS origin allowed to pair a remote browser for click-to-update; unset means remote updates are entirely off (`tokdash update-enroll` mints the codes)
- `TOKDASH_NO_RETENTION_NOTICE` (set to `1` to silence the history-retention reminder printed on `tokdash serve`)
- `TOKDASH_SETUP_NO_OPEN` (set to `1` to skip the optional browser open at the end of `tokdash setup`)

For remote access through Tailscale Serve, SSH forwarding, or an explicit
network bind, see [`docs/guides/REMOTE_ACCESS.md`](../guides/REMOTE_ACCESS.md).
Interactive `tokdash setup` can configure and record the Tailscale Serve rule
after you opt in.

## Session active time (estimated)

Every session reports `active_ms` alongside `span_ms`. Span is first-to-last
event; active time subtracts idle by counting each gap between consecutive token
events only up to an idle cap, so a session left open overnight no longer reads
as a 14-hour session.

It is an estimate, and the API says so: `summary.active_time_estimated` is
`true` and `summary.active_time_method` is `capped-inter-event-gap`. The limits
follow from the method — a short pause between events is indistinguishable from
work, a single operation longer than the cap is truncated to it, and a session
with one token event measures zero because nothing precedes it.

Concurrent work is counted two ways. `active_ms` is clock time, with overlap
counted once; `active_ms_sum` adds the overlap up, i.e. agent time. Both appear
per session and per tool in `summary`. Kimi agents and Claude subagents run
alongside the main agent, and each is timed as its own stream: a subagent
working one minute in parallel adds a minute of agent time and none of clock
time.

- `TOKDASH_ACTIVE_GAP_CAP_SECONDS` (default: `300`) — idle cap in seconds; gaps longer than this contribute only the cap. Clamped to 1s–6h.

## Persistent usage DB (default on)

Tokdash maintains a local SQLite index at `~/.tokdash/usage.sqlite3` by default.
It stores parsed token rows and Codex/Claude/Kimi/DeepSeek Harness/Reasonix
session summaries so repeated dashboard and API reads can use indexed SQL
instead of reparsing every source log. Source logs remain the source of truth;
the DB is a local performance index, and Tokdash falls back to live parsing if
it is disabled or unavailable.

Cached session rows are price-neutral: they hold each turn's billing inputs
(model, fresh input, cache reads and writes, output), and cost is calculated
when they are read, with whatever pricing that process has loaded. Editing a
rate therefore reprices instantly instead of rereading gigabytes of logs, and
two Tokdash versions sharing one database do not invalidate each other's rows
over pricing. Sharing is only safe while both builds support the same database
schema, though: schema migrations run forward only, so once the newer build
migrates the file the older one refuses to open it until it is upgraded, and
every cached read fails until the versions match. `tokdash doctor` reports the
schema on disk alongside the one the running build supports. Parser and
source-file changes still reparse normally. Rows written before this (including
any kept by `TOKDASH_USAGE_DB_DURABLE` after their log is gone) reprice from
their stored totals, which reproduces the same figure but cannot separate a
Claude or Kimi cache write from fresh input; only a reparse of those logs can
restore that distinction. Codex bills under `provider/model` and stores the bare
name, so its older rows are reparsed once instead of reused.

Run a checkout against its own data directory so it never migrates the installed
service's database:

```bash
TOKDASH_DATA_DIR=output/dev-data PYTHONPATH=src python3 main.py
```

- `TOKDASH_USAGE_DB` (default: `1`) — set to `0`, `false`, `no`, or `off` to disable the persistent usage DB
- `TOKDASH_DATA_DIR` (default: `~/.tokdash`) — base directory for Tokdash local state
- `TOKDASH_USAGE_DB_PATH` (default: `$TOKDASH_DATA_DIR/usage.sqlite3`) — explicit SQLite file path
- `TOKDASH_USAGE_DB_DURABLE` (default: `1`) — keep already indexed rows if a source file temporarily disappears or a parser returns no rows; set to `0` for strict source replacement
- `TOKDASH_USAGE_DB_WATCH` (default: `0`) — set to `1` to run a background sync loop inside `tokdash serve`
- `TOKDASH_USAGE_DB_WATCH_INTERVAL` (default: `30` seconds) — sync interval for `tokdash db watch` and the serve-time watch loop

## Output speed cache

Output timing is built on demand in a separate `output_speed.sqlite3` beside the
configured usage database. Ordinary usage, Overview and core session reads do
not create it or start timing work. Model and session speed share one worker;
visible views observe its progress through events. Reader versions and native
DB/WAL identities invalidate timing independently of pricing.

- `TOKDASH_SPEED_CPU_FRACTION` (default: `0.25`, clamped to `0.05`–`1`) — duty-cycle target applied between file inputs; a large file can use one CPU continuously before its balancing pause. Native projection and SQL publication use the one-CPU/niceness limits without this pause.
- `TOKDASH_SPEED_MEMORY_MB` (default: `1024`) — worker address-space limit in MiB on platforms with resource limits. A failed build retains its last successful snapshot and reports the failure.

On Linux the worker also uses nice 10 and one available CPU. These controls apply
to the timing child, not the API process. The derived cache is disposable and
contains scalar measurements, verdicts and session memberships, rather than
transcripts. Primary schema migration removes old timing payloads and makes
their pages reusable; it does not automatically shrink the physical SQLite file.

Model/hour reads use a scalar covering index in the derived database. Existing
caches receive that index through the speed worker, rather than an HTTP request
or ordinary usage migration. Speed database connections allow a bounded 16 MiB
SQLite page cache. This memory and index storage are separate from Overview's
ordinary accounting path.

The timing worker records a process creation identity and probes Windows without
sending signals. Memory statistics are optional: lack of Unix `resource` support
does not fail extraction or publication, and macOS RSS units are normalized to MiB.
Failed input scopes do not suppress unrelated builds. Model/hour aggregate results
use a demand-only process cache (32 entries, 8 MiB encoded results), invalidated by
publication revision and filters; Overview does not import or access that cache.

## DB maintenance commands

```bash
tokdash db status --pretty
tokdash db sync --pretty
tokdash db verify --verify-period today --pretty
tokdash db repair --dry-run --pretty
tokdash db resync --pretty
tokdash db watch --pretty
```
