# Tokdash Roadmap / Notes

_Last updated: 2026-02-28_

## Goals
- **Easy install**: `pip install tokdash` (no Docker on the roadmap for now).
- **Easy run**: `tokdash serve` to start the local dashboard.
- **Accurate accounting**: only emit usage when clients provide **explicit** token fields.
- **Safe defaults**: bind to localhost by default; no surprise LAN exposure.

## Current state (today)
- FastAPI backend in:
  - `src/tokdash/api.py` (routes/app; serves `src/tokdash/static/index.html` + `/api/*`)
  - `src/tokdash/compute.py` (aggregation/merging logic)
- Local parsers in `src/tokdash/sources/coding_tools.py`:
  - ✅ OpenCode
  - ✅ Codex
  - ✅ Claude Code
  - ✅ Gemini CLI
  - 🟡 Amp (placeholder)
- OpenClaw parser in `src/tokdash/sources/openclaw.py` (reads `~/.openclaw/agents/*/sessions`).
- Local pricing DB: `src/tokdash/pricing_db.json`.

## Packaging plan (pip)
_Target UX:_
- `pip install tokdash`
- `tokdash serve` → open `http://127.0.0.1:55423`

_Phased approach:_
1. Add `pyproject.toml` and a `src/tokdash/` package layout.
2. ✅ Moved backend + parsers into `src/tokdash/` and removed `sys.path` hacks.
3. Bundle `static/` + `pricing_db.json` as package data (setuptools package-data).
4. ✅ Added a small CLI (`tokdash`) with subcommands:
   - `tokdash serve` (host/port/CORS/cache-ttl flags; env vars still supported)
   - `tokdash export --json` (one-shot terminal output)
5. Keep `python3 main.py` as a compatibility entrypoint temporarily (then deprecate).
6. ✅ Added GitHub Actions Trusted Publishing (OIDC) to publish to PyPI on version tags.

## Serving / background process
✅ We **document** background options but do not auto-install services. See `docs/agents/systemd/BACKGROUND_RUN.md`.

- **Linux (recommended):** systemd *user* service template.
- **macOS:** launchd plist template.
- **Cross-platform fallbacks:** `tmux`, `nohup`, `screen`.
- Templates:
  - `docs/agents/systemd/templates/tokdash.service`
  - `docs/agents/systemd/templates/com.tokdash.tokdash.plist`

## Terminal mode (interactive TUI)
Decision: build a **full interactive** terminal UI (like `nvitop`/`nvtop`), as an optional extra.

- Command: `tokdash tui`
- Dependency: `tokdash[tui]` (keep core install minimal)
- Implementation: `textual`-based (interactive tables, keybindings, live refresh)
- v1 scope:
  - Period selector (today/week/month/N days)
  - Views: Overview / Tools / Models
  - Search + sort + drilldown (tool → model)
  - Auto-refresh toggle + manual refresh
  - Export current view to JSON (file/stdout)
- Non-goals (v1): charts/3D views; keep it fast and table-first

## Client / IDE support
Principle: **no inference**. Only emit entries when numeric token fields exist.

- Cursor:
  - Not supported right now.
  - Current best-known approach requires copying a browser session cookie/token (unsafe) and calling unofficial Cursor web APIs (unstable).
  - We will revisit only if Cursor exposes a safer official mechanism or reliable local artifacts with explicit token fields.
- VS Code extensions (Continue/Cline/Roo/Windsurf/Amazon Q):
  - Probe VS Code storage dirs for explicit token fields (JSON/SQLite).

## README / polish
- Add a clear “Supported clients” matrix (✅ implemented vs 🟡 placeholder).
- Create a Tokdash logo (AIGC is fine) and set dashboard favicon to it.
- Add supported-client logos/badges (probably via shields.io) and a security note (LAN binding + CORS).
- Add UI demo assets:
  - screenshots in `docs/assets/` (overview + stats)
  - optional short GIF of refresh + scrolling

## Performance
Symptom: browser becomes less responsive while refreshing/scrolling.

Likely causes:
- Recreating Chart.js objects each refresh (and animating).
- Large DOM updates (apps breakdown + combined models) happening immediately.
- Overlapping refreshes (manual + auto refresh).

Fixes to keep:
- Reuse Chart.js instances and disable animations.
- Defer big table renders via `requestIdleCallback`.
- Cap combined-model rows by default with a “Show all” toggle.
- Skip Overview DOM work while user is on Stats.

## Pricing DB updates
- Keep `src/tokdash/pricing_db.json` open-source.
- Add a periodic pricing updater/scraper (in a separate private repo) to scrape OpenAI/OpenRouter/etc and regenerate `pricing_db.json` (bump `version` + `lastUpdated`), then open a PR here.
  - Keep schema/validator tests in the updater repo; Tokdash only consumes the generated JSON.

## Cleanup candidates
- Audit static assets for unused files.
- Delete local `__pycache__/` directories (already gitignored).
- Keep `requirements-dev.txt` (optional) as a pinned convenience file for contributors.

## Open questions
- Publish to PyPI immediately, or start with `pip install git+https://...`?
- If we allow `0.0.0.0`, do we want an auth token / basic auth option?
- Do we want Windows path support in v1?
