"""Shared package-upgrade mechanics for the CLI ``update`` verb and the dashboard helper.

The dashboard updater runs from a *staged, dependency-free script* (see
:mod:`tokdash.onboard.update_helper`) so it cannot import this module at run time — but
the commands it must build are defined here once, and the helper mirrors them through the
same builders when its job record carries them. Keeping argv construction and
target-version validation in one place is what stops the CLI and the dashboard from
drifting into two different notions of "upgrade to X.Y.Z".

Everything here is pure: no subprocess, no filesystem. ``cmd_update`` in
:mod:`tokdash.onboard.engine` feeds the argv to ``subprocess.run``; the helper receives
the pre-computed argv inside its job record and validates it again (no shell, exact
execve-style list, one package spec).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# Accepted shapes for a --to / dashboard target version. Deliberately stricter than PEP
# 440 at the boundary: the value becomes a pip requirement, so it must not contain
# spaces, semicolons, or any other character that could survive into a shell or confuse
# pip's requirement parser. Plain dotted versions with an optional local/pre/dev suffix.
_TARGET_RE = re.compile(r"^\d+(\.\d+)+(?:[a-zA-Z0-9_.+-]+)?$")

MANUAL_COMMAND = "tokdash update"


def valid_target_version(value: Optional[str]) -> bool:
    """Whether ``value`` is a well-formed, structurally safe release target."""
    return bool(value) and bool(_TARGET_RE.match(str(value).strip()))


def validate_target(to_version: Optional[str], *, current: str, latest: Optional[str] = None) -> Optional[str]:
    """Validate an offered target; return an error string, or None when acceptable.

    The click authorizes exactly one release, so the target must be well-formed, newer
    than what is running, and — when the conserved update-check knows the newest
    release — exactly that release. Stale or substituted targets are rejected rather
    than silently upgraded to a different version.
    """
    if not valid_target_version(to_version):
        return "A specific target version is required (e.g. 2.7.0)."
    to = str(to_version).strip()
    from . import updatecheck

    if not updatecheck._is_newer(to, current):
        return f"Target v{to} is not newer than the running v{current}."
    if latest and to != str(latest):
        return f"Target v{to} is not the available release (v{latest})."
    return None


def install_argv(method: str, python_path: str, target_version: Optional[str] = None) -> Optional[List[str]]:
    """Build the in-place upgrade argv for a recorded install method.

    ``target_version=None`` reproduces today's terminal semantics (unpinned upgrade);
    a pinned target installs the *exact* accepted release. A managed-venv and a pipx venv
    are both plain venvs, so pinning is done with that venv's own ``pip`` — pipx's
    ``upgrade`` subcommand has no version selector. Returns None for methods there is no
    in-place command for.
    """
    py = str(python_path or "")
    if not py:
        return None
    pin = f"tokdash=={str(target_version).strip()}" if target_version else None
    if method == "pipx":
        if target_version:
            return [py, "-m", "pip", "install", "--no-input", pin]
        return ["pipx", "upgrade", "tokdash"]
    if method == "managed-venv":
        if target_version:
            return [py, "-m", "pip", "install", "--no-input", pin]
        return [py, "-m", "pip", "install", "-U", "tokdash"]
    return None


def safe_install_argv(argv: Any) -> bool:
    """Re-validate a staged install argv inside the helper.

    The helper trusts nothing it cannot check: a plain list of plain strings, an argv[0]
    that is either pipx or the recorded interpreter, ``pip install`` semantics, and
    exactly one ``tokdash==<safe version>`` requirement. No shell metacharacters can
    appear because the argv is exec'd, never interpolated — this check is defence in
    depth against a tampered job record.
    """
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
        return False
    if argv[0] != "pipx" and (len(argv) < 4 or argv[1] != "-m" or argv[2] != "pip" or argv[3] != "install"):
        return False
    specs = [a for a in argv[4:] if a.startswith("tokdash")]
    if len(specs) != 1 or not specs[0].startswith("tokdash=="):
        return False
    return valid_target_version(specs[0].split("==", 1)[1])
