# Tokdash Companion app plan

## 1. Product statement

Tokdash Companion is a resident, glanceable client for the existing Tokdash service.
Clicking its macOS menu-bar item or Windows notification-area icon opens two compact
views:

1. **Usage** — What have I spent and used today, and how am I tracking this month?
2. **Quota** — Which subscription windows are closest to their limits?

Anything requiring investigation opens the existing web dashboard.

The companion is a client, not another Tokdash runtime. It must not parse logs, calculate
prices, scan credentials, manage provider tokens, or duplicate dashboard features.

## 2. MVP scope

### Included

- Connect to a configurable Tokdash base URL, defaulting to
  `http://127.0.0.1:55423`.
- Verify the endpoint with `GET /health`; require `service == "tokdash"`.
- Provide two views inside one flyout/popover: Usage and Quota.
- Show today's cost, tokens, and previous-day comparison on Usage.
- Show current-month cost as context on Usage.
- Show subscription windows on Quota, ordered by lowest remaining percentage.
- Show the leading tool and model when space permits.
- Show connected, cached/stale, busy, partial, and offline states.
- Refresh displayed data on demand.
- Open the existing dashboard in the default browser.
- Store only settings locally. Keep the last good payload in memory.
- Follow light/dark mode, high contrast, reduced transparency, reduced motion, text
  scaling, and keyboard navigation.
- Offer Launch at login as an explicit setting.
- Remain single-instance and provide a clear Quit/Exit command.
- Offer opt-in low-quota notifications with deduplication.

### Excluded from MVP

- Session browsing, charts, pricing editing, or contribution heatmaps.
- Provider consent and other Tokdash configuration writes.
- Reading Tokdash logs, SQLite data, or provider credential files directly.
- Automatic Tokdash installation, startup, upgrade, or repair.
- Multiple simultaneous server profiles.
- Persistent usage history in the companion.

## 3. Product decisions

### Native clients, shared contract

Use separate native implementations:

- macOS: SwiftUI and system menu-bar APIs.
- Windows: C#/WinUI 3 and the Win32 notification-area API.

Share behavior through fixtures and a short contract, not through a cross-platform UI
framework. Electron is too heavy for this role. A webview/Tauri approach would still need
substantial platform-specific tray, window-material, activation, packaging, and
accessibility work while making both surfaces feel less native.

### Suggested repository

Create one separate `tokdash-companion` repository after UI alignment:

```text
tokdash-companion/
  macos/
    TokdashCompanion/
    TokdashCompanionTests/
  windows/
    TokdashCompanion/
    TokdashCompanion.Tests/
  contract/
    fixtures/
    COMPANION_API.md
  docs/
```

This keeps native installers, signing, platform CI, and release cadence out of the Python
package repository. The Tokdash API remains the integration boundary. If a separate repo is
undesirable, the same structure can live under `companion/` here, but packaging must be
excluded from the Python wheel and sdist.

### macOS development must use the Mac

Use the MacBook as the macOS implementation and validation machine. Linux can edit Swift
files, review diffs, and maintain fixtures, but it cannot run Xcode, build against the
macOS SDK, sign/notarize a bundle, or exercise real menu-bar behavior.

Recommended workflow:

1. Clone the companion repository on the MacBook.
2. Make Git commits the source of truth; do not maintain two copied working trees with
   ad-hoc `rsync`.
3. The agent may use `ssh macbook` to edit, run `xcodebuild`, run unit tests, inspect the
   bundle, and perform signing checks.
4. Howard performs visual and interaction checks on the Mac.
5. CI repeats clean macOS builds, but it does not replace physical menu-bar testing.

The checked Mac is suitable: macOS 26.5.2 on arm64, Xcode 26.6, Swift 6.3.3, and about
306 GiB free.

### Windows development must use Windows

Keep source in Git and compile/run the Windows app on Windows. WSL is useful for repository
work but is not the authority for XAML compilation, notification-area behavior, MSIX
packaging, or Fluent material rendering.

The checked host runs Windows 11 build 26200 and has Visual Studio 2022 Build Tools. It has
no .NET SDK, so the WinUI toolchain is incomplete. Before the Windows spike, install:

- a supported .NET SDK;
- Visual Studio with the WinUI/Windows App SDK and .NET desktop development components;
- a current Windows SDK.

Discuss the exact SDK versions at implementation time instead of adding an environment now.

## 4. API contract

The existing API is enough for the first vertical slice.

| Endpoint | Use | Trigger |
|---|---|---|
| `GET /health` | fingerprint, version, fast connectivity | startup and reconnect |
| `GET /api/usage?period=today` | primary totals, comparison, tool/model ranking | popover open and scheduled refresh |
| `GET /api/usage?period=month` | month-to-date context | popover open and scheduled refresh |
| `GET /api/quota` | current subscription windows and freshness | popover open and scheduled refresh |
| `GET /api/version` | About/settings diagnostics | settings open |
| `GET /` | existing full dashboard | Open Dashboard |

`GET /api/quota/refresh` is intentionally not part of automatic refresh. It performs
provider network polling, can return `409` when quota tracking is disabled, and has a
60-second cooldown. If exposed later, label it **Refresh provider quotas** and require a
separate explicit action.

### Client behavior

- Join endpoint paths correctly when the configured base URL contains `/tokdash`.
- Use short connection timeouts for `/health` and longer timeouts for usage requests,
  which can trigger a cold parse.
- Fetch today, month, and quota concurrently after a successful health check.
- Decode additively: ignore unknown fields and tolerate absent optional fields.
- Treat a valid response with no usage as an empty state, not an error.
- On `503`, keep the last good in-memory data and show **Tokdash is busy — retrying**.
- On a partial failure, render successful sections and mark only the failed section.
- Use the usage `timestamp` and `response_cache.age_seconds` when available to calculate
  freshness.
- Cancel or coalesce overlapping refreshes.
- Never send a browser `Origin` header from a native client.

### Refresh cadence

- Fetch immediately when the flyout opens if data is older than 60 seconds.
- While open, refresh no more often than every 60 seconds.
- While resident and closed, check every 10 minutes, matching Tokdash's default
  600-second response cache.
- Pause periodic work during sleep and resume with one coalesced request.
- Back off after failures: 15 seconds, 30 seconds, 60 seconds, then 5 minutes.
- The ordinary Refresh control refetches the client view. Avoid forcing two distinct
  heavy usage recomputations at once.

If three requests prove too slow or inconsistent, add a read-only
`GET /api/companion-summary` endpoint later. Do not add it before measurements show a need.

## 5. macOS architecture

Suggested deployment target: macOS 13 or later. `MenuBarExtra` is available there; standard
SwiftUI controls adopt the current system appearance on newer macOS releases.

```text
TokdashCompanionApp
  MenuBarExtra(.window)
    CompanionPopover
      SummarySection
      QuotaSection
      ActivitySection
      ActionBar
  Settings scene

TokdashClient actor
  URLSession
  health()
  usage(period:)
  quota()

CompanionStore @MainActor
  connection state
  decoded snapshot
  refresh scheduler
  settings
```

Implementation notes:

- Set `LSUIElement = true` so the utility does not appear in the Dock or app switcher.
- Use a template menu-bar symbol with a clear accessibility label.
- Use `.menuBarExtraStyle(.window)` because the content is data-rich.
- Prefer standard SwiftUI popover, control, material, and typography behavior. On macOS
  26, these pick up Liquid Glass automatically.
- Do not paint a custom glass background over every control. Use explicit glass effects
  only for a specific gap that standard components cannot solve, behind availability
  checks.
- Test Reduce Transparency and Reduce Motion. The data hierarchy must remain clear when
  glass becomes opaque.
- Consider `SMAppService` for opt-in launch at login after the core slice works.

## 6. Windows architecture

Target Windows 11 only. Do not add a Windows 10 compatibility promise to MVP.

```text
App (single instance)
  NotificationIconHost
    hidden/message-only Win32 window
    Shell_NotifyIconW lifecycle
  FlyoutWindow
    WinUI 3 content
    Acrylic transient backdrop
    taskbar-edge positioning and light dismiss
  SettingsWindow

TokdashClient
  HttpClient
  health / usage / quota

CompanionViewModel
  connection state
  decoded snapshot
  refresh scheduler
  settings
```

Implementation notes:

- Use `Shell_NotifyIconW` directly through a small, isolated interop layer. The Windows
  App SDK does not currently provide a complete modern notification-area abstraction.
- Use `NOTIFYICON_VERSION_4` so mouse and keyboard activation follow current semantics.
- Build the flyout as a real WPF window positioned against the notification-area
  work area. It must handle top/side taskbars, multiple displays, DPI changes, focus,
  Escape, outside-click dismissal, and keyboard activation.
- Use DWM-composited rounded corners and native opaque WPF surfaces so ClearType
  remains sharp in the transient flyout and settings window.
- Use one stable icon. Change it only for high-level disconnected/warning state; do not
  animate rapid usage changes.
- Use an icon-only notification-area presence. Put today's short summary in the tooltip.
- Ship a signed self-contained portable ZIP for v0.1.0. Defer MSIX until an
  installed clean-machine test proves loopback access to unpackaged Tokdash and
  packaged startup behavior without developer exemptions.

The first Windows milestone should be a throwaway technical spike that proves icon
activation, accessible keyboard invocation, correct flyout placement, light dismiss,
Acrylic, and clean exit before building the final content.

## 7. Delivery phases

### Phase 0 — alignment

- Review the HTML concepts for both platforms.
- Choose the default content density and quota behavior.
- Decide separate repository versus `companion/` in this repository.
- Confirm minimum OS versions and whether secure remote URLs are MVP scope.

Exit: one approved macOS concept and one approved Windows concept.

### Phase 1 — contract and macOS vertical slice

- Freeze tolerant DTOs and realistic JSON fixtures from the documented API.
- Build the Mac menu-bar extra, connection state, today/month summary, quota section,
  Usage/Quota switching, Open Dashboard, Refresh, Settings, Quit, and low-quota
  notifications.
- Run unit tests and `xcodebuild` over SSH; Howard performs visual checks.

Exit: installable unsigned development bundle that survives offline/restart cases.

### Phase 2 — Windows tray spike and vertical slice

- Complete the Windows toolchain.
- Prove `Shell_NotifyIconW` plus the WPF flyout mechanics.
- Reuse the same fixtures and behavior contract.
- Build, extract, launch, update, and remove a portable x64 development package.

Exit: both platforms meet the same data/state acceptance suite.

### Phase 3 — hardening

- Accessibility, localization readiness, DPI/display, sleep/wake, proxy/Tailscale path,
  empty data, 503 backpressure, malformed payload, and server upgrade tests.
- Verify notification permission, threshold crossing, deduplication, reset epochs, and
  notification-click behavior.
- Measure cold start, idle CPU, memory, and request rate.
- Add opt-in launch at login.
- Add structured local diagnostic logging with no usage payloads or secrets.

### Phase 4 — release

- macOS Developer ID signing, notarization, and a signed ZIP or DMG.
- Windows Authenticode signing for the portable executable. MSIX distribution
  remains a later milestone.
- GitHub Actions builds on `macos-*` and `windows-*`.
- Checksums, release notes, install/uninstall docs, and upgrade verification.

## 8. Acceptance criteria

- Opening the surface shows cached in-memory data immediately, then refreshes without
  blocking interaction.
- With Tokdash stopped, the icon remains responsive and the flyout explains how to start
  or configure it.
- No provider credentials, logs, SQLite files, or browser cookies are read by the
  companion.
- No telemetry or third-party network requests exist.
- The resident app consumes effectively zero CPU when idle; memory is measured and kept
  within a platform-appropriate budget.
- The app never starts multiple overlapping heavy Tokdash computations.
- Both apps support keyboard open, keyboard navigation, Escape dismissal, screen-reader
  labels, high contrast, and reduced transparency.
- The macOS build passes unit tests and `xcodebuild` on the MacBook.
- The Windows build passes unit tests plus portable extraction, launch, update,
  launch-at-login, and removal checks on the Windows host.

## 9. Decisions Howard should make after the UI prototype

1. Should macOS optionally show `$today` beside the menu-bar icon? Recommendation: icon
   only in MVP; add an optional text mode later.
2. What should the initial low-quota notification threshold be? Recommendation: opt-in,
   default 20% remaining, one notification per provider/bucket/reset epoch.
3. Separate repository or this repository? Recommendation: one separate
   `tokdash-companion` repository containing both native clients.

## 10. Confirmed product choices

- Use two views inside the same transient surface: **Usage** and **Quota**.
- Keep the leading tool/model activity line in the Usage view for the prototype.
- Accept one manually configured Tailscale HTTPS URL in MVP, with localhost as default.
- Include low-quota notifications in MVP.
- Support Windows 11 only initially.
- Launch at login is explicit opt-in.
- Repository placement remains undecided.
