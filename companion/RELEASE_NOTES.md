# Tokdash Companion 1.0.1

A correctness release. The "most used today" line named the wrong model on
mixed workloads, on both platforms.

## Changes

- Both apps now read `top_models_by_cost`, the spend podium Tokdash serves, to
  name the day's leading model. The previous code took a maximum by cost over
  `combined_models ?? top_models`, and those arrays hold the five biggest models
  *by tokens* — the costliest model need not be among them, so a cheap
  high-volume model could displace the one actually driving spend.
- Where the server does not serve that field, the fallback now takes its maximum
  over the full model list rather than the token-ranked top five, so the answer
  is right against older Tokdash versions too.
- Merging several servers ranks the combined arrays the same way a single server
  does, so a multi-server podium matches a single-server one.
- The Store MSIX is built in CI on every release tag, and the Microsoft Store
  submission procedure is documented.

Update checks remain optional. They never download or install software;
**View update** opens the validated Tokdash GitHub release page in the default
browser.

## Important: unsigned binaries

These binaries are **not code signed**. GitHub hosting and `SHA256SUMS` verify
the downloaded files against this release, but they do not establish an
operating-system-trusted publisher.

- macOS Gatekeeper will warn that Apple cannot verify the developer. If you
  trust this repository and the checksum, Control-click the app, choose
  **Open**, and confirm **Open**.
- Windows SmartScreen may show an unknown-publisher warning. If you trust this
  repository and the checksum, choose **More info**, then **Run anyway**.
- Do not download these binaries from mirrors or third-party sites.

The Microsoft Store build is signed by the Store during certification and is not
covered by this policy.

## Assets

- `Tokdash-Companion-1.0.1-macos-universal-unsigned.dmg` supports Apple Silicon
  and Intel Macs on macOS 14 or newer.
- `Tokdash-Companion-1.0.1-windows-x64-unsigned.zip` is a self-contained Windows
  11 x64 portable build. Windows 11 on Arm may run it through x64 emulation.
- `SHA256SUMS` covers both downloadable binaries.

To update on macOS, quit Tokdash Companion, open the DMG, drag the app to
Applications, and choose **Replace**. Existing settings migrate automatically.

The companion has no telemetry, credential discovery, or port scanning. It
connects only to Tokdash endpoints configured by the user and, for manual or
opted-in update checks, GitHub's public releases API.
