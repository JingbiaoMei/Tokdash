# Agent prompt: install + background-run Tokdash

Goal: help the user install Tokdash and keep it running in the background for local dashboards.

## Questions to ask first
- OS: Linux (systemd) or macOS (launchd)?
- Bind + security: keep `127.0.0.1` (recommended) or expose on LAN (`0.0.0.0`, only if the user explicitly wants this)?
- Port: keep default `55423` or change due to conflicts?
- Install method: venv vs `pipx` vs system Python?

## Procedure (do this, in order)
1. Install Tokdash (`pip install tokdash`) and record the **absolute path** of `tokdash` (`which tokdash`).
2. Verify it works in foreground: `tokdash serve --bind 127.0.0.1 --port 55423`, then open `http://127.0.0.1:55423/`.
3. Set up a user-level service using the repo templates:
   - Linux: `docs/agents/systemd/templates/tokdash.service`
   - macOS: `docs/agents/systemd/templates/com.tokdash.tokdash.plist`
4. Edit the template to use the user’s **absolute** `tokdash` path and desired `--bind/--port`.
5. Enable + start:
   - systemd: `systemctl --user enable --now tokdash`
   - launchd: `launchctl load -w ~/Library/LaunchAgents/com.tokdash.tokdash.plist`
6. Validate via API:
   - `curl 'http://127.0.0.1:55423/api/usage?period=today'`
7. Show where logs are:
   - systemd: `journalctl --user -u tokdash -f`
   - launchd: `/tmp/tokdash.out.log` and `/tmp/tokdash.err.log` (or whatever the plist sets)

## Safety notes
- Default to `127.0.0.1` unless the user explicitly requests LAN exposure.
- If the user asks for `0.0.0.0`, warn them that anyone on the LAN may be able to access the dashboard unless they add auth / firewall rules.

Reference: `docs/agents/systemd/BACKGROUND_RUN.md`.
