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
| `GET` | `/api/usage?period=today` | Today hero + activity line | popover open, scheduled refresh |
| `GET` | `/api/usage?period=month` | Month context line | popover open, scheduled refresh |
| `GET` | `/api/quota` | Quota section | popover open, scheduled refresh |
| `GET` | `/api/version` | About / Settings diagnostics | settings open only |

Not used by the companion:
- `POST /api/quota/consent`, `POST /api/quota/settings` (write-gated; companion
  is read-only).
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

### `GET /api/usage?period=today`

Fixture: `fixtures/usage-today.json` (realistic data), `fixtures/usage-today-empty.json` (empty).

Fields used by the companion:

| Field | Type | Use |
|---|---|---|
| `total_cost` | float | Today hero primary value |
| `total_tokens` | int | Today hero secondary line (compact notation) |
| `total_messages` | int | Today hero secondary line |
| `comparison.cost_pct` | float \| null | "12% below yesterday" / "8% above yesterday". Omit when `null`. |
| `comparison.cost_prev` | float \| null | Previous-period cost used to recompute a combined percentage across reachable servers. Hide the comparison when any contributing server omits it. |
| `by_tool` | object | Leading tool by cost (activity line) |
| `combined_models` / `top_models` | array | Leading model by cost (activity line) |
| `timestamp` | string (ISO 8601) | Freshness calculation |
| `response_cache.age_seconds` | float | Freshness "· cached" hint when useful |

Additive decoding: ignore unknown fields, tolerate absent optional fields. A
valid response with `total_tokens == 0` is the **empty state**, not an error.

### `GET /api/usage?period=month`

Fixture: `fixtures/usage-month.json`.

Fields used: `total_cost`, `total_tokens`. The month label (e.g. "JULY") is
derived client-side from the local calendar, not from the response.

### `GET /api/quota`

Fixture: `fixtures/quota.json` (enabled, multi-provider), `fixtures/quota-disabled.json` (disabled), `fixtures/quota-provider-error.json` (one provider failed refresh).

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

Buckets with `remaining_percent == null` are rendered without a percentage and
without a bar fill; they are not candidates for the Low view.

## Client behavior rules

Companion settings schema v2 stores a `servers` array (`id`, `label`, `baseUrl`,
`enabled`). A v1 `BaseURL`/`baseURL` value migrates to the first entry. Refreshes
fan out across enabled servers; failed servers are excluded from combined figures
until they recover. Native clients do not require a passing Test before saving a
valid URL.

1. **Health gate.** Call `/health` first. Require `service == "tokdash"`. On
   mismatch or non-2xx, enter wrong-service / offline state. Do not call usage
   or quota endpoints until health passes.

2. **Concurrent fetch.** After a successful health check, fetch today, month,
   and quota concurrently. Cancel or coalesce overlapping refreshes.

3. **Timeouts.** Short timeout (3-5s) for `/health`; longer (15-30s) for usage
   requests which can trigger a cold parse.

4. **Empty is not error.** A 2xx usage response with zero totals is the empty
   state.

5. **503 backpressure.** On `503`, keep last-good in-memory data, show the busy
   banner, and back off: 15s, 30s, 60s, 5min. Do not treat 503 as offline.

6. **Partial failure.** If today succeeds but quota fails (or vice versa), render
   the successful sections normally and show an inline warning on the failed
   section. The header stays connected.

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
for low-quota notifications. True when the group is failed **and**
`buckets[].captured_at < providers.*.status_at`: the failure is newer than this
row's data, so the row is last-known. The comparison is strictly `<`, so a row
captured in the same cycle as the failure counts as fresh. When either timestamp
is absent or null (older servers), fall back to the group's value rather than
un-suppressing a row that may well be stale.

Do **not** use `buckets[].status` for this. It is always `"ok"`: the server only
writes failure statuses to a synthetic `api` bucket, which it then filters out of
the payload. Freshness is the only field that discriminates.

Worked examples:

| Provider | `captured_at` | `status_at` | Row failed | Why |
|---|---|---|---|---|
| codex, fully failed | 1785000000 | 1785030000 | yes | data predates the failure; last-known |
| minimax, healthy credential | 1785030000 | 1785030000 | no | refreshed in the failing cycle |
| minimax, broken credential | 1785000000 | 1785030000 | yes | not refreshed this cycle |

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

## Expected behavior cases

Each file in `contract/expected/` describes the observable UI outcome for a
given fixture combination. Both native test suites assert against these.

| Case | Health | Usage today | Usage month | Quota | Expected outcome |
|---|---|---|---|---|---|
| `healthy` | health.json | usage-today.json | usage-month.json | quota.json | Connected; Today $3.42 / 18.7M / 248; "12% below yesterday"; July $48.90 / 281M; Low shows Codex 5h (14%) + Claude weekly (8%); activity "Codex · gpt-5.6-sol"; "Updated 2 min ago" |
| `empty` | health.json | usage-today-empty.json | usage-month.json | quota.json | Connected; hero "No usage recorded today"; month line still shows; activity omitted; quota section unchanged |
| `quota-disabled` | health.json | usage-today.json | usage-month.json | quota-disabled.json | Connected; Today/month normal; quota section shows "Subscription tracking is off" with Open Dashboard |
| `wrong-service` | health-wrong-service.json | - | - | - | "This address is not a Tokdash service"; no usage/quota calls |
| `offline` | (timeout/connection refused) | - | - | - | "Tokdash is not reachable"; Retry + Settings buttons; last-good data dimmed; footer "· stale" |
| `busy` | health.json | 503 | 503 | 503 | "Tokdash is busy - retrying"; last-good data dimmed; back off |
| `partial` | health.json | usage-today.json | 503 | 503 | Connected; Today hero normal; month + quota show inline "will retry shortly" warnings |
| `loading` | (pending) | (pending) | (pending) | (pending) | "Connecting…"; skeletons for Today/month/quota values; no spinner |
| `provider-error` | health.json | usage-today.json | usage-month.json | quota-provider-error.json | Connected; Today/month normal; quota All view shows an inline "Couldn't refresh - showing last known" warning under the failed provider's header, its rows still visible; healthy providers render normally; Low view prefixes ⚠ on the failed provider's low row (both Codex buckets have `captured_at` older than its `status_at`) |
| `partial-failure` | health.json | usage-today.json | usage-month.json | quota-partial-failure.json | Connected; MiniMax header carries the "Couldn't refresh - showing last known" warning, but only `cn_general_5h` (older `captured_at`) gets the row ⚠ — `global_general_5h` was captured in the failing cycle and renders clean; Low view shows both, ⚠ on the 9% row only; notifications fire for `global_general_5h` and are suppressed for `cn_general_5h` |

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
tokens >= 1_000_000  -> "{value/1M}M"   (one decimal: "18.7M")
tokens >= 1_000      -> "{value/1k}k"   (no decimal: "24966k")
else                 -> str(value)
```

Exact value in accessibility text / tooltip.

## Cost formatting

```
cost -> "${value:.2f}"   # always two decimals: "$3.42", "$0.06", "$0.00"
```
