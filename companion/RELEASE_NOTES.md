# Tokdash Companion 0.1.7

This preview polishes the macOS companion interface and replaces its generic
menu-bar symbol with the Tokdash mark.

## Changes

- Added a monochrome Tokdash menu-bar icon based on the approved companion
  mockup. It adapts automatically to light, dark, and selected menu-bar states.
- Fixed the menu-bar icon's intrinsic size and optical alignment so the vector
  artwork remains proportionate and centered across display scale factors.
- Reused the same Tokdash mark in the popover header while retaining the
  full-color application icon in Finder and Applications.
- Made the freshness timestamp and **Quit** action slightly darker so they are
  easier to find without competing with primary content.
- Changed the macOS Settings content surface from grey to white in light mode,
  with the corresponding system-managed dark appearance in dark mode.

The update-checking feature introduced in 0.1.5 remains available on both
macOS and Windows. Update checks never download or install software; **View
update** opens the validated Tokdash GitHub release page in the default browser.

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

- `Tokdash-Companion-0.1.7-macos-universal-unsigned.dmg`
  supports Apple Silicon and Intel Macs on macOS 14 or newer.
- `Tokdash-Companion-0.1.7-windows-x64-unsigned.zip`
  is a self-contained Windows 11 x64 portable build. Windows 11 on Arm may run
  it through x64 emulation.
- `SHA256SUMS` covers both downloadable binaries.

To update on macOS, quit Tokdash Companion, open the DMG, drag the app to
Applications, and choose **Replace**. Existing settings remain in Application
Support.

The companion has no telemetry, credential discovery, or port scanning. It
connects to the Tokdash endpoint configured by the user and, only for manual or
opted-in update checks, GitHub's public releases API.
