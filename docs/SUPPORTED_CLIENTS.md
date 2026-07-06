# Supported clients

Tokdash reads usage **locally** from each tool's own session/log files — nothing is uploaded. Install one or more of the clients below and Tokdash discovers them automatically.

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

## Where each client logs

- **OpenCode**: `~/.local/share/opencode/`
- **Mimo / Mimocode**: `~/.local/share/mimocode/mimocode.db`
- **Codex**: `~/.codex/sessions/`
- **Claude Code**: `~/.claude/projects/`
- **Gemini CLI**: `~/.gemini/tmp/*/chats/session-*.json` and `session-*.jsonl`
- **Antigravity CLI**: `~/.gemini/antigravity-cli/conversations/*.db` (token usage only; Session Explorer drill-down is not yet supported)
- **OpenClaw**: `~/.openclaw/agents/*/sessions/`
- **Kimi CLI**: `~/.kimi/sessions/*/*/wire.jsonl`
- **Pi**: `~/.pi/agent/sessions/` (override via `PI_AGENT_DIR` env var, comma-separated list of dirs)
- **GitHub Copilot CLI**: `~/.copilot/otel/` (full input/cache/cost data — set `COPILOT_OTEL_FILE_EXPORTER_PATH` to enable OTel export) and `~/.copilot/session-state/*/events.jsonl` (output-only fallback when OTel is not enabled)
- **Hermes**: `~/.hermes/state.db` (override via `HERMES_HOME` env var, comma-separated list of dirs)

---

← Back to the [README](../README.md) · [中文 README](../README_CN.md)
