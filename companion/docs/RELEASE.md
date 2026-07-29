# Tokdash Companion release guide

Tokdash Companion is versioned independently from the Python package.
`companion/VERSION` is the authority and release tags use
`companion-vX.Y.Z`. The first release is `0.1.0` and requires Tokdash `1.5.2`
or newer.

## v0.1.0 assets

Publish one GitHub **prerelease** with exactly these assets:

```text
Tokdash-Companion-0.1.0-macos-universal.dmg
Tokdash-Companion-0.1.0-windows-x64.zip
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

## Non-negotiable signing policy

Unsigned native binaries must not be published, even as a prerelease.

- macOS requires Developer ID Application signing, Hardened Runtime,
  notarization, ticket stapling, and Gatekeeper verification.
- Windows requires an Authenticode signature and RFC 3161 timestamp on
  `TokdashCompanion.exe` before the executable is compressed.
- GitHub attestations and SHA-256 checksums supplement platform signatures;
  they do not replace them.

The active `companion-release.yml` is therefore a read-only guard that rejects
release tags while signing is unconfigured. Replace the guard only after the
Windows signing provider, Apple credentials, and protected environments have
been reviewed.

## Unsigned CI builds

Unsigned builds are permitted only inside local development or secret-free CI.
They are never uploaded:

```powershell
powershell -NoProfile -File companion/scripts/build_windows_release.ps1
```

```bash
bash companion/scripts/build_macos_release.sh
```

The scripts build in Release mode, keep WPF trimming disabled, use the
committed build number, and put `unsigned` in diagnostic filenames. Companion
CI uploads no Actions artifacts. Repository artifact and log retention is set
to one day for any other workflow that needs temporary storage.

## Install, update, and remove

### macOS

1. Open the notarized DMG and drag TokdashCompanion to Applications.
2. Start Tokdash, then open TokdashCompanion.
3. To update, quit the companion and replace the application.
4. To remove it, disable **Launch at Login**, quit, and delete the application.
5. Optional settings cleanup: delete
   `~/Library/Application Support/TokdashCompanion`.

### Windows portable ZIP

1. Extract the complete ZIP to a stable directory.
2. Verify the executable signature, then run `TokdashCompanion.exe`.
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

- Choose and configure one Windows signing provider.
- Configure Apple Developer ID and App Store Connect notarization credentials.
- Protect separate Windows, macOS, and publish environments and restrict them
  to `companion-v*`.
- Replace the release guard with a reviewed build → sign → verify → package →
  checksum → attest → prerelease workflow.
- Complete `VISUAL_VERIFICATION_CHECKLIST.md` on clean standard-user Windows
  and macOS systems.
- Verify the downloaded ZIP and DMG, not runner staging files.
- Confirm the companion makes network requests only to the explicitly
  configured Tokdash endpoint and performs no telemetry, credential
  discovery, or port scanning.
- Tag the current merged `main` commit only. Never replace assets under an
  existing tag; release fixes under a new companion version.
