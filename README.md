<p align="center">
  <a href="README.md">English</a> &nbsp;|&nbsp; <a href="README_CN.md">中文</a>
</p>

<p align="center">
  <a href="https://tokdash.github.io/"><img src="https://raw.githubusercontent.com/JingbiaoMei/tokdash/main/docs/assets/tokdash_logo_full.png" alt="Tokdash" width="420" /></a>
</p>

<p align="center">
  <b>Local token &amp; cost dashboard for AI coding tools</b>
</p>

<p align="center">
  <a href="https://opencode.ai/" title="OpenCode"><img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/agents/pills/opencode.png" alt="OpenCode" height="34"></a>
  <a href="https://openai.com/codex/" title="Codex"><img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/agents/pills/codex.png" alt="Codex" height="34"></a>
  <a href="https://www.claude.com/product/claude-code" title="Claude Code"><img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/agents/pills/claude.png" alt="Claude Code" height="34"></a>
  <a href="https://github.com/google-gemini/gemini-cli" title="Gemini CLI"><img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/agents/pills/gemini.png" alt="Gemini CLI" height="34"></a>
  <a href="https://openclaw.ai/" title="OpenClaw"><img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/agents/pills/openclaw.png" alt="OpenClaw" height="34"></a>
  <a href="https://github.com/MoonshotAI/kimi-cli" title="Kimi CLI"><img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/agents/pills/kimi.png" alt="Kimi CLI" height="34"></a>
  <a href="https://pi.dev/" title="Pi"><img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/agents/pills/pi.png" alt="Pi" height="34"></a>
  <a href="https://github.com/features/copilot" title="GitHub Copilot CLI"><img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/agents/pills/copilot.png" alt="GitHub Copilot CLI" height="34"></a>
  <a href="https://hermes-agent.nousresearch.com/" title="Hermes"><img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/agents/pills/hermes.png" alt="Hermes" height="34"></a>
</p>

<p align="center">
  <sub><a href="docs/SUPPORTED_CLIENTS.md">Supported clients &amp; where they log →</a></sub>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/FastAPI-009688?style=flat&logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=flat&logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/License-MIT-green?style=flat" alt="License" />
  <a href="https://tokdash.github.io/"><img src="https://img.shields.io/badge/Website-tokdash.github.io-1E40AF?style=flat&logo=githubpages&logoColor=white" alt="Website" /></a>
  <a href="https://tokdash.github.io/demo/"><img src="https://img.shields.io/badge/Live%20Demo-tokdash.github.io%2Fdemo-F59E0B?style=flat&logo=githubpages&logoColor=white" alt="Live Demo" /></a>
</p>

<p align="center">
  <b>Try it without installing → <a href="https://tokdash.github.io/demo/">tokdash.github.io/demo</a></b><br/>
  <sub>Click through the full UI (themes, date ranges, sessions, heatmap) backed by in-browser synthetic data — nothing is uploaded.</sub>
</p>

## Table of Contents

- [Live demo](#live-demo)
- [Features](#features)
- [Supported clients](docs/SUPPORTED_CLIENTS.md)
- [Platform support](#platform-support)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Privacy \& security](#privacy--security)
- [API (local)](#api-local)
- [Cost Accuracy Note](#cost-accuracy-note)
- [Roadmap](#roadmap)
- [Contributing / security](#contributing--security)
- [Project structure](#project-structure)
- [License](#license)

## Features

- **Exact token counts**: Input/Output/Cache token breakdowns
- **Statusline integration** *[new]*: drop a live token-usage indicator into Claude Code's statusline (or any agent that can hit a local HTTP endpoint) — see [Quick start](#statusline-integration)
- **Custom date ranges**: Flatpickr date picker + quick range buttons (Today, Last 7 Days, This Month, etc.)
- **Contribution calendar**: 2D heatmap + 3D isometric view with Tokens/Cost/Messages metrics
- **Session explorer**: per-session drill-down for Codex, Claude Code, and OpenCode
- **10 style themes**: Elevated, Classic, Vibrant, Midnight, Paper, Liquid, Terminal, Brutalist, Arcade, Studio
- **Light & dark mode**: auto-detects system preference, manual toggle
- **PWA support**: installable as a progressive web app

<p align="center">
  <a href="https://tokdash.github.io/demo/">
    <img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/demo.png" alt="Tokdash dashboard — click for live demo" width="900" />
  </a>
</p>
<p align="center">
  <a href="https://tokdash.github.io/demo/">
    <img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/demo-stats.png" alt="Tokdash stats & heatmap — click for live demo" width="900" />
  </a>
</p>

## Live demo

A static demo of the current dashboard is hosted at
**[tokdash.github.io/demo](https://tokdash.github.io/demo/)** — no install required.
(The project home page is **[tokdash.github.io](https://tokdash.github.io/)**.)

The demo runs the unmodified Tokdash frontend against an in-browser shim that
returns deterministic, fully synthetic data. You can:

- switch between Overview / Sessions / Stats / Pricing tabs,
- pick any date range (or the Today / 7-day / 30-day shortcuts),
- toggle light/dark and all 10 style themes,
- drill into a synthetic Codex / Claude Code / OpenCode session,
- browse the read-only pricing database.

Source for the demo lives at
[tokdash/tokdash.github.io](https://github.com/tokdash/tokdash.github.io).
Nothing is uploaded; nothing is read from your machine.

## Platform support

- **Linux (including WSL2):** supported
- **macOS:** experimental

## Quick start

### Prerequisites

- Python **3.10+**
- One or more [supported clients](docs/SUPPORTED_CLIENTS.md) installed

### Install (pip)

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

Binding to `0.0.0.0` is possible, but **not recommended**: it listens on all interfaces and can expose the dashboard beyond your LAN (VPN/Wi-Fi/etc.). Only do this if you understand the risk and have firewall/auth in place.

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

### Updating Tokdash

If you installed Tokdash with pip and are running it via systemd:

```bash
# 1. Upgrade the package
pip install --upgrade tokdash

# 2. Restart the systemd service to pick up changes
systemctl --user daemon-reload
systemctl --user restart tokdash

# 3. Verify the new version
pip show tokdash | grep Version
systemctl --user status tokdash --no-pager

# 4. Test the API is responding
curl 'http://127.0.0.1:55423/api/usage?period=today'
```

View logs if needed:
```bash
journalctl --user -u tokdash -f
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

### Statusline integration

The local API can power a statusline item in your coding agent (Claude Code, etc.) showing live token/cost stats. Hand your agent this prompt:

> *"I would like to add a statusline item from the tokdash endpoint's API; it should show the total tokens used today."*

Point it at [`docs/API.md`](docs/API.md) for endpoint details and let it wire the rest.

<p align="center">
  <img src="https://raw.githubusercontent.com/JingbiaoMei/Tokdash/main/docs/assets/demo-statusline.png" alt="Tokdash statusline integration example" width="900" />
</p>

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

By default `tokdash serve` opens the dashboard in your browser once on startup. Pass `--no-open` to disable this (it is also skipped automatically in headless/SSH environments and in the background service templates).

## Privacy & security

- **No telemetry**: Tokdash does not intentionally send your data anywhere.
- **Local parsing**: usage is computed from local session files (see [supported clients](docs/SUPPORTED_CLIENTS.md)).
- **Server exposure**: Tokdash binds to `127.0.0.1` by default. Prefer Tailscale Serve or SSH tunneling for remote access; avoid `--bind 0.0.0.0` unless you understand it listens on all interfaces and have firewall/auth in place.

## API (local)

Tokdash is a local HTTP server. Common endpoints:

- `GET /api/usage?period=today|week|month|N`
- `GET /api/usage?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD`
- `GET /api/tools?period=...` (coding tools only)
- `GET /api/openclaw?period=...` (OpenClaw only)
- `GET /api/sessions?tool=codex|claude|opencode&period=...`
- `GET /api/stats` (contribution calendar & statistics)

Example:
```bash
curl 'http://127.0.0.1:55423/api/usage?period=today'
```

Full API reference: [`docs/API.md`](docs/API.md) — schema, parameters, and response shapes for every endpoint.

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
│       ├── dateutil.py           # Shared date-range parsing
│       ├── sessions.py           # Session explorer logic
│       ├── pricing.py            # PricingDatabase wrapper
│       ├── assets.py             # Static asset management
│       ├── model_normalization.py
│       ├── pricing_db.json
│       ├── sources/
│       │   ├── openclaw.py       # OpenClaw session log parser
│       │   └── coding_tools.py   # Local coding tools parsers
│       └── static/
│           ├── index.html        # Single-page dashboard
│           ├── theme-config.js   # Theme palettes & heatmap colors
│           └── themes.css        # Per-theme CSS overrides
└── docs/                   # Roadmap + background-run docs + agent prompts
```

## License

MIT License - see `LICENSE`.
