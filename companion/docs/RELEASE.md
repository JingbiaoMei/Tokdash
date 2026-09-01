# Tokdash Companion release guide

Tokdash Companion is versioned independently from the Python package.
`companion/VERSION` is the authority and release tags use
`companion-vX.Y.Z`. The current release is `1.0.1` and requires Tokdash `1.5.2`
or newer.

## v1.0.1 assets

Publish one GitHub **release** with exactly these assets:

```text
Tokdash-Companion-1.0.1-macos-universal-unsigned.dmg
Tokdash-Companion-1.0.1-windows-x64-unsigned.zip
SHA256SUMS
```

- The DMG contains one native `arm64 + x86_64` application.
- The ZIP contains one self-contained, single-file Windows x64 executable,
  the icon, Tokdash license, .NET third-party notices, and this guide.
- Windows x86 is unsupported. Windows 11 on Arm may run the x64 build through
  emulation; native ARM64 is a later target.
- MSIX is built by `companion/scripts/build_windows_msix.ps1` and published
  through the Microsoft Store, not as a GitHub release asset. The Store signs
  that package during certification, so this unsigned-binary policy covers only
  the GitHub ZIP and DMG.

## Microsoft Store package

The release workflow builds the Store MSIX on every `companion-v*` tag, in the
same Windows job as the ZIP, and attaches it to the run as the
`tokdash-companion-<version>-store-msix` artifact. Download it from the workflow
run and upload it to Partner Center; nothing is built by hand.

It is an artifact rather than a release asset on purpose. Partner Center signs
the package it ingests, so what CI produces is unsigned, and Windows refuses to
install an unsigned MSIX outright — there is no click-through as there is for
SmartScreen. Publishing it next to the ZIP would only generate failed installs.

The artifact retains for one day, matching the repository-wide rule. If that
window is missed, re-run the `windows` job: the build is driven entirely by the
tag, so it reproduces the same package.

### Publishing a Store update

1. Land the change on `main` and bump `companion/VERSION`.
2. Run `python3 companion/scripts/check_release.py --tag companion-vX.Y.Z`.
3. Tag that commit `companion-vX.Y.Z` and push. One tag builds all three packages.
4. Download the `tokdash-companion-<version>-store-msix` artifact from the run.
5. Partner Center > the product > Packages: upload the `.msix`. Identity is matched
   here, so a mismatch is rejected at upload rather than in certification.
6. Update Store listings only if the copy changed. The current en-US and zh-Hans
   field text is kept out of the public repo, under
   `docs/local/companion/listing-paste/`.
7. Submit. Certification re-runs the Windows App Certification Kit tests on
   Microsoft's side; a local WACK pass is a pre-check, not a substitute.
8. Publication follows certification automatically.

`runFullTrust` approval is granted per product and carries across submissions. That
form reappears only if a package declares a restricted capability it did not have
before, so adding one means re-justifying it.

### Identity

The build needs the Partner Center identity triple in repository variables
(Settings > Secrets and variables > Actions > Variables):

| Variable | Value |
|---|---|
| `MSIX_IDENTITY_NAME` | `Package/Identity/Name` from Partner Center |
| `MSIX_PUBLISHER` | `Package/Identity/Publisher`, starting `CN=` |
| `MSIX_PUBLISHER_DISPLAY_NAME` | `Package/Properties/PublisherDisplayName` |

These are not secrets — they are readable in any installed package — but they
must match Partner Center exactly, since a mismatch is rejected at upload rather
than in certification. A dedicated step fails the build when any is unset,
because the build script would otherwise fall back to its test identity and
produce a package Partner Center rejects.

## v1.0.1 unsigned-binary policy

The maintainer explicitly accepted unsigned distribution for this GitHub
release. Every user-facing surface must say that the binaries are unsigned:

- Both binary filenames end in `-unsigned`.
- The release title includes `unsigned build`.
- Release notes explain the Gatekeeper and SmartScreen warnings.
- `SHA256SUMS` covers the final GitHub Release assets.
- The GitHub Release is published with `--latest=false`. It is an ordinary
  release, not a prerelease, but it must never take the repository's "Latest"
  pointer: this repo also publishes the Python package, and a companion tag
  newer than the newest `vX.Y.Z` would otherwise become what `/releases/latest`
  resolves to for everyone installing Tokdash itself.

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
2. creates a draft GitHub release;
3. builds and tests Windows x64 and the macOS universal application;
4. uploads both binaries directly to the draft release;
5. builds the Store MSIX and attaches it to the run as an artifact, never to the
   release;
6. downloads those final assets, generates `SHA256SUMS`, and publishes.

The public download path uses no Actions artifacts: the ZIP and DMG move through
the draft release. The single artifact is the Store MSIX, which has no public
download path by design. Artifact and log retention is one day.

## Install, update, and remove

### macOS

1. Verify the DMG against `SHA256SUMS`.
2. Open the DMG and drag TokdashCompanion to Applications.
3. Because this build is unsigned, Control-click the app, choose **Open**,
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

- Record the maintainer's explicit acceptance of unsigned distribution.
- Protect the publish environment and restrict it to `companion-v*`.
- Confirm both filenames and the release title clearly say `unsigned`.
- Complete the maintainer's visual verification pass on clean standard-user
  Windows and macOS systems.
- Verify the downloaded ZIP and DMG, not runner staging files.
- Confirm the companion makes network requests only to the explicitly
  configured Tokdash endpoint and, when the user requests or enables update
  checks, GitHub's public releases API. It performs no telemetry, credential
  discovery, or port scanning.
- Tag the current merged `main` commit only. Never replace assets under an
  existing tag; release fixes under a new companion version.

For the Store track specifically:

- Confirm the three `MSIX_*` repository variables are still set; the build fails
  fast without them rather than producing a test-identity package.
- Never attach the `.msix` to the GitHub Release. `check_release.py` enforces this.
- Confirm `companion/VERSION` has a non-zero major. The Store rejects a zero major,
  and it reserves the fourth version field, which the builder pins to `0`.
- A version already accepted by the Store cannot be resubmitted; ship a bump.
