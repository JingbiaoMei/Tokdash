# Tokdash API Reference

Tokdash exposes a local HTTP API (FastAPI) for querying token usage, costs, and session data across Claude Code, Codex, OpenClaw, and other supported tools.

- **Default bind:** `127.0.0.1:55423`
- **Start:** `tokdash serve --bind 127.0.0.1 --port 55423`
- **OpenAPI schema:** `GET /openapi.json`
- **Interactive docs:** `GET /docs` (Swagger UI), `GET /redoc`

All endpoints return JSON. The API is unauthenticated and intended to bind to loopback
only. **State-changing requests are gated** (loopback bind + Host/Origin allowlist +
per-session token); see [`docs/SECURITY.md`](SECURITY.md) and `PUT /api/pricing-db` below.

---

## Endpoint Summary

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness probe (with a Tokdash fingerprint) |
| `GET` | `/api/version` | Runtime version + setup install method |
| `GET` | `/api/csrf-token` | Per-session write token (loopback/same-origin only) |
| `POST` | `/api/update-check` | Opt-in cached PyPI version check (write-gated) |
| `POST` | `/api/update-check/consent` | Persist one-time update-check consent (write-gated) |
| `GET` | `/api/usage` | Aggregated token usage and cost across all tools |
| `GET` | `/api/tools` | Per-tool usage breakdown (coding apps only) |
| `GET` | `/api/quota` | Current subscription quota state from local snapshots |
| `GET` | `/api/quota/history` | Quota utilization and derived consumption history |
| `POST` | `/api/quota/consent` | Persist per-provider quota API consent (write-gated) |
| `POST` | `/api/quota/settings` | Persist the quota master switch and poll interval (write-gated) |
| `POST` | `/api/quota/refresh` | Run an immediate consented quota API poll (write-gated, cooldown) |
| `GET` | `/api/sessions` | List sessions for a given tool |
| `GET` | `/api/session` | Detailed turns for a single session |
| `GET` | `/api/codex/sessions` | Convenience wrapper: Codex sessions |
| `GET` | `/api/codex/session` | Convenience wrapper: single Codex session |
| `GET` | `/api/openclaw` | OpenClaw model breakdown |
| `GET` | `/api/stats` | Annual stats aggregation |
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
| `<integer>` | Last N days (e.g. `"30"` for 30 days) |

For arbitrary ranges, use `date_from` and `date_to` (format `YYYY-MM-DD`) where supported.

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

**Response**
```json
{
  "service": "tokdash",
  "runtime_version": "1.0.7",
  "install_method": "pipx",
  "update_check_enabled": false
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

## `POST /api/update-check`

Opt-in, default-off PyPI version check (see `docs/ONBOARDING.md` → Update checks). **Write-gated**
(loopback bind + `Host`/`Origin` allowlist + `X-Tokdash-Token`). No-op unless update checks are
enabled (`TOKDASH_UPDATE_CHECK=1` or saved consent). Result is cached for hours; never an
automatic/background call, and it only *reports* — it never runs an upgrade.

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

Returns current subscription quota state. This route never performs provider network I/O; it reads the local `quota_snapshots` table (and local plan/tier metadata). Session files are not scanned here — the background poller ingests them. Provider API polling is default-off and happens only through `POST /api/quota/refresh`, the background poller after consent, or `tokdash quota poll`.

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
  "consent": {"codex_api": false, "claude_api": false, "antigravity_api": false},
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

History series are unified per `(provider, bucket)`: a Codex session row (account `default`) and an API row (real account id) for the same window merge into one series, keeping the freshest point on a timestamp collision. Series are always bounded by `max_points` (points and consumption deltas are downsampled independently).

## `POST /api/quota/consent`

Persists per-provider quota API **network** consent to `<data_dir>/config.json`. **Write-gated** like all mutations. `TOKDASH_QUOTA_POLL=0` remains a hard kill switch.

**Request**
```json
{"codex_api": true, "claude_api": false, "antigravity_api": true}
```

**Response**
```json
{"consent": {"codex_api": true, "claude_api": false, "antigravity_api": true}}
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

## `POST /api/quota/refresh`

Runs an immediate network poll for consented providers and stores snapshots in the local usage DB. **Write-gated** and rate-limited with a 60 second cooldown. It never refreshes provider tokens; expired tokens produce stale-token snapshots. Returns `409` when quota tracking is disabled (master switch off or `TOKDASH_QUOTA_POLL=0`).

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
| `top_models` | array | Top N models by token usage |
| `openclaw_models` | array | OpenClaw-specific model breakdown |
| `combined_models` | array | All models from all sources, merged and ranked |
| `comparison` | object | Comparison vs previous period: `tokens_prev`, `cost_prev`, `messages_prev`, `tokens_pct`, `cost_pct`, `messages_pct` |
| `timestamp` | string | ISO 8601 timestamp when the response was generated |

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
| `tool` | string | **yes** | – | Tool name: `codex`, `claude`, `opencode`, `pi_agent`, or `mimo` (the session explorer set; OpenClaw is served only via `/api/openclaw`) |
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

Yearly stats aggregation.

**Query parameters**

| Name | Type | Required | Description |
|---|---|---|---|
| `year` | integer | no | Year to query. Defaults to current year if omitted. |

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

> **Ready-made templates:** [`docs/examples/statusline/`](examples/statusline/) ships a minimal and a full statusline script plus install/config notes. The snippet below is the minimal one, reproduced here for reference.

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
