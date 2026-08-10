# Tokdash Companion release guide

Tokdash Companion is versioned independently from the Python package.
`companion/VERSION` is the authority and release tags use
`companion-vX.Y.Z`. The current release is `0.2.0` and requires Tokdash `1.5.2`
or newer.

## v0.2.0 assets

Publish one GitHub **prerelease** with exactly these assets:

```text
Tokdash-Companion-0.2.0-macos-universal-unsigned.dmg
Tokdash-Companion-0.2.0-windows-x64-unsigned.zip
SHA256SUMS
```

- The DMG contains one native `arm64 + x86_64` application.
- The ZIP contains one self-contained, single-file Windows x64 executable,
  the icon, Tokdash license, .NET third-party notices, and this guide.
- Windows x86 is unsupported. Windows 11 on Arm may run the x64 build through
  emulation; native ARM64 is a later target.
- MSIX is deferred until clean-machine tests prove loopback access to an
  unpackaged Tokdash service and packaged startup behavior without developer
  exemptions.

## v0.2.0 unsigned-preview policy

The maintainer explicitly accepted unsigned distribution for this GitHub
prerelease. Every user-facing surface must say that the binaries are unsigned:

- Both binary filenames end in `-unsigned`.
- The release title includes `unsigned preview`.
- Release notes explain the Gatekeeper and SmartScreen warnings.
- `SHA256SUMS` covers the final GitHub Release assets.
- The GitHub Release remains marked as a prerelease.

Checksums detect file changes but do not establish a trusted publisher.
Developer ID/notarization and Windows Authenticode remain goals for a later
release. Do not silently replace the unsigned assets with signed binaries under
the same tag; publish a new companion version.

## Build and publication flow

The same builders are available for local validation:

```powershell
powershell -NoProfile -File companion/scripts/build_windows_release.ps1
```

```bash
bash companion/scripts/build_macos_release.sh
```

The scripts build in Release mode, keep WPF trimming disabled, use the
committed build number, and put `unsigned` in the filenames. The tag workflow:

1. verifies `companion-vX.Y.Z` against `companion/VERSION` and checks that the
   tagged commit is on `main`;
2. creates a draft GitHub prerelease;
3. builds and tests Windows x64 and the macOS universal application;
4. uploads both binaries directly to the draft release;
5. downloads those final assets, generates `SHA256SUMS`, and publishes.

No Actions artifacts are uploaded. Repository artifact and log retention is
set to one day for any other workflow that needs temporary storage.

## Install, update, and remove

### macOS

1. Verify the DMG against `SHA256SUMS`.
2. Open the DMG and drag TokdashCompanion to Applications.
3. Because this preview is unsigned, Control-click the app, choose **Open**,
   then confirm **Open** if you trust this repository and checksum.
4. Start Tokdash, then open TokdashCompanion.
5. To update, quit the companion and replace the application.
6. To remove it, disable **Launch at Login**, quit, and delete the application.
7. Optional settings cleanup: delete
   `~/Library/Application Support/TokdashCompanion`.

### Windows portable ZIP

1. Verify the ZIP against `SHA256SUMS`, then extract it to a stable directory.
2. Run `TokdashCompanion.exe`. SmartScreen may report an unknown publisher;
   choose **More info** and **Run anyway** only if you trust this repository and
   checksum.
3. Launch at login is opt-in. It uses only the named
   `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` value, quotes the
   absolute executable path, and passes `--startup`.
4. Moving the extracted directory invalidates and removes the stale startup
   entry. Re-enable the setting from the new location.
5. To update, disable launch at login, quit, replace the extracted directory,
   restart, and re-enable it.
6. To remove it, disable launch at login, quit, and delete the directory.
7. Optional settings cleanup: delete `%LOCALAPPDATA%\TokdashCompanion`.

Because the Windows build is self-contained, relevant .NET servicing updates
require a new companion build.

## Release controls

Before merging:

- Preserve the companion commits with a merge commit; do not squash or rebase.
- Merge current `main` into the branch first.
- Pass the complete Python suite and Release-mode Windows/macOS suites.
- Treat Swift and Clang warnings as errors.
- Pass `python3 companion/scripts/check_release.py`.
- Keep every external Action pinned to a reviewed 40-character commit SHA.

Before tagging:

- Record the maintainer's explicit acceptance of unsigned preview distribution.
- Protect the publish environment and restrict it to `companion-v*`.
- Confirm both filenames and the release title clearly say `unsigned`.
- Complete `VISUAL_VERIFICATION_CHECKLIST.md` on clean standard-user Windows
  and macOS systems.
- Verify the downloaded ZIP and DMG, not runner staging files.
- Confirm the companion makes network requests only to the explicitly
  configured Tokdash endpoint and, when the user requests or enables update
  checks, GitHub's public releases API. It performs no telemetry, credential
  discovery, or port scanning.
- Tag the current merged `main` commit only. Never replace assets under an
  existing tag; release fixes under a new companion version.
