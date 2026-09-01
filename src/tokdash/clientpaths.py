"""Centralized per-client + data-dir path resolution (Tier 0 seams refactor).

Every coding-tool log location and env-var override that ``sources/coding_tools.py``
and ``sessions.py`` need lives here, in one place, so a later Windows-support pass
(Tier 1) only has to branch on OS in this module instead of at every call site.

This module is intentionally a pure centralization: it computes EXACTLY what the
call sites computed inline before (same env vars, same ``Path.home()`` lookups,
same defaults). No Windows-specific branches are added here yet.

Paths are resolved fresh on every call (``Path.home()`` / ``os.environ`` are read
at call time, never cached at import time) so that tests which monkeypatch
``Path.home`` or set env vars before constructing a parser keep working unchanged.

Note: the Tokdash data dir (``TOKDASH_DATA_DIR``) also has an independent copy in
``onboard/paths.py::data_dir()`` for the setup engine. That copy is left untouched
by this refactor (see module docstring there for why) — only ``usage_store.py``
and the coding-tool sources/sessions call sites are centralized here.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from . import osinfo


# --- OpenCode ---------------------------------------------------------------


def opencode_data_dir() -> Path:
    explicit = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(explicit).expanduser() if explicit else Path.home() / ".local/share"
    return base / "opencode"


def opencode_config_dir() -> Path:
    explicit = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(explicit).expanduser() if explicit else Path.home() / ".config"
    return base / "opencode"


def opencode_auth_path() -> Path:
    return opencode_data_dir() / "auth.json"


def opencode_config_paths() -> List[Path]:
    explicit = os.environ.get("OPENCODE_CONFIG", "").strip()
    if explicit:
        return [Path(explicit).expanduser()]
    root = opencode_config_dir()
    return [root / "opencode.json", root / "opencode.jsonc"]


def opencode_messages_dir() -> Path:
    return opencode_data_dir() / "storage/message"


def opencode_db_path() -> Path:
    return opencode_data_dir() / "opencode.db"


# --- Kilo Code -------------------------------------------------------------------


def kilo_data_dir() -> Path:
    explicit = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(explicit).expanduser() if explicit else Path.home() / ".local/share"
    return base / "kilo"


def kilo_db_paths() -> List[Path]:
    """Kilo SQLite databases, most canonical first.

    Stable installs have a single ``kilo.db``; a dev-channel install can add a
    ``kilo-<channel>.db``. Pre-rename installs wrote an ``opencode*.db`` into
    this same data dir (Kilo is built on the OpenCode codebase), and the app
    reads that legacy name only while no kilo-named file exists — mirrored
    here so a migrated install is never read twice.
    """
    root = kilo_data_dir()
    kilo_named = sorted(
        (p for p in root.glob("kilo*.db") if p.is_file()),
        key=lambda p: (p.name != "kilo.db", p.name),
    )
    if kilo_named:
        return kilo_named
    return sorted(p for p in root.glob("opencode*.db") if p.is_file())


# --- Cline ---------------------------------------------------------------------


def cline_data_dir() -> Path:
    """Cline data dir: ``$CLINE_DATA_DIR``, else ``$CLINE_DIR/data``, else ``~/.cline/data``."""
    explicit = os.environ.get("CLINE_DATA_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    cline_dir = os.environ.get("CLINE_DIR", "").strip()
    if cline_dir:
        return Path(cline_dir).expanduser() / "data"
    return Path.home() / ".cline" / "data"


# --- Mimo / Mimocode -----------------------------------------------------------


def mimocode_db_path() -> Path:
    return Path.home() / ".local/share/mimocode/mimocode.db"


# --- Codex --------------------------------------------------------------------


def codex_home() -> Path:
    """``$CODEX_HOME`` if set, else ``~/.codex``."""
    explicit = os.environ.get("CODEX_HOME", "").strip()
    return Path(explicit).expanduser() if explicit else Path.home() / ".codex"


def codex_sessions_dir() -> Path:
    return codex_home() / "sessions"


def codex_archived_sessions_dir() -> Path:
    """Codex moves completed rollouts here (``codex archive`` / auto-archive).

    Files keep their content, so the stable event key collapses any overlap with
    ``sessions/`` — scanning both roots cannot double-count.
    """
    return codex_home() / "archived_sessions"


def codex_state_db_path() -> Path:
    return codex_home() / "state_5.sqlite"


# --- Claude Code ----------------------------------------------------------------


def claude_config_dir() -> Path:
    """``$CLAUDE_CONFIG_DIR`` if set, else ``~/.claude``."""
    explicit = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(explicit).expanduser() if explicit else Path.home() / ".claude"


def claude_project_dirs() -> List[Path]:
    """``projects/`` dir under every ``~/.claude*`` install (base + variants)."""
    return [p / "projects" for p in sorted(Path.home().glob(".claude*")) if (p / "projects").is_dir()]


# --- Gemini CLI -----------------------------------------------------------------


def gemini_root() -> Path:
    return Path.home() / ".gemini"


def antigravity_cli_dir() -> Path:
    return gemini_root() / "antigravity-cli"


def antigravity_conversations_dir() -> Path:
    return antigravity_cli_dir() / "conversations"


def antigravity_conversations_glob() -> str:
    return str(antigravity_conversations_dir() / "*.db")


def antigravity_summaries_db_path() -> Path:
    return antigravity_cli_dir() / "conversation_summaries.db"


def gemini_chats_json_glob(root: Optional[Path] = None) -> str:
    root = root if root is not None else gemini_root()
    return str(root / "tmp" / "*" / "chats" / "session-*.json")


def gemini_chats_jsonl_glob(root: Optional[Path] = None) -> str:
    root = root if root is not None else gemini_root()
    return str(root / "tmp" / "*" / "chats" / "session-*.jsonl")


# --- Amp --------------------------------------------------------------------


def amp_root() -> Path:
    return Path.home() / ".amp"


# --- Kimi CLI ---------------------------------------------------------------


def kimi_root() -> Path:
    """Legacy Kimi CLI root: ``$KIMI_SHARE_DIR`` if set, else ``~/.kimi``."""
    kimi_share_dir = os.environ.get("KIMI_SHARE_DIR", "").strip()
    return Path(kimi_share_dir).expanduser() if kimi_share_dir else (Path.home() / ".kimi")


def kimi_code_root() -> Path:
    """Kimi Code (>=0.26) root: ``$KIMI_CODE_HOME`` if set, else ``~/.kimi-code``."""
    kimi_code_home = os.environ.get("KIMI_CODE_HOME", "").strip()
    return Path(kimi_code_home).expanduser() if kimi_code_home else (Path.home() / ".kimi-code")


def kimi_roots() -> List[Path]:
    """All candidate Kimi data roots, newest install first, deduplicated.

    Kimi Code 0.26 moved the data dir from ``~/.kimi`` to ``~/.kimi-code`` and
    dropped ``KIMI_SHARE_DIR`` in favour of ``KIMI_CODE_HOME``. Old sessions are
    not migrated, so both roots may hold data and callers should scan each one.
    """
    roots: List[Path] = []
    for root in (kimi_code_root(), kimi_root()):
        if root not in roots:
            roots.append(root)
    return roots


# --- MiniMax CLI ------------------------------------------------------------


def minimax_cli_root() -> Path:
    """``$MMX_CONFIG_DIR`` if set, else ``~/.mmx``."""
    explicit = os.environ.get("MMX_CONFIG_DIR", "").strip()
    return Path(explicit).expanduser() if explicit else Path.home() / ".mmx"


# --- Grok Build -------------------------------------------------------------


def grok_home() -> Path:
    """``$GROK_HOME`` if set, else ``~/.grok``."""
    explicit = os.environ.get("GROK_HOME", "").strip()
    return Path(explicit).expanduser() if explicit else Path.home() / ".grok"


def grok_sessions_dir() -> Path:
    return grok_home() / "sessions"


# --- DeepSeek Harness (dsh) ---------------------------------------------------


def dsh_home() -> Path:
    """``$DSH_HOME`` if set, else ``~/.dsh``. Empty/whitespace counts as unset."""
    explicit = os.environ.get("DSH_HOME", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_absolute() else path.resolve()
    return Path.home() / ".dsh"


def dsh_sessions_dir() -> Path:
    return dsh_home() / "sessions"


# --- CC Switch --------------------------------------------------------------


def cc_switch_root() -> Path:
    explicit = os.environ.get("CC_SWITCH_CONFIG_DIR", "").strip()
    return Path(explicit).expanduser() if explicit else Path.home() / ".cc-switch"


def cc_switch_db_path() -> Path:
    return cc_switch_root() / "cc-switch.db"


# --- Pi Agent -----------------------------------------------------------------


def pi_agent_search_dirs() -> List[Path]:
    """Pi session-dir candidates, most specific override first.

    Upstream pi coding-agent reads ``PI_CODING_AGENT_SESSION_DIR`` (the session
    dir, single path) and ``PI_CODING_AGENT_DIR`` (the agent dir; sessions live
    under ``<dir>/sessions``) — see the Pi row of
    ``docs/development/technical-notes/WINDOWS_CLIENT_PATHS.md``, which found
    ``PI_AGENT_DIR`` to be a test constant, not the live var, and no
    comma-splitting anywhere upstream. The legacy ``PI_AGENT_DIR`` comma-list
    is still honored for existing overrides, then the portable dotfile default.
    """
    session_dir_env = os.environ.get("PI_CODING_AGENT_SESSION_DIR", "").strip()
    if session_dir_env:
        return [Path(session_dir_env).expanduser()]
    agent_dir_env = os.environ.get("PI_CODING_AGENT_DIR", "").strip()
    if agent_dir_env:
        return [Path(agent_dir_env).expanduser() / "sessions"]
    pi_dir_env = os.environ.get("PI_AGENT_DIR", "").strip()
    if pi_dir_env:
        return [Path(d.strip()).expanduser() for d in pi_dir_env.split(",") if d.strip()]
    return [Path.home() / ".pi" / "agent" / "sessions"]


# --- omp (oh-my-pi) -----------------------------------------------------------------


def omp_agent_search_dirs() -> List[Path]:
    """omp session-dir candidates, most specific override first.

    omp is a port of pi-mono (its ``packages/utils/src/dirs.ts`` is the source
    of record). The config root is ``~/.omp`` unless ``PI_CONFIG_DIR``
    overrides the root name; sessions live under
    ``<config root>/agent/sessions``, and named profiles under
    ``<config root>/profiles/<name>/agent/sessions``. On linux (including WSL) and darwin
    the default profile may be migrated with ``omp config init-xdg``; omp then
    reads sessions from ``$XDG_DATA_HOME/omp/sessions`` — or
    ``~/.local/share/omp/sessions`` when the variable is unset, mirroring
    ``kilo_data_dir`` — note the flattened ``agent/`` prefix — and only
    trusts that path once the ``omp`` app root under it exists.

    ``PI_CODING_AGENT_DIR`` is deliberately NOT a candidate: omp honors it in
    default-profile mode, but ``pi_agent_search_dirs`` already claims that
    override exclusively, and if both parsers scanned it, every token would
    count twice — the usage store dedups on ``(source, entry_key)``, never
    across sources.
    """
    config_root = Path.home() / (os.environ.get("PI_CONFIG_DIR", "").strip() or ".omp")
    dirs: List[Path] = [config_root / "agent" / "sessions"]

    xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
    if not osinfo.is_windows():
        # init-xdg may have run without the variable ever being exported;
        # its default root is ~/.local/share (mirror kilo_data_dir).
        base = Path(xdg_data).expanduser() if xdg_data else Path.home() / ".local/share"
        app_root = base / "omp"
        if app_root.is_dir():
            dirs.append(app_root / "sessions")

    profiles_root = config_root / "profiles"
    if profiles_root.is_dir():
        for profile in sorted(profiles_root.iterdir()):
            if profile.is_dir():
                dirs.append(profile / "agent" / "sessions")

    # De-duplicate, keeping order (PI_CONFIG_DIR may point at ".omp" itself).
    seen: set = set()
    out: List[Path] = []
    for d in dirs:
        key = str(d)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


# --- GitHub Copilot CLI -----------------------------------------------------------


def copilot_otel_dir() -> Path:
    return Path.home() / ".copilot" / "otel"


def copilot_events_glob() -> str:
    return str(Path.home() / ".copilot" / "session-state" / "*" / "events.jsonl")


def copilot_otel_exporter_path() -> str:
    """``$COPILOT_OTEL_FILE_EXPORTER_PATH``, stripped; empty string when unset."""
    return os.environ.get("COPILOT_OTEL_FILE_EXPORTER_PATH", "").strip()


# --- Hermes -------------------------------------------------------------------


def hermes_search_dirs() -> List[Path]:
    """Hermes home(s) plus their named profiles, most general first.

    Base homes are ``$HERMES_HOME`` (comma-separated) when set, else
    ``~/.hermes`` (``%LOCALAPPDATA%\\hermes`` on Windows). Each base then
    contributes its named profiles from ``<base>/profiles/<name>``, which keep
    their own ``state.db`` — ``hermes profile create`` never shares session
    history between profiles (``--clone-all`` explicitly excludes it), so the
    databases hold disjoint sessions and the row-id dedup in HermesParser /
    _load_hermes_sessions has nothing to collapse. Mirrors the profile scan in
    ``omp_agent_search_dirs``.

    Pointing ``HERMES_HOME`` at a profile dir directly still works: a profile
    has no ``profiles/`` of its own, so it adds nothing, and the order-keeping
    de-duplication below absorbs a base that is also listed explicitly.
    """
    hermes_home_env = os.environ.get("HERMES_HOME", "").strip()
    if hermes_home_env:
        bases = [Path(d.strip()).expanduser() for d in hermes_home_env.split(",") if d.strip()]
    elif osinfo.is_windows():
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        bases = [Path(base) / "hermes"]
    else:
        bases = [Path.home() / ".hermes"]

    dirs: List[Path] = []
    for base in bases:
        dirs.append(base)
        profiles_root = base / "profiles"
        try:
            profiles = sorted(profiles_root.iterdir()) if profiles_root.is_dir() else []
        except OSError:
            profiles = []
        dirs.extend(p for p in profiles if p.is_dir())

    seen: set = set()
    out: List[Path] = []
    for d in dirs:
        key = str(d)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


# --- Reasonix -----------------------------------------------------------------


def reasonix_home() -> Path:
    """``$REASONIX_HOME`` if set, else ``~/.reasonix``. Empty/whitespace counts as unset.

    This override is Tokdash-side: it points the reader at a Reasonix home,
    which is not the same as Reasonix itself honoring the variable.
    """
    explicit = os.environ.get("REASONIX_HOME", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_absolute() else path.resolve()
    return Path.home() / ".reasonix"


def reasonix_stats_dir() -> Path:
    return reasonix_home() / "stats"


def reasonix_projects_dir() -> Path:
    return reasonix_home() / "projects"


# --- WorkBuddy ----------------------------------------------------------------


def workbuddy_roots() -> List[Path]:
    """WorkBuddy data roots: ``$WORKBUDDY_DATA_DIR`` (comma-separated) else ``~/.workbuddy-ai``.

    The native path is the same on macOS, Linux, and Windows. On WSL the user
    points the override at the Windows store (``/mnt/c/Users/<user>/.workbuddy-ai``),
    or at several roots at once, comma-separated.
    """
    explicit = os.environ.get("WORKBUDDY_DATA_DIR", "")
    roots: List[Path] = []
    for part in explicit.split(","):
        part = part.strip()
        if not part:
            continue
        path = Path(part).expanduser()
        if not path.is_absolute():
            path = path.resolve()
        if path not in roots:
            roots.append(path)
    if roots:
        return roots
    return [Path.home() / ".workbuddy-ai"]


# --- OpenClaw -----------------------------------------------------------------


def openclaw_home() -> Path:
    """``$OPENCLAW_HOME`` if set, else ``~/.openclaw``. Empty/whitespace counts as unset.

    This override is Tokdash-side: it points the reader at an OpenClaw home,
    which is not the same as OpenClaw itself honoring the variable. The
    native-Windows data dir is unverified (no row in WINDOWS_CLIENT_PATHS.md),
    so the default is the portable-dotfile assumption shared by every other
    client here — if it differs on Windows, set ``OPENCLAW_HOME``.
    """
    explicit = os.environ.get("OPENCLAW_HOME", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_absolute() else path.resolve()
    return Path.home() / ".openclaw"


def openclaw_agent_sessions_glob() -> str:
    """Session dirs of every OpenClaw agent: ``<home>/agents/*/sessions``."""
    return str(openclaw_home() / "agents" / "*" / "sessions")


# --- ZCode --------------------------------------------------------------------


def zcode_home() -> Path:
    """``$ZCODE_HOME`` if set, else ``~/.zcode``. Empty/whitespace counts as unset.

    ZCode itself honors ``ZCODE_HOME`` for its data dir, so the reader follows an
    overridden home; on Windows the default is ``%USERPROFILE%\\.zcode``.
    """
    explicit = os.environ.get("ZCODE_HOME", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_absolute() else path.resolve()
    return Path.home() / ".zcode"


def zcode_db_path() -> Path:
    """ZCode session database (WAL mode; ``-wal``/``-shm`` siblings live beside it)."""
    return zcode_home() / "cli" / "db" / "db.sqlite"


# --- Qoder -------------------------------------------------------------------

_QODER_IDE_DB_SUFFIX = Path("SharedClientCache") / "cache" / "db" / "local.db"


def _wsl_windows_root() -> Path:
    """The drvfs mount of the Windows C: drive, for the WSL branch only.

    Kept as a seam so the WSL candidate glob can be tested against a tmp tree.
    """
    return Path("/mnt/c")


def qoder_ide_db_path() -> Optional[Path]:
    """First existing Qoder IDE cache DB in priority order, else None.

    One install per machine is the normal state, and the parser snapshots
    exactly one DB. A single deterministic winner (not a scan of all
    candidates) keeps a session present in two stores -- reinstall/migration
    copies -- from being counted twice: chat_message.id is a content id, so a
    copied row keeps its id and would collide on entry_id.

    Priority: QODER_IDE_DATA_DIR override > native platform dirs > WSL glob.
    Brand order follows each platform's candidate list: QoderCN before Qoder
    on Windows/WSL, Qoder before QoderCN on macOS and Linux.
    """

    def first_file(paths: List[Path]) -> Optional[Path]:
        return next((p for p in paths if p.is_file()), None)

    env = os.environ.get("QODER_IDE_DATA_DIR", "").strip()
    if env:
        return first_file([Path(env).expanduser() / _QODER_IDE_DB_SUFFIX])

    kind = osinfo.os_kind()
    if kind == "windows":
        appdata = os.environ.get("APPDATA", "").strip()
        base = Path(appdata).expanduser() if appdata else Path.home() / "AppData" / "Roaming"
        return first_file([base / d / _QODER_IDE_DB_SUFFIX for d in ("QoderCN", "Qoder")])
    if kind == "wsl":
        # The Windows GUI lives under the drvfs mount. Brand priority first,
        # then path order within a brand: sorting one combined set would
        # order by Windows user name and break the QoderCN-before-Qoder
        # priority across users.
        candidates: List[Path] = []
        suffix = str(_QODER_IDE_DB_SUFFIX)
        for d in ("QoderCN", "Qoder"):
            candidates += sorted(_wsl_windows_root().glob(f"Users/*/AppData/Roaming/{d}/{suffix}"))
        return first_file(candidates)
    if kind == "macos":
        base = Path.home() / "Library" / "Application Support"
        return first_file([base / d / _QODER_IDE_DB_SUFFIX for d in ("Qoder", "QoderCN")])
    # Native Linux.
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return first_file([base / d / _QODER_IDE_DB_SUFFIX for d in ("Qoder", "QoderCN")])


def qoder_cli_roots() -> List[Path]:
    """All existing Qoder CLI data roots, in scan order, deduplicated.

    A union, not a switch: QODER_CLI_HOME (Tokdash-only, comma-separated)
    first, then QODER_CONFIG_DIR (Qoder's real single-root override), then
    the two default homes. A custom root does not displace the defaults --
    older sessions can still live under a default dir and the usage history
    should still count. Overlapping roots can present the same session twice
    (symlinks, migration copies); the CLI parser dedupes by request id.
    """
    roots: List[Path] = []
    for raw in os.environ.get("QODER_CLI_HOME", "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = path.resolve()
        if path not in roots:
            roots.append(path)
    explicit = os.environ.get("QODER_CONFIG_DIR", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if path not in roots:
            roots.append(path)
    for default in (Path.home() / ".qoder", Path.home() / ".qoder-cn"):
        if default not in roots:
            roots.append(default)
    return [root for root in roots if root.is_dir()]


# --- Zed ---------------------------------------------------------------------


def zed_data_dir() -> Path:
    """Zed data dir (mirrors paths.rs ``data_dir``).

    No env-var override exists in Zed; the ``--user-data-dir`` launch flag
    is the only relocation knob and is a documented blind spot here.
    Flatpak substitutes FLATPAK_XDG_DATA_HOME for the XDG data dir, but
    paths.rs (152-158) joins APP_NAME_LOWERCASE onto the whole if/else,
    so the Flatpak dir is ``$FLATPAK_XDG_DATA_HOME/zed`` like every other
    Linux install.
    """
    kind = osinfo.os_kind()
    if kind == "macos":
        return Path.home() / "Library" / "Application Support" / "Zed"
    if kind == "windows":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local).expanduser() if local else Path.home() / "AppData" / "Local"
        return base / "Zed"
    flatpak = os.environ.get("FLATPAK_XDG_DATA_HOME", "").strip()
    if flatpak:
        return Path(flatpak).expanduser() / "zed"
    explicit = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(explicit).expanduser() if explicit else Path.home() / ".local" / "share"
    return base / "zed"


def zed_threads_db() -> Optional[Path]:
    """Zed agent-thread database, or None when the install has none."""
    path = zed_data_dir() / "threads" / "threads.db"
    return path if path.is_file() else None


# --- Qwen Code ------------------------------------------------------------------


def qwen_runtime_base() -> Path:
    """Qwen Code runtime base dir: ``$QWEN_RUNTIME_DIR`` > ``$QWEN_HOME`` >
    ``~/.qwen`` (storage.ts getRuntimeBaseDir; the in-process and
    settings-file overrides are not reachable from tokdash — a documented
    blind spot)."""
    explicit = os.environ.get("QWEN_RUNTIME_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    home = os.environ.get("QWEN_HOME", "").strip()
    if home:
        return Path(home).expanduser()
    return Path.home() / ".qwen"


def qwen_chat_files(base: Optional[Path] = None) -> List[Path]:
    """All Qwen Code session JSONL files, de-duplicated.

    Current layout ``<base>/projects/<id>/chats/*.jsonl`` plus the
    pre-rename legacy ``<base>/tmp/<id>/chats/*.jsonl`` (the class
    docstring still documents it; older installs may have files there).
    Resolved paths are de-duplicated in case the two trees overlap.
    Absent base -> no files.
    """
    root = base if base is not None else qwen_runtime_base()
    seen = set()
    out: List[Path] = []
    for pattern in ("projects/*/chats/*.jsonl", "tmp/*/chats/*.jsonl"):
        for path in sorted(root.glob(pattern)):
            if not path.is_file():
                continue
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved in seen:
                continue
            seen.add(resolved)
            out.append(path)
    return out


# --- Crush ---------------------------------------------------------------------


def crush_data_dirs() -> List[Path]:
    """Crush data dirs from ``$CRUSH_DATA_DIR`` (comma-separated, ``~``
    allowed).

    Crush stores ``crush.db`` inside each project's data dir (default:
    ``.crush`` relative to the working directory) — there is no global
    root to scan, so Tokdash reads only the dirs the user lists. Entries
    without a ``crush.db`` are dropped. Unset/empty -> no dirs, no rows.
    """
    out: List[Path] = []
    for raw in os.environ.get("CRUSH_DATA_DIR", "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = path.resolve()
        if (path / "crush.db").is_file() and path not in out:
            out.append(path)
    return out


# --- Tokdash data dir / usage DB -------------------------------------------------
#
# Mirrors onboard/paths.py::data_dir() (kept as a separate, untouched copy there —
# see this module's docstring). Centralized here only for usage_store.py and the
# sources/sessions call sites.


def tokdash_data_dir() -> Path:
    """Resolved Tokdash data dir: ``$TOKDASH_DATA_DIR`` if set, else ``~/.tokdash``."""
    return Path(os.environ.get("TOKDASH_DATA_DIR", "~/.tokdash")).expanduser()


def usage_db_path() -> Path:
    """``$TOKDASH_USAGE_DB_PATH`` if set, else ``<data dir>/usage.sqlite3``."""
    explicit = os.environ.get("TOKDASH_USAGE_DB_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return tokdash_data_dir() / "usage.sqlite3"
