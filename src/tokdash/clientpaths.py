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


# --- CC Switch --------------------------------------------------------------


def cc_switch_root() -> Path:
    explicit = os.environ.get("CC_SWITCH_CONFIG_DIR", "").strip()
    return Path(explicit).expanduser() if explicit else Path.home() / ".cc-switch"


def cc_switch_db_path() -> Path:
    return cc_switch_root() / "cc-switch.db"


# --- Pi Agent -----------------------------------------------------------------


def pi_agent_search_dirs() -> List[Path]:
    """``$PI_AGENT_DIR`` (comma-separated) if set, else ``~/.pi/agent/sessions``."""
    pi_dir_env = os.environ.get("PI_AGENT_DIR", "").strip()
    if pi_dir_env:
        return [Path(d.strip()).expanduser() for d in pi_dir_env.split(",") if d.strip()]
    return [Path.home() / ".pi" / "agent" / "sessions"]


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
    """``$HERMES_HOME`` (comma-separated) if set, else ``~/.hermes`` (``%LOCALAPPDATA%\\hermes`` on Windows)."""
    hermes_home_env = os.environ.get("HERMES_HOME", "").strip()
    if hermes_home_env:
        return [Path(d.strip()).expanduser() for d in hermes_home_env.split(",") if d.strip()]
    if osinfo.is_windows():
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return [Path(base) / "hermes"]
    return [Path.home() / ".hermes"]


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
