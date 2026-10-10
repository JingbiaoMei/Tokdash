# Tokdash API Reference

Tokdash exposes a local HTTP API (FastAPI) for querying token usage, costs, and session data across Claude Code, Codex, OpenClaw, and other supported tools.

- **Default bind:** `127.0.0.1:55423`
- **Start:** `tokdash serve --bind 127.0.0.1 --port 55423`
- **OpenAPI schema:** `GET /openapi.json` (feed it to any OpenAPI viewer or client generator)
- **Interactive docs:** none. Tokdash doesn't serve `/docs` (Swagger UI) or `/redoc`, because
  both load their scripts from a third-party CDN onto the dashboard's origin. This page is
  the reference.

All endpoints return JSON. The API is unauthenticated and intended to bind to loopback
only. **State-changing requests are gated** (loopback bind + Host/Origin allowlist +
per-session token); see [`docs/SECURITY.md`](../SECURITY.md) and `PUT /api/pricing-db` below.

---

## Endpoint Summary

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness probe (with a Tokdash fingerprint) |
| `GET` | `/api/version` | Runtime version + setup install method |
| `GET` | `/api/csrf-token` | Per-session write token (loopback/same-origin only) |
| `GET` | `/api/update-check` | Opt-in cached PyPI version check (read-only) |
| `POST` | `/api/update-check/consent` | Persist one-time update-check consent (write-gated) |
| `GET` | `/api/update/capability` | Click-to-update eligibility + target (live job details require a session) |
| `POST` | `/api/update/enroll` | Redeem a host-minted pairing code for a remote session |
| `POST` | `/api/update/start` | Admit/attach one update job and launch the isolated updater |
| `GET` | `/api/update/status` | Job progress for the click-to-update flow (session-gated when remote) |
| `GET` | `/api/usage` | Aggregated token usage and cost across all tools |
| `GET` | `/api/tools` | Per-tool usage breakdown (coding apps only) |
| `GET` | `/api/quota` | Current subscription quota state from local snapshots |
| `GET` | `/api/quota/history` | Quota utilization and derived consumption history |
| `POST` | `/api/quota/consent` | Persist per-provider quota API consent (write-gated) |
| `POST` | `/api/quota/settings` | Persist the quota master switch and poll interval (write-gated) |
| `GET` | `/api/quota/refresh` | Run an immediate consented quota API poll (read-only, cooldown) |
| `GET` | `/api/sessions` | List sessions for a given tool |
| `GET` | `/api/session` | Detailed turns for a single session |
| `GET` | `/api/active-time` | Estimated active time across every session tool |
| `GET` | `/api/output-speed` | Measured model throughput and within-model hour-of-day comparison |
| `POST` | `/api/session-speeds` | Cache-only, selected-range speed summaries for up to 50 sessions |
| `GET` | `/api/session-speed` | One session's response timeline and model-specific aggregates |
| `POST` | `/api/output-speed/ensure` | Request timing builds for a bounded batch of sessions |
| `GET` | `/api/output-speed/jobs/{job_id}` | Read one timing build's state and progress |
| `GET` | `/api/output-speed/jobs/{job_id}/events` | Observe that timing build through server-sent events |
| `GET` | `/api/codex/sessions` | Convenience wrapper: Codex sessions |
| `GET` | `/api/codex/session` | Convenience wrapper: single Codex session |
| `GET` | `/api/openclaw` | OpenClaw model breakdown |
| `GET` | `/api/stats` | Annual stats aggregation |
| `GET` | `/api/insights` | Fine-grained analytics (hour-of-day, weekday, heatmap, projects, streaks) |
| `GET` | `/api/pricing-db` | Current pricing database snapshot |
| `PUT` | `/api/pricing-db` | Update the pricing database (write-gated, requires token) |
| `GET` | `/` | Web dashboard (HTML) |

---

## Period parameter

Most endpoints accept a `period` query parameter. Supported values:

| Value | Meaning |
|---|---|
| `today` (default) | Current day (00:00 local time → now) |
| `3days` | Last 3 days |
| `week` | Last 7 days |
| `14days` | Last 14 days |
| `month` | Current calendar month (1st → today) |
| `year` | Last 365 days |
| `all` | All recorded history |
| `<integer>` | Last N days (e.g. `"30"` for 30 days) |
| `<integer><unit>` | Shorthand, where unit is `d`/`w`/`m`/`y` (e.g. `7d`, `2w`, `3m`, `1y`) |

For arbitrary ranges, use `date_from` and `date_to` (format `YYYY-MM-DD`) where supported.

### Unrecognised periods, and the `range` block

A `period` that matches none of the above resolves to **all time**, and the response is
still `200`. On its own the echoed `period` field cannot show this, because it repeats the
caller's own token back — so a consumer that sent a typo receives a century of data under
its own label.

Every period-taking response therefore carries a `range` block describing the window that
was actually queried:

```json
"range": {
  "period_requested": "7d",
  "period_resolved": "week",
  "from": "2026-08-26",
  "to": "2026-09-01",
  "days": 7,
  "recognized": true
}
```

`period_resolved` is always a value you could have sent yourself to get the same window
(`custom` when `date_from`/`date_to` were used). **Check `recognized`:** when it is `false`
the period was not understood and `from`/`to` show the all-time window that was substituted.
`days` reflects the window actually queried, not the nominal mapping — `month` on the 1st of
the month is 1 day, not 30.

---

## `GET /health`

Liveness check. Carries a distinctive `service`/`version` fingerprint so a port probe can
tell "this is Tokdash" rather than trusting a generic `{"status":"ok"}` any app could return.

`instance_id` identifies this daemon, so a dashboard that reaches one machine over
several URLs (loopback, Serve name, SSH forward) can recognise it as one server instead
of counting its tokens twice. It is a random UUID4 created on first start and kept in
`<data_dir>/instance.json`: it survives a restart, two daemons sharing a data directory
agree on it because they read the same session logs, and two daemons in two directories
differ even on one machine. The field is omitted rather than guessed when the file can be
neither written nor read, and the daemon logs one warning naming that file. A file it
cannot parse is left exactly as it is and is not re-read until the daemon starts again, so
clearing that up means deleting the file and restarting Tokdash.

It is a durable identifier for this machine, so it is covered by the same origin policy as
everything else on this route, and that policy must not be widened to make it readable
from a page that cannot already read the daemon.

**Response**
```json
{
  "status": "ok",
  "service": "tokdash",
  "version": "1.0.7",
  "instance_id": "6c1f2b0e-9a4d-4c37-8f1e-2d0b5a7c9e11"
}
```

---

## `GET /api/version`

Local version/provenance. `install_method` is read from the setup manifest
(`<data_dir>/install.json`) when present, else `null`.

`usage_db_schema_supported` is the usage-database schema version this build can
read — a compile-time constant, not a read of the database, so this route stays
as cheap as `/health`. Comparing it across two Tokdash processes that share a
data directory is how a version skew is spotted without opening the file;
migrations run forward only, so the older build cannot read a database the newer
one has migrated. `tokdash doctor` reports the schema actually stored on disk.

**Response**
```json
{
  "service": "tokdash",
  "runtime_version": "1.0.7",
  "install_method": "pipx",
  "update_check_enabled": false,
  "usage_db_schema_supported": 9
}
```

---

## `GET /api/csrf-token`

Issues the per-session write token the dashboard echoes back as `X-Tokdash-Token` on
mutating requests. Returns `403` unless the server is loopback-bound and the request's
`Host`/`Origin` are in the loopback allowlist (so a page on another localhost port cannot
read it).

**Response**
```json
{ "token": "<per-session-token>" }
```

---

## `GET /api/update-check`

Opt-in, default-off PyPI version check (see `docs/guides/ONBOARDING.md` → Update checks). **Read-only**
— PyPI read plus an in-memory cache, no disk write — so it is served as `GET` and is **not**
write-gated; it works over Tailscale/WSL/any forward like `GET /api/quota/refresh` (see
`docs/SECURITY.md`). No-op unless update checks are enabled (`TOKDASH_UPDATE_CHECK=1` or saved
consent). Result is cached for hours; never an automatic/background call, and this endpoint
itself only *reports*. Starting an upgrade is a separate, gated flow — the eligible managed
install's dashboard `POST /api/update/start` (see the update endpoints above and
`docs/guides/ONBOARDING.md` → "Dashboard updates"), or `tokdash update` from a terminal.

**Response (enabled)**
```json
{ "enabled": true, "current": "1.0.7", "latest": "1.0.8", "update_available": true, "error": null, "cached": false }
```
**Response (disabled)**
```json
{ "enabled": false, "update_available": false }
```

---

## `POST /api/update-check/consent`

Persists one-time consent (`update_check: true`) to `<data_dir>/config.json` so update checks are
enabled. **Write-gated** like all mutations. `TOKDASH_UPDATE_CHECK=0` remains a hard kill switch that
overrides saved consent.

**Response**
```json
{ "enabled": true }
```

---

## `GET /api/quota`

Returns current subscription quota state. This route never performs provider network I/O; it reads the local `quota_snapshots` table (and local plan/tier metadata). Session files are not scanned here — the background poller ingests them. Provider API polling is default-off and happens only through `GET /api/quota/refresh`, the background poller after consent, or `tokdash quota poll`.

`enabled` is the quota master switch (`config.json` `quota.enabled`, default `true`, forced `false` by the `TOKDASH_QUOTA_POLL=0` kill switch). When it is `false` the dashboard renders an *enable quota tracking* card instead of provider data. `poll.interval` is the **effective** interval in seconds and `poll.interval_source` is one of `env` / `config` / `default`.

**Response shape**
```json
{
  "providers": {
    "codex": {
      "network_enabled": false,
      "plan": "pro",
      "buckets": [
        {"bucket": "5h", "bucket_label": "5-hour window", "used_percent": 25.0, "resets_at": 1782910800}
      ]
    },
    "claude": {
      "network_enabled": true,
      "plan": "Max 5x",
      "buckets": [
        {"account": "default", "bucket": "session", "bucket_label": "Session", "used_percent": 40.0, "resets_at": 1782910800},
        {"account": "academic", "bucket": "academic_session", "bucket_label": "Session", "used_percent": 12.0, "resets_at": 1782910800}
      ],
      "accounts": [
        {"account": "default", "plan": "Max 5x", "tier": "default_claude_max_5x", "status": "ok", "credential_path": "/home/me/.claude/.credentials.json", "status_detail": null, "status_at": null, "updated_at": 1782910800},
        {"account": "academic", "plan": "Pro", "tier": "default_claude_pro", "status": "ok", "credential_path": "/home/me/.claude-academic/.credentials.json", "status_detail": "stale_token", "status_at": 1782910800, "updated_at": 1782910800}
      ]
    }
  },
  "consent": {
    "credential_scan": false,
    "codex_api": false,
    "claude_api": false,
    "antigravity_api": false,
    "minimax_api": false,
    "kimi_api": false,
    "grok_api": false
  },
  "enabled": true,
  "poll": {
    "enabled": true,
    "network_enabled": false,
    "interval": 1800,
    "interval_source": "default",
    "interval_minutes": 30,
    "interval_choices": [15, 30, 60, 120],
    "last_run": null,
    "kill_switch": false
  }
}
```

Every bucket row carries the `account` it belongs to.

**`accounts`** is additive and lists the accounts a card measures separately: Claude Code
installs (`~/.claude`, plus every `~/.claude-<profile>` sibling with its own
`.credentials.json`) and MiniMax regions. It appears only once a card measures more than one
account, so a single-account card's payload is unchanged, and only for those two providers —
everywhere else the stored account id is a placeholder or a rotating id, and the card measures
one account. The two extra fields `tier` and `credential_path` are Claude's own. Claude's whole
list needs `credential_scan` consent, because which installs exist, and each one's plan, comes
off the filesystem: with consent withheld the stored rows still render, unnamed.

`status` and `status_detail` name the **newest error any of the provider's accounts is still
carrying**, with `status_at` dating it. A card speaks for every credential behind it, so one
broken credential keeps warning about the provider (see
`companion/contract/COMPANION_API.md`); `accounts` is what says whose error it is, and a card
that can read the list prints the notice under that account rather than over the whole card.
Errors clear per account: an account's own newer success retires its error, a success on
another account does not silence it, and a recovered account stops warning the card.
The synthetic account a credential-less failure is filed under is the one exception: nothing
it was asked about ever answered, so it cannot recover by its own answer, and a newer success
on any of the provider's real accounts retires it. Otherwise a credential file missing for one
poll cycle keeps the card reading "couldn't refresh" after the provider recovered. That
exception is decided by whether the account has ever reported a successful API observation,
not by its name: an account named `default` because the credential carried no id of its own
(an Antigravity sign-in with no email on its ID token, every Kimi, Z.ai, OpenCode Go and
Command Code account) is a real account, and its error keeps warning the card until it
answers again.

`status_account` ships beside `accounts` and names which entry the card's `status_detail`
belongs to, or is `null` when it belongs to none of them — a provider whose credentials could
not be read at all records that failure under a synthetic account that is not a credential and
is not listed. It is there because "does any account carry an error" is a different question
with a different answer: a card that cannot read its credentials *and* holds an older
per-account failure would otherwise read as attributed, and an unreadable provider would count
as working. Absent on a single-account card, where the card's error is that account's by
construction.

`plan` stays provider-wide. `providers.claude.plan`, `tier` and `credential_path` describe the
default install where there is one and the measured install where there is not, so an existing
consumer sees what it always did — the per-install plans are in `accounts[].plan`.

**`reset_credits`** is the reset inventory, in one shape for both providers that have it:
`{"available_count": 1, "credits": [{"id": "...", "title": "...", "expires_at": ...}]}`.
Codex's comes from `wham/rate-limit-reset-credits` and passes its credits through as the
server sent them (`expires_at` an ISO string). Claude Code's limit resets come from the same
usage request as its windows (`?cedar_ember=1`). Each credit there is a grant that can be
spent now, with `expires_at` in epoch seconds, `resets_left`, `clears` (the limit types it
refills) and `status: "available"`, soonest expiry first. A Claude account appears only while
it holds at least one reset, and only while those resets came from its newest successful poll:
a poll that answered without the reset block hides them rather than leaving an old count up.
`providers.claude.reset_credits` belongs to the card's primary install (the default install, or
the first install when there is no default), and never falls back to another install's the way
`plan` can. Each install's own copy is on its `accounts[]` entry. That key is absent, not null,
on an install without resets.

A Claude install leaves both `buckets` and `accounts` when it can no longer be polled, which
happens two ways.

**Its config directory is observed to be gone** — a renamed or deleted install, a
`~/.claude-<profile>` sibling or `~/.claude` itself once the sign-in has moved to a sibling.
Absence has to be observed: the test is whether the listing that names the installs (the home
directory, or the paths in `TOKDASH_CLAUDE_PROFILES`) could be read, named at least one
install, and did not name this one.

This is about what this machine holds, not whether the subscription still works. A sign-in
whose token Anthropic has stopped accepting is still a sign-in we have: the install is
polled, it keeps its windows and its `stale_token` warning, and the card says so. Only a
sign-in that is no longer on this machine stops being reported.

**Its sign-in is observed to be gone.** `claude logout` deletes a sibling's
`.credentials.json` and leaves the directory, so the listing still names the install — but a
sibling is only polled while it has that file, so from the logout on nothing refreshes its
window rows and nothing supersedes the error it last recorded, `stale_token` included. A card
that kept those rows would quote numbers that can never be corrected again, and give advice
about refreshing a sign-in that is not there. The default install is exempt: it is polled
whether or not it has a credential, so its own `unavailable` row is what names its state, and
its rows are what drive the consent and "not detected" card.

An install that is present and **merely unreadable right now** keeps its data — a permissions
error on the file, an install directory that cannot be searched. Either can clear on its own,
and the path still holds a file, so the install is still polled; only the two triggers above
stop it. "No sign-in" is asked in the polling rule's own terms, which demand a regular file, so
a directory left where `.credentials.json` was counts as absent: nothing could poll it either.
What neither covers is a credential that vanishes and comes back — an unlink-then-relink, a
dangling symlink, an atomic rename caught mid-flight — which reads as absent and blanks that
install's bars for one poll cycle. Nothing is deleted, so the next cycle restores them.

Nor is anything retired when the answer is unavailable rather than negative: the listing could
not be read, it named no install at all (an unmounted or still-locked home, which is not the
news that every subscription was deleted), or `quota.credential_scan` consent is off.

Neither rule deletes anything. Retirement hides stored rows from the current card, so an
install that signs in again, or reappears, has its windows back on the next poll; history is
unaffected either way.

What membership measures is directory presence, not the name a directory is given on this run.
`CLAUDE_CONFIG_DIR` reassigns account names around whichever install it points at — aim it at
`~/.claude-academic` and that install becomes `default` while `~/.claude` becomes `claude` — so
an install is recognised under every name it could have been stored under, and changing the
variable does not retire the install it renamed.

Row age is deliberately not part of any of this: it cannot tell a removed install from a
provider nothing has polled lately, and since Claude API polling is opt-in the second case is
the common one.

## `GET /api/quota/history`

Returns stored quota utilization points and derived consumption deltas.

**Query parameters**

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `providers` | comma-separated string | no | all | Filter providers, e.g. `codex,claude` |
| `granularity` | `hour` or `day` | no | `hour` | Period used to aggregate consumption deltas |
| `start` | integer epoch seconds | no | – | Inclusive lower bound |
| `end` | integer epoch seconds | no | – | Inclusive upper bound |
| `max_points` | integer | no | `300` | Max points per series; series longer than this are evenly downsampled, always keeping the most recent point. Must be a positive integer. |

History series are unified per `(provider, bucket)`: a Codex session row (account `default`) and an API row (real account id) for the same window merge into one series, keeping the freshest point on a timestamp collision. MiniMax uses region-qualified bucket IDs so global and mainland-China Token Plans remain separate series, and a `~/.claude-<profile>` install qualifies its windows the same way (`academic_session`) so two Claude subscriptions stay two series. Series are always bounded by `max_points` (points and consumption deltas are downsampled independently).

## `POST /api/quota/consent`

Persists the separate local-credential-read consent (`credential_scan`) and per-provider quota API **network** consent to `<data_dir>/config.json`. Network polling requires both `credential_scan=true` and the matching provider flag. **Write-gated** like all mutations. `TOKDASH_QUOTA_POLL=0` remains a hard kill switch.

**Request**
```json
{"credential_scan": true, "codex_api": true, "minimax_api": true, "kimi_api": true, "grok_api": true, "commandcode_api": true}
```

**Response**
```json
{"consent": {"credential_scan": true, "codex_api": true, "claude_api": false, "antigravity_api": false, "minimax_api": true, "kimi_api": true, "grok_api": true, "commandcode_api": true}}
```

## `POST /api/quota/settings`

Persists the quota master switch and background poll interval to `<data_dir>/config.json` (`quota.enabled` and `quota.poll_interval_minutes`). Both fields are optional. **Write-gated** like all mutations. A `poll_interval_minutes` outside `[15, 30, 60, 120]` returns `400`.

**Request**
```json
{"enabled": true, "poll_interval_minutes": 30}
```

**Response**
```json
{"enabled": true, "config_enabled": true, "poll_interval_minutes": 30, "interval": 1800, "interval_source": "config"}
```

## `GET /api/quota/refresh`

Runs an immediate network poll for consented providers and stores snapshots in the local usage DB. This only reads providers' usage endpoints (no quota is consumed), so it is served as `GET`, not write-gated, and works over Tailscale Serve/WSL/any forward — see [`SECURITY.md`](../SECURITY.md#quota-refresh-and-update-check-are-read-only-gets). It is still rate-limited with a 60 second cooldown (`429`). It never refreshes provider tokens; expired tokens produce stale-token snapshots. Returns `409` when quota tracking is disabled (master switch off or `TOKDASH_QUOTA_POLL=0`).

**Response**
```json
{"snapshots": 3, "inserted": 3}
```

---

## `GET /api/usage`

Aggregated token usage and cost across all configured tools.

**Query parameters**

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `period` | string | no | `"today"` | See [Period parameter](#period-parameter) |
| `date_from` | string | no | – | Start date (`YYYY-MM-DD`). Overrides `period` when paired with `date_to`. |
| `date_to` | string | no | – | End date (`YYYY-MM-DD`). |

**Response fields**

| Field | Type | Description |
|---|---|---|
| `period` | string | Period that was queried |
| `total_tokens` | int | Total tokens across all tools |
| `total_cost` | float | Total cost in USD |
| `total_messages` | int | Total assistant/user message count |
| `sparkline` | object\|null | Matching local-time buckets from the same aggregation as the totals; hourly for one calendar day, daily for 2–31 days, monthly for 32–366 days, `null` for longer or unsupported windows |
| `by_tool` | object | Per-tool aggregates: `{ tool_name: { tokens, cost } }` |
| `apps` | object | Per-app detailed breakdown (includes `tokens_in`, `tokens_out`, `tokens_cache`, `cost`, `messages`, `models[]`) |
| `coding_apps` | object | Same shape as `apps`, filtered to coding tools (excludes browser/research tools) |
| `coding_models` | array | Flat list of models from coding apps, each tagged with `source` |
| `top_models` | array | First five entries of `combined_models` |
| `top_models_by_cost` | array | The five costliest models, ranked by cost |
| `openclaw_models` | array | OpenClaw-specific model breakdown |
| `combined_models` | array | All models from all sources, merged |
| `comparison` | object | Comparison vs previous period: `tokens_prev`, `cost_prev`, `messages_prev`, `tokens_pct`, `cost_pct`, `messages_pct`. For an explicit `date_from`/`date_to` the prior period is the equal-length range ending the day before `date_from`, which can land wholly or partly before the first recorded session; a `tokens_pct` over +1000% usually means exactly that, so a consumer printing a delta should bound what it prints |
| `timestamp` | string | ISO 8601 timestamp when the response was generated |

**Model ordering**

Every model array in this response -- `coding_models`, `top_models`,
`openclaw_models`, `combined_models`, and each `apps[].models` -- is ordered by
**total tokens, descending**, with cost and then name breaking ties. `/api/insights`
uses the same ordering for its `models` facet.

`top_models_by_cost` is the one exception: it is ordered by **cost, descending**,
with tokens and then name breaking ties. It is served rather than left to the
caller because it cannot be derived from `top_models` -- the five biggest models
need not contain the five costliest, so a client holding only the token podium
has no way to compute the spend one. Both podiums are drawn from the same
`combined_models` list.

These arrays were ordered by cost in earlier versions, against what this table
said. Tokens and cost usually rank models the same way over a week or a month,
which is why the mismatch was easy to miss; over a year, where a cheaper model
can out-work a pricier one, they diverge. For the spend ranking beyond the top
five, sort `combined_models` client-side.

`sparkline` has `{granularity: "hour"|"day"|"month", buckets: [...]}`. Each sparse bucket
contains `key` (`YYYY-MM-DDTHH`, `YYYY-MM-DD`, or `YYYY-MM`), `tokens`, unrounded `cost`,
`messages`, `input` (including cache writes), `cache` (cache reads), and `models`
(canonical model name to tokens). It uses the headline's pricing and message
counting rules. Model visibility is evaluated across the selected range: a
tokenless bucket retains its fees and messages when that model has visible
token usage elsewhere in the range. A missing bucket at or before `timestamp`
means zero recorded usage; a future bucket is unknown. Hourly responses also provide `keys` listing
the clock hours that exist on that date. Repeated hours are combined; an hour
skipped by a clock change is unavailable. Cache rate is `cache / (input + cache)`;
no prompt input is unavailable. Monthly buckets contain only the selected portion
of each local calendar month; a 366-day window has at most 13 buckets. The
current month is partial, and leap days and clock changes retain real elapsed
time for agent durations.
The Overview fixes the Top Model for the whole range and plots that model's
bucket tokens. These buckets do not depend on the annual Stats snapshot, and
travel in the existing cached response without another API call or source scan.
Displayed range costs use decimal half-even cent rounding after an eight-decimal
normalization of binary summation noise. Model and bucket costs stay unrounded;
changing the grouping resolution cannot flip an exact half-cent total.

**Per-app object shape**

```jsonc
{
  "tokens": 45990135,        // total tokens
  "tokens_in": 7786995,      // input tokens (non-cache)
  "tokens_out": 375005,      // output tokens
  "tokens_cache": 37828135,  // cache read + write tokens
  "cost": 39.52,             // USD
  "messages": 566,
  "models": [
    {
      "name": "anthropic/claude-opus-4-7",
      "tokens": 23980934,
      "tokens_in": 1468105,
      "tokens_out": 233771,
      "tokens_cache": 22279058,
      "cost": 26.15,
      "messages": 196
    }
  ]
}
```

**Example**
```bash
curl -s http://127.0.0.1:55423/api/usage?period=today | jq '{total_tokens, total_cost}'
# { "total_tokens": 71091234, "total_cost": 56.4 }
```

---

## `GET /api/tools`

Per-tool breakdown limited to coding apps (excludes auxiliary tools like browser/research).

**Query parameters**

| Name | Type | Required | Default |
|---|---|---|---|
| `period` | string | no | `"today"` |

**Response fields**

| Field | Type | Description |
|---|---|---|
| `total_tokens` | int | Sum across coding tools |
| `total_cost` | float | Sum in USD |
| `total_messages` | int | Message count |
| `apps` | object | Same per-app shape as `/api/usage` `apps` field |

---

## `GET /api/sessions`

List of sessions for a specific tool.

**Query parameters**

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `tool` | string | **yes** | – | Session tool name. Accepted values are the ones registered in `SESSION_TOOLS` (`src/tokdash/sessions.py`) -- the enumeration lives with the code, not here |
| `period` | string | no | `"today"` | See [Period parameter](#period-parameter) |
| `date_from` | string | no | – | Start date (`YYYY-MM-DD`) |
| `date_to` | string | no | – | End date (`YYYY-MM-DD`) |
| `include_review_sessions` | boolean | no | `false` | Include Codex review / auto-permission sessions (hidden by default) |

**Response fields**

| Field | Type | Description |
|---|---|---|
| `tool` | string | Echo of tool param |
| `tool_label` | string | Human-readable name (e.g. `"Claude Code"`) |
| `period` | string | Period queried |
| `latest_session` | object | Most recent session (same shape as items in `sessions[]`) |
| `sessions` | array | All sessions in the period, sorted by `last_seen_at` desc |

**Costs**

`cost` is calculated when the response is built, from the billing inputs stored
per turn (model, fresh input, cache reads, cache writes, output) and the pricing
database this process has loaded. Two Tokdash builds sharing one usage database
each report under their own pricing.

Editing a rate reprices `codex`, `claude` and `kimi` without rereading any
source log: those are the tools whose parsed sessions are cached in the usage
database, and pricing is no longer part of the signature that invalidates a
cached row. `opencode`, `pi_agent` and `mimo` are read live from their own
databases and logs on each cold request, keyed on the pricing signature among
others, so a rate edit does make them reread.

Non-zero provider-reported costs from OpenCode, Pi and Mimo are kept verbatim —
Pi from `usage.cost.total`, the other two from the message's `cost`. They are the
provider's own figures, so no rate edit moves them. Zero means the provider
reported nothing (plan and subscription accounts), and those turns are estimated
from rates like any other. Qoder CLI credit rows behave the same way in Sessions:
their cost is the transcript's credits at the estimated `QODER_USD_PER_CREDIT`
rate, fixed at parse time and never repriced; token-only qoder_cli turns price
from the DB like everyone else's.

Rows written before turns carried billing inputs — including rows kept by
`TOKDASH_USAGE_DB_DURABLE` after their source log disappeared — are priced from
their stored totals instead. That reproduces the same number under any rates,
because every cached source billed a turn as
`get_cost(model, tokens_in, tokens_out, tokens_cache, 0)`, but it cannot separate
a Claude or Kimi cache write from fresh input again. If a provider ever prices
those apart, only reparsing those logs restores the distinction; a durable row
whose log is gone keeps the combined figure.

Codex is the exception: it bills under `provider/model` and stores the bare
name, and the pricing file keys some aliases by provider, so its rows from
before that change are reparsed once rather than reused. A durable Codex row
whose log is already gone cannot be, and prices under the bare model name — if
that name later gets a provider-specific rate, that row keeps the unqualified
one.

**Session object shape**

```jsonc
{
  "tool": "claude",
  "session_id": "5a8aafce-67f6-4e08-8963-c01eebf9f520",
  "project": "howard",
  "model": "claude-opus-4-7",
  "token_events": 14,         // number of recorded API calls
  "tokens_in": 72528,
  "tokens_cache": 1136969,
  "tokens_out": 6161,
  "tokens_reasoning": 0,
  "tokens": 1215658,           // sum of in + cache + out + reasoning
  "cache_ratio": 0.9353,       // tokens_cache / tokens (0.0–1.0)
  "cost": 1.085,
  "started_at": "2026-05-21T20:41:54.357000+00:00",
  "last_seen_at": "2026-05-21T20:44:22.891000+00:00"
}
```

---

## `GET /api/output-speed`

Reads the separate disposable `output_speed.sqlite3` timing cache. A cold or stale
request queues a resource-bounded worker process and returns immediately with
build metadata and any last-good rows. Native SQLite inputs remain read-only. It serves the standalone
**Model output speed** page under **Intelligence**, using the dashboard's top date
range controls. Opening Overview or Usage Report does not request this endpoint.

| Parameter | Default | Description |
|---|---|---|
| `view` | `across-models` | `across-models` or `time-of-day` |
| `period` | `year` | Standard period resolver, unless explicit dates are provided |
| `date_from`, `date_to` | — | Inclusive date range; supply both as `YYYY-MM-DD` |
| `source` | — | Optional comparison filter; required for `time-of-day` |
| `model`, `measurement_kind`, `token_basis` | — | Required with `source` for `time-of-day` |
| `refresh` | `false` | Queue a lightweight usage/ownership synchronization followed by a timing build; avoids full usage-report aggregation |
| `cache_only` | `false` | Read the published snapshot without queuing work, even if inputs changed; takes precedence over `refresh` |

Across-model responses contain `rows`, grouped by source, normalized model, measurement kind
and token basis, and `source_status` for tools with usage but no measured speed.
Each measured row includes additive `speed_tokens`, `speed_ms`, `speed_calls`,
`output_tok_per_s`, `eligible_calls`, `coverage`, measured timestamps and day counts.
The rate is `1000 * SUM(speed_tokens) / SUM(speed_ms)`. Rates from individual calls
are never averaged. Unmeasured throughput is absent or `null`, and empty hours are
`null`, rather than zero.

Time-of-day responses include `selected_key`, 24 `hourly` buckets and four
`six_hour` buckets, with the same totals and coverage fields. Buckets use the
server's local clock. `days_with_measurements` counts distinct measured dates;
the observed span does not imply complete logging between its endpoints.

`measurement_generation` names the accounting generation of the latest scoped
publication. Unrequested inputs can retain older snapshots; readiness remains
scoped to the verified window or session. `cache.publication_revision` increments
on every successful publication, including native updates at an unchanged
accounting generation. Numerators, durations, eligibility and cached
measurement-date bounds are read in one transaction. `cache` contains `state`
(`ready`, `building`, `stale`, `error`), `published_generation`,
`target_usage_generation`, `updated_at`, `job_id`, `completed_inputs`,
`total_inputs` and `pending_inputs`; input progress fields are present for jobs,
while cache-only reads contain freshness and pending counts. Last-good rows may accompany stale/error
states. Pending inputs are distinct from indexed responses lacking telemetry.
Read failures retain the last successful snapshot; explicit refresh or an input
change permits retry. An empty measurement range is never used to hide failure.

Primary usage schema **13** removes timing columns/index and redundant cached
session timing, and repairs legacy `_speed`, `output_speed` and flat timing fields
in primary usage JSON in bounded batches. It also repairs already-migrated
schema-12 caches without reparsing missing source logs or changing accounting
fields. Ordinary ingestion and Overview do not open the derived database
or invoke timing helpers. Existing primary pages become reusable; migration does
not automatically compact the SQLite file. Timing reader versions are independent
of pricing and ordinary parser identities. Derived schema is **3**, speed API
contract **6**, and measurement contract **3**.
Publication identity **3** retires older model-window completion markers without
discarding valid timing rows. A window is current only at the generation of its
exact full source/date build. Session builds retain the last full-window source
census and cannot certify model windows; source-filtered or differently dated
builds also leave other window generations unchanged.

The separate single-flight worker synchronizes lightweight file ownership before
extracting whole contributing files. It uses canonical `(source, entry_key)`
ownership, including forks and copies, and atomically replaces only changed
contributing input populations and memberships. Unrelated published responses
remain in place. Unchanged native full populations can satisfy a session request
without staging their history; selected native overlays use the membership index.
Session completion and errors remain scoped to their inputs. A mixed session batch
still queues its unaffected sessions when another scope has a suppressed failure.
For native batches, recorded `session:<id>` failure ownership applies only to that
session, including when other sessions use the same harness. A `native:all`
failure applies to that harness's database population. Final validation records
the input being checked, its attempted signature and the pinned accounting
generation; transaction/publication errors use job context instead.
Native DB/WAL identities include path, device, inode, mtime and size;
SHM is excluded. Native measurement bounds are cached with the index instead of
scanning native JSON on range changes. A session refresh overlays only that
session on any stale full native population; unrelated last-good rows and their
original signatures remain pending. Removed databases retire their rows. A
verified current full index can satisfy a session request; a stale full index
cannot suppress that session's bounded read. A model request rebuilds deferred
native scopes, and a full publication supersedes partial scopes. Native timing
reader versions are 3; this invalidates only the disposable timing cache.
Native freshness reconciliation shares one signature snapshot across a source's
session scopes and filters unchanged unrelated scopes in SQL. Extraction and
final publication validation still take fresh signatures; final validation
shares each native source's check across its requested scopes. This does not
remove SQL metadata work proportional to cached scopes or full-window staging.
A scalar covering index serves model/hour
aggregation without scattered reads of wide response identities. The worker
upgrades existing derived caches during atomic publication; requests use a linear
fallback until that index exists. Complete model/hour aggregates are memoized
by publication revision, API/measurement contract, database instance, all filters,
date range and local calendar identity. Identical concurrent reads coalesce. The
process cache retains at most 32 results and 8 MiB of encoded results; freshness,
pending inputs and failure/job metadata remain current separately. Speed connections
have a bounded 16 MiB page cache; ordinary accounting does not allocate it. A
changing input is discarded/pending or
fails the build; it cannot donate timing to a different accounting snapshot.

Manual speed refresh calls this endpoint with `refresh=1`, then follows its job;
it does not call `/api/usage?refresh=1`. Date changes only read/ensure speed.
Visible pages may subscribe to `GET /api/output-speed/jobs/{job_id}/events`
(server-sent JSON status). `GET /api/output-speed/jobs/{job_id}` returns one status
snapshot. Status reads validate the owning process creation identity and make a
crashed worker terminal without another ensure request. A quiet live worker is
never expired by a progress timeout. Windows uses non-destructive process queries;
optional memory telemetry is unavailable where `resource` is absent. Navigation
and hidden documents close subscriptions. There is no global
speed polling or shared report-compute semaphore on this route.

After a job ends, clients re-read with `cache_only=true`, including automatic
hourly/group follow-up requests. This read has `job_id: null` and may show a
stale pinned generation when logs advanced during the build. It never queues a
replacement job, acquires a publication write lock, or creates an absent derived
database. Last-good failures remain errors for the affected scope/version/signature;
unrelated tools and sessions can read and build normally. Explicit opening, range switching
and manual refresh can request work. Explorer terminal re-reads likewise use the
cached batch endpoint without immediately re-ensuring the completed row set.
The UI labels a stale snapshot with no active job as the last successful cached
snapshot. Loading/pending labels identify active builds or unindexed timing;
stale data is not presented as an indefinitely running request.

SQLite publication locks on speed reads/build requests return a retryable **503**
with a Tokdash `detail`, so clients retain route health and use bounded backoff.
Other direct database request errors remain **500**; a lock is not an empty timing
range. Worker failures retain their `error` cache metadata and last-good rows.

| Reader | Measurement kind | Ownership and exclusions |
|---|---|---|
| Kimi | `server_decode` | Counted usage paired with its verified step-end decode duration; existing reader unchanged |
| OMP | `post_first_token` | Same assistant record's duration minus TTFT; existing reader unchanged |
| Codex | `response_window` | Explicit response/turn owner validated against the counted usage snapshot; first/last timed model-item bracket minus the clipped union of verified tool intervals. Missing owners, collapsed reasoning, unknown items, interruptions, compaction and output without a complete model-item clock are excluded |
| dsh | `response_window` | First explicit assistant chunk to the matching usage chunk in the same stream, after the existing `(turn, step, attempt)` fold and seed boundary. Retry-contaminated steps and unfinished attempts are excluded |
| Qwen Code | `post_first_token` | Direct assistant `parentUuid` to unique API-telemetry UUID in the same session/agent; duration minus TTFT. Token/model agreement validates the link. Missing telemetry is normal |
| OpenCode, KiloCode, mimo | `request_window` | Message creation to completion; includes request overhead and awaited tools/retries. Explicit errors, abnormal finishes, absent completion and invalid clocks are excluded; imported mimo messages retain existing exclusion. MiMo `max` ensemble/replay messages are excluded because one clock covers several calls |

Codex and dsh output counters already include reasoning. Qwen's recorded total-token
identity establishes whether its candidate counter includes reasoning or stores it
separately; a positive reasoning counter without that proof stays unmeasured.
The OpenCode family stores disjoint visible-output and reasoning counters, added once.
These new readers use `output_including_reasoning`. Measurement kinds and token
bases remain separate groups, even for the same model.

`source_status` distinguishes `unsupported_reader`, `timing_unavailable`,
`pending_reprocessing` and `read_failure`. A supported source can report pending
rows alongside measured rows while a partial reprocess finishes. Unsupported
readers do not imply that the harness itself records no timing. Other tools stay
in the picker when their usage intersects the range.

Across-model responses also carry `available_measurement_range`, the earliest
and latest local dates with measured calls anywhere in the cache, or `null` when
none exist. These bounds are independent of the selected range and do not imply
continuous coverage. Empty views offer an explicit action to apply those dates
to the shared top range. The comparison defaults to all measured tools; choosing
a tool without measurements offers a return to all tools. Response contract and
cache version: `6`; measurement contract: `3`; usage schema: `13`. Derived schema
and publication identity are `3`. The separate
derived database uses a partial measured-timestamp index for these endpoint seeks,
including the empty case. It does not scan native JSON for available bounds.

---

## `GET /api/session`

Detailed view of a single session including per-turn breakdown.

**Query parameters**

| Name | Type | Required | Description |
|---|---|---|---|
| `tool` | string | **yes** | Tool name |
| `session_id` | string | **yes** | Session UUID |

**Response fields**

| Field | Type | Description |
|---|---|---|
| `session` | object | Same shape as `latest_session` from `/api/sessions` |
| `turns` | array | Per-turn token + cost records |
| `speed_measurement` | object | Compatibility summary; ordinary detail has no timing measurements. Use `/api/session-speed` for indexed coverage and verdicts |

**Turn object shape**

```jsonc
{
  "turn_index": 1,
  "model": "claude-sonnet-4-6",
  "tokens_in": 23361,
  "tokens_cache": 0,
  "tokens_out": 118,
  "tokens_reasoning": 0,
  "tokens": 23479,
  "cost": 0.0718,
  "timestamp": "2026-05-20T16:02:07.514000+00:00"
}
```

Core turns retain `output_speed` as a nullable compatibility field, normally
`null` on ordinary reads. They also expose `turn_key` where a verified stable
identity exists, and `stream_id`; anonymous path-based identities stay private.
Core detail and lists neither open the timing cache nor start builds. The UI
loads the separate session-speed API independently, allowing core detail to paint
before timing is available.

---

## `POST /api/session-speeds`

Cached aggregation starts from indexed membership for each requested session;
it does not scan all responses belonging to that tool for every visible row.

Cache-only batch summaries for visible explorer rows. The JSON body contains
`sessions: [{tool, session_id}]`, `date_from` and `date_to` (both required). At most
50 keys are accepted and duplicates are collapsed. This read never creates the
derived cache or launches a build. The ordinary `/api/sessions` payload is unchanged.

The response has `schema_version: 1`, `scope: {kind: "date_range", from, to}`,
`measurement_generation` and `sessions`. Each summary includes its tool/session,
`cache_state`, `measurement_status`, nullable `output_tok_per_s`, `speed_calls`,
`eligible_calls`, `coverage`, `partial_model_scope` and model/kind/basis `groups`.
A scalar requires exactly one eligible normalized model and one measured method
and token basis. Other eligible models make the result `mixed`, even if their
timing is missing. Project rows have no scalar speed. Not-indexed/building,
unsupported, indexed-without-timing, stale and read failures remain distinct.

## `POST /api/output-speed/ensure`

Queues or joins the same worker for a bounded visible-session batch. Body and
limit match `/api/session-speeds`, with optional `refresh`. Returns `cache`
metadata including `job_id`; already-ready sessions return without a new job.
Only contributing physical inputs receive timing extraction. Core ownership
synchronization precedes extraction. Collapsed panels dispatch neither batch
reads nor builds. These structured POST operations carry no user configuration
mutation and use the same server-routing authority as GET reads.

## `GET /api/session-speed`

Demand-built session timeline and model-specific aggregates. Required `tool` and
`session_id`; optional paired `date_from`/`date_to` (default **Entire session**),
`model`, `measurement_kind`, `token_basis`, `max_points` (1–1000, default 1000),
`refresh` and `cache_only` (default false). Terminal reloads and their selected
group follow-ups use `cache_only=true` with the same snapshot semantics as model
speed. An absent cache returns pending coverage without creating a database.

Returns `scope`, `cache`, `measurement_generation`, complete `summary`/`groups`,
`series`, `total_responses`, `returned_points`, `bucketed` and `truncated`.
Each point carries nullable stable `call_key`/`turn_key`, stream, recorded order
and clock/basis, model/kind/basis, additive speed totals, rate, eligibility and
verdict. Missing measurements and filtered groups remain explicit null gaps;
`break_before` separates contributing-input boundaries. Concurrent streams stay
separate. Recorded clocks are not inferred decode start/end times. Equal clocks
do not deduplicate responses. Without clocks, clients label response order.
The dashboard's elapsed axis starts at the earliest recorded response in the
session timeline; those timestamp differences position points, never supply the
rate denominator.

Long timelines use ordered additive buckets, with min/max response rates and
last recorded time. Consecutive null responses in one stream may collapse into
one explicit gap marker with `gap_responses`; they never become a zero rate.
Measured buckets never cross gaps, stream, model, method or token-basis
boundaries. If boundaries exceed the point budget, truncation is explicit and
aggregates still cover the full session. Bucket rate is a ratio of token/duration
sums, never a mean of call rates. These points describe response averages, not a
continuous within-response decoder trace. The modal reuses one Chart/Responses
panel; the explorer uses the selected top-date range.

---

## `GET /api/active-time`

Estimated active time across every session tool, for the Overview KPI. Kept
separate from `/api/usage` because it reads every supported session source.

**Query parameters**

| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `period` | string | no | `"today"` | See [Period parameter](#period-parameter) |
| `date_from` | string | no | – | Start date (`YYYY-MM-DD`) |
| `date_to` | string | no | – | End date (`YYYY-MM-DD`) |
| `include_review_sessions` | boolean | no | `false` | Include Codex review / auto-permission sessions |
| `refresh` | boolean | no | `false` | Bypass the response cache and recompute, as on `/api/usage`. The dashboard's Refresh button sends this |

**Response fields**

| Field | Type | Description |
|---|---|---|
| `period` | string | Echo of the period param |
| `active_ms` | int | Clock time any agent was working: overlapping sessions *and tools* count once |
| `active_ms_sum` | int | Agent time: per-stream intervals added up, so concurrent agents count separately |
| `sparkline` | object\|null | Same bounded hourly/daily/monthly bucket contract as `/api/usage`, with `{key, agent_ms}` rows whose sum equals `active_ms_sum`; intervals crossing a bucket boundary are split |
| `comparison` | object\|null | The same two figures for the previous window and the percentage change: `{active_ms_prev, active_ms_sum_prev, active_ms_pct, active_ms_sum_pct}`. A percentage is `null` when the previous window is empty, and the whole object is `null` if that window could not be read |
| `by_tool` | object | Per-tool `{tool_label, session_count, active_ms, active_ms_sum}` |
| `unavailable_tools` | array | Tools that could not be read or summarized for this window (excluded from the totals, so the rest still answer) |
| `active_gap_cap_ms` | int | Idle cap in effect (`TOKDASH_ACTIVE_GAP_CAP_SECONDS`) |
| `active_time_estimated` | bool | Always `true` — see the method below |
| `active_time_method` | string | `"capped-inter-event-gap"` |
| `include_review_sessions` | bool | The effective setting applied, param or server default |
| `timestamp` | string | ISO 8601 time the payload was computed |

Both figures are estimates: each gap between a stream's token events counts up to
the idle cap, so a short pause reads the same as work, one long operation is
truncated at the cap, and a session with a single event measures zero. The same
fields appear per tool in `/api/sessions` under `summary`, and per session in
`sessions[]`, where the union is over that session's agent streams only.

`comparison` covers the window immediately before this one — the previous day,
month or N days, or for an explicit `date_from`/`date_to` the range of equal
length ending where it begins. That is the same window `/api/usage` compares
against, so the runtime delta on the Overview means what the token, cost and
message deltas beside it mean. Computing it aggregates a second window; the
response cache makes that a per-window cost rather than a per-request one.

```jsonc
{
  "period": "week",
  "active_ms": 331980000,       // 92h 13m of clock time
  "active_ms_sum": 494880000,   // 137h 28m of agent time
  "comparison": {               // the week before, on the same terms
    "active_ms_prev": 298620000,
    "active_ms_sum_prev": 421200000,
    "active_ms_pct": 11.2,
    "active_ms_sum_pct": 17.5
  },
  "by_tool": {
    "codex": {"tool_label": "Codex", "session_count": 38, "active_ms": 273780000, "active_ms_sum": 332820000}
  },
  "unavailable_tools": [],
  "active_gap_cap_ms": 300000,
  "active_time_estimated": true,
  "active_time_method": "capped-inter-event-gap",
  "include_review_sessions": false,
  "timestamp": "2026-08-14T14:31:05.412000"
}
```

---

## `GET /api/codex/sessions`

Convenience wrapper for Codex sessions. Equivalent to `/api/sessions?tool=codex`.

**Query parameters**

| Name | Type | Required | Default |
|---|---|---|---|
| `period` | string | no | `"today"` |
| `include_review_sessions` | boolean | no | `false` (Codex review / auto-permission sessions hidden by default) |

---

## `GET /api/codex/session`

Convenience wrapper for a single Codex session. Equivalent to `/api/session?tool=codex&...`.

**Query parameters**

| Name | Type | Required |
|---|---|---|
| `session_id` | string | **yes** |

---

## `GET /api/openclaw`

OpenClaw-specific model breakdown (aggregate). The per-session drill-down lives at `/api/sessions?tool=openclaw`; this endpoint stays the aggregate one.

**Query parameters**

| Name | Type | Required | Default |
|---|---|---|---|
| `period` | string | no | `"today"` |

**Response fields**

| Field | Type | Description |
|---|---|---|
| `total_tokens` | int | Sum across all OpenClaw models |
| `total_cost` | float | Sum in USD |
| `total_messages` | int | Message count |
| `models` | object | `{ model_name: { tokens, tokens_in, tokens_out, tokens_cache, cost, messages } }` |

---

## `GET /api/stats`

Yearly stats aggregation: a contribution grid plus headline totals.

**Query parameters**

| Name | Type | Required | Description |
|---|---|---|---|
| `year` | integer | no | Year to query. Defaults to current year if omitted. |

**`stats` fields**

| Field | Type | Description |
|---|---|---|
| `favorite_model` | string | Most-used model, by tokens. Alias of `most_used_model`. |
| `most_used_model` | string | Model with the most tokens in range |
| `highest_cost_model` | string | Model with the highest cost in range — often a different model |
| `total_tokens` | integer | Tokens across the window |
| `messages` | integer | Assistant messages across the window |
| `sessions` | integer | **Deprecated** — a message count, not a session count. Equal to `messages`; read that instead. |
| `current_streak` | integer | Consecutive active days ending today or yesterday (`0` when the streak has lapsed) |
| `longest_streak` | integer | Longest run of consecutive active days in range |
| `active_days` | integer | Days with any recorded usage |
| `total_days` | integer | Span from first to last active day, inclusive |

**`contributions[]` fields**

Each entry is one active day, carrying `date`, `totals`, a full `tokenBreakdown`, a
`sources[]` array (with `modelId` / `providerId`), and `intensity` — a `1`–`4` rank of that
day's token volume against the other active days in the window (`0` only when a day has no
tokens). Being a rank rather than an absolute threshold, it stays meaningful as usage grows;
it is the value a calendar heatmap shades by.

---

## `GET /api/insights`

Fine-grained analytics for report-style consumers — hour-of-day activity, weekday rhythm,
per-project attribution, streaks. Built for a "year in review" page: one request covers
every facet, rather than one request per metric.

**Query parameters**

| Name | Type | Required | Description |
|---|---|---|---|
| `period` | string | no | Window to analyse (default `year`). See [Period parameter](#period-parameter). |
| `date_from` / `date_to` | string | no | Explicit `YYYY-MM-DD` range, instead of `period` |
| `facets` | string | no | Comma-separated facet list. Omitted, returns the default set. An unknown name is a `400`. |
| `include_project_names` | boolean | no | `false` replaces project names with `project-1`, `project-2`, … keeping ranks and volumes but not identities. Default `true`. |
| `refresh` | boolean | no | Bypass the response cache |

**Facets**

| Facet | Contents |
|---|---|
| `hourly` | 24 buckets, plus `peak_hour` and `night_share` (the 22:00–02:00 token share) |
| `weekday` | 7 buckets, plus `peak_weekday` (0 = Monday) |
| `heatmap` | The dense 7×24 grid (168 cells) plus `max_tokens`, for shading |
| `daily` | Per-day totals with the same `intensity` ranking `/api/stats` uses |
| `models` | Ranked by tokens, with `most_used` and `highest_cost` named separately |
| `tools` | Same ranking per source tool |
| `projects` | Token totals per project, plus an `unattributed` bucket |
| `streaks` | `current_streak`, `longest_streak`, `active_days`, `total_days` |
| `firsts` | First/last active day, busiest day and its tokens, peak hour |

Default set: `hourly`, `weekday`, `heatmap`, `models`, `tools`, `streaks`, `firsts` —
everything the single composite scan already pays for. `daily` and `projects` are opt-in:
`daily` is the largest payload and duplicates `/api/stats`, and `projects` needs a second
scan plus a session-record read.

**Response shape**

```json
{
  "schema_version": 1,
  "range": { "period_requested": "year", "period_resolved": "year", "days": 365, "recognized": true },
  "facets": ["hourly", "streaks"],
  "timezone": "BST",
  "coverage": { "stored_sources": ["claude", "codex"], "live_sources": ["opencode"], "group_count": 8234 },
  "totals": { "tokens": 0, "cost": 0.0, "messages": 0, "entries": 0 },
  "hourly": { "buckets": [], "peak_hour": 11, "night_share": 0.2056, "night_hours": [0, 1, 22, 23] },
  "streaks": { "current_streak": 171, "longest_streak": 171, "active_days": 232, "total_days": 287 }
}
```

**Notes**

- **Timezone.** Hour and day buckets are cut in the server's local zone, reported as
  `timezone`. A machine that changes zone re-buckets its own history; label charts with this
  value rather than assuming UTC.
- **Coverage.** `coverage` lists the sources behind the numbers. Tools that keep their own
  database (OpenCode, KiloCode, Mimo, Zcode, Qoder) are parsed live and appear under
  `live_sources`; everything else is read from the usage database.
- **Attribution.** `projects` maps usage rows to projects through the transcript path
  recorded on each session. Sources whose rows carry no usable path — OpenClaw among them —
  land in `unattributed` rather than being dropped, so the totals still reconcile.
- **Caching.** A window that has closed is cached indefinitely, so a past year is computed
  once and every later request is a cache hit. Only a window including today recomputes.
- **Totals.** `totals` is this scan's own sum over the rows the facets were folded from, and
  it need not equal `/api/usage`'s `total_tokens` for the same window: the two read the store
  through different paths. Print one of them per figure, and compute facet shares against
  `totals` so the rows add up against the number above them.
- **Fixture mode.** Under `--dev-fixture dense` this route answers from the seeded fixture,
  which invents rows and folds them with the same `insights._fold_*` helpers production uses,
  then marks the payload with `fixture`. Real usage history is never read while a fixture is
  active, and the facet shapes are pinned by `tests/test_insights_api.py`.

---

## `GET /api/pricing-db`

Returns the **effective** pricing database: the user override under `TOKDASH_DATA_DIR` when
present (it fully replaces the baseline — WYSIWYG editor semantics), otherwise the packaged
baseline. A corrupt override falls back to the baseline (never wipes pricing).

**Response fields**

| Field | Type | Description |
|---|---|---|
| `path` | string | Where edits PERSIST — the override file under the data dir (`<data_dir>/pricing_db.json`) |
| `baseline_path` | string | The read-only packaged baseline (`…/site-packages/tokdash/pricing_db.json`) |
| `baseline_version` | string \| null | The shipped baseline's `version`, reported even when an override is active so a UI can warn when an override has drifted behind newer bundled pricing |
| `source` | string | `"override"` if the data dir override is in effect, else `"baseline"` |
| `data` | object | The effective pricing database (versions, aliases, model rates) |
| `text` | string | Pretty-printed canonical JSON of `data` (trailing newline) — what the editor renders |

> **Trade-off (by design).** Because a saved override **fully replaces** the baseline, it also
> **freezes future bundled pricing updates** for the models it covers until you delete it. This is
> intentional — it keeps the editor WYSIWYG (a deletion stays deleted). Compare `baseline_version`
> against your override's `version` to decide when to re-fork; delete `<data_dir>/pricing_db.json`
> to return to the shipped baseline and resume receiving updates.

The `data` object contains:
- `version` — pricing DB version
- `lastUpdated` — ISO timestamp
- `note` — description string
- `aliases` — `{ alias: canonical_name }` for model name normalization
- `models` — `{ model_name: { input, output, cache_read, cache_write } }` (USD per million tokens)

## `PUT /api/pricing-db`

Saves pricing edits. Body must match the GET response `data` shape (or `{"text": "<json>"}`).
Edits are written to the **override** file under `TOKDASH_DATA_DIR` (never the packaged
baseline), so they survive `tokdash update` (a pip/pipx reinstall) and succeed on a read-only
install. The override fully replaces the baseline once saved (so deletions stick); delete the
override file to revert to the shipped defaults. Returns the same `{path, baseline_path,
baseline_version, source, data, text}` shape as GET (with `source: "override"`).

**Write protection.** As a state-changing endpoint it is gated (returns `403` otherwise):

- the server must be bound to loopback;
- `Host` (and any `Origin`/`Referer`) must be a loopback address in the allowlist;
- the request must carry a valid `X-Tokdash-Token` (fetch it from `GET /api/csrf-token`).

The dashboard does this automatically. A scripted client must fetch the token first:

```bash
TOKEN=$(curl -s http://127.0.0.1:55423/api/csrf-token | jq -r .token)
curl -s -X PUT http://127.0.0.1:55423/api/pricing-db \
  -H "Content-Type: application/json" -H "X-Tokdash-Token: $TOKEN" \
  -d '{"data": { ... }}'
```

---

## Integration Example: Claude Code Status Line

> **Ready-made templates:** [`docs/guides/statusline/`](../guides/statusline/) ships a minimal and a full statusline script plus install/config notes. The snippet below is the minimal one, reproduced here for reference.

Tokdash's `/api/usage` endpoint is well suited for embedding daily totals into the Claude Code status line. The snippet below queries today's usage with a 1-second timeout, falls back silently if tokdash is unreachable, and renders a compact summary like `📊 69.9M ($55.64) today`.

### Status line script (`~/.claude/scripts/statusline.sh`)

```bash
#!/bin/bash
input=$(cat)

MODEL=$(echo "$input" | jq -r '.model.display_name')
DIR=$(echo "$input" | jq -r '.workspace.current_dir')

# Fetch tokdash totals — fail silently if unreachable
TOKDASH_STR=""
TOKDASH_JSON=$(curl -s -m 1 "http://127.0.0.1:55423/api/usage?period=today" 2>/dev/null)
if [ -n "$TOKDASH_JSON" ]; then
  TODAY_TOKENS=$(echo "$TOKDASH_JSON" | jq -r '.total_tokens // 0' 2>/dev/null)
  TODAY_COST=$(echo "$TOKDASH_JSON" | jq -r '.total_cost // 0' 2>/dev/null)
  if [ -n "$TODAY_TOKENS" ] && [ "$TODAY_TOKENS" != "0" ]; then
    if [ "$TODAY_TOKENS" -ge 1000000 ]; then
      TOK_FMT=$(awk "BEGIN {printf \"%.1fM\", $TODAY_TOKENS/1000000}")
    elif [ "$TODAY_TOKENS" -ge 1000 ]; then
      TOK_FMT="$(( (TODAY_TOKENS + 500) / 1000 ))k"
    else
      TOK_FMT="$TODAY_TOKENS"
    fi
    COST_TODAY=$(printf '$%.2f' "$TODAY_COST")
    TOKDASH_STR=" | 📊 ${TOK_FMT} (${COST_TODAY}) today"
  fi
fi

echo "[$MODEL] 📁 ${DIR##*/}${TOKDASH_STR}"
```

### Claude Code settings (`~/.claude/settings.json`)

```json
{
  "statusLine": {
    "type": "command",
    "command": "bash ~/.claude/scripts/statusline.sh",
    "refreshInterval": 30
  }
}
```

`refreshInterval` (added in Claude Code 2.1.97) re-runs the script every N seconds so the totals stay live even while you're idle.

### Output

```
[Claude Sonnet 4.6] 📁 myproject | 📊 69.9M ($55.64) today
```

### Notes

- Keep the curl timeout small (`-m 1`) so the status line doesn't stall if tokdash is restarting.
- The `📊 ...` segment is omitted entirely when tokdash returns nothing — no error noise in the status bar.
- For per-tool detail, swap in `.by_tool.claude.tokens` or similar from the same response.
- For weekly/monthly totals, change `period=today` to `period=week` or `period=month`.

---

## Other Integration Patterns

### Shell alias for quick check

```bash
alias tokens-today='curl -s http://127.0.0.1:55423/api/usage?period=today | jq "{tokens: .total_tokens, cost: .total_cost, by_tool}"'
```

### Polling for cost alerts

```bash
#!/bin/bash
# Warn when daily spend crosses $50
COST=$(curl -s http://127.0.0.1:55423/api/usage?period=today | jq -r '.total_cost')
if (( $(echo "$COST > 50" | bc -l) )); then
  notify-send "Tokdash" "Daily spend has exceeded \$50 ($COST)"
fi
```

### Prometheus / metrics scraping

For richer monitoring setups, the `/api/usage` JSON can be parsed by a small exporter sidecar. The `comparison` block gives period-over-period deltas without extra requests.
