# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

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
