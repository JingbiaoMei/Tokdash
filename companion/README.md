# Tokdash Companion

Native, lightweight menu-bar / notification-area clients for the Tokdash
service. macOS uses SwiftUI `MenuBarExtra`; Windows uses C#/WPF with
Win32 `Shell_NotifyIconW` interop. Both read the existing Tokdash HTTP API -
they do not parse logs, calculate prices, scan credentials, or replace the
web dashboard.

## Layout

```
companion/
  docs/                 frozen planning docs + IMPLEMENTATION_SPEC (authority)
    UI_CONCEPT.html     approved visual prototype (visual authority)
    IMPLEMENTATION_SPEC.md   reconciled implementation spec
    COMPANION_APP_PLAN.md    historical product plan
    UI_CONTENT_SPEC.md       historical content spec
    RESEARCH.md              platform research
  contract/             shared API contract + fixtures + expected behavior
    COMPANION_API.md
    fixtures/           realistic JSON responses from the Tokdash API
    expected/           observable UI outcomes per state
  assets/               logo assets (PNG)
  macos/                SwiftUI app + tests (built on the MacBook via SSH)
  windows/              C#/WPF app + tests (built on Windows via PowerShell)
  scripts/              version checks and native release packaging
  VERSION               companion release version (independent of Python)
```

## Authority order

When documents disagree, the order is:

1. `companion/docs/IMPLEMENTATION_SPEC.md` - reconciled implementation spec.
2. `companion/docs/UI_CONCEPT.html` - approved visual prototype.
3. `companion/contract/COMPANION_API.md` - API contract.
4. `companion/docs/COMPANION_APP_PLAN.md` and `UI_CONTENT_SPEC.md` - history.

The HTML prototype is the visual authority. The planning Markdown predates the
final HTML and describes separate Usage/Quota views; the HTML replaced those
with one combined spend-first surface and an inline Low/All quota selector.
`IMPLEMENTATION_SPEC.md` records the reconciliation.

## Build authority

| Platform | Authority | How |
|---|---|---|
| macOS | MacBook | `ssh macbook`, then `xcodebuild` against Xcode 26.6 / Swift 6.3.3 |
| Windows | Windows host | `powershell.exe -NoProfile -Command "dotnet build ..."` |

WSL is the source-of-truth checkout. Git sync carries edits to each native
host. See `docs/IMPLEMENTATION_SPEC.md` §10.

## Out of scope

Electron, Tauri, webview, telemetry, credential scanning, third-party UI
frameworks. The companion is a read-only client.

## Status

Release preparation on branch `feat/companion-mvp`. Native builds and automated
tests pass; packaging and CI are tracked in the repository. Signing and the
manual visual/accessibility/privacy sign-off remain release gates.

Release details, artifact names, install/update/uninstall steps, and the
unsigned-prerelease policy are in [`docs/RELEASE.md`](docs/RELEASE.md).
