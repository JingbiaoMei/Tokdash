# Agent brand assets

These small local assets are used only to identify coding-tool rows in Tokdash. They are served from the package so the dashboard makes no runtime requests to brand websites.

- Existing Tokdash source marks: OpenCode, Codex, Claude, Gemini, Antigravity, OpenClaw, Kimi, Grok, Pi, GitHub Copilot, Hermes, MiMo, DeepSeek, Reasonix, WorkBuddy, and Qoder. ZCode's mark is the official app icon (`docs/assets/agents/`). The WorkBuddy and Qoder marks are resized copies of the official icons installed with their Windows apps. `dsh.svg` is filled with the DeepSeek brand blue (`#4D6BFE`), which reads on both themes: it therefore carries no `darkInvert` (inverting it would turn the blue muddy yellow) and needs no `prefers-color-scheme` block, which would in any case track the system theme rather than Tokdash's own `html.dark` toggle once the file is fetched as a blob into an `<img>`. The packaged copy and the source mark are byte-identical; keep them that way.
- Amp mark: [Amp](https://ampcode.com/amp-mark-color.svg).
- Cursor cube: [Cursor brand assets](https://cursor.com/brand). Cursor is registered for future compatibility but is not currently a Tokdash data source.
- Zed mark: the official Zed logo from the [Zed repository](https://github.com/zed-industries/zed), black fill (`zed.svg`). The packaged copy and the source mark are byte-identical; keep them that way.
- Qwen Code mark: the Qwen hexagon logo from the qwen-code desktop-shell bootstrap asset, Qwen purple `#6D44E8` (`qwen_code.svg`). The packaged copy and the source mark are byte-identical; keep them that way.
- Crush mark: the official Crush icon (`crush-icon-solo.png`) from the [Crush repository](https://github.com/charmbracelet/crush), 512x512 transparent (`crush.png`). The packaged copy is a 32x32 downscale of the same icon.

All marks remain the property of their respective owners.

`codex-transparent.png` and `grok-transparent.png` are locally normalized,
transparent-background derivatives of the corresponding source PNGs. The
original files remain alongside them for provenance and future reprocessing.
