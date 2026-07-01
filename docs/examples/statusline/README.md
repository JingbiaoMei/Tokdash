# Claude Code statusline templates

Drop-in [Claude Code statusline](https://docs.claude.com/en/docs/claude-code/statusline) scripts that pull live token & cost totals from a locally running Tokdash. They only ever issue **GET** requests to `http://127.0.0.1:55423`, so Tokdash's write-protection gate never blocks them, and they **fail silently** (just hide the 📊 segment) when Tokdash isn't running.

| Template | Output | Use it when |
|---|---|---|
| [`statusline-minimal.sh`](statusline-minimal.sh) | `[Claude Sonnet 4.6] 📁 myproject \| 📊 12.3M ($4.56) today` | You just want today's total on one line. Best starting point. |
| [`statusline-full.sh`](statusline-full.sh) | Four rows: model/branch/effort · context bar/session cost · dir/env/**tokdash today+week**/git · clock/rate-limits/**top-3 tools** | You want a full dashboard in the status bar. The tokdash parts are fenced in a clearly marked block you can keep or delete. |
| [`statusline.ps1`](statusline.ps1) | Same one-liner as `statusline-minimal.sh`: `[Claude Sonnet 4.6] 📁 myproject \| 📊 12.3M ($4.56) today` | You're running Claude Code natively on Windows and want the minimal template without installing `curl`/`jq`. See [Windows](#windows) below. |

## Requirements

- [`jq`](https://jqlang.github.io/jq/) and `curl` on your `PATH` (bash templates). On native Windows, `statusline.ps1` only needs PowerShell 5.1+ — `Invoke-RestMethod`/`ConvertFrom-Json` ship in the box, no extra install. See [Windows](#windows).
- Tokdash running locally (`tokdash serve`, or a `tokdash setup` background service).
- Claude Code **2.1.97+** for `refreshInterval` (older versions only repaint the statusline on each turn).

## Install

```bash
# 1. Copy your chosen template into place
mkdir -p ~/.claude/scripts
cp statusline-minimal.sh ~/.claude/scripts/statusline.sh   # or statusline-full.sh
chmod +x ~/.claude/scripts/statusline.sh
```

```jsonc
// 2. Add this to ~/.claude/settings.json
{
  "statusLine": {
    "type": "command",
    "command": "bash ~/.claude/scripts/statusline.sh",
    "refreshInterval": 30
  }
}
```

`refreshInterval` re-runs the script every N seconds so the totals stay live even while you're idle. Keep it ≥ Tokdash's cache TTL behavior in mind — every 30s is comfortable.

## Windows

Running Claude Code natively on Windows (not under WSL/Git Bash)? Use
[`statusline.ps1`](statusline.ps1) instead of the bash templates — it renders the same
minimal one-liner as `statusline-minimal.sh` using `Invoke-RestMethod` /
`ConvertFrom-Json`, which ship with PowerShell, so there's nothing extra to install.

### Install

```powershell
# 1. Copy the template into place
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.claude\scripts" | Out-Null
Copy-Item statusline.ps1 "$env:USERPROFILE\.claude\scripts\statusline.ps1"
```

```jsonc
// 2. Add this to %USERPROFILE%\.claude\settings.json
{
  "statusLine": {
    "type": "command",
    "command": "powershell -NoProfile -ExecutionPolicy Bypass -File \"%USERPROFILE%\\.claude\\scripts\\statusline.ps1\"",
    "refreshInterval": 30
  }
}
```

`-NoProfile` skips your PowerShell profile for a faster, cleaner start; `-ExecutionPolicy
Bypass` lets this one invocation run regardless of your machine's script execution
policy without changing that policy globally. If you use PowerShell 7+, `pwsh` works the
same way in place of `powershell`.

The same environment variables as the bash templates apply — set them with `$env:` instead
of `export`, e.g. in your PowerShell profile (`$PROFILE`):

```powershell
$env:TOKDASH_URL = "http://127.0.0.1:55423"
$env:TOKDASH_STATUSLINE_PERIOD = "today"
```

`statusline.ps1` is read-only, localhost-only, and fails silently (drops the 📊 segment)
if Tokdash isn't running or unreachable — the same fail-silent behavior as the bash
templates.

> Native Windows support for Tokdash itself (foreground `tokdash serve` and the
> Task Scheduler background service) is implemented in this branch but still awaiting
> real-Windows validation — see
> [Platform support](../../../README.md#platform-support) in the main README. This
> statusline template targets Claude Code running on Windows independently of that
> rollout; it only needs a reachable Tokdash `/api/usage` endpoint, wherever Tokdash is
> actually running.

## Configuration

All templates honor the same environment variables (set them in your shell profile, e.g.
`~/.bashrc`; on Windows, via `$env:` — see [Windows](#windows) above):

| Variable | Default | Meaning |
|---|---|---|
| `TOKDASH_URL` | `http://127.0.0.1:55423` | Tokdash base URL. Set this if you changed `TOKDASH_HOST` / `TOKDASH_PORT`. |
| `TOKDASH_STATUSLINE_PERIOD` | `today` | (minimal only) Which window to show: `today`, `3days`, `week`, `14days`, `month`, `year`, `all`. |

## What it queries

Both templates read from Tokdash's `/api/usage` endpoint — see [`docs/API.md`](../../API.md) for the full reference. The minimal template makes one request for the configured period; the full template makes two read-only requests (`today` and `week`):

```
GET /api/usage?period=today
```

Relevant response fields:

```jsonc
{
  "total_tokens": 12345678,      // → the 📊 token figure
  "total_cost": 4.56,            // → the ($x.xx) figure
  "by_tool": {                   // → the per-tool breakdown (full template)
    "claude":   { "tokens": 9800000, "cost": 3.10, "cache_hit_rate": 0.82 },
    "codex":    { "tokens": 1700000, "cost": 0.90 },
    "openclaw": { "tokens":  820000, "cost": 0.56 }
  }
}
```

Swap `period=today` for any window above to change what's shown. A quick way to preview the raw data:

```bash
curl -s 'http://127.0.0.1:55423/api/usage?period=today' | jq '{total_tokens, total_cost, by_tool}'
```

## Customizing

- **Different window:** change the `period=` value (minimal: set `TOKDASH_STATUSLINE_PERIOD`).
- **A single tool instead of the total:** read `.by_tool.claude.tokens` (or `codex`, `gemini`, `copilot`, `opencode`, `openclaw`, `kimi`, `pi`, `hermes`).
- **Trim the full template:** delete any of its four `echo -e` rows; the tokdash-specific logic lives between the `TOKDASH BLOCK` / `END TOKDASH BLOCK` markers.
- **Add cache hit rate:** each `by_tool` entry (and the header) carries `cache_hit_rate` (0–1).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| No 📊 segment ever appears | Tokdash isn't running, or `TOKDASH_URL` points at the wrong host/port. Test with the `curl` command above. |
| Statusline is blank / errors | `jq` not installed, or the script isn't executable. Run `bash ~/.claude/scripts/statusline.sh < /dev/null` to see errors. |
| Totals look frozen | Bump or add `refreshInterval`; without it the bar only repaints when Claude Code produces output. |
| Brief stalls when restarting Tokdash | Expected and bounded — each `curl -m 1` call waits at most 1s. The minimal template makes one Tokdash request; the full template makes two. |
| (Windows) Statusline blank / "cannot be loaded" error | Script execution policy blocked it — make sure the `command` in `settings.json` includes `-ExecutionPolicy Bypass` exactly as shown in [Windows](#windows). Run `powershell -NoProfile -ExecutionPolicy Bypass -File "%USERPROFILE%\.claude\scripts\statusline.ps1"` directly (with no piped stdin) to see the raw error. |

> These scripts are read-only and localhost-only. They never send your usage data anywhere — they just render what your local Tokdash already computed.
