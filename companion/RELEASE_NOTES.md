# Tokdash Companion 0.2.0

This preview adds multi-server monitoring on macOS and Windows while preserving
the direct single-server path for existing installations.

## Changes

- Added schema-v2 server settings with automatic migration from the previous
  single Base URL preference.
- Added parallel refresh across enabled servers, per-server health tracking,
  partial-failure handling, cancellation, and escalating retry backoff.
- Combined reachable-server usage into one summary and grouped quota rows as
  `server · provider`, including canonical provider handling and Low-view
  subscription deduplication.
- Added native settings controls for adding, testing, enabling, naming, and
  removing servers. Connection tests use the lightweight `/health` endpoint.
- Redesigned the macOS server editor as aligned per-server cards with visible
  toggle tint, inline status feedback, and long-URL handling.
- Embedded the Tokdash tray icon in the Windows executable so installed and
  copied builds do not fall back to the generic application icon.
- Retained the Tokdash macOS menu-bar mark and the corrected **Open dashboard**
  and **Quit** action colors from the latest companion build.

Update checks remain optional. They never download or install software;
**View update** opens the validated Tokdash GitHub release page in the default
browser.

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

- `Tokdash-Companion-0.2.0-macos-universal-unsigned.dmg` supports Apple Silicon
  and Intel Macs on macOS 14 or newer.
- `Tokdash-Companion-0.2.0-windows-x64-unsigned.zip` is a self-contained Windows
  11 x64 portable build. Windows 11 on Arm may run it through x64 emulation.
- `SHA256SUMS` covers both downloadable binaries.

To update on macOS, quit Tokdash Companion, open the DMG, drag the app to
Applications, and choose **Replace**. Existing settings migrate automatically.

The companion has no telemetry, credential discovery, or port scanning. It
connects only to Tokdash endpoints configured by the user and, for manual or
opted-in update checks, GitHub's public releases API.
