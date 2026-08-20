"""Centralized OS detection (Tier 0 seams refactor).

This is the canonical home for OS-kind detection. ``onboard/detect.py``
historically owned ``os_kind()``/``is_wsl()`` for the setup engine; those now
delegate here so there is exactly one implementation, while every existing
caller of ``detect.os_kind()`` (or anything reading ``detection["os"]``)
keeps working unchanged.

Each probe fails safe: detection must never raise, and an unknown answer is
treated conservatively by callers.
"""
from __future__ import annotations

import os
import platform
import sys
from pathlib import Path


def is_wsl() -> bool:
    """True when running on Linux under Windows Subsystem for Linux."""
    if sys.platform != "linux":
        return False
    if "microsoft" in platform.release().lower():
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def os_kind() -> str:
    """One of ``linux`` | ``wsl`` | ``macos`` | ``windows``."""
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    if is_wsl():
        return "wsl"
    return "linux"


def is_windows() -> bool:
    """True when ``os_kind()`` is ``windows``."""
    return os_kind() == "windows"


def is_macos() -> bool:
    """True when ``os_kind()`` is ``macos``."""
    return os_kind() == "macos"


def is_linux() -> bool:
    """True when ``os_kind()`` is ``linux`` (native Linux, not WSL)."""
    return os_kind() == "linux"


def has_display() -> bool:
    """Best-effort check for a usable GUI session.

    The SINGLE implementation shared by every code path that may open a browser
    (``cli.serve``'s auto-open and ``tokdash setup``'s optional open) — do not
    fork it. Returns False in headless contexts (CI, SSH sessions, test runs,
    systemd/launchd services, Linux without an X11/Wayland display) so no
    caller tries to launch a browser where there is none, or where the window
    would be detached and uncloseable.
    """
    # CI runners are headless regardless of OS. Most providers (GitHub Actions,
    # GitLab, Travis, CircleCI, ...) set CI=true; an explicit CI=false/0/no
    # counts as "not CI".
    ci = os.environ.get("CI", "").strip().lower()
    if ci and ci not in {"0", "false", "no"}:
        return False
    # A test run has a display but no one to show it to: a browser opened from
    # pytest is detached from the test process and nothing would ever close it
    # (incident: tests/conftest.py::no_browser_open). PYTEST_CURRENT_TEST
    # is inherited by subprocesses, so this intentionally also covers a
    # `tokdash serve` or `tokdash setup` shelled out from another project's
    # test suite.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    # A remote shell with no local console: opening a browser is wrong here
    # even on macOS/Windows.
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    # On Linux a GUI needs an X11 or Wayland display. macOS and Windows don't
    # expose these vars but do have a desktop session, so only gate on Linux.
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True
