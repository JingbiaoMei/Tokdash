# Tokdash Companion - Implementation Spec

This document is the **implementation authority** for the companion apps. It
reconciles the older planning Markdown (`COMPANION_APP_PLAN.md`,
`UI_CONTENT_SPEC.md`) with the final approved HTML prototype
(`UI_CONCEPT.html`), which is the **visual authority**.

Where the Markdown and the HTML disagree, **the HTML wins**. The disagreements
are called out below so the Markdown can be read as history, not as a spec.

## 1. Resolved product contract

These decisions are final for the MVP. They supersede the "open product
decisions" / "decisions Howard should make" sections of the planning docs.

| Decision | Resolution | Source |
|---|---|---|
| Surface layout | **One combined spend-first surface.** No Usage/Quota tab switcher. Today hero, month context, quota section, and activity line all render together in a single scrollable popover/flyout. | HTML body + review notes |
| Quota display | Inline **Low / All** selector inside the quota section, not a top-level view switch. Low = windows below threshold (max two). All = every detected window grouped by provider, capped at four rows, scrolls. | HTML `quotaSegHTML` / `quotaHTML` |
| Thresholds | 5-hour: **20% remaining**. Weekly: **10% remaining**. Default (other buckets): **15%**. All configurable in Settings. | HTML `DATA.quotaThresholds` |
| macOS closed state | **Icon-only** menu-bar presence. No permanent `$today` text beside the icon. Optional text mode is deferred (not MVP). | HTML review notes + spec |
| Tailscale / HTTPS URLs | **Supported in MVP** as a manually configured base URL. Default stays `http://127.0.0.1:55423`. | Plan §10 + prompt |
| Low-quota notifications | **Opt-in but included in MVP.** Default off. 20% remaining default threshold. Deduplicate by provider/bucket/reset epoch. | Plan §10 + prompt |
| Windows version | **Windows 11 only.** No Windows 10 compatibility promise. | Plan §6 + prompt |
| Launch at login | **Opt-in.** Off by default. macOS via `SMAppService`; Windows via Startup folder / registry. | Plan §10 + prompt |
| Busy/offline banners | Appear **above dimmed last-good data**, not replacing the hero. | HTML review notes |
| Repository | **In this repo under `companion/`**, not a separate repo. Excluded from the Python wheel/sdist. | Prompt |

## 2. Surface anatomy (single combined surface)

This replaces the "two views inside the same popover" language in
`UI_CONTENT_SPEC.md` §2. The header, action row, and freshness footer are
always visible; there is no view switching.

```
┌─ Header ──────────────────────────────────────┐
│ [logo] Tokdash   • Local · Connected   [gear]  │
├────────────────────────────────────────────────┤
│ (busy/offline banner, only when applicable)    │
├────────────────────────────────────────────────┤
│ TODAY                                          │
│ $3.42                                          │  ← Today hero (spend-first)
│ 18.7M tokens · 248 messages                    │
│ ▼ 12% below yesterday                          │
├────────────────────────────────────────────────┤
│ JULY   $48.90   281M tokens                    │  ← Month context
├────────────────────────────────────────────────┤
│ Subscription              [Low | All]          │  ← Quota section
│ Codex · 5-hour    14% left   resets 14:40      │
│ ███████░░░░░░░░░░░░░░░░░░░                     │
│ Claude · weekly   8% left    resets Mon        │
│ ██████░░░░░░░░░░░░░░░░░░░░░                    │
├────────────────────────────────────────────────┤
│ Most used today  Codex · gpt-5.6               │  ← Activity line
├────────────────────────────────────────────────┤
│ [Open Dashboard]  [↻]                          │  ← Action row
├────────────────────────────────────────────────┤
│ Updated 2 min ago              [Quit]          │  ← Freshness footer
└────────────────────────────────────────────────┘
```

### Quota section: Low vs All

- **Low (default):** windows where `remaining_percent <= threshold[bucket]`.
  Sorted by `remaining_percent` ascending. **At most two rows.** When none are
  low, the section collapses to one quiet line: "No subscription window is
  below its alert threshold."
- **All:** every detected window, **grouped by provider** (provider order as
  detected, window order within a provider as returned by the API). Capped at
  four visible rows with the fifth peeking, then scrolls. Never stretches the
  surface.
- The selector is labelled **Low / All**, not "Urgent". The thresholds carry
  the urgency semantics; the toggle is a filter, not an alarm list.
- No separate subscriptions window. Everything quota-related lives inside this
  one surface.
- Do not ship custom scrollbar styling - use `NSScroller` overlay style on
  macOS and WinUI `ScrollViewer` defaults on Windows.

### What gets cut if crowded (in order)

1. Activity line ("Most used today") - first to cut.
2. Month context line - survives in Empty state where it is the only real data.
3. Nothing else.

## 3. States

All states render inside the same combined surface. The banner sits above
dimmed last-good data; it never replaces the hero.

| State | Header | Body |
|---|---|---|
| **loading** (no prior data) | "Connecting…" | Real layout with skeleton placeholders for main values. No full-screen spinner. |
| **refreshing** (prior data) | Connected (unchanged) | All data visible. Only the Refresh affordance pulses (respect reduced motion). |
| **offline** | "Offline" (red dot) | Banner: "Tokdash is not reachable / Start Tokdash, or check the server address in Settings. [Retry] [Settings]". Last-good data dimmed below. Footer appends "· stale". |
| **busy** (503) | "Busy" (amber dot) | Banner: "Tokdash is busy - retrying / Last data from 3 min ago. Backing off automatically." Last-good data dimmed. Do not translate 503 into "offline". |
| **partial** | Connected (unchanged) | Successful sections render normally. Failed section shows inline warning: "Quota data unavailable - will retry shortly. [Retry now]". |
| **empty** | Connected (unchanged) | Hero: "No usage recorded today / Tokdash is running. Today's totals will appear as tools report usage." Month context still shows. Activity line omitted. |
| **wrong service** | (offline path) | If `/health` returns `service != "tokdash"`: "This address is not a Tokdash service". Do not call usage endpoints. |

## 4. Data mapping (API -> UI)

| UI element | API source | Field |
|---|---|---|
| Today cost | `GET /api/usage?period=today` | `total_cost` |
| Today tokens | same | `total_tokens` (compact notation: `18.7M`; exact in tooltip/a11y) |
| Today messages | same | `total_messages` |
| Today comparison | same | `comparison.cost_pct` → "12% below yesterday" / "8% above yesterday". Omit if previous-period data absent. |
| Month cost | `GET /api/usage?period=month` | `total_cost` |
| Month tokens | same | `total_tokens` |
| Month label | client local | current month name (e.g. "JULY") |
| Quota windows | `GET /api/quota` | `providers.*.buckets[]` → `remaining_percent`, `resets_at`, `bucket_label`, `estimated` |
| Provider name | same | top-level key under `providers` (capitalize for display) |
| Bucket name | same | `bucket` / `bucket_label` |
| Estimated badge | same | provider-level `estimated: true` |
| Activity line | `GET /api/usage?period=today` | leading tool by cost from `by_tool`; leading model by cost from `combined_models` / `top_models` |
| Freshness | same responses | `timestamp` and `response_cache.age_seconds` when available |
| Connection | `GET /health` | `service == "tokdash"` required |

### Quota row construction

```
label    = "{Provider} · {bucket}"           # Low view (cross-provider)
label    = "{bucket}"                         # All view (grouped under provider header)
left     = bucket.remaining_percent           # display "14% left"
resets   = humanize(bucket.resets_at)         # "resets 14:40" / "resets Mon" - user locale & TZ
estimated = provider.estimated                # show "Estimated" badge if true
bar      = left < 25 ? red : left < 50 ? amber : green
```

When quota tracking is disabled (`enabled: false`), show one quiet row:
"Subscription tracking is off" with a path to Open Dashboard. Do not configure
consent in the companion.

## 5. Platform architecture

### macOS (SwiftUI MenuBarExtra)

- `MenuBarExtra(.window)` with `LSUIElement = true`.
- One monochrome template icon in the menu bar. Accessible name "Tokdash".
- Tooltip: "Tokdash - Today $3.42 · 18.7M tokens".
- Standard SwiftUI controls adopt Liquid Glass on macOS 26 automatically. Do
  not paint custom glass over controls.
- Test Reduce Transparency, Reduce Motion, Increase Contrast.
- `SMAppService` for opt-in launch at login.
- Settings and Quit in an NSMenu-style dropdown from the header gear, plus a
  quiet Quit in the footer.
- Deployment target: macOS 13 (MenuBarExtra availability). Build against
  macOS 26.5 SDK on the MacBook.

```
TokdashCompanionApp
  MenuBarExtra(.window)
    CompanionPopover
      HeaderSection
      BannerSection        (conditional)
      TodayHeroSection
      MonthContextSection
      QuotaSection         (Low/All selector inline)
      ActivitySection      (conditional)
      ActionBarSection
      FreshnessFooter
  Settings scene

TokdashClient actor         (URLSession)
  health() -> HealthResponse
  usage(period:) -> UsageResponse
  quota() -> QuotaResponse

CompanionStore @MainActor
  connectionState
  decodedSnapshot
  refreshScheduler
  settings
```

### Windows (C#/WPF + Win32 interop)

- Windows 11 only.
- `Shell_NotifyIconW` with `NOTIFYICON_VERSION_4` through an isolated interop
  layer (hidden message-only window). The Windows App SDK has no modern tray
  abstraction.
- One stable icon. Tooltip: "Tokdash - Today $3.42 · 18.7M tokens". Offline
  swaps stroke opacity only; do not animate rapid changes.
- Single click and keyboard activation open the flyout. Right-click opens a
  short native menu: Open Tokdash, Refresh, Settings, Exit.
- Flyout is a **WPF** window (`AllowsTransparency`, `WindowStyle=None`,
  `Topmost`, 8px rounded corners) positioned against the notification-area
  work area. Handles top/side taskbars, multiple displays, DPI changes, focus,
  Escape, outside-click dismissal, keyboard activation.
- **Acrylic** via Win32 `SetWindowCompositionAttribute`
  (`ACCENT_ENABLE_ACRYLICBLURBEHIND`) with a translucent solid fallback for
  battery saver / high contrast / unsupported configs.
- MSIX packaging for release (deferred). Unpackaged debug builds for now.
- Launch at login via the Startup folder shortcut or registry Run key.

> Note: the original plan called for WinUI 3. The VS Build Tools install on
> this host is too stripped to offer the Windows App SDK workload, and the
> WinUI 3 PRI resource tooling only ships with that workload. WPF builds with
> the .NET 10 SDK + WindowsDesktop runtime already installed (no VS workload),
> supports Acrylic via the same Win32 interop, and uses the same tray host +
> API client. Visually equivalent for a flyout.

```
App (single instance, WPF)
  NotificationIconHost
    hidden/message-only Win32 window
    Shell_NotifyIconW lifecycle
  FlyoutWindow
    WPF content (AllowsTransparency, WindowStyle=None, Topmost)
    Acrylic via SetWindowCompositionAttribute
    taskbar-edge positioning + light dismiss

TokdashClient              (HttpClient)
  health / usage / quota

CompanionViewModel          (BindableBase / INotifyPropertyChanged)
  connectionState
  decodedSnapshot
  refreshScheduler
  settings
```

## 6. API client behavior

- Base URL configurable, default `http://127.0.0.1:55423`. Support Tailscale
  HTTPS URLs (e.g. `https://wsl.tail76535.ts.net/tokdash`). Join endpoint
  paths correctly when the base URL contains `/tokdash`.
- Verify endpoint with `GET /health`; require `service == "tokdash"`. On
  mismatch, show "wrong service" state and do not call usage endpoints.
- Short connection timeout for `/health`; longer timeout for usage requests
  (cold parse).
- Fetch today, month, and quota **concurrently** after a successful health
  check.
- Decode additively: ignore unknown fields, tolerate absent optional fields.
- Valid response with no usage = empty state, not an error.
- On `503`: keep last-good in-memory data, show busy banner, back off
  (15s → 30s → 60s → 5min).
- On partial failure: render successful sections, mark only the failed section.
- Use `timestamp` and `response_cache.age_seconds` for freshness.
- Cancel or coalesce overlapping refreshes.
- Never send a browser `Origin` header from a native client.

### Refresh cadence

- Fetch immediately when the flyout opens if data older than 60 seconds.
- While open, refresh no more often than every 60 seconds.
- While resident and closed, check every 10 minutes (matches Tokdash's 600s
  response cache).
- Pause periodic work during sleep; resume with one coalesced request.
- Back off after failures: 15s, 30s, 60s, 5min.
- Refresh control refetches the client view. Never force two heavy usage
  recomputations at once.

## 7. Low-quota notifications

- Opt-in, default off.
- Default threshold: 20% remaining (configurable in Settings; per-bucket-type:
  5-hour 20%, weekly 10%, default 15%).
- Evaluate from already-scheduled `/api/quota` reads. **Never create extra
  provider polling.**
- Notify only on a crossing from above to at-or-below the threshold.
- Deduplicate by (provider, account, bucket, reset epoch, threshold).
- A new reset epoch re-arms the notification.
- Clicking a notification opens the companion directly to the quota section
  (Low view).
- Do not notify for: offline, busy, estimated-data staleness, or quota
  recovery.
- If a bucket has no reset time, keep it suppressed until an explicit product
  rule for re-arming exists.

## 8. Out of scope (MVP)

- Session browsing, charts, pricing editing, contribution heatmaps.
- Provider consent and other Tokdash configuration writes.
- Reading Tokdash logs, SQLite data, or provider credential files directly.
- Automatic Tokdash installation, startup, upgrade, or repair.
- Multiple simultaneous server profiles.
- Persistent usage history in the companion.
- Electron, Tauri, webview, telemetry, credential scanning, third-party UI
  frameworks.

## 9. Forbidden changes

- Do not modify the Tokdash server unless an evidenced API-contract gap
  requires it. Document the gap and ask before changing the server.
- Do not publish, sign a release, create tags, or alter the live released
  Tokdash installation.
- Do not switch the conda base env to `pip install -e .` (see AGENTS.md).

## 10. Build authority

| Platform | Build authority | WSL role |
|---|---|---|
| macOS | MacBook via `ssh macbook` (`xcodebuild` on Xcode 26.6 / Swift 6.3.3) | Swift source edits, fixtures, review |
| Windows | Windows host via `powershell.exe` (`dotnet build` / MSBuild) | C# source edits, fixtures, review |

Both native systems compile and test. WSL is the source-of-truth checkout;
Git sync carries edits to each native host.

## 11. Development order (from prompt)

1. Copy/freeze documentation and assets. ✅
2. Add shared API fixtures and expected behavior cases.
3. Protect Python wheel/sdist packaging from companion files.
4. Build the Windows tray technical spike.
5. Build the macOS MenuBarExtra technical spike.
6. Implement API clients and all loading/error states.
7. Match the approved native layouts.
8. Add settings, launch at login, thresholds, and notification deduplication.
9. Build, test, install, launch, and inspect logs on both native systems.
10. Prepare a manual visual-verification checklist for Howard.

Stop after verified development builds and the manual visual-test checklist.
No release, no tags, no signing.
