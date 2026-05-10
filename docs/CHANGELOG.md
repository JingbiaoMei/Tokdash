# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

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
