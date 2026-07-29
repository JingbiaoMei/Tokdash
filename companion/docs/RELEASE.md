# Tokdash Companion release guide

Tokdash Companion has its own version and tags. `companion/VERSION` is the
authority; release tags use `companion-vX.Y.Z`. Python/PyPI releases continue
to use `vX.Y.Z`.

The first supported companion version is `0.1.0`. It targets Tokdash `1.5.2`
or newer, Windows 11 x64, and macOS 14 or newer.

## Artifacts

| Platform | Artifact | Architecture | Intended use |
|---|---|---|---|
| macOS | `TokdashCompanion-X.Y.Z-macos-universal-*.dmg` | arm64 + x86_64 | Primary macOS install |
| Windows | `TokdashCompanion-X.Y.Z-windows-x64-*.msix` | x64 | Primary Windows install after signing |
| Windows | `TokdashCompanion-X.Y.Z-windows-x64-portable-*.zip` | x64 | Portable/fallback install |

Windows x86 is not supported. Windows on Arm can run the x64 portable build
through Windows 11 emulation, but native ARM64 is not a `0.1.0` release target.

## Local release builds

Run from the repository root.

Windows:

```powershell
powershell -NoProfile -File companion/scripts/build_windows_release.ps1
```

macOS:

```bash
companion/scripts/build_macos_release.sh
```

Both scripts read `companion/VERSION`, produce SHA-256 checksums, and label
unsigned output explicitly. Windows produces both the portable ZIP and MSIX.
macOS verifies that the app binary contains both `arm64` and `x86_64`.

## Install, update, and uninstall

### macOS DMG

1. Open the DMG and drag TokdashCompanion to Applications.
2. Start Tokdash first, then open TokdashCompanion.
3. For an update, quit the companion and replace the existing application.
4. To uninstall, turn off **Launch at Login**, quit, and remove the app.
5. Optional settings cleanup: remove
   `Library/Application Support/TokdashCompanion` from your user folder in
   Finder.

### Windows MSIX

1. Install the signed MSIX and start TokdashCompanion from Start.
2. A later package with the same identity and higher version updates in place.
3. Uninstall from **Settings > Apps > Installed apps**.
4. Optional settings cleanup:
   remove `%LOCALAPPDATA%\TokdashCompanion`.

The MSIX manifest declares a disabled-by-default startup task. The Settings
toggle uses `Windows.ApplicationModel.StartupTask` for an installed MSIX.

### Windows portable ZIP

1. Extract the complete ZIP to a stable directory.
2. Run `TokdashCompanion.exe`; do not move the executable away from `Assets`.
3. For an update, turn off **Launch at Login**, quit, replace the extracted
   directory, restart, and re-enable the setting.
4. To uninstall, turn off **Launch at Login**, quit, and delete the directory.
5. Optional settings cleanup:
   remove `%LOCALAPPDATA%\TokdashCompanion`.

Portable launch-at-login uses
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.

## Signing policy

A clean production release remains blocked on platform signing:

- macOS: Developer ID Application signing, Hardened Runtime, notarization, and
  ticket stapling.
- Windows: Authenticode signing for the executable and trusted signing for the
  MSIX. The package Publisher must match the signing certificate subject.

An unsigned build may be published only as a GitHub **prerelease** with
`unsigned` in every artifact name:

- macOS Gatekeeper will not treat an unsigned/unnotarized DMG as a normal
  trusted download.
- Windows SmartScreen may warn for the portable executable.
- An unsigned MSIX is packaging evidence, not a normal end-user installer.

The repository does not tell users to disable operating-system security
features. Production/latest releases must be signed.

## GitHub Actions

- `companion-ci.yml` tests both native apps and exercises both packagers.
- `companion-release.yml` responds only to `companion-v*` tags and creates a
  GitHub prerelease from explicitly unsigned assets.
- CI does not upload temporary build artifacts.
- Any workflow artifact upload uses `retention-days: 1`.
- Final binaries are attached directly to the GitHub Release rather than
  stored as temporary Actions artifacts.

Configure a protected `companion-release` GitHub Environment before tagging.
Require reviewer approval and restrict it to protected companion release tags.
The signing workflow will be finalized after the signing-provider decision.

## Release gate

Before merging:

- Run the full Python suite and both native test suites.
- Run `python3 companion/scripts/check_release.py`.
- Build the Windows ZIP/MSIX and universal macOS DMG.
- Keep the English implementation docs consistent with the WPF implementation.

Before tagging:

- Complete `VISUAL_VERIFICATION_CHECKLIST.md` on physical Windows and macOS
  systems, including accessibility, multiple displays/DPI, sleep/wake,
  launch-at-login, and privacy/network checks.
- Confirm the working trees used for native validation are clean and at the
  release commit.
- Decide and configure signing. Otherwise publish only an explicit prerelease.
- Verify the tag is `companion-v$(cat companion/VERSION)` at current `HEAD`.
- Verify checksums and install/update/uninstall on clean user accounts.
