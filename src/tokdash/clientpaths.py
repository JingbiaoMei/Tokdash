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
from typing import List, Optional, Tuple

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


# --- Command Code ---------------------------------------------------------------


def commandcode_home() -> Path:
    """Command Code's own home: ``~/.commandcode``.

    The vendor CLI has no env override for this directory (it builds the path from
    ``$HOME``/``%USERPROFILE%`` and the literal name ``.commandcode``), so there is
    nothing to resolve but the home plus the fixed name.
    """
    return Path.home() / ".commandcode"


def commandcode_auth_path() -> Path:
    """Command Code's persisted sign-in: ``~/.commandcode/auth.json`` (``{apiKey, userName}``)."""
    return commandcode_home() / "auth.json"


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


CLAUDE_CONFIG_PREFIX = ".claude"
CLAUDE_DEFAULT_PROFILE = "default"


def _resolve(path: Path) -> str:
    """Best-effort canonical path for dedupe; ``""`` when the filesystem says no."""
    try:
        return str(path.resolve())
    except Exception:
        return ""


def _sanitize_profile_slug(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in text).strip("-.")


def claude_profile_slug(path: Path) -> str:
    """Profile name for one Claude config dir: ``~/.claude-academic`` -> ``academic``.

    The slug doubles as the quota account id, so it is restricted to characters that
    read well in a bucket id (``academic_session``) and in a dashboard label. A dir
    named ``.claude-default`` keeps its full name rather than taking the built-in
    profile's id, which would merge two subscriptions into one account.

    This is the name a directory has on its own, before ``claude_profile_dirs`` reassigns
    it around whatever ``CLAUDE_CONFIG_DIR`` claimed the default slot. Callers that need to
    recognise a directory by a name it may already have been stored under -- rather than
    the name it happens to be given on this run -- want this one.
    """
    name = path.name
    if name == CLAUDE_CONFIG_PREFIX:
        return CLAUDE_DEFAULT_PROFILE
    if name.startswith(f"{CLAUDE_CONFIG_PREFIX}-"):
        slug = _sanitize_profile_slug(name[len(CLAUDE_CONFIG_PREFIX) + 1:])
        if slug and slug != CLAUDE_DEFAULT_PROFILE:
            return slug
    return _sanitize_profile_slug(name) or name


def claude_profile_dirs() -> List[Tuple[str, Path]]:
    """Every Claude Code install on this machine, as ``(profile, config_dir)`` pairs.

    ``~/.claude`` (or ``$CLAUDE_CONFIG_DIR``) is always first and is named ``default``.
    A sibling such as ``~/.claude-academic`` is a second install the user set up by hand
    and runs as ``CLAUDE_CONFIG_DIR=~/.claude-academic claude``; it signs in to its own
    subscription and keeps its own ``.credentials.json``, so it needs its own quota. The
    directory suffix is the profile name, which is what the quota dashboard displays.

    ``TOKDASH_CLAUDE_PROFILES`` (path-separated dirs) replaces the ``~/.claude*`` scan for
    installs that live outside the home directory; the profile name is then the directory
    name. Duplicates of the default dir (and of each other, following symlinks) are
    dropped, since one subscription must not show up as two.

    Only the existence of the directory is checked here. Callers decide what makes a
    profile worth reporting (for quota, a ``.credentials.json`` of its own).
    """
    default_dir = claude_config_dir()
    out: List[Tuple[str, Path]] = [(CLAUDE_DEFAULT_PROFILE, default_dir)]
    seen: set[str] = {str(default_dir)}
    used: set[str] = {CLAUDE_DEFAULT_PROFILE}
    resolved = _resolve(default_dir)
    if resolved:
        seen.add(resolved)
    explicit = os.environ.get("TOKDASH_CLAUDE_PROFILES", "").strip()
    if explicit:
        candidates = [Path(entry).expanduser() for entry in explicit.split(os.pathsep) if entry.strip()]
    else:
        candidates = [p for p in sorted(Path.home().glob(f"{CLAUDE_CONFIG_PREFIX}*")) if p.is_dir()]
    for path in candidates:
        keys = {str(path)}
        resolved = _resolve(path)
        if resolved:
            keys.add(resolved)
        if keys & seen or not path.is_dir():
            continue
        seen |= keys
        slug = claude_profile_slug(path)
        # Distinct dirs must still land on distinct account ids, or their windows
        # would share a bucket and overwrite each other in the quota history.
        if slug in used:
            # ``$CLAUDE_CONFIG_DIR`` took the default slot, so this dir (usually a
            # plain ``~/.claude`` beside it) is named after itself instead.
            slug = _sanitize_profile_slug(path.name) or slug
        candidate = slug
        suffix = 2
        while candidate in used:
            candidate = f"{slug}-{suffix}"
            suffix += 1
        slug = candidate
        used.add(slug)
        out.append((slug, path))
    return out


def claude_project_dirs() -> List[Path]:
    """``projects/`` dir under every ``~/.claude*`` install (base + variants)."""
    return [p / "projects" for p in sorted(Path.home().glob(".claude*")) if (p / "projects").is_dir()]


# --- Gemini CLI -----------------------------------------------------------------


def gemini_root() -> Path:
    return Path.home() / ".gemini"


# Antigravity ships as three products that share one trajectory format and one
# sibling layout under ~/.gemini: the CLI, the ACP kernel (agy_acp_server, the
# binary an ACP host such as Paseo, Zed or JetBrains spawns), and the IDE.
# "antigravity-acp" is quoted from the official kernel's own help text;
# "antigravity-ide" is reported without a citation and unconfirmed here. Both
# are safe to list either way -- discovery drops homes that do not exist -- but
# see docs/development/technical-notes/WINDOWS_CLIENT_PATHS.md before treating
# the IDE path as verified.
ANTIGRAVITY_SIBLING_DIR_NAMES = ("antigravity-acp", "antigravity-ide")


def antigravity_cli_dir() -> Path:
    return gemini_root() / "antigravity-cli"


def antigravity_oauth_token_paths() -> List[Path]:
    """Current and legacy Antigravity OAuth token paths, in precedence order."""
    cli_dir = antigravity_cli_dir()
    return [
        cli_dir.parent / "jetski-standalone-oauth-token",
        cli_dir / "antigravity-oauth-token",
    ]


def antigravity_product_dirs() -> List[Path]:
    """Every existing Antigravity product home, in scan order, deduplicated.

    A union, not a switch: ``$ANTIGRAVITY_HOME`` (Tokdash-only, comma-separated)
    comes first, then the CLI home, then its ACP and IDE siblings. A custom home
    does not displace the defaults -- an ACP host and the CLI write to different
    dirs and both sets of sessions should count.

    Each product home holds ``conversations/*.db`` in the same schema, so one
    parser reads all of them. Only the CLI and IDE write a
    ``conversation_summaries.db``; the ACP kernel does not, and callers treat it
    as optional.

    The siblings are derived from ``antigravity_cli_dir()`` rather than
    ``gemini_root()`` so that patching the CLI home relocates the whole tree.

    Note the name is Tokdash's own: Antigravity does not currently read
    ``ANTIGRAVITY_HOME`` itself. Should Google ever ship it, expect relocate
    semantics rather than the additive ones here.
    """
    roots: List[Path] = []
    seen: set = set()

    def add(path: Path) -> None:
        # Dedupe on the canonical path, not the spelling: ANTIGRAVITY_HOME is
        # most useful pointed at a symlink, and a home reached twice would open
        # and reparse its conversation_summaries.db once per spelling.
        key = _resolve(path) or str(path)
        if key in seen:
            return
        seen.add(key)
        roots.append(path)

    for raw in os.environ.get("ANTIGRAVITY_HOME", "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = path.resolve()
        add(path)
    cli_dir = antigravity_cli_dir()
    for default in (cli_dir, *(cli_dir.parent / name for name in ANTIGRAVITY_SIBLING_DIR_NAMES)):
        add(default)
    return [root for root in roots if root.is_dir()]


def antigravity_conversation_dirs() -> List[Path]:
    return [d for d in (root / "conversations" for root in antigravity_product_dirs()) if d.is_dir()]


def antigravity_conversation_globs() -> List[str]:
    return [str(d / "*.db") for d in antigravity_conversation_dirs()]


def antigravity_summary_db_paths() -> List[Path]:
    """``conversation_summaries.db`` of every product home that has one."""
    paths = [root / "conversation_summaries.db" for root in antigravity_product_dirs()]
    return [p for p in paths if p.is_file()]


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


# --- MiniMax Code (mcode) ----------------------------------------------------
#
# MiniMax Code (npm @minimax-ai/code, CLI ``mcode``) is a different product
# from the ``mmx`` CLI above: mmx keeps its credentials in ``~/.mmx`` (which
# is what the MiniMax quota provider reads), while MiniMax Code lives under
# ``~/.minimax`` and provides its own session history.

_MINIMAX_CODE_DEFAULT_DIR = ".minimax"


def minimax_code_data_dir() -> Path:
    """``$MINIMAX_DATA_DIR``, else the legacy ``$MAVIS_DATA_DIR``, else ``~/.minimax``.

    Unlike most overrides in this module this one is the harness's own: the
    v0.4.12 bundle resolves the data dir as
    ``env.MINIMAX_DATA_DIR || env.MAVIS_DATA_DIR || default``, so an install
    relocated by either variable is followed automatically. Empty or
    whitespace-only values count as unset, matching the harness's ``.trim()``.
    """
    for name in ("MINIMAX_DATA_DIR", "MAVIS_DATA_DIR"):
        explicit = os.environ.get(name, "").strip()
        if explicit:
            return Path(explicit).expanduser()
    return Path.home() / _MINIMAX_CODE_DEFAULT_DIR


def minimax_code_session_files() -> List[Path]:
    """Every ``messages.jsonl`` under ``<home>/v2/sessions``, sorted.

    Layout verified against an installed v0.4.12: each session gets one
    nested date-sharded directory
    ``YYYY/MM/DD/<HH-MM-SS-mmm-session_<id>>/`` (the session manifest's own
    ``layout`` key reads ``v2-final-dated-session``) holding the
    append-only transcript ``messages.jsonl`` (plus ``llm-call.json``
    request captures and a ``manifest.json``, none of which carry usage the
    transcript does not). Discovery is a deep walk rather than a date-shaped
    pattern on purpose: the shard shape is the harness's business, and a build
    that changes it should cost nothing here. Dot-directories are skipped.
    Mirrors ``muse_session_files`` including the iterate-then-sort and
    keep-the-partial-walk contracts.
    """
    root = minimax_code_data_dir() / "v2" / "sessions"
    if not root.is_dir():
        return []
    out: List[Path] = []
    try:
        for path in root.rglob("messages.jsonl"):
            try:
                if not path.is_file():
                    continue
                rel_parts = path.relative_to(root).parts[:-1]
                if any(part.startswith(".") for part in rel_parts):
                    continue
            except OSError:
                continue
            out.append(path)
    except OSError:
        # Mid-walk OSError (permission, cloud placeholder): keep whatever the
        # walk collected before the failure, matching _rglob_sigs' behavior.
        pass
    out.sort()
    return out


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


# --- Muse (Meta Muse Code) ---------------------------------------------------------


def muse_sessions_root() -> Path:
    """Muse session store root: ``$XDG_DATA_HOME/muse/sessions`` (default
    ``~/.local/share/muse/sessions``).

    The XDG layout is the verified one (``evidence/muse_live_record_census.txt``,
    ``MUSE-FORMAT.md:8`` and the independent reader in ``acs_paths.ts:107`` all
    agree). Whether Muse honours anything else — a dedicated env var or the
    macOS/Windows app-data dirs — is unverified (FINDINGS.md open item 5), so
    no other override is promised here. ``MUSE_DATA_DIR`` appears in a
    community reader but in no Muse launcher or doc; that is the reader's own
    convention, not Muse's.

    A RELATIVE ``XDG_DATA_HOME`` is invalid and ignored per the Base
    Directory spec ("should be ignored" for relative values): resolving it
    against Tokdash's working directory would miss the real Muse history and
    scan an unrelated project-relative dir instead. Fall back to
    ``~/.local/share`` unless the value resolves absolute.
    """
    explicit = os.environ.get("XDG_DATA_HOME", "").strip()
    base = None
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_absolute():
            base = candidate
    if base is None:
        base = Path.home() / ".local/share"
    return base / "muse/sessions"


def muse_session_files() -> List[Path]:
    """Every ``session.jsonl`` under the Muse sessions root, recursively.

    Covers the date-sharded top-level logs (``YYYY/MM/DD/<uuid>/session.jsonl``)
    and the per-child ``subagent/<uuid>/session.jsonl`` logs in one pass; the
    rule never pattern-matches the date directories, so a build that changes
    the shard shape costs nothing here. Dot-directories are skipped — the
    ``.msp-view-v1`` view cache sits alongside the real logs and is not
    transcripts. Discovery is windowless on purpose: a first sync must index
    the whole history.
    """
    root = muse_sessions_root()
    if not root.is_dir():
        return []
    out: List[Path] = []
    try:
        # NOT sorted(root.rglob(...)): sorted() buffers the whole generator
        # before yielding anything, so a mid-walk OSError would escape with
        # nothing collected and the "keep the partial walk" contract below
        # would silently hold nothing. Iterate first, sort at the end —
        # matching _rglob_sigs.
        for path in root.rglob("session.jsonl"):
            try:
                if not path.is_file():
                    continue
                # Skip any file under a dot-directory (.msp-view-v1 cache and
                # any future hidden view/state dir).
                rel_parts = path.relative_to(root).parts[:-1]
                if any(part.startswith(".") for part in rel_parts):
                    continue
            except OSError:
                continue
            out.append(path)
    except OSError:
        # Mid-walk OSError (permission, cloud placeholder): keep whatever the
        # walk collected before the failure, matching _rglob_sigs' behavior.
        pass
    out.sort()
    return out


# --- Devin CLI ----------------------------------------------------------------


# Both names appear in the shipped binary's string table; one store keeps one
# live file, so the lookup order below is what stops a double read.
_DEVIN_DB_NAMES = ("sessions.db", "cli_sessions.db")


def devin_cli_roots() -> List[Path]:
    """Existing Devin CLI data dirs, in scan order, deduplicated.

    Devin CLI (Cognition) keeps its whole history in one SQLite store,
    ``<data dir>/sessions.db``. Documented locations (its own troubleshooting
    page, which names ``~/.local/share/devin/cli/logs/`` and
    ``%APPDATA%\\devin\\cli\\logs\\``): macOS and Linux use the XDG data dir
    with the CLI's ``devin/cli`` namespace, Windows uses Roaming AppData -- not
    Local, unlike Hermes.

    A union, not a platform switch, on purpose. One physical machine can hold
    both a WSL-guest install and a Windows-host install, which are two stores
    with disjoint session ids, and both are real usage. From WSL the host store
    sits behind the drvfs mount, so it is globbed the same way
    ``qoder_ide_db_path()`` globs it.

    The reverse direction is NOT globbed. A Tokdash running on the Windows host
    does not walk ``\\\\wsl.localhost\\<distro>\\home\\<user>\\.local\\share\\devin\\cli``,
    because enumerating every distro means reading a registry-backed namespace
    that a read-only path resolver has no business touching, and a dead WSL
    instance makes the UNC path hang rather than fail. Same limit as
    ``qoder_ide_db_path()``. A Windows-host dashboard pointed at a guest store
    sets ``DEVIN_CLI_DATA_DIRS`` to that UNC path.

    ``DEVIN_CLI_DATA_DIRS`` (Tokdash-only, comma-separated) is read FIRST and
    ADDS to the defaults rather than replacing them: a store on a Windows drive
    other than C: (this author's WSL has c through j mounted) or any relocated
    dir must be reachable without losing the normal one. The CLI's own variables
    are ``DEVIN_MODEL``, ``DEVIN_PERMISSION_MODE`` and ``DEVIN_SANDBOX``, none of
    which relocates the store; this one is named differently on purpose so it is
    never mistaken for a vendor variable.

    A RELATIVE ``XDG_DATA_HOME`` is invalid per the Base Directory spec and is
    ignored, matching ``muse_sessions_root()``. ``DEVIN_CLI_DATA_DIRS`` entries
    are explicit relocations, so a relative one is resolved, matching
    ``crush_data_dirs()``. Roots that contain neither database name are dropped:
    an empty ``.../devin/cli`` is not usage.
    """
    candidates: List[Path] = []

    for raw in os.environ.get("DEVIN_CLI_DATA_DIRS", "").split(","):
        raw = raw.strip()
        if raw:
            candidates.append(Path(raw).expanduser())

    kind = osinfo.os_kind()
    if kind == "windows":
        appdata = os.environ.get("APPDATA", "").strip()
        base = Path(appdata).expanduser() if appdata else Path.home() / "AppData" / "Roaming"
        candidates.append(base / "devin" / "cli")
    else:
        xdg = os.environ.get("XDG_DATA_HOME", "").strip()
        if xdg and not Path(xdg).expanduser().is_absolute():
            xdg = ""
        base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
        candidates.append(base / "devin" / "cli")
        if kind == "macos":
            # The XDG path above is DOCUMENTED for macOS, not borrowed from
            # Linux by omission: the CLI's own troubleshooting bundle gives
            # macOS the same layout as Linux (logs under
            # ``~/.local/share/devin/cli/logs/``, summaries under
            # ``$XDG_DATA_HOME/devin/``, config under ``~/.config/devin``), and
            # the ``/Library/Application Support/Devin/system.json`` in the same
            # docs is system-wide enterprise policy, not user data. So this
            # differs from qoder_ide_db_path() and zed's macOS branch, which
            # really are app-bundle apps.
            #
            # Library is still tried, second, and only for cheap insurance: the
            # binary is a Rust build and an app-data directory crate would map
            # there instead. A candidate that does not exist costs one stat; a
            # missing candidate costs a macOS user their whole history with no
            # failure signal at all. Roots are kept only when the store file is
            # in them, so both can be listed without a double read.
            candidates.append(Path.home() / "Library" / "Application Support" / "devin" / "cli")

    if kind == "wsl":
        # The host install lives under the drvfs mount. glob() returns nothing
        # when the mount is absent, so a bare-metal Linux host costs one stat.
        candidates += sorted(_wsl_windows_root().glob("Users/*/AppData/Roaming/devin/cli"))

    out: List[Path] = []
    for path in candidates:
        if not path.is_absolute():
            path = path.resolve()
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in out:
            continue
        if any((path / name).is_file() for name in _DEVIN_DB_NAMES):
            out.append(key)
    return out


def devin_db_paths() -> List[Path]:
    """The Devin databases to read: one per root, first existing name wins.

    ``sessions.db`` is the live name and ``cli_sessions.db`` the older one; both
    strings are compiled into v3000.10.31, and which one a given install writes
    is capture-pending (Q1). Precedence rather than union: reading both in one
    root would count the same history twice, which is the same rule Kilo applies
    to its pre-rename ``opencode*.db``. A root holding only the legacy file is
    still read, so an unmigrated install is not silently dropped.
    """
    out: List[Path] = []
    for root in devin_cli_roots():
        for name in _DEVIN_DB_NAMES:
            candidate = root / name
            if candidate.is_file():
                if candidate not in out:
                    out.append(candidate)
                break
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
