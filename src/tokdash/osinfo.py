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
