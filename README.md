<p align="center">
  <a href="README.md">English</a> &nbsp;|&nbsp; <a href="README_CN.md">中文</a> &nbsp;|&nbsp; <a href="README_JA.md">日本語</a> &nbsp;|&nbsp; <a href="README_KO.md">한국어</a> &nbsp;|&nbsp; <a href="README_ES.md">Español</a> &nbsp;|&nbsp; <a href="README_PT.md">Português</a>
</p>

# better-tokdash

**High-Performance Local Telemetry, Session Analytics & Interactive Conversation Inspector Dedicated to Hermes AI Agents**

`better-tokdash` is an overhaul of Tokdash engineered from the ground up for developer productivity, deep telemetry, and conversation visibility. While upstream Tokdash attempts to serve as a generic, shallow meter for 25+ diverse coding tools, **better-tokdash discards fragmented third-party integrations to focus exclusively on Hermes**.

By focusing 100% on Hermes, `better-tokdash` delivers rich conversational inspection, thinking/reasoning chain transparency, granular turn telemetry, gamified achievements, real-time CO2 footprint analysis, and modern UI capabilities.

---

## Why better-tokdash? (Hermes Specialization)

Upstream Tokdash attempts to spread itself thin across dozens of external CLI tools, resulting in flat, generic metrics and unverified connectors. 

**better-tokdash has no multiple third-party integrations.** We do not support Codex, Claude Code, OpenCode, Kimi, Cursor, or Cline. **Our sole, first-class integration is Hermes.**

By dedicating the architecture entirely to Hermes, `better-tokdash` provides:
- **First-Class Hermes Telemetry**: Deep parsing of Hermes agent sessions, message roles, tool dispatches, and multi-turn workflows.
- **Granular Turn Synthesis**: Over 70+ realistic conversation turns per session with precise input, cache-read, output, and reasoning token distributions.
- **Full Conversation Inspection**: Interactive chat mode ("View Chat") that allows you to inspect user prompts, assistant replies, reasoning traces, and tool execution status inside the dashboard.
- **Zero Configuration Bloat**: No noisy credential scrapers or background pollers for unused external providers.

---

## What We Have Done: Tokdash vs. better-tokdash

| Feature / Area | Upstream Tokdash | better-tokdash (Our Repo) |
|---|---|---|
| **Integration Focus** | Scattered across 25+ external tools with shallow metrics | **Exclusively specialized for Hermes** with deep telemetry |
| **Session Explorer** | Small, cramped modal with coarse aggregate data | **Near full-screen (`96vw`) responsive explorer** with deep turn telemetry |
| **Conversation Mode** | None (only token numbers and metadata) | **Interactive "View Chat" Mode**: Full conversation timeline rendering user and assistant message bubbles |
| **Reasoning Traces** | Not visible or parsed | **Collapsible thinking chain (`<details>`) drawers** showing full model reasoning and thinking paths |
| **Tool Execution Auditing**| Generic counter table | **Structured tool cards** with input arguments, output results, and model tool affinity bias tracking |
| **Session Turn Charts** | Flat mock lines; single view | **Responsive 3-column turn charts** with `ⓘ` hover tooltips and a dedicated full-screen graph expansion modal |
| **Hall of Achievements** | Empty/hidden badge counter (`100+`) | **104 badges across 6 categories** with live progress tracking, category filtering, search, and celebratory milestone confetti |
| **Milestone Celebrations** | None | **Interactive Canvas Particle Confetti engine** (`launchMilestoneConfetti`) triggered on achievement unlocks |
| **Carbon Footprint** | None | **Live CO2 Emissions calculation** (`tokens * 1.425e-7 kg CO2`) formatted dynamically in grams or kilograms |
| **Financial Benchmarking** | Basic historical cost sums | **"Price Saved" KPI card** comparing actual cost against commercial frontier models (~$15/M baseline) |
| **Token Composition** | Integer rounding | **High-precision 1-decimal percentage breakdowns** (e.g., `52.4%`) |
| **Model Intelligence** | Broken/stale data binding in tab views | **Fully bound model intelligence matrix** with stacked token volume breakdown charts |
| **Stats Heatmap** | Low-contrast white-on-white metric selector | **High-contrast, theme-aware surface controls** with smooth hover transitions |
| **Rhythm & Hourly Filter**| Shows full 24 hours including future unelapsed hours | **"Today" hourly breakdown filtered strictly up to the current elapsed hour** |
| **World Currencies** | Hardcoded to US Dollar ($) | **Top 100 World Currencies** with live conversion rates, currency symbols, and persistent storage |
| **Theme System** | Cluttered dropdown of 17 options | **Curated 4-theme picker** (*Dark Obsidian, Clean Light, Midnight Blue, Paper*) while maintaining compatibility with all **17 style themes** |
| **Navigation & Layout** | Crowded header; scattered buttons | **Relocated What's New, Export, and Settings to sidebar footer** with floating popovers; uniform 38px control height; right-aligned quick ranges |
| **Distraction Removal** | Static "LIVE" and "ACTIVE" badges | **Cleaned sidebar navigation** with distracting badges removed |
| **Motion & Animation** | Basic CSS transitions | **Spring physics animation system** powered by Anime.js with reduced-motion accessibility support |

---

## Detailed Features

### 1. Interactive Session Explorer & "View Chat"
Inside the Session Explorer, click on any Hermes session to open the full-screen inspector:
- **Overview & Graphs**: View the responsive 3-column chart grid:
  - *Tokens Per Turn*: Stacked bar chart showing Input, Cache Read, Output, and Reasoning tokens per turn.
  - *Cumulative Tokens*: Line graph showing token accumulation across turns.
  - *Cumulative by Time*: Line graph plotting token growth along the session timeline.
  - *Hover Tooltips & Full-Screen Expansion*: Click the expand icon on any graph to open it in `#graphExpandModal` for high-resolution analysis.
- **View Chat Mode**: Toggle seamlessly into conversational view to review:
  - User prompts in distinct indigo cards.
  - Assistant responses in purple cards.
  - Collapsible **Reasoning Process (Thinking Chain)** drawers revealing the model's inner thoughts.
  - Tool execution callouts displaying parameters and returned values.

### 2. Hall of Achievements (104 Badges & Milestone Confetti)
Track your Hermes agent operations with a complete gamified milestone matrix:
- **104 Badges** categorized across 6 domains:
  - 🚀 **Pioneer (18 badges)**: Telemetry setup, initial runs, multi-turn milestones, and autonomous sessions.
  - ⚡ **Token Titan (18 badges)**: Volume thresholds from 50k tokens up to 1 Billion tokens.
  - 💎 **Cost Slayer (18 badges)**: Cost savings achieved compared to frontier model benchmarks.
  - 🛠️ **Tool Sorcerer (18 badges)**: Autonomous tool execution counts from 5 to 100,000 tool calls.
  - 🧠 **Model Connoisseur (16 badges)**: Multi-model evaluation and cross-architecture benchmarking.
  - 👑 **Secret & Legendary (16 badges)**: Unique agent milestones (Night Owl, Cache Alchemist, Marathoner, etc.).
- **Live Filtering**: Filter by category, toggle "Unlocked Only", or search badge titles in real time.
- **Milestone Confetti**: Canvas particle physics animation triggers upon unlocking milestones and exploring achievements.

### 3. Environmental & Financial Intelligence
- **CO2 Footprint Tracking**: Every token processed consumes compute energy. `better-tokdash` automatically computes carbon impact in real time (`1.425e-7 kg CO2 / token`), displayed prominently in the Token Composition banner.
- **Price Saved Benchmark**: Quantifies how much money you have saved using efficient local or open weights via Hermes compared to commercial frontier models ($15/M tokens benchmark).

### 4. Global Currency Support & Curated Themes
- **Top 100 Currencies**: Select your local currency from the Settings menu (USD, EUR, GBP, JPY, CAD, AUD, INR, CHF, CNY, BRL, and 90 more) with automatic conversion rates and localized currency symbols.
- **Curated 4-Theme Picker**: Switch instantly between *Dark Obsidian (Classic)*, *Clean Light (Minimal)*, *Midnight Blue (Deep)*, and *Paper (System Default)*.

### 5. Streamlined Sidebar & Topbar Layout
- Topbar center controls (`#refreshBtn`, `#themeToggleQuick`, `.date-range-trigger`) now feature a uniform `38px` height.
- Quick date range selectors ("Today", "Yesterday", "7 Days", "30 Days") are neatly docked to the rightmost corner.
- "What's New", "Export", and "Settings" are placed at the bottom of the left sidebar, complete with floating popovers for quick JSON exports.

---

## Quick Start

### Prerequisites
- Python **3.10+**
- Local or networked **Hermes** session storage

### Installation

Clone the repository and install in editable mode:

```bash
git clone https://github.com/Dhruv1401/better-tokdash.git
cd better-tokdash
pip install -e .
```

### Running the Dashboard

Start the local server:

```bash
tokdash serve
```

Or run in foreground mode on a custom port:

```bash
tokdash serve --port 55423 --no-open
```

Open your browser to:
```text
http://127.0.0.1:55423
```

---

## Local API Endpoints

`better-tokdash` runs a fast, lightweight local HTTP server powered by FastAPI:

- `GET /api/usage?period=today|week|month|all`: Token totals, costs, model distributions, and cache savings.
- `GET /api/sessions?tool=hermes`: List all Hermes sessions with turns, tokens, and active time.
- `GET /api/sessions/hermes/{session_id}`: Full session payload with turns, message contents, reasoning traces, and tool calls.
- `GET /api/hermes/analytics`: Model-tool affinities, execution counts, and capability distribution.
- `GET /api/stats`: Daily heatmap and token contribution matrices.
- `GET /api/stream`: Real-time SSE event stream for live dashboard updates.
- `GET /health`: Health and status verification endpoint.

---

## Configuration

The dashboard works out-of-the-box with zero configuration, but supports environment variable overrides when needed:

- `TOKDASH_HOST` (default: `127.0.0.1`) — Bind address.
- `TOKDASH_PORT` (default: `55423`) — Port number.
- `TOKDASH_DATA_DIR` (default: `~/.tokdash`) — Local database and preferences directory.
- `TOKDASH_CACHE_TTL` (default: `600`) — Cache time-to-live in seconds.
- `TOKDASH_NO_RETENTION_NOTICE` (default: `1`) — Silence retention warning on startup.

---

## Project Structure

```text
better-tokdash/
├── main.py                     # Application entry point
├── tokdash                     # CLI script executable
├── src/
│   └── tokdash/
│       ├── api.py              # FastAPI routes and SSE streaming
│       ├── compute.py          # Token aggregation and pricing calculations
│       ├── sessions.py         # Hermes session extraction and turn synthesizer
│       ├── usage_store.py      # SQLite storage and session indexing
│       ├── pricing_db.json     # Model pricing matrix
│       └── static/
│           ├── index.html      # Single-page dashboard application
│           ├── css/            # Modular styles and themes
│           └── js/             # Interactive tabs, charts, animations, and confetti
├── tests/                      # Automated test suite (FastAPI TestClient & Pytest)
└── docs/                       # Architectural documentation
```

---

## License

This project is open-source software licensed under the [MIT License](LICENSE).
Original Tokdash project by Jingbiao Mei. Modernized and specialized for Hermes by Dhruv Jadav ([better-tokdash](https://github.com/Dhruv1401/better-tokdash)).
