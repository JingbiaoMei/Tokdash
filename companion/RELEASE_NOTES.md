# Tokdash Companion 0.1.3

This preview fixes the Antigravity quota card so it shows the correct
window label ("5-hour" vs "Weekly") instead of always assuming 5-hour.

## Changes

- Antigravity's quota window is now auto-determined from its reset time
  across the macOS and Windows flyouts. A weekly limit (e.g. resetting in
  3 days) now reads "Weekly" instead of the stale "5-hour" label, matching
  the web dashboard.
- Antigravity pool labels shortened to "Gemini" / "Claude/GPT" so the
  window fits the narrow flyout alongside the pool name (e.g.
  "Gemini · Weekly").
- No other functional changes; quota data comes from the connected Tokdash
  server unchanged.

## Important: unsigned preview

These binaries are **not code signed**. GitHub hosting and `SHA256SUMS` verify
the downloaded files against this release, but they do not establish an
operating-system-trusted publisher.

- macOS Gatekeeper will warn that Apple cannot verify the developer. If you
  trust this repository and the checksum, Control-click the app, choose
  **Open**, and confirm **Open**.
- Windows SmartScreen may show an unknown-publisher warning. If you trust this
  repository and the checksum, choose **More info**, then **Run anyway**.
- Do not download these binaries from mirrors or third-party sites.

## Assets

- `Tokdash-Companion-0.1.3-macos-universal-unsigned.dmg`
  supports Apple Silicon and Intel Macs on macOS 14 or newer.
- `Tokdash-Companion-0.1.3-windows-x64-unsigned.zip`
  is a self-contained Windows 11 x64 portable build. Windows 11 on Arm may run
  it through x64 emulation.
- `SHA256SUMS` covers both downloadable binaries.

The companion has no telemetry, credential discovery, or port scanning. It
connects only to the Tokdash endpoint configured by the user.
