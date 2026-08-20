# Tokdash docs

## Root

- [Contributing](CONTRIBUTING.md) — how to propose changes and the manual release checklist pointer.
- [Security policy](SECURITY.md) — how to report a vulnerability and the write-protection model.

## guides/ — task-oriented setup guides

- [Onboarding](guides/ONBOARDING.md) — `setup`, `doctor`, `update`, and `uninstall`, Tokdash's Python-native service lifecycle.
- [Remote access](guides/REMOTE_ACCESS.md) — reaching a Tokdash instance from another machine (Tailscale Serve, SSH forwarding, wildcard binding).
- [Statusline templates](guides/statusline/README.md) — ready-made Claude Code statusline scripts (bash + PowerShell) that read local Tokdash totals.
- [Background service & agents](guides/agents/systemd/BACKGROUND_RUN.md) — run Tokdash as a systemd/launchd service, the health-probe auto-restart, and the OpenClaw reporting cron.

## reference/ — lookup material

- [API reference](reference/API.md) — the local HTTP API (FastAPI) for token usage, costs, and session data.
- [Supported clients](reference/SUPPORTED_CLIENTS.md) — which coding tools Tokdash reads usage from and how detection works.
- [History retention](reference/HISTORY_RETENTION.md) — why Tokdash's past months can shrink, and how to prevent it.
- [Day boundaries](reference/DAY_BOUNDARIES.md) — Tokdash buckets by your local day; why a provider's own usage page shows a different number.

## development/ — maintainer workflows, release history, and design notes

- [Changelog](development/CHANGELOG.md) — notable changes to the project, release by release.
- [Releasing](development/RELEASING.md) — checklist for manual PyPI/Git tag/GitHub Releases publishing.
- [Roadmap](development/ROADMAP.md) — notes on planned and deferred work.
- [Companion release guide](../companion/docs/RELEASE.md) — tagging the menu-bar/tray app, the unsigned GitHub binaries, and the Microsoft Store (MSIX) track.

### development/technical-notes/ — public technical notes and research

- [Codex usage counting](development/technical-notes/CODEX_USAGE_COUNTING.md) — how Tokdash avoids double-counting Codex usage from MultiAgent V2 subagent replay.
- [Windows support plan](development/technical-notes/WINDOWS_SUPPORT_PLAN.md) — status and design of native Windows support.
- [Windows client data paths](development/technical-notes/WINDOWS_CLIENT_PATHS.md) — research backing the Windows-support pass, per-client path survey.
- [DeepSeek Harness support design](development/technical-notes/DSH_SUPPORT_DESIGN.md) - implementation plan for durable dsh token usage and Session Explorer tracking.
- [Reasonix support design](development/technical-notes/REASONIX_SUPPORT_DESIGN.md) - implementation plan for Reasonix token usage, per-request stats mapping, and Session Explorer tracking.
