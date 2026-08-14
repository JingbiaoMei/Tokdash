# Agent brand assets

These small local assets are used only to identify coding-tool rows in Tokdash. They are served from the package so the dashboard makes no runtime requests to brand websites.

- Existing Tokdash source marks: OpenCode, Codex, Claude, Gemini, Antigravity, OpenClaw, Kimi, Grok, Pi, GitHub Copilot, Hermes, MiMo, and DeepSeek (`docs/assets/agents/`). `dsh.svg` is filled with the DeepSeek brand blue (`#4D6BFE`), which reads on both themes: it therefore carries no `darkInvert` (inverting it would turn the blue muddy yellow) and needs no `prefers-color-scheme` block, which would in any case track the system theme rather than Tokdash's own `html.dark` toggle once the file is fetched as a blob into an `<img>`. The packaged copy and the source mark are byte-identical; keep them that way.
- Amp mark: [Amp](https://ampcode.com/amp-mark-color.svg).
- Cursor cube: [Cursor brand assets](https://cursor.com/brand). Cursor is registered for future compatibility but is not currently a Tokdash data source.

All marks remain the property of their respective owners.

`codex-transparent.png` and `grok-transparent.png` are locally normalized,
transparent-background derivatives of the corresponding source PNGs. The
original files remain alongside them for provenance and future reprocessing.
