"""Environment detection for the setup engine (plan §8.2).

Each external probe is a small, separately-monkeypatchable function so the planner
and tests can simulate any machine. Every probe fails safe: detection must never
crash setup, and an unknown answer is treated conservatively by the planner.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from .. import osinfo
from . import manifest, paths

MIN_PYTHON = (3, 10)

# --- OS / session ---------------------------------------------------------------
#
# Canonical implementation lives in ``tokdash.osinfo`` (Tier 0 seams refactor).
# These wrappers exist so existing callers of ``detect.os_kind()`` / ``detect.is_wsl()``
# (and tests that ``monkeypatch.setattr(detect, "os_kind", ...)``) keep working unchanged.


def is_wsl() -> bool:
    return osinfo.is_wsl()


def os_kind() -> str:
    """One of ``linux`` | ``wsl`` | ``macos`` | ``windows``."""
    return osinfo.os_kind()


def is_tty() -> bool:
    """True only when both stdin and stdout are real terminals."""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def systemd_user_available() -> bool:
    """Whether per-user systemd units can be managed without ``sudo``.

    Linux/WSL only; requires a running user manager (``systemctl --user`` answers).
    Fails closed so a missing/disabled systemd falls back to foreground guidance.
    """
    if os_kind() not in {"linux", "wsl"}:
        return False
    if not shutil.which("systemctl"):
        return False
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            capture_output=True,
            timeout=5,
        )
        return proc.returncode == 0
    except Exception:
        return False


# --- Python fitness -------------------------------------------------------------


def _query_python_version(executable: str) -> Optional[tuple]:
    try:
        out = subprocess.run(
            [executable, "-c", "import sys;print('%d %d %d' % sys.version_info[:3])"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            return tuple(int(p) for p in out.stdout.split())
    except Exception:
        return None
    return None


def python_fitness(executable: Optional[str] = None) -> Dict[str, Any]:
    """Assess an interpreter for running Tokdash and building a managed venv.

    Checks: version >= 3.10 (``requires-python``), and that ``venv`` and
    ``ensurepip``/``pip`` are importable (a ``venv``-less ``python3`` cannot create
    the managed runtime — common on Debian without ``python3-venv``).
    """
    executable = executable or sys.executable
    is_current = Path(executable) == Path(sys.executable)

    if is_current:
        version = sys.version_info[:3]
        has_venv = importlib.util.find_spec("venv") is not None
        has_pip = importlib.util.find_spec("pip") is not None
        has_ensurepip = importlib.util.find_spec("ensurepip") is not None
    else:
        version = _query_python_version(executable)
        if version is None:
            return {
                "executable": executable,
                "version": None,
                "version_ok": False,
                "has_venv": False,
                "has_pip": False,
                "fit": False,
                "reason": f"could not run {executable}",
            }

        def _probe(mod: str) -> bool:
            try:
                return (
                    subprocess.run(
                        [executable, "-c", f"import importlib.util,sys; sys.exit(0 if importlib.util.find_spec({mod!r}) else 1)"],
                        capture_output=True,
                        timeout=5,
                    ).returncode
                    == 0
                )
            except Exception:
                return False

        has_venv = _probe("venv")
        has_pip = _probe("pip")
        has_ensurepip = _probe("ensurepip")

    version_ok = tuple(version) >= MIN_PYTHON
    fit = version_ok and has_venv and (has_pip or has_ensurepip)

    reason = None
    if not version_ok:
        reason = (
            f"Python {'.'.join(map(str, version))} is too old; Tokdash needs "
            f">= {'.'.join(map(str, MIN_PYTHON))}."
        )
    elif not has_venv:
        reason = "the `venv` module is missing (Debian/Ubuntu: `sudo apt install python3-venv`)."
    elif not (has_pip or has_ensurepip):
        reason = "`pip`/`ensurepip` are missing (Debian/Ubuntu: `sudo apt install python3-pip`)."

    return {
        "executable": executable,
        "version": ".".join(map(str, version)),
        "version_ok": version_ok,
        "has_venv": has_venv,
        "has_pip": has_pip or has_ensurepip,
        "fit": fit,
        "reason": reason,
    }


def launchd_available() -> bool:
    """Whether per-user launchd agents can be managed (macOS only)."""
    return os_kind() == "macos" and shutil.which("launchctl") is not None


def winsched_available() -> bool:
    """Whether a per-user Windows Task Scheduler task can be managed (Windows only)."""
    return os_kind() == "windows" and shutil.which("schtasks") is not None


def tailscale_available() -> bool:
    """Whether the `tailscale` CLI is present (for wizard-run Tailscale Serve)."""
    return shutil.which("tailscale") is not None


def find_pipx() -> Optional[str]:
    return shutil.which("pipx")


def pipx_tokdash_python() -> Optional[str]:
    """Path to the python inside a user ``pipx install tokdash`` env, if present.

    Used by ``--runtime pipx`` (detect-and-use only; setup never *creates* a pipx env).
    """
    home = Path("~").expanduser()
    candidates = [
        home / ".local" / "pipx" / "venvs" / "tokdash" / "bin" / "python",
        home / ".local" / "share" / "pipx" / "venvs" / "tokdash" / "bin" / "python",
    ]
    if os_kind() == "windows":
        # pipx's default Windows venv layout puts the interpreter under Scripts/, same as
        # any other Windows venv (paths.managed_venv_python() makes the same swap).
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        if local_appdata:
            candidates.insert(0, Path(local_appdata).expanduser() / "pipx" / "venvs" / "tokdash" / "Scripts" / "python.exe")
        candidates.append(home / "pipx" / "venvs" / "tokdash" / "Scripts" / "python.exe")
    pipx_home = os.environ.get("PIPX_HOME", "").strip()
    if pipx_home:
        suffix = ("Scripts", "python.exe") if os_kind() == "windows" else ("bin", "python")
        candidates.insert(0, Path(pipx_home).expanduser() / "venvs" / "tokdash" / suffix[0] / suffix[1])
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def classify_current_runtime() -> Dict[str, Any]:
    """Classify the interpreter currently running setup.

    The invoking interpreter necessarily already has Tokdash (bootstrap reality,
    §13.1). We record whether it looks pipx-managed or managed-venv so a later
    ``update`` knows how to upgrade in place.
    """
    exe = str(Path(sys.executable))
    norm = exe.replace("\\", "/")
    managed = str(paths.managed_venv_python()).replace("\\", "/")
    if norm == managed:
        kind, method = "venv", "managed-venv"
    elif "pipx" in norm and "venvs" in norm:
        kind, method = "pipx", "pipx"
    else:
        kind, method = "existing", "existing"
    return {
        "kind": kind,
        "install_method": method,
        "python": exe,
        "command": [exe, "-m", "tokdash"],
    }


# --- existing service / runtime -------------------------------------------------


def existing_service() -> Dict[str, Any]:
    """Report any service file present (setup-written or manual)."""
    unit = paths.systemd_unit_path()
    plist = paths.launchd_plist_path()
    task = paths.winsched_task_path() if os_kind() == "windows" else None
    return {
        "systemd_unit": str(unit) if unit.is_file() else None,
        "launchd_plist": str(plist) if plist.is_file() else None,
        "winsched_task": str(task) if task and task.is_file() else None,
    }


def managed_runtime_present() -> bool:
    return paths.managed_venv_python().is_file() and paths.runtime_marker_path().is_file()


# --- port probe -----------------------------------------------------------------


def probe_port(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> Dict[str, Any]:
    """Is ``port`` occupied, and if so is the occupant Tokdash?

    Identity comes from the distinctive ``/health`` fingerprint
    (``service == "tokdash"``), never a generic ``{"status":"ok"}`` (plan §11, §21).
    """
    info: Dict[str, Any] = {"port": port, "open": False, "is_tokdash": False, "version": None}
    # Socket *creation* (not just connect) can raise in a restricted sandbox (seccomp ->
    # PermissionError), so it must be inside the fail-closed try or `setup --dry-run`
    # crashes instead of producing a plan. Treat any socket error as "port not open".
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
            info["open"] = True
        finally:
            sock.close()
    except OSError:
        return info

    if not info["open"]:
        return info

    url_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    try:
        with urllib.request.urlopen(f"http://{url_host}:{port}/health", timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if isinstance(body, dict) and body.get("service") == "tokdash":
            info["is_tokdash"] = True
            info["version"] = body.get("version")
    except Exception:
        pass
    return info


def find_free_port(start: int, host: str = "127.0.0.1", limit: int = 64) -> Optional[int]:
    """First free port at or after ``start`` (auto-pick when the default is busy)."""
    for candidate in range(start, start + limit):
        if not probe_port(candidate, host)["open"]:
            return candidate
    return None


# --- aggregate ------------------------------------------------------------------


def detect_all(port: int) -> Dict[str, Any]:
    """One-shot detection snapshot used by the planner and ``doctor``."""
    return {
        "os": os_kind(),
        "is_wsl": is_wsl(),
        "tty": is_tty(),
        "systemd_user": systemd_user_available(),
        "launchd": launchd_available(),
        "winsched": winsched_available(),
        "tailscale": tailscale_available(),
        "python": python_fitness(),
        "pipx": find_pipx(),
        "pipx_tokdash": pipx_tokdash_python(),
        "current_runtime": classify_current_runtime(),
        "existing_service": existing_service(),
        "managed_runtime": managed_runtime_present(),
        "port": probe_port(port),
        "data_dir": str(paths.data_dir()),
        "manifest": manifest.read_manifest(),
    }
