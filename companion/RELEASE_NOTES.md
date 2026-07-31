# Tokdash Companion 0.1.2

This preview refreshes the macOS app icon with a crisper rendering of the
Tokdash logo. No functional changes.

## Changes

- Re-rendered the macOS app icon (Dock, Finder, About panel, DMG) from
  vector artwork at every size, replacing the softer 0.1.1 raster.
- The source SVG now lives at `companion/assets/tokdash_logo_icon.svg` so the
  icon set is reproducible.
- No functional changes; the Windows build matches 0.1.1 apart from the
  version number.

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

- `Tokdash-Companion-0.1.2-macos-universal-unsigned.dmg`
  supports Apple Silicon and Intel Macs on macOS 14 or newer.
- `Tokdash-Companion-0.1.2-windows-x64-unsigned.zip`
  is a self-contained Windows 11 x64 portable build. Windows 11 on Arm may run
  it through x64 emulation.
- `SHA256SUMS` covers both downloadable binaries.

The companion has no telemetry, credential discovery, or port scanning. It
connects only to the Tokdash endpoint configured by the user.
