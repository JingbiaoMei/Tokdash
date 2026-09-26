# Remote access

Tokdash binds to `127.0.0.1:55423` by default and authenticates nothing: anyone who can
reach the HTTP port can read every dashboard and API response — session names, project
info, usage, costs, and quota. Tokdash never checks who is asking — the access method you
choose **is** the security decision. Pick a category whose access control you trust, and do
not expose Tokdash to the public internet without authentication (see
[`SECURITY.md`](../SECURITY.md)).

Browser access through an external hostname is **read-only as long as the proxy preserves
the browser's external `Host`/`Origin`/`Referer` headers** — every recipe in this guide
does. Tokdash's write gate requires a loopback bind, a loopback `Host`, a loopback HTTP
`Origin`/`Referer` (or none at all), and a per-process token. A browser on an external
HTTPS origin sends that external `Origin`, which fails the gate. A proxy that rewrites
`Host` to a loopback address **and** also rewrites `Origin`/`Referer` to loopback values —
or strips them — can satisfy the gate, expose the write-token endpoint, and enable writes
against the unauthenticated API. (Stripping `Origin`/`Referer` alone is not enough: the
external `Host` still fails.) Never configure a gateway that way. Only SSH forwarding keeps
browser writes, because it preserves a loopback `Host` end to end. See
[Why browser writes fail through proxies](#why-browser-writes-fail-through-proxies).

## Choose an access category

| Category | Methods | Who can read | Browser writes | Recommendation |
|---|---|---|---|---|
| Private network | Tailscale Serve | Devices on your tailnet | No | Recommended |
| Authenticated forwarding | SSH local forwarding | SSH-authorized users | Yes | Recommended; the only write-capable method |
| Authenticated web gateway | Cloudflare Tunnel + Access, authenticated Caddy/nginx | Users the gateway authenticates | No | Recommended for browser access |
| Direct LAN exposure | `--bind 0.0.0.0` with firewall restrictions | Reachable networks | No | Use cautiously |
| Public tunnel | Funnel, Quick Tunnel, ngrok, LocalTunnel | Public internet | No | Unsafe without added authentication |

## Private network: Tailscale Serve

A private network limits readers to devices you have enrolled. Tailscale Serve runs as a
persistent background rule on the Tailscale service and requires a Tailscale account; every
device needs Tailscale installed and signed in. A self-hosted VPN (WireGuard,
ZeroTier-style) is an equivalent private transport — the same header-preservation caveat
above applies to whatever fronts Tokdash on it.

Keep Tokdash bound to loopback and let Tailscale expose it only to authenticated devices on
your tailnet.

Tailscale Serve is integrated into interactive onboarding:

```bash
tokdash setup
```

When Tokdash detects Tailscale, the setup wizard offers to configure Serve. The prompt
defaults to **No**; `tokdash setup --auto` and `--yes` never configure it. If you confirm,
onboarding applies a background configuration equivalent to:

```bash
tailscale serve --bg --https=443 --set-path=/tokdash \
  http://127.0.0.1:55423
```

The wizard prints the resulting URL, which resembles:

```text
https://<machine-name>.<tailnet-name>.ts.net/tokdash
```

Open that URL from Windows or any other device signed in to the same tailnet. Tokdash
remains bound to `127.0.0.1`; Tailscale supplies the private HTTPS transport. `--set-path`
is a Tailscale CLI option that strips the `/tokdash` prefix before proxying; it is not a
Tokdash option.

Tailscale Serve is read-only for state-changing API actions. Serve preserves the tailnet
hostname as `Host` and the browser sends an HTTPS origin, both of which fail Tokdash's
loopback write gate. The Quota tab's "Refresh now" button (`GET /api/quota/refresh`) still
works over Serve — it only polls providers' read-only usage endpoints, so it is a `GET` and
is exempt from the write gate like any other read.

If onboarding creates the Serve rule, it records the matching teardown command in
`install.json`; `tokdash uninstall` removes that specific `/tokdash` rule without resetting
unrelated Tailscale configuration.

If Tailscale rejects the Serve configuration because your user lacks permission, the wizard
can offer to run this one-time operator grant before retrying:

```bash
sudo tailscale set --operator=$USER
```

## Authenticated forwarding: SSH local forwarding

Keep Tokdash bound to loopback, then forward a local port through SSH. From Windows or
another client:

```bash
ssh -N -L 55423:127.0.0.1:55423 <user>@<tokdash-host>
```

Open:

```text
http://127.0.0.1:55423
```

The browser continues to use a loopback URL and Host header, so Tokdash permits writes. SSH
provides authentication and encryption. The Tokdash host must run an SSH server that the
client can reach.

For WSL2, `<tokdash-host>` can be the current WSL address when Windows can reach it. Find
the address inside WSL with:

```bash
hostname -I
```

WSL addresses can change after Windows or WSL restarts.

Nothing persists: exit or Ctrl-C the `ssh` process and the forwarding — and the remote
access — is gone.

## Authenticated web gateway

A web gateway terminates TLS on a public hostname and requires login before any request
reaches Tokdash. Supported only with all four of: gateway-level authentication, TLS,
network controls (firewall or bind rules that keep the gateway as the only way in), and
header preservation — the gateway must pass the browser's external `Host`/`Origin`/`Referer`
through unchanged (the recipes below do), never rewrite them to loopback values or strip
them. A gateway runs persistently as a service; it requires a Cloudflare account with
Access, or your own domain, TLS certificates, and Caddy/nginx.

**Client compatibility.** Authenticated gateways are for opening the gateway's own URL
directly in a browser. They do **not** work as additional servers in another dashboard's
Settings (cross-origin `fetch()` does not carry the gateway's credentials) or as server
URLs for the native companion apps (no Cloudflare Access, Basic Auth, or custom-header
support). See [Combine several Tokdash servers](#combine-several-tokdash-servers).

Proxy at `/`; Tokdash has no path-prefix configuration for arbitrary subpaths (see
[Subpath deployment](#subpath-deployment-optional) for the one supported exception).

### Cloudflare Tunnel + Access

This recipe targets Linux; adjust paths and service steps for other platforms. A **named**
Cloudflare Tunnel fronts Tokdash with Cloudflare's network, and a Cloudflare Access
application in front of the hostname supplies the authentication Tokdash lacks. **Keep the
tunnel stopped until Access is configured and verified — a running tunnel without Access is
a public, unauthenticated endpoint** (see
[Public tunnels](#public-tunnels-unsafe-without-authentication)).

Create the tunnel, route a hostname to it, and write the ingress config:

```bash
cloudflared tunnel login
cloudflared tunnel create tokdash
cloudflared tunnel route dns tokdash dash.example.com
```

`~/.cloudflared/config.yml` (a per-user Linux path):

```yaml
tunnel: <tunnel-id>
credentials-file: /home/<user>/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: dash.example.com
    service: http://127.0.0.1:55423
  - service: http_status:404
```

Then create **and verify** the Access policy before starting anything: in the Cloudflare
Zero Trust dashboard, create an Access application (self-hosted) covering
`dash.example.com` with an Include rule that restricts **who** may log in — specific email
addresses, an email domain, or an IdP group. Do not use "Login Methods: One-time PIN" as
the sole Include rule: Cloudflare warns that it matches **anyone with any valid email
address**. If you want OTP, enable one-time PIN as the *login method* while keeping an
email or domain restriction in the policy. Confirm the application is active and the policy
evaluates the way you expect.

Only then start the tunnel, in the foreground:

```bash
cloudflared tunnel run tokdash
```

The dashboard opens at `https://dash.example.com`. Access enforces login before any request
reaches Tokdash; reads then work normally, and browser writes return `403` because
cloudflared preserves the external `Host` and HTTPS `Origin`.

Verify end to end once the tunnel is running. What matters is that an Access challenge
comes **before** Tokdash — not necessarily Cloudflare's own login page: with instant
authentication, Cloudflare redirects straight to your identity provider, so a protected
deployment may never show it. Open `https://dash.example.com` in a private browser window
(no cached Cloudflare session) and confirm you hit an Access login page or an IdP redirect
before the dashboard. Or check without a browser:

```bash
curl -sI https://dash.example.com
```

A protected hostname answers with a redirect into the Access/IdP login flow; a `200`
serving the dashboard means Access is not in front — stop the tunnel and fix the Access
application before continuing. Checking the policy in the dashboard alone cannot prove the
hostname is protected.

To run the tunnel as a service so it starts at boot, follow Cloudflare's platform-specific
[run as a service](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/as-a-service/)
documentation — service managers differ per platform and run with a different `$HOME`, so
the per-user credentials path above does not apply unchanged.

Works with: opening `https://dash.example.com` directly in a browser. Not as an additional
server in a dashboard's Settings, and not as a companion-app server URL.

To tear it down: stop the foreground tunnel with Ctrl-C (or remove the service per the
service documentation), delete the DNS route's CNAME record, run
`cloudflared tunnel delete tokdash`, and remove the Access application in the Zero Trust
dashboard.

### Caddy or nginx reverse proxy

Caddy terminates TLS automatically and can require HTTP Basic auth:

```caddyfile
dash.example.com {
    basic_auth {
        <user> <bcrypt-hash>
    }
    reverse_proxy 127.0.0.1:55423
}
```

Generate the hash with `caddy hash-password`. Caddy passes the external `Host` through by
default; do not add header rewrites that replace `Host`/`Origin`/`Referer` with loopback
values.

nginx, behind TLS certificates you manage (for example with certbot):

```nginx
server {
    listen 443 ssl;
    server_name dash.example.com;

    ssl_certificate     /etc/letsencrypt/live/dash.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/dash.example.com/privkey.pem;

    auth_basic "Tokdash";
    auth_basic_user_file /etc/nginx/.htpasswd_tokdash;

    location / {
        proxy_pass http://127.0.0.1:55423;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

Create the password file with `htpasswd -c /etc/nginx/.htpasswd_tokdash <user>`. The
`proxy_set_header Host $host` line keeps the external hostname in `Host`; keep it, and do
not strip `Origin`/`Referer`.

Keep Tokdash bound to `127.0.0.1` so the proxy is the only entry point. Basic auth over TLS
is the minimum; an SSO-aware proxy (oauth2-proxy, Authelia, forward auth) is stronger. With
the external headers preserved as above, browser writes return `403` through the proxy.

Works with: opening the site URL directly in a browser (the browser prompts for basic
auth). Not as an additional server in a dashboard's Settings, and not as a companion-app
server URL.

To remove the exposure, delete the site block or server config and reload the proxy
(`systemctl reload caddy` or `systemctl reload nginx`). Tokdash never left loopback, so the
proxy is the only thing to clean up.

### Subpath deployment (optional)

Tokdash's generated URLs support exactly one path prefix, `/tokdash` — the same support
Tailscale Serve uses — but the proxy must strip the prefix before forwarding. Tokdash's
internal routing handles `/tokdash` for the HTML shell and API routes, but the static file
mount resolves assets from the request path relative to that prefix and 404s under a
preserved prefix. Verified behavior: `GET /tokdash/`, `/tokdash/api/*`, `/tokdash/sw.js`,
and `/tokdash/manifest.webmanifest` work unchanged, but `GET /tokdash/static/*` fails. The
proxy must also pass `X-Forwarded-Prefix` so generated manifest and service-worker URLs
keep the prefix. The dashboard also detects `/tokdash` in `window.location` for page and
API URLs.

These snippets belong **inside** the authenticated TLS site config from the parent section
— they are location blocks, not standalone sites.

Caddy (`handle_path` strips the prefix but does not match bare `/tokdash`, so redirect it):

```caddyfile
redir /tokdash /tokdash/ 308
handle_path /tokdash/* {
    reverse_proxy 127.0.0.1:55423 {
        header_up X-Forwarded-Prefix /tokdash
    }
}
```

nginx (`location` with a trailing slash and a trailing-slash `proxy_pass` strips the
prefix; it likewise does not match bare `/tokdash`, so add an exact-match redirect). The
`/tokdash/` location must set `Host` itself — nginx does not inherit `proxy_set_header`
from the sibling `/` location, and the default would send the loopback backend address as
`Host`, which breaks the header-preservation rule:

```nginx
location = /tokdash {
    return 308 /tokdash/;
}
location /tokdash/ {
    proxy_pass http://127.0.0.1:55423/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Prefix /tokdash;
}
```

Proxying at `/` is simpler and recommended; use the subpath only when you need Tokdash to
share a hostname with other applications.

## Direct LAN exposure: wildcard bind

On a trusted private network, you can expose Tokdash directly:

```bash
tokdash serve --bind 0.0.0.0
```

To persist the same bind through the onboarding-managed background service, run interactive
setup:

```bash
tokdash setup --bind 0.0.0.0
```

Then open:

```text
http://<host-address>:55423
```

For WSL2, `<host-address>` is usually the WSL address shown by `hostname -I`.

`0.0.0.0` listens on every available IPv4 interface, including LAN, VPN, container,
mirrored, and other interfaces. Use this option only when all of the following are true:

- the network is trusted;
- no router or host forwards the port from the public internet;
- firewall rules restrict access to intended clients;
- you accept that read endpoints have no authentication.

Because the configured bind is non-loopback, Tokdash disables state-changing endpoints and
`GET /api/csrf-token`. Remote clients receive `403` for writes.

`tokdash setup --auto` refuses non-loopback binds. Use interactive setup so the exposure is
visible and explicitly confirmed.

To return to the default, restart with a loopback bind (`tokdash serve` with no `--bind`),
or re-run interactive `tokdash setup` without `--bind 0.0.0.0` and remove the firewall rule
you added.

## Public tunnels: unsafe without authentication

These methods put Tokdash on the public internet with nothing authenticating readers. They
run only while the tunnel process is up and need at most a tunnel account — that is the
problem. Because Tokdash has no read authentication and
[`SECURITY.md`](../SECURITY.md) prohibits public exposure, do not use them as sharing
options:

- **Tailscale Funnel** — exposes the service beyond your tailnet to the public internet.
- **Cloudflare Quick Tunnels** (`cloudflared tunnel --url`) — a public `trycloudflare.com`
  hostname with no Access application.
- **Bare ngrok** (`ngrok http 55423`) — a public URL with no edge authentication.
- **LocalTunnel** and similar free tunnel services — public URL, no authentication.

Writes still fail through all of them — they preserve the external `Host`/`Origin`, which
fail the write gate — so the exposure is read-only. But the read side is your complete
session names, project info, usage history, costs, and quota, open to anyone with the URL.
If a tunnel service offers edge authentication (for example ngrok with OAuth or basic auth
configured), it becomes an
[authenticated web gateway](#authenticated-web-gateway) and the same requirements apply:
authentication, TLS, network controls, and header preservation.

## Why browser writes fail through proxies

Every state-changing request (`POST`/`PUT`/`PATCH`/`DELETE`) must clear all four checks of
Tokdash's write gate:

1. the server is bound to loopback;
2. the `Host` header is a recognized loopback address;
3. any `Origin`/`Referer`, if present, is a loopback HTTP origin (an absent
   `Origin`/`Referer` does not fail this check — the token below is what stops
   header-less cross-site form posts);
4. the request carries the valid per-process `X-Tokdash-Token`.

With the header-preserving configurations in this guide, a browser on any external
hostname — Cloudflare, Caddy, nginx, ngrok, Funnel, LocalTunnel, Tailscale Serve — sends
that external origin, which fails check 3, and the external `Host` fails check 2
independently. A non-loopback bind fails check 1 before any header is consulted. The token
endpoint (`GET /api/csrf-token`) is itself loopback-gated, so a remote browser cannot mint
a token.

These guarantees flip only if a proxy rewrites `Host` to a loopback address **and** either
rewrites `Origin`/`Referer` to a loopback origin or removes them entirely: checks 2 and 3
then pass, `/api/csrf-token` becomes reachable, and the unauthenticated API becomes
writable from the network. Stripping `Origin`/`Referer` alone is not enough — the external
`Host` still fails check 2. Header preservation is therefore a hard requirement for every
gateway recipe above. SSH forwarding
is the one write-capable method by design — the forward preserves a genuine loopback
`Host`, and SSH itself is the authentication layer. See
[`SECURITY.md`](../SECURITY.md) for the complete write-protection model.

## Combine several Tokdash servers

The dashboard and companion apps can read several Tokdash instances at once. In dashboard
Settings, add each server URL and choose All or a custom subset. Overview, Sessions, and
Stats combine reachable servers; Quota stays grouped by server. An unreachable server drops
out until a later refresh succeeds.

### Several addresses for one server

One machine often has more than one way to reach it: a Tailscale Serve name, an
`ssh -L` forward, a LAN address, and the loopback address it listens on. Every Tokdash
answers `GET /health` with an `instance_id`, so adding a second address for a server that
is already in the list joins it to that server instead of creating a second row. The
combined figures count the machine once, and Settings shows the server with one row per
address, each with its own latency.

The dashboard reads through the quickest address the browser is allowed to use, and moves
to the next one within the same request when an address dies. Two details worth knowing
before the numbers look wrong:

- It will not thrash. A challenger takes over only when it is clearly quicker (by at
  least 10 ms and 25 %), so the address in use is not always the fastest one; the row says
  which is in use and why. Pin an address to keep it.
- Which addresses a page can read is decided by the browser, not the daemon. A dashboard
  opened at `http://127.0.0.1:55423` can read every server it can reach. A dashboard
  opened at a `https://<machine>.<tailnet>.ts.net` address can read same-tailnet servers
  and nothing else: no plain-HTTP address, and no loopback address at all, including the
  one on the machine serving that page. A `127.0.0.1` row left on a Serve page is therefore
  not a slow route but an unreadable one; the dashboard marks it **Blocked from this page**
  with the rule that blocks it and a remove button, and never merges it.

If a second address does land in two separate rows, they are two daemons even when one
machine runs both, which is what two `TOKDASH_DATA_DIR`s give you, and their tokens really
are counted twice.

CORS only matters when **one dashboard page connects to additional Tokdash servers at
different origins**. A dashboard and its API served through the same proxy origin — one
Cloudflare Tunnel hostname, one reverse-proxy vhost, one Serve URL — need no CORS entry at
all. When you do add remote servers: by default, a dashboard opened on loopback can read
remote servers, and Tailscale Serve dashboards can read Tokdash servers under the same
`<tailnet-name>.ts.net` suffix. Other remote origins must be added to every server with
`TOKDASH_ALLOW_ORIGINS=https://<dashboard-host>`.

**An explicit CORS setting replaces the default policy, it does not extend it.** Setting
`TOKDASH_ALLOW_ORIGINS` or `TOKDASH_ALLOW_ORIGIN_REGEX` on a server switches off both the
loopback rule and the same-tailnet rule, so admitting one origin there silently takes away
the ability of a loopback dashboard to read that server, and the Serve page you were
looking at keeps working, which is what makes the mistake easy to ship. Include every
browser origin that must connect, or set nothing at all. An HTTPS page cannot
fetch a plain-HTTP server; use HTTPS Tailscale Serve URLs for every server. Native
companions do not send an `Origin` header and are unaffected by CORS and browser
mixed-content rules.

Authenticated-gateway URLs — Cloudflare Access, basic-auth Caddy/nginx — cannot serve as
additional servers: cross-origin `fetch()` does not include the gateway's login or basic
auth, and the native companions have no Access, Basic Auth, or custom-header support. Use
loopback or Tailscale Serve URLs as additional servers; Serve's HTTPS makes it the normal
browser-compatible choice. Wildcard-bound `http://` URLs work only from a loopback HTTP
dashboard or a native companion — an HTTPS dashboard (Serve or proxy) cannot fetch them.

CORS configuration can permit cross-origin **reads**; it never grants write access. The
loopback write gate applies regardless of `TOKDASH_ALLOW_ORIGINS`.

Reads follow the exposure model above. Per-server writes still pass through the existing
loopback write gate: SSH-forwarded loopback URLs can write, while header-preserving
external-hostname methods remain read-only.

## Security model

WSL detection, a private IP address, and network reachability are not authentication
signals. Tokdash therefore does not treat WSL or `0.0.0.0` as trusted for writes. See
[`SECURITY.md`](../SECURITY.md) for the complete write-protection model.
