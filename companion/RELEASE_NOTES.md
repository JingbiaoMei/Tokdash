# Tokdash Companion 0.1.1

This companion preview adds Simplified Chinese and improves subscription quota
labels and reset-time presentation on macOS and Windows.

## Changes

- Added System, English, and Simplified Chinese language choices. Changes apply
  immediately and preserve existing settings when upgrading from 0.1.0.
- Normalized Claude's general Session and Weekly All windows to 5-hour and
  Weekly labels and thresholds.
- Preserved model-specific Claude weekly labels such as Fable and Opus while
  applying the weekly alert threshold.
- Changed reset times to relative text: minutes below two hours, then hours.
- Localized companion settings, connection states, notifications, quota
  details, and usage summaries.

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

- `Tokdash-Companion-0.1.1-macos-universal-unsigned.dmg`
  supports Apple Silicon and Intel Macs on macOS 14 or newer.
- `Tokdash-Companion-0.1.1-windows-x64-unsigned.zip`
  is a self-contained Windows 11 x64 portable build. Windows 11 on Arm may run
  it through x64 emulation.
- `SHA256SUMS` covers both downloadable binaries.

The companion has no telemetry, credential discovery, or port scanning. It
connects only to the Tokdash endpoint configured by the user.
