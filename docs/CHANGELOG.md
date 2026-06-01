# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Added
- **Install button for the PWA.** When the dashboard is installable (Chromium browsers, served with the manifest + service worker), an **Install** button appears in the header toolbar so you can pin Tokdash as a desktop/mobile app in one click. It hides itself automatically when the app is already installed or when the browser exposes no install prompt (e.g. iOS Safari).

### Changed
- Renamed the **pi-agent** client to **Pi** across the dashboard and docs. The detection path (`~/.pi/agent/sessions/`) and the `PI_AGENT_DIR` override are unchanged — this is a display-name change only.
- README (English + 中文): added an agent logo strip under the tagline and moved the detailed client list + log paths to [`docs/SUPPORTED_CLIENTS.md`](SUPPORTED_CLIENTS.md). Demo links now point at `tokdash.github.io/demo/` (the root `tokdash.github.io` is the project home page).

## 0.5.1 - 2026-06-01

### Added
- **`tokdash serve` now opens the dashboard in your browser on startup**, with a new `--no-open` flag to disable it. Auto-open is skipped automatically in headless contexts — CI (`CI` env var), SSH sessions (`SSH_CONNECTION`/`SSH_TTY`), and Linux without an X11/Wayland display — and the bundled systemd/launchd service templates now pass `--no-open`. The browser launch fires from a short-delay daemon timer so the server is listening before the page loads, and any failure to open is swallowed so it can never take down the server. (Thanks @KurokawaShiorei for the original contribution in #5.)

## 0.5.0 - 2026-05-30

### Added
- **Sortable columns in the Overview breakdown tables.** Tools Breakdown, Apps & Models Breakdown, and Combined Models now support click-to-sort on every column, mirroring the Sessions ranking: click a header to sort, click again to flip direction (numeric columns rank high→low first, the name column A→Z), with a ▲/▼ indicator on the active column. In Apps & Models all per-app sub-tables sort in lockstep, and the active-sort indicator survives a language toggle. The Combined Models list sorts before its top-N cap, so the cap reflects the chosen sort.

### Fixed
- **Apps & Models Breakdown column alignment.** Each app renders as its own table, which under auto layout sized columns independently so they did not line up across apps. The sub-tables now use a fixed layout with a shared column template, so Input/Output/Cache/… align vertically across every app (long model names ellipsize with a hover tooltip and the table scrolls horizontally on very narrow screens).
- **Overview "Total Tokens" overflow under wide date ranges.** Large totals (e.g. hundred-million+ under "This Year") overflowed the narrow KPI card. The Total Tokens / Total Cost / Total Messages values now shrink to fit their card only when the number would actually overflow — measured against the real card width, and re-fit on resize. Normal/small values are unchanged.

## 0.4.1 - 2026-05-30

### Fixed
- **`period=all` / `period=year` silently returned today only.** `period_to_days()` mapped every unrecognised named period — including `all` and `year` — to 1 day, so `/api/usage?period=all` and `?period=year` returned just the current day's data and looked like a large undercount. Named periods now resolve correctly (`year` → 365 days, `all` → all-time), and any unknown period defaults to all-time (which visibly over-reports) rather than collapsing to today. The dashboard UI was unaffected — it sends explicit `date_from`/`date_to` ranges — so this only bit direct API callers.
- **`/api/sessions` and `/api/usage` disagreed on named periods.** `sessions.py` carried its own copy of the period→days mapping that still collapsed `year`/`all`/unknown to today, so `/api/sessions?period=all` behaved like today while `/api/usage?period=all` spanned all-time. `sessions` now delegates to the single canonical mapping in `compute`, keeping both endpoints consistent (with a regression test locking the alignment).

### Changed
- Polished the README header (English + 中文): the logo, tagline, badges, and demo callout are now centered, and the wordmark logo serves as the title (the redundant text heading was removed).

## 0.4.0 - 2026-05-29

### Added
- **Cache Hit Rate** across the dashboard. The metric is the token-weighted share of *prompt input* served from cache — `cacheRead / (input + cacheWrite + cacheRead)` — matching the published definitions of DeepSeek (`prompt_cache_hit_tokens / prompt_tokens`), Anthropic, OpenAI, and Gemini. Cache *writes* (cache creation) count as misses (they are prompt input not served from cache), and output/reasoning tokens are excluded. It appears as: an `Avg Cache Hit Rate` KPI card on the Overview header; a `Hit %` column in the Tools Breakdown, Apps & Models, and Combined Models tables (and per-app in the breakdown headers); a `Hit %` column plus a per-session figure in the Sessions tab (Codex/Claude/OpenCode/Combined); and a `Cache Hit Rate` figure in the Stats Month panel and the Day Details panel (Month and Year tabs). Sources that do not report cache data show `n/a`. Backend exposes `cache_hit_rate` on `/api/usage` (header + `by_tool` + `combined_models`), `/api/tools`, `/api/openclaw`, and `/api/sessions` / `/api/session` (per-session and per-turn).

### Fixed
- **Gemini CLI token & cost double-count.** Gemini CLI logs `tokens.input` *inclusive* of the cached prompt tokens (`tokens.cached`), but the parser previously also added `cached` separately as cache-read, counting those tokens twice in totals and cost on every cache-hit turn. The parser now subtracts (`input = tokens.input − tokens.cached`) to recover the fresh/uncached portion — matching how the Codex and Copilot parsers already handle cache-inclusive input. Effect: Gemini CLI total tokens and cost now match Gemini's own reported `total` (they decrease for sessions with cache hits); all other tools are unaffected. The session-level `cache_ratio` (cacheRead ÷ all tokens, incl. output) is retained for back-compat but is no longer surfaced as a hit rate; the Sessions panel now shows the faithful `cache_hit_rate`.

## 0.3.3 - 2026-05-29

### Added
- Added Claude Opus 4.8 pricing entry (`claude-opus-4.8`) with the same rates as Opus 4.7 (input $5 / output $25 per 1M; cache read $0.50 / cache write $6.25), plus an `opus-4.8` shorthand alias. Names such as `claude-opus-4-8` and `opus-4.8` normalize to the canonical entry.

## 0.3.2 - 2026-05-26

### Fixed
- Claude Code sessions from third-party builds that write a zero-token placeholder entry before the real assistant entry (sharing the same `message.id`) are no longer silently dropped. The deduplication step now ignores placeholders so the real usage gets counted. In practice this restores token, cost, and session-count data from `~/.claude-mi` (mimo-v2.5) and `~/.claude-infini` (glm-5.1) installs; the official `~/.claude` build is unaffected.

## 0.3.1 - 2026-05-25

### Added
- Added Xiaomi MiMo V2.5 pricing entries: `mimo-v2.5` (input $0.40 / output $2 per 1M) and `mimo-v2.5-pro` (input $1 / output $3 per 1M), matching OpenRouter's published rates.
- Added a `Monthly Totals` table below the Year heatmap on the `Stats` tab, showing per-month total tokens, total cost, and total energy for the selected year (Jan through the current month for the current year; full year otherwise).
- Added `Total Tokens` and `Energy` columns to the `Models Used` table in the Day Details panel.
- Added click-to-navigate from the Year view to the Month view: clicking a month label above the year heatmap, or a row in the Monthly Totals table, jumps to that month.

### Changed
- Reorganized the Month Stats sidebar into a 2-column grid so the panel takes roughly half the previous vertical space.

### Fixed
- Year heatmap previous/next arrow buttons now update the title and grid immediately on click instead of waiting for the async year-stats fetch to complete, so rapid back-to-back clicks register correctly.

## 0.3.0 - 2026-05-21

### Added
- Added support for pi-agent token usage parsing from ~/.pi/agent/sessions/. Override the location via the `PI_AGENT_DIR` env var (comma-separated list of dirs). Captures input/output/cache tokens and per-message cost when present.
- Added support for Hermes agent token usage parsing from ~/.hermes/state.db. Override the location via the `HERMES_HOME` env var. Reads session-level aggregates including per-session message counts, reasoning tokens, and recorded cost (with pricing-table fallback for subscription-included sessions where Hermes records a zero cost).
- Added support for GitHub Copilot CLI token usage. Full input/cache/reasoning/cost data is read from OpenTelemetry exporter JSONL at ~/.copilot/otel/ or the file pointed at by `COPILOT_OTEL_FILE_EXPORTER_PATH`. For sessions without OTel enabled, output-only token counts are recovered from ~/.copilot/session-state/*/events.jsonl as a fallback.
- Added [`docs/API.md`](API.md) — full HTTP API reference for the Tokdash server, intended for building external integrations (e.g. Claude Code statusline items, IDE plugins, custom dashboards).

### Notes
- To capture full GitHub Copilot CLI usage (input + cache + cost), set `COPILOT_OTEL_FILE_EXPORTER_PATH` in your shell profile before launching the Copilot CLI; e.g. `export COPILOT_OTEL_FILE_EXPORTER_PATH="$HOME/.copilot/otel/usage.jsonl"`. Without this, Tokdash will still surface output-token counts from the local events log.
- The Sessions tab does not yet support pi-agent, GitHub Copilot CLI, or Hermes — these agents currently appear only in Overview/Stats aggregates. Per-session drill-down is planned for a follow-up.
- **Statusline integration**: Tokdash's local HTTP API can power a Claude Code (or any other agent) statusline item showing live token/cost stats. Hand your coding agent the prompt below, plus [`docs/API.md`](API.md) for endpoint details:
  > *"I would like to add a statusline item from the tokdash endpoint's API; it should show the total tokens used today."*

## 0.2.7 - 2026-05-20

### Added
- Added local benchmark scripts for parser-cache and API endpoint latency checks.

### Fixed
- Included all local `.claude*` project directories when parsing Claude Code usage and session drill-down data, so alternate Claude installs are counted with the default `~/.claude/projects` logs.

## 0.2.6 - 2026-05-11

### Changed
- Updated the Tokdash logo across the dashboard header, PWA icons, and README assets.

## 0.2.5 - 2026-05-10

### Added
- Added an opt-in **Energy** metric on the `Stats` tab: a `Total Energy (kWh)` row in the Month Stats sidebar, an `Energy` field in the Day Details modal, and a fourth `Energy` button in the Daily Activity metric switcher that recolors the heatmap, 3D cubes, and Peak Day / Peak Week / Peak Weekday / Avg-Active-Day insight cards. `Overview`, `Sessions`, `Pricing`, and `/api/*` responses are unchanged.
- Energy is estimated entirely in the frontend from the existing token breakdown using model-family `(prefill, cached, decode)` Joule-per-token coefficients derived from TokenPowerBench (AAAI 2026) and "How Hungry is AI?" (Jegham et al., 2025). Order-of-magnitude accuracy; intended for relative trends rather than absolute reporting. Month totals are shown in kWh; day details and metric values auto-format as Wh or kWh.

## 0.2.4 - 2026-04-24

### Added
- Added a dashboard `Pricing` tab, contributed by StormTian, for viewing, formatting, validating, reloading, and saving the packaged `pricing_db.json` from the local Tokdash UI.
- Added `/api/pricing-db` read and write endpoints with JSON parsing, schema-shape validation, atomic file replacement, and test coverage for valid saves, invalid JSON, and missing `models` data.
- Added `gpt-5.5` pricing support to the local pricing database and release-safe contract tests.
- Added `deepseek-v4-pro` pricing from OpenRouter at `$1.74` input / `$3.48` output per million tokens.
- Added `deepseek-v4-flash` pricing from OpenRouter at `$0.14` input / `$0.28` output per million tokens.
- Added `kimi-k2.6` Moonshot AI pricing at `$0.95` input / `$4.00` output / `$0.16` cache-read per million tokens, including `k2p6`, `k2-6`, `kimi-2.6`, `kimi2.6`, and `moonshot-ai/kimi-k2.6` aliases.

### Changed
- Normalized saved pricing JSON through the editor API so dashboard edits produce stable, readable formatting before replacing the on-disk database.
- Expanded Kimi model normalization so K2.6 variants group under `kimi-k2.6` without collapsing into the existing `kimi-k2.5` dashboard bucket.
- Extended pricing contract coverage so newly added DeepSeek V4 and Kimi K2.6 entries are verified through the same `PricingDatabase` lookup path used at runtime.

### Fixed
- Cleared cached API responses after pricing database saves so refreshed dashboard views use the updated pricing file.
- Reloaded the session-level pricing database and cleared parsed session caches after pricing edits, preventing already-parsed Codex, Claude Code, and OpenCode session detail costs from staying stale until process restart.

## 0.2.3 - 2026-04-16

### Added
- Added `claude-opus-4.7` pricing to the local pricing database with the same rates as `claude-opus-4.6`, plus `opus-4.7` shorthand alias coverage.

## 0.2.2 - 2026-04-15

### Added
- Added regression coverage for OpenClaw's inner message timestamps and archived/checkpoint transcript discovery.

### Changed
- Reworked coding-tool parsing caches so repeated API requests can reuse short-lived file signatures, shared parser results, and bounded OpenCode query caches instead of rescanning logs for each date switch.

### Fixed
- Updated OpenClaw date filtering to prefer each assistant message's inner `message.timestamp`, with fallback to the outer entry timestamp and file mtime, matching current OpenClaw transcript semantics more closely.
- Restored OpenClaw scanning for archived `.jsonl.deleted.*`, `.jsonl.reset.*`, and checkpoint `.jsonl` transcripts while still excluding `.lock` files.

## 0.2.1 - 2026-04-09

### Added
- Added `Paper`, `Liquid`, `Vibrant`, `Midnight`, `Terminal`, `Brutalist`, `Arcade`, and `Studio` dashboard style themes, with localized labels in English and Chinese.
- Added a dedicated `docs/RELEASING.md` checklist and linked it from `docs/CONTRIBUTING.md` so the manual tag, push, GitHub Release, and verification steps stay documented.

### Changed
- Moved theme-specific palettes and overrides out of `src/tokdash/static/index.html` into standalone static assets, reducing dashboard-shell sprawl and making future theme work easier to maintain.
- Expanded the style selector into a broader theme gallery while keeping light/dark mode compatibility across the dashboard.

### Fixed
- Fixed charts, heatmaps, and browser `theme-color` metadata to stay synchronized with the selected style theme in both light and dark mode.

## 0.2.0 - 2026-04-09

### Added
- Added calendar-based custom date range selection with quick presets spanning `Yesterday`, rolling day/week windows, month presets, and year presets.
- Added a `Style` selector in the dashboard header with `Classic` and `Elevated` presentation modes, alongside the existing light/dark theme toggle.
- Added `GLM-5.1` pricing and alias resolution (`glm5.1`, `glm-5-1`, `z-ai/glm-5.1`, `zhipu/glm-5.1`) to the local pricing database.

### Changed
- Reworked the dashboard header controls so the date picker, quick-range actions, refresh button, language toggle, theme toggle, and style selector align more cleanly across desktop widths.
- Expanded packaged static assets to include the full `static/` tree, ensuring icons, manifest assets, and service-worker resources ship with the installed package.
- Switched service-worker cache versioning to a content-derived cache name so upgraded installs pick up new static assets more reliably.

### Fixed
- Fixed custom date-range requests to serialize local calendar dates correctly instead of drifting backward in UTC-positive timezones.
- Fixed API validation for incomplete, malformed, and reversed `date_from` / `date_to` query pairs.
- Applied no-cache headers consistently to the dashboard shell, service worker, manifest, and static assets to reduce stale-client behavior after upgrades.
- Hardened release metadata validation so packaging checks continue to work with the current static-version layout and remain compatible with future dynamic-version setups.

## 0.1.0 - 2026-03-31

### Changed
- Promoted tokdash to its first minor release after stabilizing the new multi-tool Sessions workflow introduced in `0.0.13`.
- Refined the Sessions tables with aligned grouped summary rows so headers, project summaries, and nested session rows line up consistently across Codex, Claude Code, OpenCode, and combined views.
- Added click-to-sort ranking on the session tables for numeric and time columns: input, cache, output, total tokens, cost, and last updated.

### Fixed
- Fixed grouped project ordering so project rows now follow the active selected sort mode instead of staying token-sorted underneath a different header state.
- Fixed `Last updated` sorting to compare real timestamps instead of plain strings.
- Fixed GitHub CI to install dev requirements before running tests, ensuring `httpx` is available for the API smoke test path.

## 0.0.13 - 2026-03-31

### Added
- Added a dedicated `Sessions` page with Codex, Claude Code, OpenCode, and combined cross-tool session views.
- Added per-session drill-down charts, including cumulative token trends over turn order and over time.
- Added `Total Messages` to the Overview KPI bar, alongside period-over-period comparisons for tokens, cost, and messages.

### Changed
- Moved session analysis out of the Overview page so the top-level dashboard stays focused on aggregate usage.
- Changed comparison semantics to use prior full calendar blocks: `today` now compares to the full previous day, fixed `N`-day ranges compare to the previous full `N` days, and `month` compares to the full previous calendar month.

### Fixed
- Fixed Claude Code session undercounting by merging subagent transcript files that share the same session ID.
- Removed the OpenCode session display cap so long-range views no longer hide many sessions.
- Replaced the old Codex-only session backend path with the shared multi-tool session API used by the new dashboard.
- Added the explicit `httpx` dev dependency required by the API smoke tests and removed stale dead code from the previous Codex-only implementation.

## 0.0.11 - 2026-03-20

### Fixed
- Restored the multilingual README setup with cross-links between the English and Chinese docs.
- Added `README_CN.md` as the Chinese project README.
- Restored dashboard language switching between English and Chinese, with browser-language detection used as the default.
- Restored automatic night mode plus a manual light/dark toggle in the dashboard.
- Preserved the current Stats calendar view when switching language or theme.

## 0.0.10 - 2026-03-20

### Reverted
- Removed the unmerged multilingual README additions and deleted the Chinese README variant.
- Reverted the dashboard language toggle, browser-language auto-selection, automatic night mode, and manual light/dark theme toggle to restore the previous light-only UI.

## 0.0.9 - 2026-03-16

- Renamed the Kimi tool label to `Kimi CLI` in the dashboard.
- Sorted Tools Breakdown views by token count in descending order.
- Bumped the package version to `0.0.9`.

## 0.0.8 - 2026-03-16

### Pricing DB
- Major pricing database overhaul: 61 models -> 137 models across 8 providers.
- Added DeepSeek (11 models), Xiaomi/MiMo (1 model) as tracked providers.
- Updated all existing model prices from OpenRouter + official provider USD pricing pages (docs.z.ai, platform.minimax.io, platform.moonshot.ai, api-docs.deepseek.com).
- Applied conservative `max(openrouter, official)` pricing policy: GLM-5 $0.72->$1.00, Kimi K2.5 $0.45->$0.60, etc.
- Corrected cache pricing for OpenAI (50% read), Anthropic (10% read / 125% write), Kimi (flat $0.15 read) using official rates instead of generic heuristics.
- Added many new OpenAI models (o3, o4-mini, gpt-5-pro, gpt-5.4-pro, gpt-4.1-nano, gpt-3.5-turbo, gpt-4-turbo, etc.), Anthropic models (claude-opus-4.1, claude-sonnet-4, claude-haiku-4.5, claude-3.5-haiku, etc.), Google Gemini models (gemini-2.5-pro, gemini-2.5-flash, gemini-3.1-pro, etc.), and Z.ai models (glm-5-turbo).

### Testing
- Added `tests/test_pricing_db_contract.py`: consumer contract test verifying manual models, aliases, derived models, and per-provider resolution survive pricing DB updates.

## 0.0.7 - 2026-03-06

- Added Kimi CLI accounting support by parsing `~/.kimi/sessions/*/*/wire.jsonl` StatusUpdate events.
- Registered Kimi as a default coding-tools source and documented the supported Kimi session path in the README.
- Added a regression test for the Kimi parser and support for overriding the Kimi data directory with `KIMI_SHARE_DIR`.
- Documented the current Kimi billing-model assumption (`kimi-for-coding` -> `kimi-k2.5`) in code for future timestamp-based model rollovers.

## 0.0.6 - 2026-03-05

- Added GPT-5.4 pricing support to the local pricing database.
- Bumped the package version to `0.0.6`.

## 0.0.1 - 2026-02-25

- Initial PyPI packaging (`pyproject.toml`) + `tokdash` CLI (`tokdash serve`, `tokdash export`).
- FastAPI server serving a local dashboard and `/api/*` endpoints.
- Local parsers for OpenCode, Codex, Claude Code, Gemini CLI, and OpenClaw sessions.
