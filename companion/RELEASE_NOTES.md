# Tokdash Companion 1.0.2

A correctness release for quota cards that measure more than one account, such
as Claude with a second `CLAUDE_CONFIG_DIR` install or MiniMax with both a
global and a mainland-China Token Plan. One failing credential used to distort
the reading of a healthy one, on both platforms.

## Changes

- A quota row's `⚠` last-known prefix and its low-quota alert eligibility now
  compare `buckets[].captured_at` against the **owning account's** `status_at`,
  from the `providers.*.accounts` list Tokdash serves, rather than the
  provider's. Against the provider's alone, one permanently broken credential
  advanced `status_at` every cycle and marked every bucket the healthy
  credential had not refreshed in that same cycle as last-known — which for
  Claude's `weekly_scoped_opus` and MiniMax's per-model buckets is the normal
  case, so a working subscription's low-quota alerts went quiet for as long as
  its sibling stayed broken. A healthy account carries no `status_at` at all, so
  the rule checks whether the row's own account failed *before* reaching for its
  timestamp.
- A card prints a failure notice under the account that owns it, so a failing
  China plan no longer reads as a problem with the healthy global plan.
- A card no longer swallows an error that belongs to none of the accounts it
  lists. Both cards now gate on `providers.*.status_account`, which names the
  account an error belongs to, instead of treating any account list as proof the
  card's own error is covered.
- The multi-server Servers tab counts a provider as OK when at least one of its
  accounts is healthy **and** the card's own error belongs to one of them, so a
  healthy `~/.claude` beside an expired `~/.claude-academic` no longer drops out
  of the OK tally. An error no account claims is a provider whose credentials
  could not be read at all, which is not OK.

Per-account attribution needs Tokdash 2.5.3 or newer, which is where
`providers.*.accounts` and `providers.*.status_account` are first served. Against
an older server the apps fall back to the provider's own status, which is the
1.0.1 behaviour, so upgrading the companion alone is safe.

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

- `Tokdash-Companion-1.0.2-macos-universal-unsigned.dmg` supports Apple Silicon
  and Intel Macs on macOS 14 or newer.
- `Tokdash-Companion-1.0.2-windows-x64-unsigned.zip` is a self-contained Windows
  11 x64 portable build. Windows 11 on Arm may run it through x64 emulation.
- `SHA256SUMS` covers both downloadable binaries.

To update on macOS, quit Tokdash Companion, open the DMG, drag the app to
Applications, and choose **Replace**. Existing settings migrate automatically.

The companion has no telemetry, credential discovery, or port scanning. It
connects only to Tokdash endpoints configured by the user and, for manual or
opted-in update checks, GitHub's public releases API.
