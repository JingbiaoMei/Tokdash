# Tokdash Companion - Manual Visual-Verification Checklist

Branch: `feat/companion-mvp`. Complete this on the exact release candidate
before creating a production/latest companion release.

Howard performs these checks on each native host. The implementation agent
has already verified: compilation, unit tests, bundle/assembly structure,
LSUIElement, and process lifetime. The checks below are the human-eye /
human-input layer that automated tests cannot cover.

## Build artifacts

### macOS (on the MacBook)

The `.xcodeproj` is tracked in the repo (deployment target macOS 14, for SwiftUI
`openSettings`), so a fresh checkout builds directly. Regenerate with
`xcodegen generate` only if `project.yml` changes.

```bash
ssh macbook
cd ~/Projects/tokdash/companion/macos
xcodebuild -project TokdashCompanion.xcodeproj -scheme TokdashCompanion -configuration Debug build
APP=$(find ~/Library/Developer/Xcode/DerivedData/TokdashCompanion-*/Build/Products/Debug/TokdashCompanion.app -maxdepth 0 | head -1)
open "$APP"
```

### Windows (on the Windows host)

```powershell
dotnet build H:\Developing\Agent\Tokdash_Project\tokdash\companion\windows\TokdashCompanion\TokdashCompanion.csproj --configuration Debug
Start-Process H:\Developing\Agent\Tokdash_Project\tokdash\companion\windows\TokdashCompanion\bin\Debug\net10.0-windows10.0.26100.0\TokdashCompanion.exe
```

---

## macOS checks (perform on the MacBook)

Start the Tokdash service first: `systemctl --user start tokdash` (or
`tokdash serve`).

### Closed state (menu bar)

- [ ] A monochrome template icon appears in the menu bar (system area, right side).
- [ ] The icon is **not** in the Dock or app switcher (LSUIElement working).
- [ ] Hovering the icon shows a tooltip: `Tokdash - Today $X.XX · Y.YM tokens` (or similar).
- [ ] The icon does not animate or flash.

### Open state (popover)

- [ ] Click the icon -> a popover opens below the menu bar, ~352pt wide.
- [ ] The popover uses a translucent material (Liquid Glass on macOS 26) - standard SwiftUI controls, not a custom painted background.
- [ ] **Header**: Tokdash wordmark + `Local · Connected` with a green dot, gear button on the right.
- [ ] **Today hero**: large `$X.XX` cost, `Y.YM tokens · N messages`, comparison line (`N% below yesterday` in green or `N% above yesterday` in red).
- [ ] **Month context**: `JULY   $XX.XX   YYYM tokens` on one line.
- [ ] **Quota section**: `SUBSCRIPTION` kicker with a `Low | All` segmented control on the right.
  - [ ] **Low** (default): at most two windows below threshold, sorted by lowest remaining. Each row: `Provider · bucket`, `N% left`, `resets HH:MM`, colored bar (red < 25%, amber < 50%, green otherwise).
  - [ ] **All**: every detected window grouped by provider (Codex / Claude / Kimi headers), capped at four rows, scrolls for more.
  - [ ] `Estimated` badge on Claude/Kimi rows where `estimated: true`.
- [ ] **Activity line**: `Most used today  Codex · gpt-5.6-sol` (or current leading tool/model). Omitted in empty state.
- [ ] **Action row**: `Open Dashboard` (primary blue) + Refresh icon button.
- [ ] **Freshness footer**: `Updated N min ago` on the left, `Quit` on the right.

### States

With Tokdash running, stop it (`systemctl --user stop tokdash`), then:

- [ ] **Offline**: header shows `Offline` with a red dot. Banner: `Tokdash is not reachable / Start Tokdash, or check the server address in Settings.` with `Retry` and `Settings` buttons. Last-good data is dimmed (45% opacity) but still visible. Footer appends `· stale`.
- [ ] Click `Retry` -> attempts reconnect.
- [ ] Click `Settings` -> opens Settings window.

Restart Tokdash, then trigger a busy state if possible (heavy usage recompute):

- [ ] **Busy (503)**: header shows `Busy` with an amber dot. Banner: `Tokdash is busy - retrying / Last data shown below. Backing off automatically.` Last-good data dimmed. Backoff auto-retries.
- [ ] **Sleep / wake**: sleep the Mac and wake it -> the menu-bar tooltip / open popover refresh within a few seconds via one coalesced request (no burst); freshness no longer reads `· stale` once it lands.

### Settings

- [ ] Click the gear icon -> Settings window opens.
- [ ] **Server** section: Base URL field, default `http://127.0.0.1:55423`. Enter a Tailscale HTTPS URL -> app reconnects to it.
- [ ] **Startup**: `Launch at Login` toggle (off by default).
- [ ] **Notifications**: `Low-quota notifications` toggle (off by default).
- [ ] **Quota Alert Thresholds**: sliders for 5-hour (20%), weekly (10%), default (15%). Changing a slider persists the value.

### Interaction / accessibility

- [ ] Click outside the popover -> it dismisses.
- [ ] Press `Escape` -> popover dismisses.
- [ ] `Cmd+Q` or click `Quit` -> app exits cleanly (icon disappears from menu bar).
- [ ] Tab key moves focus through controls in a logical order.
- [ ] VoiceOver (Cmd+F5) reads the Today cost, quota rows, and buttons with useful labels.
- [ ] **Reduce Transparency** (System Settings -> Accessibility -> Display): popover becomes opaque, hierarchy still clear.
- [ ] **Reduce Motion**: no animation on refresh; data updates in place.
- [ ] **Increase Contrast**: borders and text remain readable.
- [ ] **Dark mode**: popover adapts (dark translucent background, light text).
- [ ] **Light mode**: popover adapts (light translucent background, dark text).

### Multi-display / DPI

- [ ] Move the menu bar to a secondary display -> popover still anchors correctly.
- [ ] On a Retina display, icon and text are crisp.

---

## Windows checks (perform on the Windows host)

Start the Tokdash service first.

### Closed state (notification area)

- [ ] A Tokdash icon appears in the notification area (may be in the overflow flyout).
- [ ] The icon has a unique outline; transparent background; readable on both light and dark taskbars.
- [ ] Hover shows tooltip: `Tokdash - Today $X.XX · Y.YM tokens`.
- [ ] The icon does not animate.

### Open state (flyout)

- [ ] Single left-click the tray icon -> flyout opens anchored to the notification-area corner.
- [ ] Flyout uses **Acrylic** material (Win32 `SetWindowCompositionAttribute`, `ACCENT_ENABLE_ACRYLICBLURBEHIND`) with a translucent solid fallback in battery saver / high contrast.
- [ ] Flyout is a WPF window: `AllowsTransparency`, `WindowStyle=None`, `Topmost`, 8px rounded corners.
- [ ] Flyout positions correctly when the taskbar is on the **bottom**, **top**, **left**, or **right**.
- [ ] Flyout positions correctly on **multi-monitor** setups (opens on the screen with the tray, not always primary).
- [ ] **DPI change**: move between displays with different DPI/scale -> flyout repositions and resizes correctly.
- [ ] Same content layout as macOS (header, today hero, month, quota with Low/All, activity, actions, freshness).

### Context menu

- [ ] Right-click the tray icon -> native context menu: `Open Tokdash`, `Refresh`, `Settings`, `Exit`.
- [ ] Click `Exit` -> app exits cleanly (icon disappears).
- [ ] Click outside the context menu -> it dismisses.
- [ ] Press `Escape` -> menu dismisses.

### Keyboard

- [ ] The tray icon is keyboard-invocable (Win+something or focus traversal reaches it).
- [ ] `Escape` closes the flyout.
- [ ] Tab moves through flyout controls logically.
- [ ] Narrator (or another screen reader) reads the content with useful labels.

### Appearance / accessibility

- [ ] **Dark mode**: flyout adapts.
- [ ] **Light mode**: flyout adapts.
- [ ] **High Contrast** mode: Acrylic falls back to solid; text readable.
- [ ] **Battery saver**: Acrylic falls back to solid.

---

## Cross-platform behavior parity

Both apps must behave identically for data and states, differing only in native chrome:

- [ ] **Wrong service**: point the companion at a non-Tokdash port -> `This address is not a Tokdash service`. No usage/quota calls made.
- [ ] **Empty**: stop all coding tools for a day, or point at a fresh Tokdash -> `No usage recorded today` hero, month context still shows, activity line omitted.
- [ ] **Quota disabled**: disable quota tracking in Tokdash -> `Subscription tracking is off` with an `Open Dashboard` path. No provider rows.
- [ ] **Tailscale URL**: set base URL to `https://wsl.tail76535.ts.net/tokdash` -> connects over HTTPS, all endpoints work.
- [ ] **Refresh cadence**: open the flyout, wait 61s -> auto-refresh. Close it, wait 10min -> background refresh. Observe no more than one refresh per 60s while open.
- [ ] **Low-quota notification** (opt-in): enable in Settings, let a window cross its threshold -> one notification per (provider, bucket, reset epoch); no repeat until the reset epoch changes; a missing-reset window, or one that is last-known (see the next item), is suppressed. Clicking the notification opens the companion to the Low quota section: on macOS a floating Low-quota window appears (the MenuBarExtra popover has no public open API); on Windows the flyout opens on Low, or switches to Low if already open on All.
- [ ] **Partially-failed provider**: point at `contract/fixtures/quota-partial-failure.json` (MiniMax global + CN with a stale CN token, so the provider reports `status: "ok"` + `status_detail: "stale_token"`) -> the All view shows the `⚠ Couldn't refresh - showing last known` warning under the MiniMax header, but `global_general_5h` (its `captured_at` equals the provider's `status_at`) renders with **no** row `⚠` and still fires its low-quota notification on a crossing. Only `cn_general_5h`, whose `captured_at` is older, carries the row `⚠` and stays silent. Low view shows both at 12% and 9%, `⚠` on the 9% row only. A group warning must never silence a healthy sibling; note `buckets[].status` is always `"ok"` and cannot be used for this.
- [ ] **Freshness**: `Updated N min ago` uses the API `timestamp` (a naive UTC datetime like `2026-07-28T17:57:43.500951` parses correctly) or falls back to `response_cache.age_seconds` (a float). `· stale` appears only when last-good data is older than the refresh window (60s while open, 600s while closed) AND the last fetch failed - not the instant the app goes offline/busy.
- [ ] **Windows startup migration**: a previously-saved blank or malformed base URL (from an earlier build) must not crash startup; it is reset to the default and the app launches to Connecting. Entering a blank / relative / non-http URL in Settings is rejected there too, so the bad value can't come back on the next launch.
- [ ] **Windows first data arrives**: launch the app with Tokdash running and open the flyout -> real numbers appear within a few seconds, not `No data yet`. There is no startup `RefreshAsync`; the 2s `DispatcherTimer` in `StartScheduler` is the only thing driving the first fetch, and `Program.Main` runs a hand-rolled `GetMessage` loop. If that loop doesn't pump the dispatcher, the timer never ticks and the app stays permanently empty with nothing to catch it.
- [ ] **Provider failure**: point at a Tokdash whose `codex` provider reports `status: "error"` (see `contract/fixtures/quota-provider-error.json`) -> All view shows an inline `⚠ Couldn't refresh - showing last known` warning under the Codex header, Codex's last-known rows still visible, Claude/Kimi render normally; Low view prefixes `⚠` on the Codex low row, and that row must not fire a notification. No full-surface quota failure.
- [ ] **Sleep / wake**: sleep the host, wake it -> stale data refreshes within a few seconds via one coalesced request (no burst of refreshes); the open flyout/popover updates.

---

## Sign-off

- [ ] macOS: all checks above pass on the MacBook.
- [ ] Windows: all checks above pass on the Windows host.
- [ ] No crashes in either app's logs during a 5-minute idle.
- [ ] No provider credentials, logs, or SQLite files read by the companion (verify with Process Monitor / `fs_usage`).
- [ ] No telemetry or third-party network requests (verify with a network monitor; only the configured Tokdash base URL should be contacted).

When all boxes are checked, the companion MVP has completed its physical
release gate. Packaging and workflow checks are separate automated gates in
`companion/docs/RELEASE.md`.
