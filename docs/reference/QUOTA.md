# Quota tracking internals

The companion to the README's [Quota tracking](../../README.md#quota-tracking-optional)
section: the consent commands, the master switch, the poller, consent semantics,
and the per-provider credential and accounting notes.

## Consent commands

```bash
tokdash quota consent --codex-api on --claude-api on --antigravity-api on
tokdash quota consent --minimax-api on --kimi-api on --grok-api on --zai-api on
tokdash quota consent --opencode-go-api on
tokdash quota consent --commandcode-api on
tokdash quota consent --credential-scan on   # allow the disclosed local credential readers
tokdash quota consent --poll-interval 30      # background poll cadence: 15, 30, 60 or 120 min
tokdash quota consent --enabled off           # master switch: turn ALL quota tracking off
tokdash quota poll
tokdash quota show
```

## Master switch

`quota.enabled` (default on) turns *all* quota work on or off — session
scanning, network polling, and snapshot writes. Toggle it from the Quota tab or
with `tokdash quota consent --enabled on|off`. When it is off (or the
`TOKDASH_QUOTA_POLL=0` kill switch is set), the background poller idles
completely, `GET /api/quota/refresh` returns a "quota tracking disabled" error,
and the tab shows an *enable quota tracking* card instead of data. Per-provider
consent keys keep their narrower network-only meaning.

## Poll interval

The background poller snapshots every **30 minutes** by default. Choose
15/30/60/120 minutes from the Quota tab, during `tokdash setup`, or with
`tokdash quota consent --poll-interval N`; it is saved as
`quota.poll_interval_minutes` in `config.json`. The
`TOKDASH_QUOTA_POLL_INTERVAL` env var (seconds, floor 300) overrides the saved
value, and the tab shows which source is active. Interval changes apply on the
next poll cycle without restarting the server. Codex session ingestion is
incremental — after a one-time backfill of your history, each cycle only
tail-reads session files that grew, so a steady-state poll costs single-digit
milliseconds.

## Reset-boundary sampling

For fixed-reset quota windows, the poller also samples near the reset boundary
so history captures the pre-reset high and post-reset baseline. Boundary
sampling is enabled by default, calls only the provider whose window triggered
it, coalesces nearby provider boundaries, and keeps at least 300 seconds between
daemon poll cycles. Set `TOKDASH_QUOTA_BOUNDARY_POLL=0` to disable it,
`TOKDASH_QUOTA_BOUNDARY_POST=0` to disable only post-reset samples, or adjust
the default 120-second leads with `TOKDASH_QUOTA_BOUNDARY_PRE_SECONDS` and
`TOKDASH_QUOTA_BOUNDARY_POST_SECONDS`.

## Consent semantics

Live polling requires two separate decisions: `quota.credential_scan` permits
read-only access to the disclosed local credential stores, then each
`<provider>_api` key permits that provider's network request. Tokdash reads
native CLI auth/config files, OpenCode's `auth.json` plus global provider
config, active Claude settings, and CC Switch's `providers` table through a
read-only SQLite connection. It never scans provider logs, shell profiles, or
arbitrary `{file:...}` references. On macOS, Claude Code may require a one-time
read-only Keychain approval. Tokdash never refreshes or writes provider
credentials. `TOKDASH_QUOTA_POLL=0` is a hard kill switch for all quota
tracking. `tokdash export` excludes quota data by default; use
`--include-quota` only when you intentionally want it in the JSON.

`tokdash setup` offers an optional quota step (per-provider network consent,
default No, plus the poll interval), and `tokdash doctor` reports the quota
state: master switch, per-provider consent, kill switch, effective interval and
its source, last poll time, and the stored snapshot count.

## Multiple Claude Code installs

Claude Code keeps one subscription per config directory, so a second sign-in you
run as `CLAUDE_CONFIG_DIR=~/.claude-academic claude` is a second subscription
with its own windows. With credential scanning consented, Tokdash reads
`$CLAUDE_CONFIG_DIR` plus every `~/.claude*` directory that has its own
`.credentials.json`, polls each one separately, and groups them inside the
Claude Code card under the profile name the directory was set up with
(`academic`). History keeps them apart too: `Claude-academic 5-hour` is its own
series beside `Claude 5-hour`. An install with an expired sign-in shows its own
notice instead of hiding a working install's numbers, and two directories
holding the same sign-in count once. Set `TOKDASH_CLAUDE_PROFILES` to a
path-separated list of directories for installs that live outside your home
directory. Usage totals needed nothing: session logs under every `~/.claude*`
install have been counted for some time.

## Claude Code limit resets

When Anthropic gives your account limit resets (the ones Claude Code's
`/limit-reset` spends), the Claude Code card lists them in the same **Reset
Credits** block the Codex card uses, with each reset's expiry, under the install
that holds it. They arrive in the same usage request, with `?cedar_ember=1`
added. Anthropic only lists them for Claude Code's own surface, so that
request's User-Agent starts with Claude Code's `claude-cli/…` and then names
`tokdash/<version>`. Tokdash only reads resets; spending one still happens in
Claude Code.

## Per-provider credential sources

- **MiniMax** accepts an `mmx` sign-in or Token Plan Subscription Key
  (`MINIMAX_TOKEN_PLAN_GLOBAL_KEY` / `MINIMAX_TOKEN_PLAN_CN_KEY`); a normal
  pay-as-you-go key is not guaranteed to have Token Plan quota.
- **Kimi** accepts a Kimi Code sign-in/key (`KIMI_API_KEY`), not a Moonshot Open
  Platform pay-as-you-go key.
- **SuperGrok / Grok Build** quota requires the xAI OAuth sign-in in
  `$GROK_HOME/auth.json`; a normal xAI API key cannot access consumer billing.
- **Z.ai** accepts a Coding Plan key from `$ZCODE_HOME/v2/config.json`, a
  supported tool configuration, `ZAI_API_KEY`, or `Z_AI_API_KEY`, and queries
  the plan's 5-hour/weekly credit windows plus legacy MCP limits.
- **OpenCode Go** uses `OPENCODE_API_KEY` first, then the `opencode-go` key from
  OpenCode's `auth.json`, and reads the rolling/weekly/monthly subscription
  windows from `opencode.ai/zen/go/v1/usage`. Zen pay-as-you-go balance has no
  key-auth endpoint and is not tracked.
- **Command Code** reads its account key from `COMMAND_CODE_API_KEY`, then
  `COMMANDCODE_API_KEY`, then `~/.commandcode/auth.json`, then the
  `commandcode` entry in OpenCode's `auth.json` (never refreshed or rewritten).
  It polls `api.commandcode.ai/alpha/billing/credits` and
  `/alpha/billing/subscriptions` for the 5-hour and Weekly usage caps plus the
  Monthly credit window. The 5-hour and Weekly bars are reported directly;
  **Monthly is derived** — the plan catalog gives the pool, remaining credits
  are subtracted from it, and the result is labelled with the resolved plan
  (Go / GOAT / Pro / Max 10x / Max 20x / Team Pro / Provider). When no active
  subscription and plan can be resolved, the Monthly bar and its plan label are
  withdrawn rather than left showing the previous reading.

## Retention

Quota snapshots and their history live in the local usage database
(`usage.sqlite3`, enabled by default) and are **kept indefinitely by default** —
set `TOKDASH_QUOTA_RETENTION_DAYS` to a positive number of days to prune older
snapshots. If you opt out of local persistence with `TOKDASH_USAGE_DB=0`, the
Quota tab loses its main data path: no snapshot history is kept, the background
poller does not run, and the tab only shows in-memory results from a manual
**Refresh** (network providers with consent) for the lifetime of the current
server process. Keep the usage DB enabled (the default) for normal quota
tracking.

## Background poll and reset-boundary sequence

The daemon in `cli.py` schedules regular polls and consults the quota boundary planner for earlier provider-scoped samples. Credential readers use the disclosed local CLI configuration and auth files; successful collection is persisted as quota snapshots in the usage database.

```mermaid
sequenceDiagram
    autonumber
    participant Daemon as Quota poll daemon (cli.py)
    participant Planner as _plan_next_quota_poll
    participant Boundary as Boundary planner (quota/__init__.py)
    participant Store as UsageEntryStore (SQLite)
    participant Paths as clientpaths.py
    participant Config as Local CLI configs and auth stores
    participant Providers as Provider quota APIs

    Daemon->>Store: Read latest quota snapshots
    Store-->>Daemon: Current fixed-window reset timestamps
    Daemon->>Planner: Plan next wake with current time and snapshots
    Planner->>Planner: Calculate jittered regular interval
    Note over Planner: RESET_JITTER_SECONDS filters near-now candidates
    Planner->>Boundary: plan_boundary_poll(..., minimum 300-second delay)
    Boundary->>Boundary: _boundary_candidate_details computes pre-reset and post-reset candidates
    Boundary-->>Planner: Earliest coalesced boundary and provider set, if sooner
    Planner-->>Daemon: Sleep duration and optional boundary target
    Daemon->>Daemon: Sleep, then recheck tracking and consent
    Daemon->>Paths: Resolve disclosed credential locations
    Paths->>Config: Read local CLI credential/config files
    Config-->>Paths: Credentials and provider settings
    Paths-->>Daemon: Credential-backed provider sources
    Daemon->>Providers: Request quota for enabled providers
    Providers-->>Daemon: Quota windows and reset timestamps
    Daemon->>Store: Insert quota snapshots and update poll metadata
    Note over Store: Snapshot writes use SQLite transactions; local Codex snapshots and their watermarks commit atomically
```

---

← Back to the [README](../../README.md) · [中文 README](../../README_CN.md)
