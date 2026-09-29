"""Dashboard click-to-update eligibility — stricter than the terminal ``tokdash update``.

The terminal verb upgrades anything whose install method it can drive in place and merely
prints guidance otherwise. The dashboard action mutates a machine through a browser, so
eligibility must *prove* — not infer — that the running installation is one setup fully
manages (plan AUTO_UPDATE_FEASIBILITY.md §3):

- a valid setup manifest matching this host's data dir,
- a recorded install method of ``pipx`` or ``managed-venv`` with a live interpreter,
- a supported, reachable user-service manager with setup's exact ownership marker on the
  *loaded* service definition, targeting the recorded runtime,
- and (Linux/systemd today) a validated platform adapter. Everything else stays
  manual-update-only: no adoption, no takeover, no privilege escalation.

Pipx nuance (§3): a user-provisioned pipx runtime registered by setup is eligible even
though ``runtime_owned_by_setup`` is false — that field gates removal on uninstall, and
an update click authorizes updating the recorded package, never removing its runtime.

Every check is read-only. The helper re-runs :func:`check_eligibility` against its own
view of the world before any mutation, so a dashboard that raced with an uninstall still
cannot push an upgrade into a machine setup no longer owns.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import detect, manifest, paths
from .update_mechanics import MANUAL_COMMAND

SYSTEMCTL_TIMEOUT = 10


def _current_interpreter() -> str:
    """Realpath of the interpreter running this code (the server, for the API plane)."""
    try:
        return os.path.realpath(sys.executable or "")
    except Exception:
        return ""


def _loaded_execstart(unit_name: str) -> Optional[List[str]]:
    """argv of the FIRST ExecStart systemd actually has LOADED for ``unit_name``.

    The manifest's unit path only says which file setup wrote; a daemon-reload can be
    missing, the unit can be masked or overridden, or another unit of the same name can
    be loaded. Only ``systemctl show`` reports what would actually start. None when the
    unit is not loaded or reports no exec line.
    """
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "show", unit_name, "-p", "ExecStart", "--value"],
            capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # systemd prefixes exec entries with option flags (+ - ! @ :); strip them.
        while line[:1] in ("+", "-", "!", "@", ":"):
            line = line[1:].lstrip()
        try:
            argv = shlex.split(line)
        except ValueError:
            return None
        return argv or None
    return None

# Platform adapters ship only after their acceptance gates pass (§9). Linux/systemd is
# validated first; macOS/Windows report "manual for now" instead of failing obscurely.
SUPPORTED_SERVICE_TYPES = {"systemd-user"}
_UNIVERSAL_MANUAL = (
    "This platform's updater has not been validated yet; run `tokdash update` in a terminal "
    "on that machine."
)


def _ineligible(reason: str) -> Dict[str, Any]:
    return {"eligible": False, "reason": reason, "manual_command": MANUAL_COMMAND, "method": None}


def check_eligibility(man: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run every dashboard-readiness check; return {eligible, reason, method, manual_command}.

    ``reason`` explains the first failed check in user terms. ``method`` echoes the
    recorded install method when one exists.
    """
    if man is None:
        man = manifest.read_manifest()
    if not man:
        return _ineligible("No setup manifest (install.json) found, so this install is not setup-managed.")

    method = man.get("install_method")
    if method not in {"pipx", "managed-venv"}:
        return {**_ineligible(
            f"Tokdash runs from a {str(method)!r} runtime that setup did not create; upgrade it the way "
            "you installed it."
        ), "method": method}

    # The manifest must describe THIS host's state — a copied/foreign manifest is stale.
    recorded_dir = man.get("data_dir")
    if recorded_dir:
        try:
            same = Path(recorded_dir).expanduser().resolve() == paths.data_dir().resolve()
        except OSError:
            same = False
        if not same:
            return _ineligible("The setup manifest points at a different data directory than this server is using.")

    python_path = str(man.get("python_path") or "")
    if not python_path or not Path(python_path).is_file():
        return _ineligible("The interpreter recorded by setup is gone, so the updater cannot run.")

    if method == "managed-venv":
        # A managed venv must still carry the ownership marker (created by setup).
        if not paths.runtime_marker_path().is_file():
            return _ineligible("The managed runtime's ownership marker is missing; refusing to touch an unowned runtime.")

    service = man.get("service") or {}
    service_type = service.get("type")
    if service_type is None:
        return _ineligible("No background service is registered, so there is nothing for the updater to restart.")
    if service_type not in SUPPORTED_SERVICE_TYPES:
        result = _UNIVERSAL_MANUAL if service_type in {"launchd", "winsched"} else (
            "The registered service backend is not one the updater can drive."
        )
        return _ineligible(f"The background service uses {str(service_type)!r}; {result}")

    if not detect.systemd_user_available():
        return _ineligible("The systemd user manager is not reachable, so the service cannot be restarted safely.")

    # The *loaded* unit — not just the file the manifest names — must carry setup's exact
    # marker and target the recorded runtime. A service name alone is not evidence (§3).
    marker = str(service.get("marker") or "")
    if "X-Tokdash-Managed" not in marker:
        return _ineligible("The manifest's service record carries no ownership marker.")
    unit_path = service.get("unit")
    target_path = unit_path or str(paths.systemd_unit_path(str(service.get("name") or "tokdash")))
    if not _unit_targets_runtime(target_path, marker, python_path):
        return _ineligible("The service definition does not match what setup wrote (marker or runtime mismatch).")

    # File-on-disk agreement is necessary but not sufficient: systemd may be running a
    # stale load, an override, or a wholly different ExecStart. Verify the LOADED unit
    # and its complete arguments — an ExecStart that merely *starts with* the recorded
    # interpreter proves the interpreter, not that Tokdash is what runs under it.
    unit_name = f"{service.get('name') or 'tokdash'}.service"
    loaded = _loaded_execstart(unit_name)
    if not loaded:
        return _ineligible(f"The user service {unit_name} is not loaded, so its live configuration cannot be verified.")
    reason = _loaded_argv_mismatch(loaded, python_path, man)
    if reason:
        return _ineligible(reason)

    # Identity: the server answering this capability/start IS the managed instance.
    # Otherwise a dev run or a second tokdash against the same data dir could drive an
    # update of a service it does not run (and stop something it owns no part of).
    if _current_interpreter() != os.path.realpath(python_path):
        return _ineligible(
            "This Tokdash process is not the managed service instance recorded by setup; "
            "trigger updates from the managed service (or run `tokdash update` in a terminal)."
        )

    return {"eligible": True, "reason": None, "manual_command": MANUAL_COMMAND, "method": method}


def _loaded_argv_mismatch(loaded: List[str], python_path: str, man: Dict[str, Any]) -> Optional[str]:
    """Whether the LOADED ExecStart runs the recorded runtime as the recorded Tokdash
    service. Returns a user-facing reason, or None when every argument agrees."""
    try:
        same_interpreter = os.path.realpath(loaded[0]) == os.path.realpath(python_path)
    except OSError:
        same_interpreter = False
    if not same_interpreter:
        return "The loaded service does not run the interpreter recorded by setup."
    if loaded[1:3] != ["-m", "tokdash"] or "serve" not in loaded[3:]:
        return "The loaded service does not run the recorded Tokdash service."
    for flag, key, stringify in (("--bind", "bind", str), ("--port", "port", str)):
        want = man.get(key)
        if want in (None, ""):
            continue
        want_s = stringify(want)
        try:
            i = loaded.index(flag)
        except ValueError:
            return f"The loaded service does not pin {flag} as setup recorded."
        if i + 1 >= len(loaded) or loaded[i + 1] != want_s:
            return f"The loaded service's {flag} differs from what setup recorded."
    return None


def _unit_targets_runtime(unit_path: str, marker: str, python_path: str) -> bool:
    """Whether the unit file on disk carries the exact marker and the recorded runtime."""
    from . import systemd

    try:
        text = Path(unit_path).read_text(encoding="utf-8")
    except (OSError, TypeError, ValueError):
        return False
    token = marker.strip()
    if not token or token not in text:
        return False
    # The unit's ExecStart must run the interpreter the manifest recorded, rendered the
    # same way setup renders it (arguments containing spaces are double-quoted).
    prefix = "ExecStart=" + systemd._quote_exec_arg(python_path)
    return any(line.strip().startswith(prefix) for line in text.splitlines())
