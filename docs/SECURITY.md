# Security policy

## Reporting a vulnerability

If you find a security issue, please **do not** open a public GitHub issue.

Preferred:
- Use GitHub “Report a vulnerability” / Security Advisories (private report)

If that’s not available for your fork:
- Open a minimal issue without sensitive details and ask for a private contact channel

## Scope notes

- Tokdash is a **local** dashboard by default (`127.0.0.1` bind).
- Tokdash does **not** provide authentication/authorization.
- If you run with `--bind 0.0.0.0`, you are exposing the dashboard to your LAN. Do not expose it to the public internet.

