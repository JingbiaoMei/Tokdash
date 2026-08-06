# Tokdash Companion 0.1.4

This preview makes multi-day quota reset countdowns easier to read in the
macOS and Windows companion flyouts.

## Changes

- Multi-day resets now use days instead of large hour counts. For example,
  a weekly window with 3 days and 22 hours remaining reads "resets in 3 days"
  instead of "resets in 94 hours".
- The macOS and Windows flyouts now match the dashboard's minute, hour, and
  day boundaries.
- Added English and Simplified Chinese day-count translations and boundary
  tests on both platforms.

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

- `Tokdash-Companion-0.1.4-macos-universal-unsigned.dmg`
  supports Apple Silicon and Intel Macs on macOS 14 or newer.
- `Tokdash-Companion-0.1.4-windows-x64-unsigned.zip`
  is a self-contained Windows 11 x64 portable build. Windows 11 on Arm may run
  it through x64 emulation.
- `SHA256SUMS` covers both downloadable binaries.

The companion has no telemetry, credential discovery, or port scanning. It
connects only to the Tokdash endpoint configured by the user.
