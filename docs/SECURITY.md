# Security policy

## Reporting a vulnerability

If you find a security issue, please **do not** open a public GitHub issue.

Preferred:
- Use GitHub “Report a vulnerability” / Security Advisories (private report)

If that’s not available for your fork:
- Open a minimal issue without sensitive details and ask for a private contact channel

## Scope notes

- Tokdash is a **local** dashboard by default (`127.0.0.1` bind).
- Tokdash does **not** provide authentication/authorization for reads.
- If you run with `--bind 0.0.0.0`, you are exposing the dashboard to your LAN. Do not expose it to the public internet.

## Write-protection model

The API is unauthenticated, so **every state-changing request** — today `PUT /api/pricing-db`,
`POST /api/update-check`, `POST /api/update-check/consent`, `POST /api/quota/consent`,
`POST /api/quota/settings`, and `POST /api/quota/refresh` (any `POST`/`PUT`/`PATCH`/`DELETE`) —
must clear a gate before it reaches a handler — it fails closed (an unknown bind is treated
as non-loopback):

- **Loopback bind required.** Mutating endpoints are served only when the effective bind is
  loopback. Bound to `0.0.0.0` (or any non-loopback address), writes return `403` — there is
  no safe way to expose a writable unauthenticated API.
- **Host/Origin allowlist.** `Host` (and any `Origin`/`Referer`) must be a loopback address
  derived from the configured bind/port. `Origin`/`Referer` are matched scheme-aware and
  HTTP-only. This blocks DNS-rebinding and writes arriving through **Tailscale Serve**: it
  forwards from `127.0.0.1` but carries the tailnet hostname as `Host` and an `https://`
  `Origin`, both of which are rejected. A malformed/unparseable `Referer` also fails closed
  (treated as cross-origin → `403`, never a `500`).
- **Per-session token.** A random token is minted each server start and required as
  `X-Tokdash-Token`. The dashboard fetches it from `GET /api/csrf-token` (itself loopback/
  same-origin gated, so another localhost port can't read it).

For remote access, prefer `tailscale serve` or `ssh -L` forwarding over a non-loopback bind.
The two differ for **writes**: **Tailscale Serve** requests are effectively read-only (their
foreign `Host` / `https` `Origin` fail the allowlist), but an **`ssh -L` forward to
`localhost`/`127.0.0.1` preserves a loopback `Host`, so writes from the SSH-authenticated user
are allowed by design** — SSH itself is the authentication layer there, and reliably
distinguishing a forwarded-localhost connection from a genuine local one is not possible from
HTTP headers. If you do not want SSH-forwarded writes, bind to a non-loopback address (which
disables all writes) or stop the service when you are done.

## Quota tracking

Quota tracking has a master switch, `quota.enabled` in `config.json` (default on), that governs
*all* quota work. When it is off — or the `TOKDASH_QUOTA_POLL=0` kill switch is set — the poller
idles entirely: no session scanning, no network calls, and no database writes. `POST /api/quota/refresh`
then returns a "quota tracking disabled" error. The per-provider consent keys are narrower: they only
govern the opt-in *network* tiers and never enable the master switch on their own.

Provider network calls are default-off. Local-only quota data may be read from
Codex session files and Claude credentials metadata. When a provider is explicitly enabled, Tokdash
reads the local CLI credential file for that provider and calls that provider's quota endpoint:

- Codex: `$CODEX_HOME/auth.json`, `https://chatgpt.com/backend-api/wham/usage`, and `.../wham/rate-limit-reset-credits`
- Claude Code: `CLAUDE_CODE_OAUTH_TOKEN` (highest-precedence override), `$CLAUDE_CONFIG_DIR/.credentials.json`, or the macOS Keychain item `Claude Code-credentials` (read-only, via `security find-generic-password`), `https://api.anthropic.com/api/oauth/usage`
- Antigravity: `~/.gemini/antigravity-cli/antigravity-oauth-token`, `https://daily-cloudcode-pa.googleapis.com/v1internal:*`

Tokdash never refreshes or writes provider tokens. Quota snapshots are stored locally in
`usage.sqlite3`; `tokdash export` excludes them unless `--include-quota` is passed. Snapshot
`raw_json` payloads never contain credentials (token material is stripped before storage, with
regression tests). With `TOKDASH_USAGE_DB=0` (local persistence opted out) nothing quota-related
is written to disk at all: no history is kept, the background poller is disabled, and the Quota
tab only shows transient in-memory results from a manual refresh.
