# Tokdash API Reference

Tokdash exposes a local HTTP API (FastAPI) for querying token usage, costs, and session data across Claude Code, Codex, OpenClaw, and other supported tools.

- **Default bind:** `127.0.0.1:55423`
- **Start:** `tokdash serve --bind 127.0.0.1 --port 55423`
- **OpenAPI schema:** `GET /openapi.json`
- **Interactive docs:** `GET /docs` (Swagger UI), `GET /redoc`

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

**Response**
```json
{ "status": "ok", "service": "tokdash", "version": "1.0.7" }
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
consent). Result is cached for hours; never an automatic/background call, and it only *reports*
— it never runs an upgrade.

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

History series are unified per `(provider, bucket)`: a Codex session row (account `default`) and an API row (real account id) for the same window merge into one series, keeping the freshest point on a timestamp collision. MiniMax uses region-qualified bucket IDs so global and mainland-China Token Plans remain separate series. Series are always bounded by `max_points` (points and consumption deltas are downsampled independently).

## `POST /api/quota/consent`

Persists the separate local-credential-read consent (`credential_scan`) and per-provider quota API **network** consent to `<data_dir>/config.json`. Network polling requires both `credential_scan=true` and the matching provider flag. **Write-gated** like all mutations. `TOKDASH_QUOTA_POLL=0` remains a hard kill switch.

**Request**
```json
{"credential_scan": true, "codex_api": true, "minimax_api": true, "kimi_api": true, "grok_api": true}
```

**Response**
```json
{"consent": {"credential_scan": true, "codex_api": true, "claude_api": false, "antigravity_api": false, "minimax_api": true, "kimi_api": true, "grok_api": true}}
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
| `tool` | string | **yes** | – | Tool name: `codex`, `claude`, `opencode`, `pi_agent`, `omp`, `mimo`, `kimi`, `dsh`, `reasonix`, `zcode`, `kilocode`, `grok`, `hermes`, `antigravity_cli`, or `cline` (OpenClaw is served only via `/api/openclaw`) |
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
from rates like any other.

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

OpenClaw-specific model breakdown.

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
