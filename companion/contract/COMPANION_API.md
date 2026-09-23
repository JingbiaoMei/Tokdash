# Tokdash Companion API Contract

The companion is a **read-only client** of the Tokdash HTTP API. It never writes,
never polls providers, and never reads logs or credentials. This document defines
the endpoints, response shapes, client behavior rules, and expected UI outcomes
that both native apps (macOS, Windows) must satisfy.

Fixtures live in `contract/fixtures/`. Expected outcomes live in
`contract/expected/`. Both native test suites consume the same fixtures so the
behavior contract is shared even though the UI code is not.

`expected/multi-server.json` reuses the shared endpoint fixtures for two named
servers and pins combined hero math, server ordering, Low-view deduplication,
partial failure, and the minimum-delay rule.

## Endpoints used

| Method | Path | Purpose | When |
|---|---|---|---|
| `GET` | `/health` | fingerprint + connectivity | startup, reconnect |
| `GET` | `/api/usage?period=<selected>` | hero, delta row, top ranks for the selected period | popover open, scheduled refresh, period change |
| `GET` | `/api/active-time?period=<selected>` | "active" figure on the hero sub-line | same |
| `GET` | `/api/insights?facets=hourly&period=today` | activity glance, today | same, when the glance is on and today is selected |
| `GET` | `/api/insights?facets=daily&date_from&date_to` | activity glance, week | same, when the glance is on and week is selected |
| `GET` | `/api/stats` | activity glance grid, month/year | same, when the glance is on and month/year is selected |
| `GET` | `/api/quota` | Quota section (incl. reset credits) | popover open, scheduled refresh |
| `GET` | `/api/version` | About / Settings diagnostics, update-check gate | settings open only |
| `GET` | `/api/update-check` | server update badge | settings open only, when `update_check_enabled` |

`<selected>` is the segmented hero control's period (see Period windows): one
of `today`, `month`, `year` as tokens, or `date_from`/`date_to` for the week.

Not used by the companion:
- `POST /api/quota/consent`, `POST /api/quota/settings`,
  `POST /api/update-check/consent` (write-gated; companion is read-only - the
  badge reads the server's existing update-check state, it never changes it).
- `GET /api/quota/refresh` (performs provider network I/O, 60s cooldown, 409 when
  disabled). If exposed later it must be a separate, explicitly labelled
  "Refresh provider quotas" action - never part of automatic refresh.
- `GET /api/sessions`, `GET /api/session` (session browsing is out of scope).
- `PUT /api/pricing-db` and all other write endpoints.

## Base URL handling

- Default: `http://127.0.0.1:55423`.
- A manually configured Tailscale HTTPS URL is supported, e.g.
  `https://wsl.tail76535.ts.net/tokdash`.
- When the base URL contains a path prefix (e.g. `/tokdash`), join endpoint paths
  correctly: `https://host/tokdash` + `/api/usage` -> `https://host/tokdash/api/usage`.
  Strip trailing slashes from the base before joining; ensure exactly one slash
  between base and endpoint.
- Never send a browser `Origin` header from a native client.

## Response shapes

### `GET /health`

```json
{ "status": "ok", "service": "tokdash", "version": "1.4.5" }
```

Fixture: `fixtures/health.json`

The client must require `service == "tokdash"`. Any other value (fixture
`fixtures/health-wrong-service.json`) puts the companion in the **wrong service**
state: show "This address is not a Tokdash service" and do not call usage or
quota endpoints.

### `GET /api/usage` (the selected period)

Fixtures: `fixtures/usage-today.json` (realistic data), `fixtures/usage-today-empty.json`
(empty), and the other period windows: `fixtures/usage-week.json`,
`fixtures/usage-month.json`, `fixtures/usage-year.json`.

Fields used by the companion:

| Field | Type | Use |
|---|---|---|
| `total_cost` | float | Hero primary value |
| `total_tokens` | int | Hero secondary line (compact notation) |
| `total_messages` | int | Hero secondary line |
| `comparison.cost_pct` | float \| null | "12% below yesterday" / "8% above yesterday". Omit when `null`. |
| `comparison.cost_prev` | float \| null | Previous-period cost used to recompute a combined percentage across reachable servers. Hide the comparison when any contributing server omits it. |
| `comparison.tokens_pct`, `comparison.messages_pct` | float \| null | `tokens_pct` completes the delta row; `messages_pct` is sent but not rendered (two-metric row, see Hero display rules) |
| `comparison.tokens_prev`, `comparison.messages_prev` | float \| null | Their combining role, same as `cost_prev` |
| `by_tool` | object | Top tools block (v1.1). Before v1.1 it fed the "Most used today" activity line, which is retired |
| `top_models_by_cost` | array | (legacy) Leading model by cost for the retired activity line — take `[0]` |
| `combined_models` | array | Full model list, ranked by tokens. Top models block takes `[0..2]`; also the cost fallback when `top_models_by_cost` is absent |
| `top_models` | array | First five of `combined_models`, so ranked by tokens |
| `timestamp` | string (ISO 8601) | Freshness calculation |
| `response_cache.age_seconds` | float | Freshness "· cached" hint when useful |

Additive decoding: ignore unknown fields, tolerate absent optional fields. A
valid response with `total_tokens == 0` is the **empty state**, not an error.

**Do not derive a cost leader from `top_models`.** Every model array except
`top_models_by_cost` is ranked by tokens, and `top_models` is the first five of
them — the costliest model need not be among the five biggest, so a maximum over
`top_models` can name the wrong model. Use `top_models_by_cost[0]`, or a maximum
over the full `combined_models` when talking to a server that predates the field.
Earlier Tokdash versions ranked the arrays by cost, which is why a client that
took a maximum over `top_models` used to get away with it.

`fixtures/usage-today.json` is built to catch exactly that. Its seven models rank
differently by tokens and by cost: `openai/o5-deep-research` is the costliest and
only the sixth largest, so it is absent from `top_models`, and `openrouter/glm-5`
is fifth largest and the cheapest, so it is absent from `top_models_by_cost`. A
client that takes a maximum by cost over `top_models` picks `openai/gpt-5.6-sol`
and fails the case. Keep that divergence when editing the fixture — an earlier
version ranked the same both ways, and no client could fail it.

### Period windows

The hero segment (Today / Week / Month / Year) maps to requests like this:

| Segment | Request | Comparison sentence |
|---|---|---|
| Today | `?period=today` | "vs yesterday" |
| Week | `?date_from=<Monday>&date_to=<today>` | "vs last week" |
| Month | `?period=month` (calendar month, 1st -> today) | "vs last month" |
| Year | `?period=year` (Jan 1 -> today) | "vs last year" |

Week is a **calendar week**, matching the calendar semantics of Month and Year:
always send `date_from` (local Monday of the current week) and `date_to`
(today). `period=week` is a rolling 7-day window and must **not** be used for
the segment. `comparison` is returned for custom ranges too (previous equal
window), and the server caches each distinct range, so switching back is free.

The top-level `period` echo in the response is the raw query parameter, not
the resolved window: a custom range with no `period` param still echoes
`"today"`. Never branch on it; the fixtures reflect this (`usage-week.json`).
The week label in the UI is "THIS WEEK", derived client-side like every other
period label.

### `GET /api/quota`

Fixture: `fixtures/quota.json` (enabled, multi-provider), `fixtures/quota-disabled.json` (disabled), `fixtures/quota-provider-error.json` (one provider failed refresh), `fixtures/quota-multi-account.json` (one card, two credentials, one of them broken — see [Accounts](#accounts)).

`fixtures/quota-multi-account.json` is regenerated from the server's own
`quota_state()` and diffed against it by `tests/test_companion_contract_accounts.py`,
so it cannot drift from the payload; both companions decode that same file and must
reach the same per-row verdicts. A rule documented here without a fixture and a test
on both sides is a rule the apps do not have.

Fields used:

| Field | Type | Use |
|---|---|---|
| `enabled` | bool | When `false`, show "Subscription tracking is off" |
| `providers.*` | object | One entry per detected provider; key is the provider id |
| `providers.*.estimated` | bool | Show "Estimated" badge on that provider's rows |
| `providers.*.status_at` | int \| null | Epoch seconds the failure status was observed; see Provider failures |
| `providers.*.buckets[]` | array | Quota windows |
| `buckets[].bucket` | string | Window id, e.g. `"5h"`, `"weekly"` |
| `buckets[].bucket_label` | string | Display label, e.g. `"5-hour window"` |
| `buckets[].remaining_percent` | float \| null | Display "14% left"; bar fill width |
| `buckets[].resets_at` | int \| null | Epoch seconds; humanize to user locale/TZ |
| `buckets[].account` | string | Part of the notification dedup key |
| `buckets[].captured_at` | int \| null | Epoch seconds this window was observed; see Provider failures |
| `providers.codex.reset_credits` | object \| absent | Codex-only reset-credits row and its expiry notification (see Reset credits). Absent on every other provider, and usually on Codex too |
| `reset_credits.available_count` | int | The row's count |
| `reset_credits.credits[]` | array of `{id, expires_at}` | Soonest future `expires_at` (ISO 8601 **string**, unlike the epoch numbers elsewhere) dates the row and arms the notification |

Buckets with `remaining_percent == null` are rendered without a percentage and
without a bar fill; they are not candidates for the Low view.

### `GET /api/active-time` (selected period)

Fixtures: `fixtures/active-time-today.json`, `fixtures/active-time-week.json`
(custom-range echo), `fixtures/active-time-month.json`,
`fixtures/active-time-year.json`, `fixtures/active-time-zero.json` (no activity).

Fields used: `active_ms` (int, **milliseconds** — the merged union of tool
activity), plus `timestamp`. Every duration in this payload is milliseconds;
every epoch in the quota payload is seconds. Request the same window as the
selected usage period (`?period=...`, or `date_from`/`date_to` for the week).

This is an optional section: 404 on a pre-active-time server, any failure, or
`active_ms == 0` simply hides the hero's active segment. No inline warning, no
banner, no stale marker (see Hero display rules). `by_tool`, `comparison` and
the `*_sum` fields are not rendered in v1.1 — decode tolerantly, ignore them.

### `GET /api/insights` (glance source: today / week)

Fixtures: `fixtures/insights-today.json` (`?facets=hourly&period=today`),
`fixtures/insights-week.json` (`?facets=daily&date_from&date_to`),
`fixtures/insights-empty.json` (zero day).

Fields used:

| Field | Type | Use |
|---|---|---|
| `hourly.buckets[].tokens` | int | today's 24 bars, buckets ordered hour 0..23 |
| `hourly.peak_hour` | int \| null | caption "Peak 14:00"; `null` when the day has no tokens |
| `daily[]` | array | week's per-day histogram: `{date, tokens, intensity}`. **Sparse** — a date with no usage has no entry; render it as an empty column |

Ask for exactly one facet per request (`facets=hourly` or `facets=daily`);
`daily` is **not** in the server's default facet set, and an unknown facet is
a 400. Optional section: failure hides the glance silently.

### `GET /api/stats` (glance source: month / year)

Fixture: `fixtures/stats-contributions.json`.

Fields used: `contributions[].date`, `contributions[].totals.tokens`,
`contributions[].intensity` (int 0..4, ranked quartiles server-side). Sparse,
like `daily`. The payload has no period parameter — it is a rolling 365 days —
and v1.1 windows it client-side (trailing 90 days for month, 180 for year).
`summary.*` (camelCase) and `stats.*` (snake_case) exist but are not rendered;
ignore them. Optional section: failure hides the glance silently.

### `GET /api/version` and `GET /api/update-check` (Settings only)

Fixtures: `fixtures/version.json`, `fixtures/update-check-available.json`,
`fixtures/update-check-off.json`.

| Field | Type | Use |
|---|---|---|
| `version.runtime_version` | string | "Tokdash v{...}" in Settings |
| `version.update_check_enabled` | bool | whether to call `/api/update-check` at all |
| `update-check.enabled` | bool | `false` = the server has no update-check consent and performed no network I/O; render nothing |
| `update-check.update_available` | bool | show the badge |
| `update-check.latest` | string \| null | badge text "Server update available: v{latest}" |

`GET /api/update-check` is deliberately consent-gated server-side and safe to
call; the consent *write* (`POST /api/update-check/consent`) stays web-only.
Settings only — never the flyout, never on a schedule. See Server update badge.

## Client behavior rules

Companion settings schema v2 stores a `servers` array (`id`, `label`, `baseUrl`,
`enabled`). A v1 `BaseURL`/`baseURL` value migrates to the first entry. Refreshes
fan out across enabled servers; failed servers are excluded from combined figures
until they recover. Native clients do not require a passing Test before saving a
valid URL.

1. **Health gate.** Call `/health` first. Require `service == "tokdash"`. On
   mismatch or non-2xx, enter wrong-service / offline state. Do not call usage
   or quota endpoints until health passes.

2. **Concurrent fetch.** After a successful health check, fetch concurrently
   for the **selected period**: usage, active-time, quota, and — when the
   Activity glance is on — its single source (insights hourly on today,
   insights daily on week, stats on month/year). Cancel or coalesce
   overlapping refreshes. Selecting a different segment fires the same group
   for the new window immediately; while it is in flight the hero, delta row,
   rank blocks and glance show their loading skeleton, and quota/connectivity
   stay exactly as they were. **Delayed-skeleton clause:** on a period switch
   the previous period's data stays on screen while the new one is in flight,
   and the skeleton is only shown if the fetch is still pending after ~150 ms
   (so a fast round-trip never visibly collapses the sections). First loads -
   when no previous period's data exists - show skeletons immediately. A component whose toggle is off must not fetch
   its source at all (a glance-off cycle contains no `/api/insights` or
   `/api/stats` request).

3. **Timeouts.** Short timeout (3-5s) for `/health`; long for data requests
   (~90s): cold parses of long windows genuinely run tens of seconds (server warm
   docs: year ~15 s, month ~25 s + a ~22 s base), and a shorter ceiling reads as a
   client failure - the year view would show "unavailable" while the server is
   merely still working.

4. **Empty is not error.** A 2xx usage response with zero totals is the empty
   state.

5. **503 backpressure.** On `503`, keep last-good in-memory data, show the busy
   banner, and back off: 15s, 30s, 60s, 5min. Do not treat 503 as offline.

6. **Partial failure.** If today succeeds but quota fails (or vice versa), render
   the successful sections normally and show an inline warning on the failed
   section. The header stays connected. Active-time, insights and stats are
   **optional decorations**: their failure hides the active segment or the
   glance silently - no inline warning, no banner, no stale marker. Only usage
   and quota failures ever warn.

7. **Freshness.** Compute "Updated N min ago" from `timestamp` (or
   `response_cache.age_seconds`). Append "· stale" only when the data is older
   than the refresh window and the last fetch failed. Do not expose cache
   implementation terms during normal operation.

8. **No extra polling.** Low-quota notifications are evaluated from already-
   scheduled `/api/quota` reads. The companion must never create extra provider
   network polling.

## Refresh cadence

- Open flyout with data older than 60s -> immediate fetch.
- While open -> no more often than every 60s.
- Closed but resident -> every 10 minutes (matches Tokdash's 600s response cache).
- Sleep/wake -> pause periodic work; resume with one coalesced request.
- Failure backoff -> 15s, 30s, 60s, 5min.

## Quota display rules

### Low view (default)

A window is "low" when `remaining_percent <= threshold[bucket]`:

| Bucket type | Threshold (default) |
|---|---|
| `5h` / 5-hour | 20% |
| `weekly` / 7d | 10% |
| other | 15% |

Thresholds are configurable in Settings.

Low view shows the low windows sorted by `remaining_percent` ascending, **at most
two rows**. Labels are cross-provider: `"{Provider} · {bucket}"`. When none are
low, the section collapses to: "No subscription window is below its alert
threshold."

### All view

Every detected window, **grouped by provider** (provider order as detected; window
order within a provider as returned by the API). Labels are bucket-only
(`"{bucket}"`) under a provider header. Capped at four visible rows with the
fifth peeking, then scrolls. Never stretches the surface.

The provider header carries a 14 px provider mark before the name, mirroring the
web brand map: claude, codex, kimi, grok and antigravity ship their own marks;
`zai` uses the Zcode badge, `minimax` the mimo wordmark, `opencode_go` the
OpenCode mark. `commandcode` and any unknown provider render text-only - never a
placeholder. Dark-ink marks (codex, grok, zcode) have pre-inverted dark copies;
every other mark renders as shipped in both themes.

### Row anatomy (Low and All)

- Line 1: label, then the window's reset text in secondary ink right after it,
  with `{N}% left` pushed to the trailing edge. Line 2: the bar at full row
  width, 4 px, fill sized to `remaining_percent`.
- **Reset text is mixed.** Within 24 h of now: the relative countdown with the
  established truncation ladder ("resets in 40 min" / "in 3 h" / "in 2 days").
  Beyond 24 h: absolute local time, "resets Thu 02:00" - a day-resolution
  countdown off a stale refresh carries no information. Weekday names follow the
  app language (English/Chinese), not the OS locale.
- **Weekly label normalization.** After the ` window` strip, a window token
  that reads "7-day" / "7 day" / "7d" (case-insensitive) displays as **Weekly**,
  so Codex reads the same as MiniMax/Kimi/Grok, which already send "Weekly".
  The rule applies to the token alone ("7-day") and inside a compound feature
  label ("Spark · 7-day" -> "Spark · Weekly"); no other label is touched.
- **Bar ramp** (same tier boundaries on every surface): fine >= 50, mid >= 25,
  low < 25 remaining. macOS uses fixed status colors in both themes:
  `#30A74C` / `#FF9F0A` / `#FF453A`. Windows uses the native status ramp:
  light `#0F7B0F` / `#CA5010` / `#C42B1C`, dark `#6CCB5F` / `#F7630C` /
  `#FF99A4`.

### Disabled state

When `enabled == false`, show one quiet row: "Subscription tracking is off" with
an `Open Dashboard` path. Do not configure consent in the companion.

### Provider failures

A failed provider produces an inline warning, not a full-surface failure. Its
`buckets` are last-known and stay visible (fixture
`fixtures/quota-provider-error.json`).

Failure is evaluated at two levels, because a provider can hold several
credentials and fail for only some of them (e.g. MiniMax global + CN, where the
CN token is stale). The two levels must not be collapsed.

**Group failed** - drives the `⚠ Couldn't refresh - showing last known` warning
under the provider header in the All view. True when the provider's `status` is
present and not `"ok"`, **or** its `status_detail` is non-empty (e.g.
`stale_token`, even when `status` is `"ok"`). Absent `status` with an empty
`status_detail` is healthy. This stays deliberately broad: a single broken
credential should still warn about the provider.

**Row failed** - drives the inline `⚠` prefix on a quota row and its eligibility
for low-quota notifications. True when the failure that applies to this row is
newer than the row's own data, so the row is last-known:

    if !groupFailed: return false
    entry = (providers.*.accounts ?? []).find(a => a.account == buckets[].account)
    if entry:
        if !accountFailed(entry): return false        # this row's own credential is fine
        statusAt = entry.status_at ?? providers.*.status_at
    else:
        statusAt = providers.*.status_at              # no accounts, or no entry for this one
    if buckets[].captured_at == null or statusAt == null: return true
    return buckets[].captured_at < statusAt

where `accountFailed` is the group rule above applied to the entry: `status` present
and not `"ok"`, **or** a non-empty `status_detail`.

Judge each row against the **owning account**, whenever `providers.*.accounts` is
present (see [Accounts](#accounts)). `providers.*.status_at` is the newest error of
*any* account behind the card, so using it for every row makes one permanently
broken credential suppress rows belonging to a credential that is working: it
advances `status_at` every cycle, while a bucket that is not reported every cycle
keeps an older `captured_at`. Those buckets are common and not an edge case -
Claude's `limits` array carries `weekly_scoped_opus` only once Opus has been used,
and MiniMax's per-model buckets come and go with the models called - so the healthy
install's rows would go quiet for as long as the sibling stayed broken.

Note the early return for a healthy entry, which is doing real work: a healthy
account has `status_at: null`, and a null timestamp otherwise means "fall back", so
without it every row of the working install would be marked failed - the same bug
in a new place. Check whether the account failed *before* reaching for its
timestamp.

The comparison is strictly `<`, so a row captured in the same cycle as the failure
counts as fresh. Everything not covered above falls back to the group's value
rather than un-suppressing a row that may well be stale: `accounts` absent (a
single-credential provider, or an older server), a `buckets[].account` with no
matching entry, or a failed account whose own `status_at` is missing.

Do **not** use `buckets[].status` for this. It is always `"ok"`: the server only
writes failure statuses to a synthetic `api` bucket, which it then filters out of
the payload. Freshness is the only field that discriminates.

Worked examples (`status_at` is the account's when `accounts` is present):

| Provider | `captured_at` | `status_at` | Row failed | Why |
|---|---|---|---|---|
| codex, fully failed | 1785000000 | 1785030000 | yes | data predates the failure; last-known |
| minimax, healthy credential | 1785030000 | 1785030000 | no | refreshed in the failing cycle |
| minimax, broken credential | 1785000000 | 1785030000 | yes | not refreshed this cycle |
| claude, healthy `default`, broken `academic` | 1785000000 | 1785000000 (`default`) | no | `academic`'s newer error is not this row's |

So a healthy window inside a partially-failed provider renders without a `⚠` and
still notifies, while its broken sibling is marked and suppressed - both under one
warned provider header. Collapsing the two levels silences alerts on healthy
windows for as long as any sibling credential stays broken, because the server
only clears `status_detail` once a *newer* successful observation exists and all
credentials in a cycle share one `captured_at`. Conversely, treating every row of
a failed provider as fresh would alert on stale numbers.

`status_detail` is one of `unavailable`, `fetch_error`, or `stale_token` (the
only values the server writes). Treat any other non-empty value as a failure
too, and an absent/empty value as healthy.

### Accounts

`providers.*.accounts` is present only on a card that measures **more than one
credential**: a `~/.claude` install beside a `~/.claude-<profile>` sibling, or a
MiniMax global and mainland-China Token Plan. One entry per credential:

```json
"accounts": [
  {"account": "default",  "plan": "Max 20x", "status": "ok",          "status_detail": null,          "status_at": null,       "updated_at": 1785080061},
  {"account": "academic", "plan": "Pro",     "status": "stale_token", "status_detail": "stale_token", "status_at": 1785080120, "updated_at": 1785000000}
]
```

- The card's own account is first (`default` for Claude, `global` for MiniMax);
  the rest follow by name. `account` matches `buckets[].account`.
- `status`, `status_detail` and `status_at` are that credential's alone, read
  exactly as the group rule above, and are what makes row-level precision
  possible. An account's own newer success retires its error; a success on
  another account does not.
- An entry may exist with no matching buckets - a credential that is signed in
  but not polled yet, or one whose key was never valid. Give it its own heading
  and print its notice under that heading, not over the card.
- Absent for a single-credential provider. Do not synthesize it; fall back to the
  provider-level fields, which is what every pre-`accounts` server returns.

`plan` on an entry is that credential's own. `providers.*.plan` stays the card's
primary account, so it does not change meaning when a second install appears.

`providers.*.status_account` ships beside `accounts` and names **which entry the
card's own `status_detail` belongs to**, or is `null` when it belongs to none of
them — a provider whose credentials could not be read at all records that failure
under a synthetic account which is not a credential and is not listed here.

Use it whenever you need "is this card's error attributed", such as deciding
whether a provider is healthy overall. Do **not** substitute "does any entry carry
a `status_detail`": the two answers diverge exactly when a card that cannot read
its credentials *also* holds an older per-account failure, which is an ordinary
sequence (a region's key expires, then the credential file is removed), and the
older failure is then mistaken for the owner of the newer one. Matching on
`status_detail` and `status_at` values would usually work and is the fragile
version — attribution is not something to infer from value equality. Treat a
missing `status_account` on a payload that has `accounts` as *unavailable*, not as
`null`, and fall back to whatever you did before this field existed.

## Components and settings (schema v3)

Settings schema v3 adds a `components` object to the v2 server settings. A v2
file (no `components`) migrates to every default below; unknown keys are
ignored in both directions.

```json
"components": {
  "fullDeltaRow": true,
  "topRanks": true,
  "resetCredits": true,
  "activityGlance": true,
  "activityHistogramTodayWeek": true,
  "perServerRows": true
}
```

| Key | Default | Gates |
|---|---|---|
| `fullDeltaRow` | on | the cost+tokens delta line vs the shipped cost-only comparison line |
| `topRanks` | on | the Top tools / Top models strip |
| `resetCredits` | on | the Codex reset-credits row **and** its expiry notification |
| `activityGlance` | on | the Activity glance component (and its endpoint reads) |
| `activityHistogramTodayWeek` | on | histogram faces on today/week; off means no strip there; month/year grids are unaffected |
| `perServerRows` | on | per-server rows when more than one server is enabled |

Each switch persists on change with no OK/Apply - using the settings window's
existing debounced autosave where one exists (the Windows window's 600 ms
pattern is the reference), immediate write on macOS. "On change" means a few
hundred ms of debounce is compliant; a separate save step is not.

The period segment and the inline active-time figure are **not** settings -
core hero furniture, always on. The selected period persists on its own as
`selectedPeriod` (`today|week|month|year`, default `today`).

The top-ranks row count persists on its own as `rankRows` (integer, default 3,
valid range 3..8). It is **one shared count** for both the tools and the models
list; the surface grows to fit. Absent (any pre-setting file) means 3; an
out-of-range value is clamped into 3..8 on read and on write - never trusted
verbatim. Changing it re-renders the strip from last-good data; no refetch.

## Hero display rules

### Period segment

Always visible above the hero: `Today | Week | Month | Year`. The selection
drives the hero number, sub-line, delta row, rank kickers and the glance face.
It is a core panel element, never a Settings option.

### Active time

The hero sub-line reads `1.24M tokens · 218 messages · active 3 h 12 m`, from
`active_ms` (the merged union - never `active_ms_sum`, which double-counts
concurrent tools):

```
active_ms == 0   -> segment absent (never "active 0 m")
< 60 s           -> "active <1 m"
< 1 h            -> "active {m} m"        floor
< 24 h           -> "active {h} h {m} m"  floor of each part
>= 24 h          -> "active {d} d {h} h"  floor of each part
```

Multi-server: sum `active_ms` across reachable servers; if any enabled server
has no active-time data this cycle, drop the segment rather than present a
known-partial sum. Against servers predating the endpoint (404) the segment is
absent with no warning - the v1.0 fallback.

### Full delta row

With `fullDeltaRow` on, one line under the hero. **Two metrics** (cost +
tokens): the line must hold one narrow flyout line in every language, and
messages turned out to be the metric nobody acted on. The server still sends
`comparison.messages_pct`; companions ignore it.

```
{glyph} {pct}% cost · {glyph} {pct}% tokens {sentence}
```

- glyph `▲` for > 0, `▼` for < 0, `±` for exactly 0; `{pct}` is a non-negative
  integer (`abs(round(pct))`): `-11.7` renders `12`.
- `{sentence}` by segment: "vs yesterday", "vs last week", "vs last month",
  "vs last year".
- a metric whose `*_pct` is `null` is omitted from the line; if both are
  `null` the line is absent entirely (`healthy-year`).
- multi-server: recompute each pct from summed current and previous totals;
  omit a metric when any contributing server omits its `*_prev`.

With the toggle off: the shipped single comparison line, cost-only and worded
("12% below yesterday"), behavior unchanged from 1.0.2.

### Top ranks

One strip as the last row of the hero card, two blocks:
`Top tools · {today|this week|this month|this year}` and `Top models · ...`.

- **Tools**: `by_tool` sorted by `tokens` descending, top `rankRows`
  (default 3). Label is the display name (Codex, Claude, Kimi, OpenCode, ...),
  value compact tokens. Each row is prefixed with the packaged harness logo;
  a tool id with no shipped mark reserves the slot - never a placeholder
  dropped mid-column. Codex's black mark inverts on dark, the same rule the
  web applies.
- **Models**: first `rankRows` of `combined_models` (tokens-ranked), provider
  prefix stripped (`openai/gpt-5.6-sol` -> `gpt-5.6-sol`), compact tokens,
  **no logos on model rows** - and no reserved logo slot either: model names
  render flush left.
- Both blocks absent when their source is empty. The 1.0 "Most used today"
  activity line is retired; top ranks replace it.

### Activity glance

One component, four faces, by selected period:

| Period | Face | Source | Geometry |
|---|---|---|---|
| today | hour histogram | `insights?facets=hourly&period=today` | 24 bars, hour 0 first |
| week | day histogram | `insights?facets=daily&date_from&date_to` | 7 columns Mon..today |
| month | contribution grid | `stats` | trailing 90 days, 7 rows Mon..Sun |
| year | contribution grid | `stats` | trailing 180 days, smaller cells |

- Histogram bars use the accent color, height proportional to `tokens`, a 2 px
  minimum visible height for non-zero values; zero buckets render as empty
  space. Caption `Peak {HH}:00` from `peak_hour` (today only); `peak_hour`
  `null` on an all-zero day means render no strip at all.
- Week columns are labeled with locale weekday abbreviations Mon..Sun;
  `daily` is sparse, so a missing date is an empty column
  (`insights-week.json` has no Tuesday).
- Grids: window `contributions[]` client-side to the trailing 90/180 calendar
  days (fixture ending 2026-07-26: 68 filled cells at 90 days, 143 at 180),
  lay out strict Mon..Sun rows, and color by `intensity` 0..4. An all-zero
  window hides the component.
- One sequential ramp, monotonic lightness, separate light/dark steps,
  identical on both platforms; never reuse the categorical quota colors.
- Failures are silent (behavior rule 6): zero non-zero cells hides the strip
  the same as a 404 would.

### Per-server rows

With `perServerRows` on and **more than one enabled server**: one row per
server for the selected period - `{label}  $3.42 · 18.7M` - in settings order,
footnoted "combined in hero · this cycle only". Values come from the fan-out
already performed for the hero; never an extra request. An unreachable server
shows its label plus "unreachable", never dimmed numbers. Absent entirely with
one enabled server (the rows would only echo the hero). This retains
per-server values within the current refresh cycle only - no persistent
history, same as the rest of the companion.

### Reset credits

`providers.codex.reset_credits` exists on Codex only, and only when credits
exist. With `resetCredits` on, quota tracking enabled, and
`available_count >= 1`, render one quiet left-accent row **under the Codex
group in the All view**:

```
⚡ {Provider} · {available_count} reset credits · expire {clause}
```

- `{clause}` from the soonest **future** `credits[].expires_at`: full days
  remaining >= 2 -> `in {d} d` (floor); 1..2 days -> `tomorrow`; < 1 day ->
  `today`; no future expiry -> drop the clause. Expired entries are ignored
  for both the clause and the notification.
- The Low view never shows the row (it is provider context, not a window).
- The clock for "days remaining" is the client's own; tests freeze it to the
  payload `timestamp` so fixtures stay deterministic.
- A muted "use or lose" hint is right-aligned on the same row (design from
  the approved mock). It is static decoration, like the bolt: not part of the
  pinned row string and not localized per-credit.

### Server update badge

Settings window only. On open: `GET /api/version` and show `runtime_version`;
when `update_check_enabled` is true, also `GET /api/update-check` and, if
`update_available` and `latest` is non-null, a muted row "Server update
available: v{latest}". `enabled == false` means the server's owner has not
given update-check consent: render nothing, and never try to change that (the
consent POST is web-only). Failures are silent.

## Low-quota notifications

- Opt-in, default off.
- Evaluate from scheduled `/api/quota` reads only.
- Notify on a crossing from above to at-or-below the threshold.
- Deduplicate by `(provider, account, bucket, reset_epoch, threshold)`.
- A new `resets_at` epoch re-arms the notification.
- Click -> open companion to quota section (Low view).
- Do not notify for: offline, busy, estimated-data staleness, quota recovery.
- If a bucket has no `resets_at`, suppress until an explicit re-arm rule exists.
- Suppress a window whose own row is failed (see Provider failures). A group
  failure alone must not suppress its healthy sibling windows.
- **Reset credits** ride the same opt-in and the same scheduled quota read:
  when the `resetCredits` component is on, notify once a future credit enters
  its last 48 hours - the edge is inclusive: `expires_at - now <= 48 h`
  arms. (`credits.json` freezes the clock at exactly 48 h and pins armed.)
  Dedup by `(provider, credits[].id, expires_at)` - a
  credit carries its own identity, so no re-arm rule is needed. Suppressed
  while the Codex provider group failed (last-known credit data is not a
  basis for an "expire in" warning). Click -> open the companion's quota
  section (All view).

## Expected behavior cases

Each file in `contract/expected/` describes the observable UI outcome for a
given fixture combination. Both native test suites assert against these.

A case file names its fixtures (`health`, `usage`, `active_time`, `insights`,
`stats`, `quota`), the `period` segment to select (default `today`), and
optional `settings_overrides` merged over the schema-v3 defaults. In a fixture
slot, `null` means the endpoint returns nothing at all this cycle (treated as
a failure), `"503"` and `"pending"` keep their existing meanings. Strings are
pinned under the English locale; both suites run cases in English.

| Case | Period | Fixtures | Expected outcome |
|---|---|---|---|
| `healthy` | today | usage-today + active-time-today + insights-today + quota | Connected; hero $3.42 / 18.7M / 248 · active 3 h 12 m; delta row "▼ 12% cost · ▼ 12% tokens vs yesterday"; top tools "Codex 13M…" + top models "gpt-5.6-sol 12.7M…"; 24-bar histogram "Peak 14:00"; Low shows Claude weekly (8%) + Codex 5h (14%); no credits row |
| `healthy-week` | week | usage-week + active-time-week + insights-week + quota | Week is a calendar window (`date_from` Mon..today, never `period=week`); hero $9.86 / 61.2M / 1043 · active 2 d 4 h; delta "…vs last week"; 7-column day histogram with an empty Tuesday column; kickers "this week" |
| `healthy-month` | month | usage-month + active-time-month + stats + quota | hero $48.90 / 281M / 4218 · active 9 d 20 h; delta "…vs last month"; 90-day Mon..Sun grid, 68 filled cells |
| `healthy-year` | year | usage-year + active-time-year + stats + quota | hero $312.40 / 1.2B / 13204 · active 74 d 5 h; delta row **absent** (previous year is zero, all `*_pct` null); 180-day grid, 143 filled cells |
| `empty` | today | usage-today-empty + active-time-zero + insights-empty + quota | hero "No usage recorded today"; no active segment; no delta row; no top ranks; glance hidden (all-zero source) |
| `active-zero` | today | usage-today + active-time-zero + quota | active segment absent (never "active 0 m") |
| `credits` | today | usage-today + quota-reset-credits | row "⚡ Codex · 2 reset credits · expire in 2 d" under the Codex group (All view only, frozen clock = fixture timestamp); credits-expiry notification armed (48 h window) |
| `delta-row-off` | today | usage-today, `fullDeltaRow` off | no delta row; shipped cost-only line "12% below yesterday" renders instead |
| `glance-off` | today | healthy set, `activityGlance` off | no glance strip **and no `/api/insights` or `/api/stats` request at all** |
| `histogram-off` | today | healthy set, `activityHistogramTodayWeek` off | no strip on today/week; month/year grids unaffected |
| `per-server` | today | two servers, Second without active-time | hero sums cost/tokens but **drops** the active segment (partial data); per-server rows + footnote |
| `multi-server` | today | two healthy servers | hero $6.84 / 37.4M · active 6 h 24 m (summed); per-server rows; Low-view dedup; minimum-delay rule; footer partial-failure wording |
| `quota-disabled` | today | usage-today + quota-disabled | quota section "Subscription tracking is off" with Open Dashboard; no credits row |
| `wrong-service` | - | health-wrong-service | "This address is not a Tokdash service"; no other calls |
| `offline` | - | (timeout/connection refused) | "Tokdash is not reachable"; Retry + Settings; last-good dimmed; footer "· stale" |
| `busy` | today | usage/active/insights/quota all 503 | "Tokdash is busy - retrying"; last-good dimmed; back off |
| `partial` | today | usage-today ok; active/insights/quota 503 | hero normal; **quota** shows inline "will retry shortly"; active segment and glance vanish **silently** (rule 6) |
| `loading` | today | all pending | "Connecting…"; skeletons (on a period switch: only after ~150 ms in flight, previous data held until then); segment stays visible; no spinner |
| `provider-error` | today | usage-today + quota-provider-error | All view: "Couldn't refresh - showing last known" under the failed provider, rows still visible; Low prefixes ⚠ on its low row; no credits row (quota fixture has none) |
| `partial-failure` | today | usage-today + quota-partial-failure | MiniMax header warns; row ⚠ only on `cn_general_5h`; notifications fire for `global_general_5h`, suppressed for `cn_general_5h` |

## Freshness text

```
age < 60s    -> "Updated just now"
age < 3600s  -> "Updated N min ago"
age < 86400s -> "Updated N h ago"
else         -> "Updated N d ago"
```

Append " · stale" when the last fetch failed and last-good data is being shown.
Append " · cached" only when it helps explain why data has not changed (rare).

## Token compact notation

```
tokens >= 1_000_000_000 -> "{value/1B}B"  (one decimal, trailing ".0" trimmed: "1.2B", "75B")
tokens >= 1_000_000  -> "{value/1M}M"   (one decimal, trailing ".0" trimmed: "18.7M", "13M")
tokens >= 1_000      -> "{value/1k}k"   (no decimal: "779k")
else                 -> str(value)
```

One decimal on the B and M tiers. Trailing ".0" is trimmed on all tiers:
`12_982_308` -> "13M", `1_200_000_000` -> "1.2B", `75_000_000_000` -> "75B".
`round` (not floor) to the shown precision. Exact value in
accessibility text / tooltip. This same rule renders the Top-ranks values and
per-server tokens.

## Cost formatting

```
cost -> "${value:.2f}"   # always two decimals: "$3.42", "$0.06", "$0.00"
```
