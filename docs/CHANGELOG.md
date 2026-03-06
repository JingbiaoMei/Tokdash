# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

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
