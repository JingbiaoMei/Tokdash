# Troubleshooting

Common errors and how to fix them.

## Port already in use (EADDRINUSE)

**Symptom:** `tokdash serve` fails with `OSError: [Errno 10048] Address already in use` or similar.

**Cause:** Another process (often another Tokdash instance) is bound to port 55423.

**Fix:**
1. Run diagnostics to identify the process holding the port:
```bash
tokdash doctor
```
2. If the process is Tokdash's own managed service, stop it via `tokdash setup` or the service manager. Otherwise, kill it manually:

```bash
# Windows: find and kill the process
netstat -ano | findstr 55423
taskkill /PID <pid> /F

# macOS/Linux
lsof -i :55423
kill <pid>
```

Or use a different port:
```bash
tokdash serve --port 55424
```

## UsageDatabaseSchemaTooNewError

**Symptom:** `tokdash serve` or `tokdash db status` fails with `UsageDatabaseSchemaTooNewError`.

**Cause:** The usage database was written by a newer version of Tokdash than the one currently running.

**Fix:** Update Tokdash:
```bash
tokdash update
```

Then restart any other Tokdash processes using this data directory.

Do NOT delete the database — it contains your full usage history.

## SQLite WAL lock / database is locked

**Symptom:** `tokdash db status` or `tokdash serve` fails with `sqlite3.OperationalError: database is locked`.

**Cause:** Another Tokdash process has the database open in WAL mode.

**Fix:**
1. Close all other Tokdash instances
2. If the error persists, repair the database:
```bash
tokdash db repair
```

**When to use `db resync` vs `db repair`:**
- `tokdash db repair` — recomputes derived counters and checkpoints the WAL. Does **not** fix physical SQLite corruption.
- `tokdash db resync` — rebuilds the database from source logs. Can drop durable rows if logs have been pruned. Use only when `db sync` cannot recover missing entries.

For missing entries, run `tokdash db sync` first.

## macOS Keychain prompts during Claude quota polling

**Symptom:** macOS shows Keychain consent prompts when Tokdash polls Claude Code quota.

**Cause:** The prompt comes from opt-in Claude quota polling reading the Claude Code credential item, not from scanning config files.

**Fix:**
- Grant access when prompted, or
- Disable Claude API polling:
```bash
tokdash quota consent --claude-api off
```
- Or disable all quota polling:
```bash
export TOKDASH_QUOTA_POLL=0
```

To limit which Claude profile directories are scanned (OS path separator, not comma-separated):
```bash
export TOKDASH_CLAUDE_PROFILES="/Users/me/.claude:/Users/me/.claude-academic"
```

## WSL2 localhost binding

**Symptom:** Dashboard is accessible from WSL2 but not from Windows browser.

**Cause:** WSL2 has its own localhost, not shared with Windows.

**Fix:**
Windows' localhost forwarding already reaches a WSL2 loopback bind, so the default `127.0.0.1` bind works from Windows. Do **not** use `--bind 0.0.0.0` — it makes the effective bind non-loopback and disables writes entirely (see [SECURITY.md](../../SECURITY.md)).

```bash
# SSH port forwarding from Windows (if localhost forwarding is insufficient)
ssh -L 55423:127.0.0.1:55423 <wsl-host>

# Set the public base path (a path prefix, not a full URL)
export TOKDASH_PUBLIC_BASE_PATH="/tokdash"
```

## Enabling OTel exports for GitHub Copilot

**Symptom:** GitHub Copilot usage is not showing in Tokdash.

**Cause:** Copilot does not write token usage by default.

**Fix:**
```bash
# Windows (use setx or System Properties for persistence — `set` only lasts for that console)
setx COPILOT_OTEL_FILE_EXPORTER_PATH "%USERPROFILE%\.copilot\otel\usage.jsonl"

# macOS/Linux
export COPILOT_OTEL_FILE_EXPORTER_PATH=~/.copilot/otel/usage.jsonl
```

`COPILOT_OTEL_FILE_EXPORTER_PATH` names a single file, not a directory.

## Dashboard shows no data / empty state

**Symptom:** Dashboard loads but shows no usage data.

**Cause:** No supported clients found, or usage database is empty.

**Fix:**
1. Run diagnostics:
```bash
tokdash doctor
```
2. Check what's in the database:
```bash
tokdash db status
```
3. If the database is empty, force a sync:
```bash
tokdash db sync
```

## Update check fails

**Symptom:** `tokdash update` or the dashboard update check fails.

**Cause:** No network connectivity, or PyPI is unreachable.

**Fix:**
```bash
# Check network
tokdash update
```

Update checks are opt-in and off by default. `TOKDASH_UPDATE_CHECK=0` only disables the automatic check — it does not affect `tokdash update`.

## See also

- [Onboarding guide](ONBOARDING.md) — initial setup and `tokdash doctor`
- [Remote access guide](REMOTE_ACCESS.md) — reaching your instance from another machine
- [Configuration reference](../reference/CONFIG.md) — all environment variables
- [API reference](../reference/API.md) — HTTP API endpoints
