"""Tests for the Hermes Windows-path branch in ``clientpaths.hermes_search_dirs``.

Only Hermes needs a real Windows branch (see ``docs/development/technical-notes/WINDOWS_CLIENT_PATHS.md``); every
other client is already correct on Windows via ``Path.home()`` and is untouched by this
chunk. Branching is done on ``tokdash.osinfo.is_windows()`` (not ``os.name``) so the
Windows path can be exercised on this Linux host without monkeypatching ``os.name``,
which would corrupt ``pathlib``'s ``WindowsPath``/``PosixPath`` dispatch process-wide
(see ``onboard/paths.py::_windows_venv_layout`` for the same pattern).
"""
from pathlib import Path

from tokdash import clientpaths, osinfo


def test_hermes_search_dirs_posix_default(monkeypatch, tmp_path):
    """POSIX default: ``~/.hermes``, and nothing more when it has no profiles.

    Home is isolated to ``tmp_path``: the profile scan reads the real
    ``~/.hermes/profiles`` otherwise, so this would fail on exactly the
    multi-profile machines the scan exists for.
    """
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(osinfo, "is_windows", lambda: False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert clientpaths.hermes_search_dirs() == [tmp_path / ".hermes"]


def test_hermes_search_dirs_windows_default(monkeypatch):
    """Simulated Windows default: ``%LOCALAPPDATA%\\hermes``."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(osinfo, "is_windows", lambda: True)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    assert clientpaths.hermes_search_dirs() == [Path(r"C:\Users\x\AppData\Local") / "hermes"]


def test_hermes_search_dirs_windows_default_no_localappdata(monkeypatch, tmp_path):
    """Simulated Windows default with ``LOCALAPPDATA`` unset: falls back to ``~/AppData/Local``.

    Home is isolated to ``tmp_path`` for the same reason as the POSIX default.
    """
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(osinfo, "is_windows", lambda: True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert clientpaths.hermes_search_dirs() == [tmp_path / "AppData" / "Local" / "hermes"]


def test_hermes_search_dirs_env_override_posix(monkeypatch):
    """``HERMES_HOME`` override still wins on POSIX, unchanged comma-split behavior."""
    monkeypatch.setenv("HERMES_HOME", "/a/b,/c/d")
    assert clientpaths.hermes_search_dirs() == [Path("/a/b"), Path("/c/d")]


def test_hermes_search_dirs_env_override_windows(monkeypatch):
    """``HERMES_HOME`` override still wins on simulated Windows, unchanged comma-split behavior."""
    monkeypatch.setattr(osinfo, "is_windows", lambda: True)
    monkeypatch.setenv("HERMES_HOME", "/a/b,/c/d")
    assert clientpaths.hermes_search_dirs() == [Path("/a/b"), Path("/c/d")]


def test_hermes_search_dirs_scans_named_profiles(monkeypatch, tmp_path):
    """Named profiles under ``<home>/profiles/<name>`` are scanned by default."""
    home = tmp_path / ".hermes"
    (home / "profiles" / "robot2").mkdir(parents=True)
    (home / "profiles" / "robot1").mkdir(parents=True)
    (home / "profiles" / "notes.txt").write_text("not a profile")
    monkeypatch.setenv("HERMES_HOME", str(home))

    assert clientpaths.hermes_search_dirs() == [
        home,
        home / "profiles" / "robot1",
        home / "profiles" / "robot2",
    ]


def test_hermes_search_dirs_profiles_under_each_env_home(monkeypatch, tmp_path):
    """Each ``HERMES_HOME`` entry contributes its own profiles, base first."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    (a / "profiles" / "p1").mkdir(parents=True)
    (b / "profiles" / "p2").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", f"{a},{b}")

    assert clientpaths.hermes_search_dirs() == [
        a, a / "profiles" / "p1",
        b, b / "profiles" / "p2",
    ]


def test_hermes_search_dirs_dedups_explicitly_listed_profile(monkeypatch, tmp_path):
    """The pre-fix workaround (home + its profiles listed by hand) yields no duplicates."""
    home = tmp_path / ".hermes"
    profile = home / "profiles" / "robot1"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", f"{home},{profile}")

    assert clientpaths.hermes_search_dirs() == [home, profile]


def test_hermes_search_dirs_no_profiles_dir(monkeypatch, tmp_path):
    """A home without ``profiles/`` is unchanged."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    assert clientpaths.hermes_search_dirs() == [home]


def test_pi_agent_search_dirs_default(monkeypatch):
    monkeypatch.delenv("PI_AGENT_DIR", raising=False)
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    assert clientpaths.pi_agent_search_dirs() == [Path.home() / ".pi" / "agent" / "sessions"]


def test_pi_agent_search_dirs_upstream_env_names(monkeypatch):
    monkeypatch.delenv("PI_AGENT_DIR", raising=False)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "/pi/agent")
    assert clientpaths.pi_agent_search_dirs() == [Path("/pi/agent/sessions")]


def test_pi_agent_search_dirs_session_dir_wins(monkeypatch):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "/pi/agent")
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", "/pi/sessions")
    assert clientpaths.pi_agent_search_dirs() == [Path("/pi/sessions")]


def test_pi_agent_search_dirs_legacy_comma_list(monkeypatch):
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    monkeypatch.setenv("PI_AGENT_DIR", "/a/b,/c/d")
    assert clientpaths.pi_agent_search_dirs() == [Path("/a/b"), Path("/c/d")]


def test_openclaw_home_and_sessions_glob(monkeypatch):
    monkeypatch.delenv("OPENCLAW_HOME", raising=False)
    assert clientpaths.openclaw_home() == Path.home() / ".openclaw"
    assert clientpaths.openclaw_agent_sessions_glob() == str(Path.home() / ".openclaw" / "agents" / "*" / "sessions")


def test_openclaw_home_env_override(monkeypatch, tmp_path):
    # tmp_path, not a POSIX-style "/claw": on Windows a rooted path with no drive
    # is not absolute, so openclaw_home() resolves it against the current drive and
    # the comparison depends on which drive the tests run from.
    home = tmp_path / "claw"
    monkeypatch.setenv("OPENCLAW_HOME", str(home))
    assert clientpaths.openclaw_home() == home
    assert clientpaths.openclaw_agent_sessions_glob() == str(home / "agents" / "*" / "sessions")


def test_quota_client_roots_honor_environment_overrides(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/tmp/codex-home")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/tmp/claude-config")

    assert clientpaths.codex_home() == Path("/tmp/codex-home")
    assert clientpaths.codex_sessions_dir() == Path("/tmp/codex-home") / "sessions"
    assert clientpaths.codex_archived_sessions_dir() == Path("/tmp/codex-home") / "archived_sessions"
    assert clientpaths.claude_config_dir() == Path("/tmp/claude-config")
    assert clientpaths.antigravity_cli_dir() == Path.home() / ".gemini" / "antigravity-cli"
    assert clientpaths.antigravity_conversations_dir() == Path.home() / ".gemini" / "antigravity-cli" / "conversations"
    assert clientpaths.antigravity_conversations_glob() == str(Path.home() / ".gemini" / "antigravity-cli" / "conversations" / "*.db")
