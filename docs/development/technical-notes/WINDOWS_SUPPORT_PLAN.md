# Windows support plan

Status: released in `v1.0.5` as **experimental native Windows support**. Foreground
`tokdash serve`, Windows-aware path/locking behavior, Windows CI, and a Windows Task
Scheduler backend for `tokdash setup` are implemented. The release has CI coverage
and initial real-Windows smoke validation, but broader real-world Task Scheduler
create/start/update/uninstall validation is still ongoing.

## 1. Goal & scope

Add native Windows support to tokdash so all three usage tiers work the same way they
do on Linux/WSL/macOS today:

1. **Foreground**: `tokdash serve` runs correctly on a stock Windows Python install.
2. **Background/autostart**: a managed background service equivalent to the
   `onboard/systemd.py` (Linux) / `onboard/launchd.py` (macOS) backends, so
   `tokdash setup` can offer a real "starts on login" option on Windows instead of the
   current foreground-only fallback.
3. **Polish**: statusline integration, docs, and CI parity so Windows is a first-class,
   tested platform rather than an unsupported afterthought.

WSL is already covered (`onboard/detect.py:os_kind()` returns `"wsl"` and treats it as
Linux for systemd purposes). This plan is about **native Windows** (`sys.platform ==
"win32"`, i.e. `os_kind() == "windows"`).

## 2. Architecture decision: one package, not `tokdash-win`

**Decision: ship Windows support inside the existing `tokdash` package. Do not create a
separate `tokdash-win` package.**

Reasoning:

- tokdash is pure Python + `subprocess` calls to OS CLIs (`systemctl`, `launchctl`,
  `tailscale`). There are no compiled extensions or OS-only library dependencies that
  would force a split.
- Roughly 90% of the codebase (parsers, `compute.py`, `pricing.py`, the FastAPI app,
  the static dashboard) is already platform-agnostic and runs unmodified on Windows
  today.
- A second package would mean two release pipelines, two version numbers to keep in
  sync, and a real risk of version skew between `tokdash` and `tokdash-win` — exactly
  the kind of multi-package maintenance burden the project's release rules
  (`AGENTS.md` → *Release rules*) are designed to avoid for a single package.
  It would also break the simple `pip install tokdash` story documented in `README.md`.

Instead, reusability comes from clean **internal seams** within the one package,
following the pattern the repo already uses for OS-specific backends:
`onboard/systemd.py` (Linux) and `onboard/launchd.py` (macOS), selected by
`onboard/detect.py:os_kind()`. Windows support is "more of the same pattern," not a new
distribution model.

### Implemented module layout

New, flat top-level modules under `src/tokdash/`, matching the repo's existing mostly-flat
module style (see the `Architecture overview` table in `AGENTS.md`):

| Module | Purpose | Replaces / centralizes |
| --- | --- | --- |
| `src/tokdash/osinfo.py` | Centralized OS detection: `os_kind()` → `linux \| wsl \| macos \| windows`, plus `is_windows()` / `is_macos()` / `is_wsl()` helpers. | The logic currently embedded in `onboard/detect.py:os_kind()` / `is_wsl()` (`src/tokdash/onboard/detect.py:28-47`), which today is onboarding-only even though other modules need the same answer. |
| `src/tokdash/clientpaths.py` | Per-OS path resolution for **every** supported client (OpenCode, Codex, Claude Code, Gemini CLI, OpenClaw, Kimi CLI, Pi, Copilot CLI, Hermes, Amp) plus tokdash's own data dir. | The scattered `Path.home() / ".local/share/opencode/..."`-style literals in `src/tokdash/sources/coding_tools.py` and `src/tokdash/sessions.py` (see the gap table below). This is the single biggest maintainability win — it's also where every Windows-specific path branch will live. |
| `src/tokdash/filelock.py` | Cross-platform advisory file lock: `fcntl.flock` on POSIX, `msvcrt.locking` on Windows, behind one context manager. | The direct `import fcntl` / Windows no-op in `src/tokdash/usage_store.py:14-17,41-56`. |
| `src/tokdash/onboard/service_base.py` | A `ServiceBackend` protocol, backend registry, and `select_service()` factory. `plan._resolve_service()` delegates service selection through this seam, while backend-specific lifecycle calls stay in `engine.py`. | The ad hoc dispatch in `src/tokdash/onboard/plan.py:_resolve_service()` (`plan.py:224-270`). |

Optional dependency to evaluate: [`platformdirs`](https://pypi.org/project/platformdirs/)
for OS-convention data/cache/config directories. **Caveat to keep in mind**: several
clients use a literal `~/.<client>` dotfile regardless of OS convention (e.g. `.codex`,
`.claude`, `.gemini`, `.copilot`, `.hermes`), which `platformdirs` would get wrong if
applied uniformly. `platformdirs` is a helper for tokdash's *own* data dir and any
client that does follow OS convention (OpenCode is the one known XDG-style case) — not a
silver bullet. Each client's real Windows layout must still be verified empirically
(see §6).

## 3. Original gap analysis

These were the gaps identified before the Windows-support work. Many are now closed
in `v1.0.5`; they remain here as the rationale for the tiered plan and for the
remaining real-Windows validation work.

| # | Gap | Where | Original Windows impact |
| --- | --- | --- | --- |
| 1 | File locking uses POSIX-only `fcntl` | `src/tokdash/usage_store.py:14-17` (`import fcntl` guarded by `except ImportError`), `usage_store.py:41-56` (`usage_db_process_lock`) | `fcntl` is `None` on Windows, so the lock silently no-ops (`if fcntl is None: yield; return`). Cross-process serialization between `tokdash serve` and `tokdash db watch`/resync is lost — a real correctness gap, not just a warning. |
| 2 | No Windows background-service backend | `src/tokdash/onboard/systemd.py` (Linux only), `src/tokdash/onboard/launchd.py` (macOS only); dispatch in `src/tokdash/onboard/plan.py:_resolve_service()` | Before Tier 2, native Windows users got foreground-only behavior by design: `--service systemd`/`--service launchd` were blocked, and auto service setup fell back to foreground guidance. |
| 3 | OpenCode data path hardcoded to XDG layout | `src/tokdash/sources/coding_tools.py:202-203` (`OpenCodeParser.__init__`: `Path.home() / ".local/share/opencode/storage/message"` and `.../opencode.db`); duplicated in `src/tokdash/sessions.py:369` (codex), `sessions.py:680` (`_opencode_db_signature`: same `~/.local/share/opencode/opencode.db` literal) | `~/.local/share/...` is an XDG/Linux convention. OpenCode does not necessarily store data there on Windows (likely `%LOCALAPPDATA%\opencode\` or similar — unverified, see §6). Until confirmed and branched, OpenCode usage will not be discovered on native Windows. |
| 4 | venv interpreter path assumes `bin/python` | `src/tokdash/onboard/paths.py:70-72` (`managed_venv_python()`, with the existing comment *"Windows venvs put the interpreter under Scripts/, but Phase 1 targets POSIX"*); `src/tokdash/onboard/detect.py:188-189` (`pipx_tokdash_python()` candidate paths end in `.../venvs/tokdash/bin/python`); consumed by `src/tokdash/onboard/runtime.py:resolve()`/`create_managed_venv()` | On Windows, `python -m venv` creates `Scripts\python.exe`, not `bin/python`. Both the managed-venv runtime and pipx-detection candidates need an OS branch before `tokdash setup --runtime venv` or `--runtime pipx` can work natively on Windows. |
| 5 | Other client paths need a Windows verification pass | `src/tokdash/sources/coding_tools.py`: Codex `:306` (`~/.codex/sessions`), Claude Code `:406` (`Path.home().glob(".claude*")`), Gemini CLI `:584` (`~/.gemini`), Amp `:690` (`~/.amp`), Kimi CLI `:746` (`~/.kimi`, override via env), Pi `:888-891` (`~/.pi/agent/sessions`, override via `PI_AGENT_DIR`), Copilot CLI `:1071-1072` (`~/.copilot/otel`, `~/.copilot/session-state/*/events.jsonl` — glob built via `str(Path.home() / ...)`), Hermes `:1518-1520` (`~/.hermes`, override via `HERMES_HOME`) | `~/.<client>` dotfiles resolve fine via `Path.home()` on Windows (`pathlib` handles `%USERPROFILE%`), so these are *probably* OK as-is — but each one needs an explicit verification pass against the real Windows install of that client, not an assumption. The Copilot CLI glob built with `str(Path / ... / "*" / "events.jsonl")` should also be checked for backslash-vs-glob interaction on Windows. |
| 6 | `uvloop` is POSIX-only | `pyproject.toml` dependency `uvicorn[standard]>=0.32.0`; consumed at `src/tokdash/cli.py:256` (`uvicorn.run(...)`) | Confirm-only, not a blocker: `uvicorn[standard]`'s `uvloop` extra is not installable on Windows, and uvicorn already falls back to the stdlib `asyncio` event loop there. No code change expected; verify under CI once `windows-latest` is added (Tier 1). |
| 7 | Headless/browser-open detection | `src/tokdash/cli.py:_has_display()` (`cli.py:193-214`) | Already OK — note only. The Linux branch gates on `DISPLAY`/`WAYLAND_DISPLAY`; for any non-Linux platform (`sys.platform.startswith("linux")` is `False`) it already returns `True` (`cli.py:212-214`), which is the right behavior for Windows desktop sessions. No change needed. |
| 8 | Statusline templates are bash-only | `docs/guides/statusline/statusline-minimal.sh`, `docs/guides/statusline/statusline-full.sh` (both `curl`+`jq`, `#!/usr/bin/env bash`) | Claude Code on Windows can still run a bash script under WSL/Git Bash, but a native PowerShell statusline template is missing for users running Claude Code natively on Windows. |
| 9 | CI is Ubuntu-only | `.github/workflows/ci.yml` (`runs-on: ubuntu-latest`, matrix `python-version: ["3.10", "3.12"]`) | No automated signal at all for Windows regressions; every gap above can silently break without CI catching it. |
| 10 | Tests assume POSIX/systemd/launchd | `tests/test_onboard.py` (monkeypatches `systemd`/`launchd` modules directly), `tests/test_usage_store.py` (locking behavior assumes `fcntl` semantics), plus any test that builds paths assuming POSIX separators | Needs skip-markers for backend-specific tests (e.g. `@pytest.mark.skipif(os_kind() != "windows", ...)` for a future `winsched` backend) and new Windows-specific tests once Tier 1/2 land. |
| 11 | Docs/metadata did not mention Windows | Before `v1.0.5`, `README.md` listed Linux/WSL2 and macOS only, and `pyproject.toml` had no Windows OS classifier. | Cosmetic but user-facing: nothing told a Windows user whether tokdash worked for them, and PyPI's classifier list did not advertise Windows support. |

## 4. Tiered plan

### Tier 0 — Seams refactor (no behavior change)

Goal: extract the seams so later tiers are additive, without changing behavior on any
currently-supported platform. This tier should be invisible to existing Linux/macOS
users — it is pure refactoring with test coverage to prove parity.

- [x] Add `src/tokdash/osinfo.py` with `os_kind()` / `is_windows()` / `is_macos()` /
      `is_wsl()`; re-implement `onboard/detect.py:os_kind()` and `is_wsl()` as thin
      wrappers (or have `detect.py` import from `osinfo.py`) so there is exactly one
      source of truth.
- [x] Add `src/tokdash/clientpaths.py` and move every client path literal listed in gap
      #3/#5 into named functions (e.g. `opencode_data_dir()`, `codex_sessions_dir()`,
      `claude_project_dirs()`, …). Update `sources/coding_tools.py` and `sessions.py`
      to call these functions instead of inlining `Path.home() / ...`. Behavior must be
      byte-for-byte identical on Linux/macOS — this tier only relocates the literals,
      it does not add Windows branches yet.
- [x] Add `src/tokdash/filelock.py` with a single `process_lock(path)` context manager;
      port the POSIX `fcntl` implementation from `usage_store.py:41-56` into it
      unchanged. `usage_store.py` calls the new module instead of importing `fcntl`
      directly. The Windows (`msvcrt`) branch is a stub/`NotImplementedError`-free no-op
      in this tier — implementing real Windows locking is Tier 1 (see below).
- [x] Define `onboard/service_base.py`: a `ServiceBackend` protocol, registry, and
      `select_service()` factory. Update `onboard/plan.py:_resolve_service()` to delegate
      through it instead of carrying the inline `if os_kind == "macos" / ...` chain.
- [x] Full test suite green with no behavior change; this is the gate for starting
      Tier 1.

**Completed after Tier 0**: the real `msvcrt`-based lock implementation and the
verified Hermes Windows path branch landed in later tiers.

### Tier 1 — Native Windows foreground

Goal: `tokdash serve` (and `tokdash export`, `tokdash db ...`) work correctly when run
directly with a stock Windows Python install (no managed service yet).

- [x] Implement the `msvcrt.locking()` branch in `filelock.py` so
      `usage_db_process_lock`-equivalent locking is real (not a no-op) on Windows.
- [x] Add OS-aware branches in `clientpaths.py` where empirically needed. Research found
      OpenCode keeps its Linux-style `.local/share` layout on Windows; Hermes was the only
      client requiring a Windows-native `%LOCALAPPDATA%\hermes` branch.
- [x] Audit and, where needed, branch the remaining client paths from gap #5
      (Codex, Claude Code, Gemini CLI, Amp, Kimi CLI, Pi, Copilot CLI, Hermes) for
      Windows; most are expected to need no change (`Path.home()` resolves
      `%USERPROFILE%` correctly), but each must be confirmed against a real install,
      not assumed.
- [x] Smoke-test `tokdash serve` end-to-end on Windows. The `v1.0.5` release was
      installed from PyPI into Windows Python 3.13, `py -m tokdash --version` and
      `doctor --json` passed, `setup --dry-run --service winsched --json` rendered a
      `pythonw.exe` Task Scheduler plan, and a temporary server on port 55424 returned
      `/health` with `version: 1.0.5`.
- [x] Add `windows-latest` to `.github/workflows/ci.yml`'s matrix; add
      `skipif`/`xfail` markers to the POSIX-only tests identified in gap #10, and add
      new Windows-specific tests (lock behavior, path resolution).
- [x] Update `README.md` `## Platform support` (gap #11) to add a Windows row, and add
      `"Operating System :: Microsoft :: Windows"` to `pyproject.toml` classifiers.
      Keep `README_CN.md` in sync per `AGENTS.md`'s release rules.

### Tier 2 — Background-service parity

Goal: `tokdash setup` offers a real managed background service on Windows, matching
what systemd/launchd already provide on Linux/macOS.

- [x] Add `src/tokdash/onboard/winsched.py`: a Task Scheduler backend backed by
      Windows Task Scheduler, registering an `ONLOGON` trigger that runs
      `pythonw -m tokdash serve` (using `pythonw` rather than `python` to avoid a
      console window, mirroring how `systemd.py`/`launchd.py` run detached).
- [x] Carry the same ownership-marker discipline systemd/launchd use today (see
      `onboard/systemd.py`'s `MARKER_COMMENT` / `onboard/manifest.py`'s
      `marker_token()`) so `tokdash uninstall` can prove a task is setup-owned before
      removing it — the same safety property `plan.py:_plan_service_removal()` already
      enforces for systemd/launchd units.
- [x] Fix the Windows venv interpreter path (gap #4): branch
      `onboard/paths.py:managed_venv_python()` to return `Scripts\python.exe` on
      Windows, and extend `onboard/detect.py:pipx_tokdash_python()`'s candidate list
      accordingly.
- [x] Wire `tokdash setup` / `tokdash doctor` / `tokdash uninstall` additively for the
      `winsched` service type, including manifest read/write.
- [x] New tests covering the winsched backend (rendering, ownership marker detection,
      install/uninstall planning) mirroring `tests/test_onboard.py`'s systemd/launchd
      coverage.

### Tier 3 — Polish

- [x] Add a PowerShell statusline template alongside
      `docs/guides/statusline/statusline-minimal.sh` /
      `statusline-full.sh` (gap #8), plus the matching Windows `settings.json`
      `statusLine` snippet for Claude Code.
- [x] Document Tailscale-on-Windows notes for remote access. The Windows Tailscale
      client exposes a `tailscale` CLI from PowerShell/cmd, but the combined native
      Windows Tokdash + Tailscale Serve path should still be treated as experimental
      until separately validated end to end.

## 5. Testing & CI strategy

Canonical local commands (unchanged from today, per `AGENTS.md`):

```bash
PYTHONPATH=src python3 -m pytest -q
python -m compileall -q src/tokdash main.py
```

CI (`.github/workflows/ci.yml`) now includes `ubuntu-latest` and `windows-latest` on
Python 3.10 and 3.12. The existing `Compile` step and `pytest -q` run across the full
matrix. Windows-specific tests cover file locking, client path resolution, service
selection, and Task Scheduler rendering, while POSIX-specific assumptions are pinned
inside their test harnesses.

## 6. Remaining validation / research

The single biggest remaining risk is **guessing** a Windows client path instead of
verifying it. Continue confirming empirically (real Windows installs, not documentation
alone) where each client stores data:

- **OpenCode** — research for `v1.0.5` found it keeps the XDG-style
  `~/.local/share/opencode/...` layout on Windows; re-check if upstream changes its
  storage layout.
- **GitHub Copilot CLI** — currently `~/.copilot/otel` and
  `~/.copilot/session-state/*/events.jsonl` (`sources/coding_tools.py:1071-1072`);
  confirm whether the Windows install uses the same dotfile-style home directory or a
  `%APPDATA%`/`%LOCALAPPDATA%` convention instead.
- **Gemini CLI** — currently `~/.gemini` (`sources/coding_tools.py:584`); confirm
  whether the Windows install uses the same dotfile-style home directory or an
  `%APPDATA%`/`%LOCALAPPDATA%` convention instead.
- Lower-priority but worth continued spot checks: Kimi CLI, Pi, Amp, Codex, and Claude
  Code currently use simple `~/.<client>` dotfiles that *should* resolve correctly via
  `pathlib.Path.home()` on Windows, but each should still get a one-time empirical
  check rather than being assumed correct by analogy. Hermes already has the
  Windows-native `%LOCALAPPDATA%\hermes` branch.

`platformdirs` (see §2) can help once the real per-client convention is known, but it
cannot substitute for checking each client's actual behavior — several of them
deliberately ignore OS convention in favor of a portable dotfile.
