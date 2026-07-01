"""Tests for the Hermes Windows-path branch in ``clientpaths.hermes_search_dirs``.

Only Hermes needs a real Windows branch (see ``docs/WINDOWS_CLIENT_PATHS.md``); every
other client is already correct on Windows via ``Path.home()`` and is untouched by this
chunk. Branching is done on ``tokdash.osinfo.is_windows()`` (not ``os.name``) so the
Windows path can be exercised on this Linux host without monkeypatching ``os.name``,
which would corrupt ``pathlib``'s ``WindowsPath``/``PosixPath`` dispatch process-wide
(see ``onboard/paths.py::_windows_venv_layout`` for the same pattern).
"""
from pathlib import Path

from tokdash import clientpaths, osinfo


def test_hermes_search_dirs_posix_default(monkeypatch):
    """POSIX default (this host): ``~/.hermes``, unchanged."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert clientpaths.hermes_search_dirs() == [Path.home() / ".hermes"]


def test_hermes_search_dirs_windows_default(monkeypatch):
    """Simulated Windows default: ``%LOCALAPPDATA%\\hermes``."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(osinfo, "is_windows", lambda: True)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    assert clientpaths.hermes_search_dirs() == [Path(r"C:\Users\x\AppData\Local") / "hermes"]


def test_hermes_search_dirs_windows_default_no_localappdata(monkeypatch):
    """Simulated Windows default with ``LOCALAPPDATA`` unset: falls back to ``~/AppData/Local``."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(osinfo, "is_windows", lambda: True)
    assert clientpaths.hermes_search_dirs() == [Path.home() / "AppData" / "Local" / "hermes"]


def test_hermes_search_dirs_env_override_posix(monkeypatch):
    """``HERMES_HOME`` override still wins on POSIX, unchanged comma-split behavior."""
    monkeypatch.setenv("HERMES_HOME", "/a/b,/c/d")
    assert clientpaths.hermes_search_dirs() == [Path("/a/b"), Path("/c/d")]


def test_hermes_search_dirs_env_override_windows(monkeypatch):
    """``HERMES_HOME`` override still wins on simulated Windows, unchanged comma-split behavior."""
    monkeypatch.setattr(osinfo, "is_windows", lambda: True)
    monkeypatch.setenv("HERMES_HOME", "/a/b,/c/d")
    assert clientpaths.hermes_search_dirs() == [Path("/a/b"), Path("/c/d")]
