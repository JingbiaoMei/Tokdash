# Remote access

Tokdash binds to `127.0.0.1:55423` by default. This local-only default protects an API that
does not authenticate read requests. Choose a remote-access method according to whether you
need read-only or write access.

## Choose an access method

| Method | Exposure | Write access | Recommended use |
|---|---|---|---|
| Tailscale Serve | Devices on your tailnet | No | Private, encrypted read-only access |
| SSH forwarding | Devices that can authenticate with SSH | Yes | Authenticated remote administration |
| `--bind 0.0.0.0` | Every reachable network interface | No | Explicit read-only LAN access on a restricted network |

## Tailscale Serve: private read-only access

Keep Tokdash bound to loopback and let Tailscale expose it only to authenticated devices on
your tailnet.

Tailscale Serve is integrated into interactive onboarding:

```bash
tokdash setup
```

When Tokdash detects Tailscale, the setup wizard offers to configure Serve. If you confirm,
onboarding applies a background configuration equivalent to:

```bash
tailscale serve --bg --https=443 --set-path=/tokdash \
  http://127.0.0.1:55423
```

The wizard prints the resulting URL, which resembles:

```text
https://<machine-name>.<tailnet-name>.ts.net/tokdash
```

Open that URL from Windows or any other device signed in to the same tailnet. Tokdash remains
bound to `127.0.0.1`; Tailscale supplies the private HTTPS transport.

Tailscale Serve is read-only for state-changing API actions. Proxied requests carry the
tailnet hostname and HTTPS origin, which fail Tokdash's loopback write gate. The Quota tab's
"Refresh now" button (`GET /api/quota/refresh`) still works over Serve — it only polls
providers' read-only usage endpoints, so it is a `GET` and is exempt from the write gate like
any other read.

The wizard requires explicit confirmation. `tokdash setup --auto` never enables remote access.
If onboarding creates the Serve rule, it records the matching teardown command in
`install.json`; `tokdash uninstall` removes that specific `/tokdash` rule without resetting
unrelated Tailscale configuration.

If Tailscale rejects the Serve configuration because your user lacks permission, the wizard
can offer to run this one-time operator grant before retrying:

```bash
sudo tailscale set --operator=$USER
```

## SSH forwarding: authenticated write access

Keep Tokdash bound to loopback, then forward a local port through SSH. From Windows or another
client:

```bash
ssh -N -L 55423:127.0.0.1:55423 <user>@<tokdash-host>
```

Open:

```text
http://127.0.0.1:55423
```

The browser continues to use a loopback URL and Host header, so Tokdash permits writes. SSH
provides authentication and encryption. The Tokdash host must run an SSH server that the client
can reach.

For WSL2, `<tokdash-host>` can be the current WSL address when Windows can reach it. Find the
address inside WSL with:

```bash
hostname -I
```

WSL addresses can change after Windows or WSL restarts.

## Wildcard bind: explicit read-only network access

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

`0.0.0.0` listens on every available IPv4 interface, including LAN, VPN, container, mirrored,
and other interfaces. Use this option only when all of the following are true:

- the network is trusted;
- no router or host forwards the port from the public internet;
- firewall rules restrict access to intended clients;
- you accept that read endpoints have no authentication.

Because the configured bind is non-loopback, Tokdash disables state-changing endpoints and
`GET /api/csrf-token`. Remote clients receive `403` for writes.

`tokdash setup --auto` refuses non-loopback binds. Use interactive setup so the exposure is
visible and explicitly confirmed.

## Security model

WSL detection, a private IP address, and network reachability are not authentication signals.
Tokdash therefore does not treat WSL or `0.0.0.0` as trusted for writes. See
[`SECURITY.md`](SECURITY.md) for the complete write-protection model.
