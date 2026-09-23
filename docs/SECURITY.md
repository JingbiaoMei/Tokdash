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
`POST /api/update-check/consent`, `POST /api/quota/consent`, and `POST /api/quota/settings`
(any `POST`/`PUT`/`PATCH`/`DELETE`) —
must clear a gate before it reaches a handler — it fails closed (an unknown bind is treated
as non-loopback):

- **Loopback bind required.** Mutating endpoints are served only when the effective bind is
  loopback. Bound to `0.0.0.0` (or any non-loopback address), writes return `403` — there is
  no safe way to expose a writable unauthenticated API.
- **Host/Origin allowlist.** `Host` (and any `Origin`/`Referer`) must be a loopback address
  derived from the configured bind/port. `Origin`/`Referer` are matched scheme-aware and
  HTTP-only. An absent `Origin`/`Referer` does not fail this check — the token below is what
  stops header-less cross-site form posts. This blocks DNS-rebinding and writes arriving
  through **Tailscale Serve**: it forwards from `127.0.0.1` but carries the tailnet hostname
  as `Host` and an `https://` `Origin`, both of which are rejected. A malformed/unparseable
  `Referer` also fails closed (treated as cross-origin → `403`, never a `500`).
- **Per-session token.** A random token is minted each server start and required as
  `X-Tokdash-Token`. The dashboard fetches it from `GET /api/csrf-token` (itself loopback/
  same-origin gated, so another localhost port can't read it).

For setup commands and a comparison of remote-access methods, see
[`REMOTE_ACCESS.md`](guides/REMOTE_ACCESS.md). Prefer `ssh -L` forwarding, Tailscale Serve,
or Cloudflare Tunnel protected by Cloudflare Access over a non-loopback bind.

The methods differ in **who authenticates the reader** and, for **writes**, all but one
fail the Host/Origin allowlist — provided the gateway preserves the browser's external
headers:

- **`ssh -L` forwarding** preserves a loopback `Host`, so writes from the
  SSH-authenticated user are allowed by design — SSH itself is the authentication layer,
  and reliably distinguishing a forwarded-localhost connection from a genuine local one is
  not possible from HTTP headers. If you do not want SSH-forwarded writes, bind to a
  non-loopback address (which disables all writes) or stop the service when you are done.
- **Tailscale Serve** requests are effectively read-only (their foreign `Host` / `https`
  `Origin` fail the allowlist). Tailscale identity is the read-access boundary: only
  devices on your tailnet can connect.
- **Cloudflare Tunnel** requests fail the same allowlist — cloudflared preserves the
  external `Host`/`Origin` — and are read-only through the gate. The tunnel alone
  authenticates no one — a **Cloudflare Access** application in front of the hostname is
  the read-access boundary.
- **Authenticated reverse proxies** (Caddy, nginx, SSO proxies) are likewise read-only
  through the gate, as long as the proxy passes the external `Host`/`Origin`/`Referer`
  through unchanged (Caddy does so by default; the guide's nginx snippet sets
  `Host $host`). The proxy's own authentication (basic auth, forward auth, SSO) behind
  TLS is the read-access boundary. Tokdash cannot verify that the proxy enforces it.
- **Public tunnels without edge authentication** (Tailscale Funnel, Cloudflare Quick
  Tunnels, bare ngrok, LocalTunnel) expose the unauthenticated read API to the public
  internet. Writes still fail closed — these services preserve the external
  `Host`/`Origin`, which fail the allowlist — but per the policy above, do not expose
  Tokdash this way.

Every read-only claim above rests on header preservation. A proxy that rewrites `Host` to a
loopback address **and** also rewrites `Origin`/`Referer` to a loopback origin — or removes
them — satisfies the allowlist, makes `GET /api/csrf-token` reachable from the network, and
turns the unauthenticated API writable. Stripping `Origin`/`Referer` alone does not: the
external `Host` still fails the allowlist. Never configure a gateway that way.

### Quota refresh and update-check are read-only GETs

`GET /api/quota/refresh` (the Quota tab's "Refresh now" button) only calls providers'
read-only usage endpoints — no quota is consumed and nothing provider-side is mutated — so it
is served as `GET`, like the other read routes, and is **not** subject to the write-protection
gate above. Likewise, `GET /api/update-check` only performs a read-only PyPI version check plus
an in-memory cache (no disk write, no config change) and is also served as `GET`. That means
both keep working over Tailscale Serve, WSL port-forwarding, or any other forward that only
proxies loopback traffic, even though those paths reject genuine writes. The config-write
endpoints (`PUT /api/pricing-db`, `POST /api/quota/consent`, `POST /api/quota/settings`,
`POST /api/update-check/consent`) are unaffected and remain loopback-only as described above.

The default CORS policy permits loopback origins and HTTPS reads between Tailscale Serve hosts
with the same `<tailnet-name>.ts.net` suffix. Other browser-page origins require
`TOKDASH_ALLOW_ORIGINS` / `TOKDASH_ALLOW_ORIGIN_REGEX`; setting either option replaces the default
origin policy, so list every required origin. CORS is unrelated to the write gate and never grants
write access to a non-loopback bind — loopback bind + Host/Origin + token are still required for
every mutating request.

On WSL2, bind to `127.0.0.1` (the default), not `0.0.0.0`. Windows' localhost forwarding into
WSL preserves a loopback `Host` header, so the guarded writes above keep working from Windows;
binding `0.0.0.0` makes the effective bind non-loopback and disables writes entirely (see
[`REMOTE_ACCESS.md`](guides/REMOTE_ACCESS.md)).

## Quota tracking

Quota tracking has a master switch, `quota.enabled` in `config.json` (default on), that governs
*all* quota work. When it is off — or the `TOKDASH_QUOTA_POLL=0` kill switch is set — the poller
idles entirely: no session scanning, no network calls, and no database writes. `GET /api/quota/refresh`
then returns a "quota tracking disabled" error. The per-provider consent keys are narrower: they only
govern the opt-in *network* tiers and never enable the master switch on their own.

Provider network calls are default-off. Local-only quota data may be read from
Codex session files and Claude credentials metadata. When a provider is explicitly enabled, Tokdash
reads the local CLI credential file for that provider and calls that provider's quota endpoint:

- Codex: `$CODEX_HOME/auth.json`, `https://chatgpt.com/backend-api/wham/usage`, and `.../wham/rate-limit-reset-credits`
- Claude Code: `CLAUDE_CODE_OAUTH_TOKEN` (highest-precedence override), `$CLAUDE_CONFIG_DIR/.credentials.json`, or the macOS Keychain item `Claude Code-credentials` (read-only, via `security find-generic-password`), `https://api.anthropic.com/api/oauth/usage?cedar_ember=1` (the flag adds Claude Code's limit-reset grants to the same response; if the server rejects it as a bad request, one retry goes to the plain URL). That request's User-Agent is `claude-cli/<version> (external, cli) tokdash/<version>`: Anthropic lists reset grants only for the Claude Code surface, so it opens with the Claude Code CLI's own prefix, since the token is that CLI's sign-in, and then names Tokdash. Tokdash only reads the grants and never calls the endpoint that spends one. With credential scanning consented, the home directory is listed for `~/.claude*` installs and each one's own `.credentials.json` is read the same way (or list the directories in `TOKDASH_CLAUDE_PROFILES`), so a second subscription signed in through `CLAUDE_CONFIG_DIR=~/.claude-academic` gets its own quota; without that consent the directory is not listed at all. The env override and the Keychain item belong to the default install only, and each install's token goes only to the same Anthropic usage endpoint. Two directories holding one sign-in are recognised by the claim in the token's payload that names the sign-in (account id, then subject, then email) — decoded locally, never sent anywhere, and deliberately not the organization id, so two seats on one team stay two subscriptions — so a copied install is not counted twice.
- Antigravity: `~/.gemini/jetski-standalone-oauth-token` (current), with fallback to `~/.gemini/antigravity-cli/antigravity-oauth-token` (legacy), `https://daily-cloudcode-pa.googleapis.com/v1internal:*`
- Command Code: `COMMAND_CODE_API_KEY` or `COMMANDCODE_API_KEY`, `~/.commandcode/auth.json`, or the `commandcode` entry in OpenCode's `auth.json`, `https://api.commandcode.ai/alpha/billing/credits` and `.../alpha/billing/subscriptions`, plus a best-effort `.../alpha/whoami` whose only use is an organization id to scope those two calls (read-only; the key is never refreshed or written back)

Tokdash never refreshes or writes provider tokens. Quota snapshots are stored locally in
`usage.sqlite3`; `tokdash export` excludes them unless `--include-quota` is passed. Snapshot
`raw_json` payloads never contain credentials (token material is stripped before storage, with
regression tests). With `TOKDASH_USAGE_DB=0` (local persistence opted out) nothing quota-related
is written to disk at all: no history is kept, the background poller is disabled, and the Quota
tab only shows transient in-memory results from a manual refresh.
