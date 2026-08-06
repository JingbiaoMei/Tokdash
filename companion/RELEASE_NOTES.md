# Tokdash Companion 0.1.5

This preview adds update checks to the macOS and Windows companion apps.

## Changes

- Added a manual **Check now** action in Settings on both platforms.
- Added opt-in automatic checks, limited to one attempt every 24 hours.
- Added a red dot to the Settings gear when a newer companion release is
  available. The badge persists across restarts until the app is updated or
  that version is skipped.
- Added **View update** and **Skip this version** actions.
- Companion releases are selected from the public GitHub releases API using
  strict `companion-vX.Y.Z` tags and numeric version comparison. Python
  releases, drafts, and malformed tags are ignored.
- Added English and Simplified Chinese update-checking text.
- Added test-only settings isolation so native test runs cannot read or
  overwrite the user's real companion settings.

Update checks never download or install software. **View update** opens the
validated Tokdash GitHub release page in the default browser.

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

- `Tokdash-Companion-0.1.5-macos-universal-unsigned.dmg`
  supports Apple Silicon and Intel Macs on macOS 14 or newer.
- `Tokdash-Companion-0.1.5-windows-x64-unsigned.zip`
  is a self-contained Windows 11 x64 portable build. Windows 11 on Arm may run
  it through x64 emulation.
- `SHA256SUMS` covers both downloadable binaries.

The companion has no telemetry, credential discovery, or port scanning. It
connects to the Tokdash endpoint configured by the user and, only for manual or
opted-in update checks, GitHub's public releases API.
