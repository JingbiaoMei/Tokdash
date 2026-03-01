# Tokdash

Local token & cost dashboard for AI coding tools (Codex, OpenCode, Claude Code, Gemini CLI, OpenClaw, etc.).

![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=flat&logo=fastapi&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=flat&logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green?style=flat)

## Features

- **Hierarchical breakdown**: app → model with full token precision
- **Multiple data sources**: local session files + optional `tokscale` fallback
- **Exact token counts**: Input/Output/Cache token breakdowns
- **Flexible ranges**: today / week / month / N days
- **Contribution calendar**: 2D heatmap + 3D isometric view

<p align="center">
  <img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/demo.png" alt="Tokdash dashboard demo" width="900" />
</p>

## Supported clients (explicit token fields)

✅ Supported:
- **OpenCode**: `~/.local/share/opencode/`
- **Codex**: `~/.codex/sessions/`
- **Claude Code**: `~/.claude/projects/`
- **Gemini CLI**: `~/.gemini/tmp/*/chats/session-*.json`
- **OpenClaw**: `~/.openclaw/agents/*/sessions/`

## Platform support

- **Linux (including WSL2):** supported
- **macOS:** experimental

## Quick start

### Prerequisites

- Python **3.10+**
- One or more supported clients installed (above)

### Install (pip)

From PyPI (after the first public release):

```bash
pip install tokdash
tokdash serve
```

Open: `http://localhost:55423`

### Run (from source)

```bash
pip install -e .

# Option A: run directly
python3 main.py

# Option B: CLI wrapper (same server)
./tokdash serve
```

Open: `http://localhost:55423`

If port conflicts:
- `python3 main.py --port <port>`
- `./tokdash serve --port <port>`

If you want to access Tokdash from another device (recommended):
- Tailscale Serve (private to your tailnet): `tailscale serve 55423`
- SSH port-forward: `ssh -L 55423:127.0.0.1:55423 <user>@<host>`

Binding to `0.0.0.0` is possible, but **not recommended**: it listens on all interfaces and can expose the dashboard beyond your LAN (VPN/Wi‑Fi/etc.). Only do this if you understand the risk and have firewall/auth in place.

### Run in background

See `docs/agents/systemd/BACKGROUND_RUN.md` for:
- Linux systemd (user service) template
- macOS launchd (LaunchAgent) template

#### For Humans

Copy and paste this prompt to your LLM agent (Claude Code, AmpCode, Cursor, etc.):

```text
Install and configure Tokdash to run in the background by following the instructions here:
https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/agents/systemd/AGENTS.md

Or read the Background Run guide, but seriously, let an agent do it.
```

#### For LLM Agents

Fetch the installation guide and follow it:

```bash
curl -s https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/agents/systemd/AGENTS.md
```

### OpenClaw digest (scheduled reports)

Tokdash can power daily/weekly/monthly OpenClaw usage reports by querying the local API on a schedule.

#### For Humans

Copy and paste this prompt to your LLM agent (Claude Code, AmpCode, Cursor, etc.):

```text
Install and configure scheduled Tokdash usage reports for OpenClaw by following the instructions here:
https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/agents/openclaw_reporting/AGENTS.md

Or read the guide yourself, but seriously, let an agent do it.
```

#### For LLM Agents

Fetch the installation guide and follow it:

```bash
curl -s https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/agents/openclaw_reporting/AGENTS.md
```

## Configuration

Tokdash is **localhost-only by default**.

- `TOKDASH_HOST` (default: `127.0.0.1`)
- `TOKDASH_PORT` (default: `55423`)
- `TOKDASH_CACHE_TTL` (default: `120` seconds)
- `TOKDASH_ALLOW_ORIGINS` (comma-separated, default: empty)
- `TOKDASH_ALLOW_ORIGIN_REGEX` (default allows only localhost/127.0.0.1)

Example (remote access via Tailscale Serve; recommended):

```bash
tokdash serve --bind 127.0.0.1 --port 55423
tailscale serve --bg 55423
```

## Privacy & security

- **No telemetry**: Tokdash does not intentionally send your data anywhere.
- **Local parsing**: usage is computed from local session files (see “Supported clients” paths above).
- **Server exposure**: Tokdash binds to `127.0.0.1` by default. Prefer Tailscale Serve or SSH tunneling for remote access; avoid `--bind 0.0.0.0` unless you understand it listens on all interfaces and have firewall/auth in place.

## API (local)

Tokdash is a local HTTP server. Common endpoints:

- `GET /api/usage?period=today|week|month|N`
- `GET /api/tools?period=...` (coding tools only)
- `GET /api/openclaw?period=...` (OpenClaw only)

Example:
```bash
curl 'http://127.0.0.1:55423/api/usage?period=today'
```

## Cost Accuracy Note

Token counts depend on what each client logs locally. Costs are computed from `src/tokdash/pricing_db.json` and may lag real provider pricing — use as an estimate and verify against your billing source if it matters.

## Roadmap

See `docs/ROADMAP.md`.

## Contributing / security

- Contributing guide: `docs/CONTRIBUTING.md`
- Security policy: `docs/SECURITY.md`

## Project structure

```
tokdash/
├── main.py                 # Source entrypoint (python3 main.py)
├── tokdash                 # Source CLI wrapper (./tokdash serve)
├── src/
│   └── tokdash/
│       ├── cli.py
│       ├── api.py                # FastAPI routes/app
│       ├── compute.py            # Aggregation/merging logic
│       ├── pricing.py            # PricingDatabase wrapper
│       ├── model_normalization.py
│       ├── pricing_db.json
│       ├── sources/
│       │   ├── openclaw.py       # OpenClaw session log parser
│       │   └── coding_tools.py   # Local coding tools parsers
│       └── static/
│           └── index.html
└── docs/                   # Roadmap + background-run docs + agent prompts
```

## License

MIT License - see `LICENSE`.
