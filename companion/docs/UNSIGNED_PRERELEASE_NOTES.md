This is an **unsigned prerelease** for compatibility and installation testing.
It is not the production/latest Tokdash Companion release.

Artifacts:

- macOS 14+: one universal DMG for Apple Silicon and Intel.
- Windows 11 x64: portable ZIP and an unsigned MSIX packaging artifact.

Important:

- macOS Gatekeeper will not treat this unsigned/unnotarized DMG as a normal
  trusted download.
- Windows SmartScreen may warn about the portable executable.
- The unsigned MSIX cannot be installed normally. It is included to validate
  the package layout while the signing decision is pending.
- Do not disable operating-system security features globally.

Tokdash `1.5.2` or newer must already be installed and running. The companion
is a read-only client and does not install, update, or manage Tokdash.

Verify downloads using the accompanying SHA-256 checksum files. Installation,
update, and uninstall instructions are in `companion/docs/RELEASE.md` in the
source tree for this tag.
