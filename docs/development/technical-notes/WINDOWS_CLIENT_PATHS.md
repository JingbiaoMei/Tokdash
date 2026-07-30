# Windows Client Data Paths (research for the Windows-support pass)

**Status:** research backing the Windows-support branch. The resulting code keeps the portable dotfile paths for
clients that use them on Windows and implements the one verified Windows-native branch: Hermes defaults to
`%LOCALAPPDATA%\hermes`. This doc records, with citations, where each supported client stores its session/usage
data on **native Windows** (not WSL), so future `clientpaths.py` changes can stay anchored to verified facts.

**Last researched:** 2026-07-01. Findings come from each tool's primary source (GitHub source code, official docs,
or — for Amp — strings pulled from the shipped Windows binary). Every non-obvious claim is cited below.

**Method note on `os.homedir()` / `Path.home()`:** For Node.js tools, `os.homedir()` returns `%USERPROFILE%`
(e.g. `C:\Users\<user>`) on native Windows. For Python tools, `pathlib.Path.home()` resolves the same via
`USERPROFILE`. So any tool that simply joins the home dir with a `.<name>` dotfile is automatically "portable" —
its Windows path is just `%USERPROFILE%\.<name>\...` with no code branch needed. The interesting cases below are
the ones that do NOT do this (Hermes) or that use a Linux-only convention on Windows anyway (OpenCode).

---

## Summary table

| Client | POSIX path (tokdash today) | Windows path | Confidence | Source URL(s) |
|---|---|---|---|---|
| **OpenCode** | `~/.local/share/opencode/storage/message` and `~/.local/share/opencode/opencode.db` | `%USERPROFILE%\.local\share\opencode\storage\message` and `%USERPROFILE%\.local\share\opencode\opencode.db` (Linux-style `.local\share` layout is used on Windows too — NO `%APPDATA%`/`%LOCALAPPDATA%` branch). Honors `XDG_DATA_HOME` even on Windows. | **Verified** | [global.ts](https://github.com/anomalyco/opencode/blob/472d0f376e654fbbf573c84aec70aaf821d78a58/packages/core/src/global.ts) · [database.ts](https://github.com/anomalyco/opencode/blob/472d0f376e654fbbf573c84aec70aaf821d78a58/packages/core/src/database/database.ts) · [xdg-basedir index.js](https://github.com/sindresorhus/xdg-basedir/blob/main/index.js) · [issue #8235](https://github.com/anomalyco/opencode/issues/8235) |
| **GitHub Copilot CLI** | `~/.copilot/otel`, `~/.copilot/session-state/*/events.jsonl` | `%USERPROFILE%\.copilot\otel\*.jsonl`, `%USERPROFILE%\.copilot\session-state\*\events.jsonl` (portable dotfile). `COPILOT_HOME` relocates whole tree; `COPILOT_OTEL_FILE_EXPORTER_PATH` honored. Separate **cache** dir is `%LOCALAPPDATA%\copilot` (not session data). | **Verified** (dir); `events.jsonl` filename detail **Uncertain** | [cli-config-dir-reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference) · [configure-copilot-cli](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/configure-copilot-cli) · [install-copilot-cli](https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli) |
| **Gemini CLI** | `~/.gemini/tmp/*/chats/session-*.json(l)` | `%USERPROFILE%\.gemini\tmp\*\chats\session-*.json(l)` (portable dotfile via Node `os.homedir()`, no XDG redirect). | **Verified** | [storage.ts](https://raw.githubusercontent.com/google-gemini/gemini-cli/main/packages/core/src/config/storage.ts) · [configuration docs](https://google-gemini.github.io/gemini-cli/docs/get-started/configuration.html) · [issue #23622](https://github.com/google-gemini/gemini-cli/issues/23622) |
| **Antigravity CLI** | `~/.gemini/antigravity-cli/conversations/*.db` | `%USERPROFILE%\.gemini\antigravity-cli\conversations\*.db` (same portable `.gemini` root; SQLite DBs are WAL-mode and `*.pb` legacy files are ignored). | **Verified on POSIX**, Windows path inferred from shared `.gemini` root | Local schema verification: `docs/local/20260702_antigravity_usage/antigravity_gen_metadata_schema.md` |
| **Codex** | `~/.codex/sessions`, `~/.codex/state_5.sqlite` | `%USERPROFILE%\.codex\sessions`, `%USERPROFILE%\.codex\state_5.sqlite` (portable dotfile via Rust `dirs::home_dir()`). `CODEX_HOME` overrides whole tree cross-platform. | **Verified** | [home-dir/src/lib.rs](https://github.com/openai/codex/blob/main/codex-rs/utils/home-dir/src/lib.rs) · [Codex on Windows](https://developers.openai.com/codex/windows) |
| **Claude Code** | `~/.claude*/projects/` | `%USERPROFILE%\.claude*\projects\` (portable dotfile; docs say so verbatim). `CLAUDE_CONFIG_DIR` overrides cross-platform. NOTE: `%APPDATA%\Claude` is the Claude **Desktop** app, not Claude Code. | **Verified** | [claude-directory docs](https://code.claude.com/docs/en/claude-directory) · [env-vars docs](https://code.claude.com/docs/en/env-vars) |
| **Kimi CLI** | `~/.kimi` (env `KIMI_SHARE_DIR`) | `%USERPROFILE%\.kimi` (portable dotfile via Python `Path.home()`; `KIMI_SHARE_DIR` honored). | **Verified** (path logic); native-Windows support **Uncertain** | [share.py](https://raw.githubusercontent.com/MoonshotAI/kimi-cli/main/src/kimi_cli/share.py) · [kimi-cli repo](https://github.com/MoonshotAI/kimi-cli) |
| **Pi** | `~/.pi/agent/sessions` (tokdash assumes env `PI_AGENT_DIR`, comma-separated) | `%USERPROFILE%\.pi\agent\sessions` (portable dotfile via Node `os.homedir()`). **Env-var mismatch (see note):** live var is `PI_CODING_AGENT_DIR` / `PI_CODING_AGENT_SESSION_DIR`, single path, no comma-list. | **Verified** (path logic); env-var name/comma-list premise **contradicted** | [config.ts](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/config.ts) · [docs/windows.md](https://github.com/earendil-works/pi/blob/main/docs/windows.md) · [settings docs](https://github.com/earendil-works/pi/blob/main/docs/settings.md) |
| **Hermes** | `~/.hermes` (env `HERMES_HOME`, comma-separated) | **`%LOCALAPPDATA%\hermes`** (state.db at `%LOCALAPPDATA%\hermes\state.db`) — **this tool DOES branch to a Windows-native dir.** Installer sets `HERMES_HOME=%LOCALAPPDATA%\hermes`. **No comma-list support** (single path; "profiles" is the multi-instance mechanism). | **Verified** | [hermes-agent repo](https://github.com/NousResearch/hermes-agent) (`hermes_constants.py`, `hermes_state.py`) · [Windows Native guide](https://hermes-agent.nousresearch.com/docs/user-guide/windows-native) · [configuration docs](https://hermes-agent.nousresearch.com/docs/user-guide/configuration) |
| **Amp** | `~/.amp` | `%USERPROFILE%\.amp` (portable dotfile; env var is `AMP_HOME`, not `AMP_DATA_HOME`). | **Verified** (via shipped Windows binary strings) | [ampcode.com/manual](https://ampcode.com/manual) · [@ampcode/cli-win32-x64](https://www.npmjs.com/package/@ampcode/cli-win32-x64) |

---

## Per-client recommended `clientpaths.py` branch

### OpenCode — HIGHEST priority; result is the surprising one
- **Do NOT branch to `%APPDATA%`/`%LOCALAPPDATA%`.** OpenCode imports `xdgData` unconditionally from the npm
  `xdg-basedir` package (`xdg-basedir` = `env.XDG_DATA_HOME || join(os.homedir(), '.local', 'share')`) with **no**
  `process.platform === 'win32'` branch anywhere. So on Windows the data root is literally
  `%USERPROFILE%\.local\share\opencode\` — the same `.local\share` layout as Linux.
- **Recommended branch:** keep the current logic essentially as-is, but resolve `XDG_DATA_HOME` first, then fall
  back to `Path.home() / ".local/share/opencode/..."`. On Windows `Path.home()` already yields `%USERPROFILE%`, so
  `Path.home() / ".local/share/opencode/storage/message"` and `.../opencode.db` are correct **without an OS branch**.
  The only Windows override that redirects these is `XDG_DATA_HOME`.
- Corroboration: [issue #8235](https://github.com/anomalyco/opencode/issues/8235) ("Config and Data directories
  follow the Linux XDG standard even on windows") documents exactly this; it was stale-bot-closed, not fixed, and
  current `dev`-branch source still has no Windows branch. (Repo note: `github.com/sst/opencode` now redirects to
  `github.com/anomalyco/opencode` — same project, renamed org.)

### GitHub Copilot CLI
- **Keep `Path.home() / ".copilot" / "otel"` and the `~/.copilot/session-state/*/events.jsonl` glob** — the dotfile
  is portable; docs confirm `~/.copilot/` on Windows (`$HOME/.copilot/...` examples). `COPILOT_OTEL_FILE_EXPORTER_PATH`
  and `COPILOT_HOME` are honored cross-platform (tokdash already reads the former).
- Watch-out: the Windows **cache** dir is `%LOCALAPPDATA%\copilot` (override `COPILOT_CACHE_HOME`) — that is NOT
  session/otel data; do not point the parser there.

### Gemini CLI
- **Keep `Path.home() / ".gemini"`** — portable dotfile. `getGlobalGeminiDir()` = `join(os.homedir(), '.gemini')`
  with no OS branch and no XDG redirect. No per-user `.gemini` relocation env var exists today
  (`GEMINI_CLI_HOME` changes the home *base*; system-level `C:\ProgramData\gemini-cli\` config is separate and not
  what tokdash parses).

### Antigravity CLI
- **Keep `Path.home() / ".gemini" / "antigravity-cli" / "conversations"`** — Antigravity usage DBs live under
  the same portable `.gemini` root. The usage parser globs only `*.db`, folds `-wal`/`-shm` sidecars into file
  signatures, and intentionally ignores legacy `.pb` files. Session Explorer support is separate future work.

### Codex
- **Keep `Path.home() / ".codex"`** — portable dotfile via the Rust `dirs` crate. ✅ *Implemented:*
  `clientpaths.codex_home()` resolves `CODEX_HOME` first (works identically on Windows, any absolute path) and
  falls back to `Path.home()/".codex"`; the sessions dir and `state_5.sqlite` both derive from it.
- Windows is natively supported (sandbox modes in `config.toml`); the in-repo `docs/install.md` "WSL2 only" line is
  stale — the live docs site supersedes it.

### Claude Code
- **Keep the `Path.home().glob(".claude*")` + `/projects` logic** — docs state verbatim that on Windows `~/.claude`
  resolves to `%USERPROFILE%\.claude`. ✅ *Implemented for the config/credentials dir:* `clientpaths.claude_config_dir()`
  resolves `CLAUDE_CONFIG_DIR` first (cross-platform; used by quota tracking); the usage parsers keep the
  `.claude*` glob for multi-install project discovery.
- Do not confuse with `%APPDATA%\Claude\` (that's Claude Desktop, a different product).

### Kimi CLI
- **Keep `Path.home() / ".kimi"`** with the existing `KIMI_SHARE_DIR` override — resolver is
  `Path(os.getenv("KIMI_SHARE_DIR")) or Path.home()/".kimi"`, and Python `Path.home()` yields `%USERPROFILE%` on
  Windows. Path logic is portable as-is. (Caveat: no docs/CI evidence that Kimi CLI is actually *tested* on native
  Windows — the path math is right, but Windows support is otherwise undocumented.)

### Pi
- **Keep `Path.home() / ".pi" / "agent" / "sessions"`** for the default — portable dotfile via Node `os.homedir()`,
  and native Windows is officially supported (Git Bash required for the shell tool).
- **Env-var discrepancy to resolve (not a path issue but affects the override branch):** tokdash's
  `pi_agent_search_dirs()` reads `PI_AGENT_DIR` as a **comma-separated** list. In current upstream source, the live
  runtime var is **`PI_CODING_AGENT_DIR`** (and `PI_CODING_AGENT_SESSION_DIR` for the session dir specifically), a
  **single** path — `PI_AGENT_DIR` only appears as a test constant / one-off migration script, and no comma-split
  logic exists anywhere upstream. This is orthogonal to Windows but should be reconciled against whichever Pi
  version tokdash targets before writing the branch. (If the comma-list override is kept, note Windows drive-letter
  paths like `D:\...` don't contain commas, so splitting on `,` is still safe.)

### Hermes — the one genuine Windows branch
- **DO branch on Windows.** On POSIX the DB is `~/.hermes/state.db`; on native Windows Hermes uses
  `%LOCALAPPDATA%\hermes\state.db` (the installer sets `HERMES_HOME=%LOCALAPPDATA%\hermes`, and source branches
  `if sys.platform == "win32": base = %LOCALAPPDATA% (or ~/AppData/Local); return base/"hermes"`).
- **Recommended branch:** resolve `HERMES_HOME` first (already done); when unset, on Windows return
  `Path(os.environ.get("LOCALAPPDATA") or Path.home()/"AppData"/"Local") / "hermes"`, else `Path.home()/".hermes"`.
- **Also reconcile the comma-list:** upstream `HERMES_HOME` is read as a single `Path(val)` — no comma-separated
  multi-dir support (multi-instance is done via "profiles", not comma-lists). tokdash's `hermes_search_dirs()`
  comma-split is a tokdash-specific convenience; keep it if desired, but it does not mirror upstream behavior.

### Amp
- **Keep `Path.home() / ".amp"`** — portable dotfile; the shipped Windows binary literally does
  `if (process.env.AMP_HOME) return process.env.AMP_HOME; ... return join(homedir(), ".amp")` and embeds
  `%USERPROFILE%\.amp\bin\amp.bat`. If an override is ever added to tokdash, the env var is **`AMP_HOME`** (not
  `AMP_DATA_HOME`). Note Amp's *settings* file lives separately at `%USERPROFILE%\.config\amp\settings.json`, which
  tokdash's `.amp`-rooted parser does not currently need.

---

## Still unverified — needs a real Windows box to confirm

These are honest gaps. Do NOT hardcode any of the below as if verified:

1. **Copilot CLI `events.jsonl` filename convention on Windows.** The docs confirm the `%USERPROFILE%\.copilot\session-state\`
   *directory* exists on Windows, but the exact per-session `*/events.jsonl` file layout was confirmed only on POSIX
   and inferred by analogy. Also note current builds (v0.0.342+) use `session-state/`; older builds used
   `history-session-state/` — verify which exists on a real Windows install. (Source:
   [cli-config-dir-reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference).)

2. **Kimi CLI actually running on native Windows.** The path-construction code is portable (`Path.home()/".kimi"`),
   but there are no Windows install instructions, docs, or CI evidence that Kimi CLI is supported/exercised on native
   Windows. The `%USERPROFILE%\.kimi\sessions\<userId>\<sessionId>\wire.jsonl` layout is inferred to hold on Windows
   but was not observed on a Windows box.

3. **OpenCode: no default-state Windows observation.** The `%USERPROFILE%\.local\share\opencode\` conclusion is from
   source (strong), but the one public `opencode debug paths` Windows dump found had a customized `XDG_CONFIG_HOME`,
   so it isn't clean default-state proof. A `opencode debug paths` run on a stock Windows install would confirm.

4. **Pi env-var reconciliation.** Whether tokdash should read `PI_CODING_AGENT_DIR`/`PI_CODING_AGENT_SESSION_DIR`
   (upstream-current) vs the `PI_AGENT_DIR` it reads today depends on the Pi version tokdash targets — needs a
   decision, not a Windows box, but flagged here because it affects the override branch.

5. **Hermes comma-list vs single path.** The `%LOCALAPPDATA%\hermes` default is verified; the divergence between
   tokdash's comma-separated `HERMES_HOME` handling and upstream's single-path reading should be reconciled against
   the Hermes version tokdash targets.
