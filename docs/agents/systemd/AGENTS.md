# Agent prompt: install + background-run Tokdash

Goal: help the user install Tokdash and keep it running in the background for local dashboards.

## Questions to ask first
- OS: Linux (systemd) or macOS (launchd)?
- Bind + security: keep `127.0.0.1` (recommended) or expose on LAN (`0.0.0.0`, only if the user explicitly wants this)?
- Remote access: local-only, or do they want secure remote access via `tailscale serve`?
- Port: keep default `55423` or change due to conflicts?
- Install method: venv vs `pipx` vs system Python?

## Procedure (do this, in order)
1. Install Tokdash (`pip install tokdash`) and record the **absolute path** of `tokdash` (`which tokdash`).
2. Verify it works in foreground: `tokdash serve --bind 127.0.0.1 --port 55423`, then open `http://127.0.0.1:55423/`.
3. Download the service template and edit the `ExecStart` line to use the **absolute** `tokdash` path from step 1.

   **Linux (systemd):**
   ```bash
   mkdir -p ~/.config/systemd/user
   curl -L 'https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/agents/systemd/templates/tokdash.service' \
     -o ~/.config/systemd/user/tokdash.service
   # Edit ExecStart to use the absolute path from step 1
   nano ~/.config/systemd/user/tokdash.service
   ```

   **macOS (launchd):**
   ```bash
   curl -L 'https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/agents/systemd/templates/com.tokdash.tokdash.plist' \
     -o ~/Library/LaunchAgents/com.tokdash.tokdash.plist
   # Edit ProgramArguments to use the absolute path from step 1
   nano ~/Library/LaunchAgents/com.tokdash.tokdash.plist
   ```

4. Enable + start:
   - systemd: `systemctl --user enable --now tokdash`
   - launchd: `launchctl load -w ~/Library/LaunchAgents/com.tokdash.tokdash.plist`
5. Validate via API:
   - `curl 'http://127.0.0.1:55423/api/usage?period=today'`
6. Show where logs are:
   - systemd: `journalctl --user -u tokdash -f`
   - launchd: `/tmp/tokdash.out.log` and `/tmp/tokdash.err.log` (or whatever the plist sets)
7. If the user wants remote/mobile access, ask if they want to run:
   - `tailscale serve --bg 55423`
   Keep Tokdash bound to `127.0.0.1`; avoid exposing `0.0.0.0` unless explicitly requested.

## Safety notes
- Default to `127.0.0.1` unless the user explicitly requests LAN exposure.
- Prefer `tailscale serve` over `0.0.0.0` for remote access.
- If the user asks for `0.0.0.0`, warn them that anyone on the LAN may be able to access the dashboard unless they add auth / firewall rules.

Reference: `https://github.com/JingbiaoMei/tokdash/blob/main/docs/agents/systemd/BACKGROUND_RUN.md`.
