"""Windows Task Scheduler *per-user* task generation and lifecycle (Tier 2).

The Windows analogue of :mod:`.systemd` (Linux/WSL) and :mod:`.launchd` (macOS). A task
named :data:`TASK_NAME` carries the same ownership marker so ``uninstall`` can prove setup
wrote it before removing it (mirrors §12.3 for the other two backends). All lifecycle calls
go through per-user ``schtasks`` only — never an elevated/system task, never ``runas``.

Task Scheduler has no single "unit file" the way systemd/launchd do; ``schtasks`` keeps the
live registration in its own store. To keep the same on-disk "does this file carry our
marker" gate the other two backends use (:func:`task_is_managed`, mirroring
``unit_is_managed``/``plist_is_managed``), setup keeps its own copy of the last XML it
registered at :func:`paths.winsched_task_path`; :func:`task_is_managed` also accepts a bare
task name, in which case it asks ``schtasks`` for the live definition instead of reading a
file (useful when only the name, not a source file, is known).

Task Scheduler's XML schema has no first-class per-action environment variable slot (unlike
systemd's ``Environment=`` or launchd's ``EnvironmentVariables`` dict) and the task always
runs ``pythonw.exe`` (never ``python.exe``) so it never flashes a console window — the
Windows analogue of how systemd/launchd detach the service from any controlling terminal.
"""
from __future__ import annotations

import subprocess
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, List, Optional, Union
from xml.sax.saxutils import escape

from . import manifest, paths

TASK_NAME = "Tokdash"
MARKER_COMMENT = "Managed-by: tokdash-setup"
LIFECYCLE_TIMEOUT = 120

def _pythonw_for(python_exe: str) -> str:
    """Best-effort swap of a ``python.exe`` interpreter path for its windowless ``pythonw.exe`` twin.

    Task Scheduler runs this task in the triggering user's interactive session (LogonTrigger +
    InteractiveToken — mirroring launchd's ``gui/<uid>`` domain / systemd's ``--user`` scope,
    never a SYSTEM/elevated task), where a console-subsystem executable like ``python.exe``
    would flash a console window. ``pythonw.exe`` is the GUI-subsystem twin Windows never
    allocates a console for. Falls back to the given path unchanged if it does not look like a
    plain ``python.exe`` (e.g. an unexpected runtime), so this never invents a nonexistent binary.
    """
    p = PureWindowsPath(python_exe)
    if p.name.lower() == "python.exe":
        return str(p.with_name("pythonw.exe"))
    return python_exe


def _module_args(runtime_command: List[str], bind: str, port: int) -> List[str]:
    """Args after the interpreter: ``<runtime_command[1:]> serve --bind <bind> --port <port> --no-open``."""
    return list(runtime_command[1:]) + ["serve", "--bind", bind, "--port", str(port), "--no-open"]


def _tokdash_argv(module_args: List[str]) -> List[str]:
    """``sys.argv`` for a ``runpy.run_module('tokdash')`` trampoline.

    ``runtime_command`` is normally ``[python, "-m", "tokdash"]``. The direct task path
    needs those ``-m tokdash`` interpreter arguments, but the ``-c`` trampoline has already
    chosen the module via ``runpy.run_module('tokdash', ...)``; leaving ``-m tokdash`` in
    ``sys.argv`` would make Tokdash's CLI parse ``-m`` as the command.
    """
    if len(module_args) >= 2 and module_args[:2] == ["-m", "tokdash"]:
        return ["tokdash"] + module_args[2:]
    return ["tokdash"] + module_args


def render_task(
    runtime_command: List[str],
    bind: str,
    port: int,
    *,
    marker_id: str,
    env_data_dir: Optional[str] = None,
) -> str:
    """Render the Task Scheduler task definition text (``schtasks /Create /XML`` shape).

    ``<Command>`` is always :func:`_pythonw_for` of ``runtime_command[0]`` so the task never
    flashes a console window. When the data dir is non-default, since Task Scheduler has no
    per-action environment slot, ``env_data_dir`` is threaded in via a small self-contained
    ``-c`` snippet that sets ``os.environ`` before dispatching into ``tokdash`` — still just
    one ``pythonw.exe`` process, no shell/console wrapper (mirror of §10.1's env line).
    """
    python_exe = runtime_command[0] if runtime_command else ""
    command = _pythonw_for(python_exe)
    module_args = _module_args(runtime_command, bind, port)

    if env_data_dir:
        argv_literal = repr(_tokdash_argv(module_args))
        snippet = (
            "import os,runpy,sys;"
            f"os.environ['TOKDASH_DATA_DIR']={env_data_dir!r};"
            f"sys.argv={argv_literal};"
            "runpy.run_module('tokdash', run_name='__main__')"
        )
        arguments = subprocess.list2cmdline(["-c", snippet])
    else:
        arguments = subprocess.list2cmdline(module_args)

    description = f"{MARKER_COMMENT}&#10;{escape(manifest.marker_token(marker_id))}"

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">',
        "  <RegistrationInfo>",
        f"    <Description>{description}</Description>",
        "  </RegistrationInfo>",
        "  <Triggers>",
        "    <LogonTrigger>",
        "      <Enabled>true</Enabled>",
        "    </LogonTrigger>",
        "  </Triggers>",
        "  <Principals>",
        '    <Principal id="Author">',
        "      <LogonType>InteractiveToken</LogonType>",
        "      <RunLevel>LeastPrivilege</RunLevel>",
        "    </Principal>",
        "  </Principals>",
        "  <Settings>",
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>",
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>",
        "    <StartWhenAvailable>true</StartWhenAvailable>",
        "    <RestartOnFailure>",
        "      <Interval>PT1M</Interval>",
        "      <Count>3</Count>",
        "    </RestartOnFailure>",
        "  </Settings>",
        '  <Actions Context="Author">',
        "    <Exec>",
        f"      <Command>{escape(command)}</Command>",
        f"      <Arguments>{escape(arguments)}</Arguments>",
        "    </Exec>",
        "  </Actions>",
        "</Task>",
        "",
    ]
    return "\n".join(lines)


def _read_definition_text(path_or_name: Union[Path, str]) -> Optional[str]:
    if isinstance(path_or_name, Path):
        try:
            return path_or_name.read_text(encoding="utf-8")
        except OSError:
            return None
    # A bare task name: ask Task Scheduler for the live XML definition it holds today.
    try:
        proc = _run(["/Query", "/TN", str(path_or_name), "/XML"], timeout=10)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def task_is_managed(path_or_name: Union[Path, str], marker_id: Optional[str] = None) -> bool:
    """Does this task (by source-file path, or by live name) carry setup's ownership marker?"""
    text = _read_definition_text(path_or_name)
    if text is None:
        return False
    if "X-Tokdash-Managed" not in text:
        return False
    if marker_id is not None:
        return f"id={marker_id}" in text
    return True


def write_task(text: str) -> Path:
    path = paths.winsched_task_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _run(args: List[str], timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, timeout=timeout)


def create(task_path: Path, name: str = TASK_NAME) -> subprocess.CompletedProcess:
    """Register (or replace, ``/F``) the task from the XML file written by :func:`write_task`."""
    return _run(["/Create", "/TN", name, "/XML", str(task_path), "/F"], timeout=LIFECYCLE_TIMEOUT)


def delete(name: str = TASK_NAME) -> subprocess.CompletedProcess:
    return _run(["/Delete", "/TN", name, "/F"], timeout=LIFECYCLE_TIMEOUT)


def run_now(name: str = TASK_NAME) -> subprocess.CompletedProcess:
    return _run(["/Run", "/TN", name], timeout=LIFECYCLE_TIMEOUT)


def end_task(name: str = TASK_NAME) -> subprocess.CompletedProcess:
    return _run(["/End", "/TN", name], timeout=LIFECYCLE_TIMEOUT)


def restart(name: str = TASK_NAME) -> subprocess.CompletedProcess:
    """``/End`` (best-effort; benign if not currently running) then ``/Run``.

    Task Scheduler has no single "restart" verb — this is the Windows analogue of launchd's
    ``kickstart -k`` / systemd's ``restart``.
    """
    try:
        end_task(name)
    except Exception:
        pass
    return run_now(name)


def register(text: str, name: str = TASK_NAME) -> Path:
    """Write the rendered definition to disk, then register it with Task Scheduler.

    A one-shot convenience combining :func:`write_task` + :func:`create`, analogous to
    ``write_unit``+``enable_now`` / ``write_plist``+``bootstrap`` being called together.
    """
    path = write_task(text)
    create(path, name)
    return path


def query(name: str = TASK_NAME) -> subprocess.CompletedProcess:
    return _run(["/Query", "/TN", name, "/FO", "LIST", "/V"], timeout=10)


def _status_field(text: str) -> Optional[str]:
    for line in text.splitlines():
        if line.strip().lower().startswith("status:"):
            return line.split(":", 1)[1].strip()
    return None


def is_registered(name: str = TASK_NAME) -> bool:
    try:
        return query(name).returncode == 0
    except Exception:
        return False


def is_registered_strict(name: str = TASK_NAME) -> bool:
    """Like :func:`is_registered` but does NOT swallow errors/timeouts.

    ``is_registered()`` returns False both for "confirmed not registered" and "could not
    determine". The uninstall teardown must tell those apart so it can fail CLOSED when it
    cannot confirm the task was removed — it calls this variant and treats a raised error
    (e.g. a hung ``schtasks``) as "state unknown -> assume still registered".
    """
    return query(name).returncode == 0


def is_running(name: str = TASK_NAME) -> bool:
    """Best-effort ``Running`` state parsed from ``schtasks /Query ... /V`` text output."""
    try:
        proc = query(name)
    except Exception:
        return False
    if proc.returncode != 0:
        return False
    return _status_field(proc.stdout) == "Running"


def status(name: str = TASK_NAME) -> Dict[str, Any]:
    registered = is_registered(name)
    return {
        "type": "winsched",
        "name": name,
        "enabled": registered,
        "active": is_running(name) if registered else False,
    }
