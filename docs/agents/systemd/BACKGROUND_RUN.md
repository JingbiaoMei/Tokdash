# Running Tokdash in the background

Tokdash is designed to be run locally (binds to `127.0.0.1` by default). If you want it to run automatically in the background, use a **user** service (recommended) rather than a system-wide service.

## Linux (systemd user service)

### 1) Install Tokdash

In a venv (recommended):

```bash
python -m venv .venv
source .venv/bin/activate
pip install tokdash
```

Confirm the full path to the installed `tokdash`:

```bash
which tokdash
```

### 2) Create the service file

Copy `docs/agents/systemd/templates/tokdash.service` to:

```bash
mkdir -p ~/.config/systemd/user
cp docs/agents/systemd/templates/tokdash.service ~/.config/systemd/user/tokdash.service
```

Edit `ExecStart=` to point at your `tokdash` binary (or set it to an absolute path to your venv binary).

### 3) Enable + start

```bash
systemctl --user daemon-reload
systemctl --user enable --now tokdash
```

View logs:

```bash
journalctl --user -u tokdash -f
```

Stop:

```bash
systemctl --user disable --now tokdash
```

### (Optional) Start at boot (no login)

By default, user services run when you have a user session. To keep it running after reboot without logging in, enable user lingering:

```bash
loginctl enable-linger "$USER"
```

This command may require admin privileges depending on your distro.

## macOS (launchd LaunchAgent)

### 1) Install Tokdash

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install tokdash
which tokdash
```

### 2) Install the LaunchAgent plist

Copy `docs/agents/systemd/templates/com.tokdash.tokdash.plist` to:

```bash
mkdir -p ~/Library/LaunchAgents
cp docs/agents/systemd/templates/com.tokdash.tokdash.plist ~/Library/LaunchAgents/com.tokdash.tokdash.plist
```

Edit the plist to set the correct `ProgramArguments` path for your `tokdash` binary and adjust `--bind/--port` as desired.

### 3) Load + start

```bash
launchctl load -w ~/Library/LaunchAgents/com.tokdash.tokdash.plist
```

To unload/stop:

```bash
launchctl unload -w ~/Library/LaunchAgents/com.tokdash.tokdash.plist
```

Logs (default locations in the plist):

- `/tmp/tokdash.out.log`
- `/tmp/tokdash.err.log`

Note: `launchd` expects **absolute** paths; `~` is not expanded in plist path fields. If you want logs under your home directory, set `StandardOutPath` / `StandardErrorPath` to an absolute path (for example, `/Users/<you>/Library/Logs/...`).
