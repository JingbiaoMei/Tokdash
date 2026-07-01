"""Runtime resolution and managed-venv creation (plan §9, §13.1).

Ownership is decided by *who provisioned the runtime*, never by its kind:
  - an existing importable Tokdash, a user pipx env  -> owned_by_setup = False (kept)
  - a managed venv setup builds under ``<data_dir>``  -> owned_by_setup = True  (removed)

setup never creates a pipx env (pipx is the user's recommended *install* path); the only
runtime setup owns is the managed venv (or a future binary).
"""
from __future__ import annotations

import subprocess
import sys
from typing import Any, Dict, Optional

from . import detect, paths


class RuntimeError_(RuntimeError):
    """A runtime selection/creation problem that should abort setup with a message."""


def resolve(runtime_flag: str, detection: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the service runtime from the ``--runtime`` choice + detection.

    Returns a dict: ``kind``, ``install_method``, ``command`` (argv), ``python``,
    ``owned_by_setup``, ``needs_create``, plus ``error`` when the choice is impossible.
    The returned ``command`` is always an executable argv (``[python, "-m", "tokdash"]``
    or the managed venv's python), never a bare ``tokdash`` (§9).
    """
    choice = (runtime_flag or "auto").lower()
    current = detection["current_runtime"]

    if choice in {"auto", "existing"}:
        # Prefer the interpreter already running setup — no install, no network. Ownership is
        # by PROVENANCE, not kind: if that interpreter IS the setup-created managed venv
        # (re-running setup from inside it), it stays setup-owned so uninstall can remove it.
        owned = current["install_method"] == "managed-venv" and detect.managed_runtime_present()
        return {
            "kind": current["kind"],
            "install_method": current["install_method"],
            "command": list(current["command"]),
            "python": current["python"],
            "owned_by_setup": owned,
            "needs_create": False,
            "error": None,
        }

    if choice == "pipx":
        py = detection.get("pipx_tokdash")
        if not py:
            return _error("--runtime pipx needs a pipx-installed Tokdash; run `pipx install tokdash` first.")
        return {
            "kind": "pipx",
            "install_method": "pipx",
            "command": [py, "-m", "tokdash"],
            "python": py,
            "owned_by_setup": False,
            "needs_create": False,
            "error": None,
        }

    if choice == "venv":
        fit = detection["python"]
        if not fit.get("fit"):
            return _error(f"cannot create a managed venv: {fit.get('reason') or 'Python is unfit'}")
        py = str(paths.managed_venv_python())
        return {
            "kind": "venv",
            "install_method": "managed-venv",
            "command": [py, "-m", "tokdash"],
            "python": py,
            "owned_by_setup": True,
            "needs_create": not detect.managed_runtime_present(),
            "error": None,
        }

    if choice == "binary":
        return _error("--runtime binary is not available yet (standalone binaries are a later phase).")

    return _error(f"unknown runtime {choice!r}")


def _error(msg: str) -> Dict[str, Any]:
    return {
        "kind": None,
        "install_method": None,
        "command": None,
        "python": None,
        "owned_by_setup": False,
        "needs_create": False,
        "error": msg,
    }


def create_managed_venv(builder_python: Optional[str] = None) -> str:
    """Create ``<data_dir>/runtime/python-venv``, install Tokdash, write the marker.

    This is the only place setup writes an owned runtime. Returns the venv python path.
    Raises ``RuntimeError_`` on failure so setup aborts cleanly. (Monkeypatched in tests
    to avoid real venv creation / network.)
    """
    import shutil

    builder = builder_python or sys.executable
    venv_dir = paths.managed_venv_dir()
    venv_dir.parent.mkdir(parents=True, exist_ok=True)

    # If `python -m venv` or `pip install` fails, we'd otherwise leave a half-built venv tree
    # with NO ownership marker — which `tokdash uninstall` can never reclaim (it gates runtime
    # removal on the marker). Clean up the tree we created on any failure so no orphan is left.
    def _abort(msg: str) -> "RuntimeError_":
        shutil.rmtree(venv_dir, ignore_errors=True)
        return RuntimeError_(msg)

    try:
        subprocess.run([builder, "-m", "venv", str(venv_dir)], capture_output=True, text=True, check=True, timeout=300)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise _abort(f"failed to create venv at {venv_dir}: {exc}") from exc

    py = paths.managed_venv_python()
    # Pin to the launching version so the service venv matches the CLI that created it
    # (reproducible, no surprise drift). `tokdash update` is the explicit way to bump it.
    from .. import __version__

    try:
        subprocess.run(
            [str(py), "-m", "pip", "install", f"tokdash=={__version__}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=600,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise _abort(f"failed to install tokdash=={__version__} into {venv_dir}: {exc}") from exc

    # Ownership marker: uninstall removes this tree only when this file is present (§12.3).
    paths.runtime_marker_path().write_text("created-by=tokdash-setup\n", encoding="utf-8")
    return str(py)
