"""Service-backend seam: the extension point the ``winsched`` module plugs into.

``systemd.py``, ``launchd.py`` and ``winsched.py`` are the three backends today, all plain
modules (not classes) exposing a small, overlapping surface: render the unit/plist/task
text, write it to disk, check whether an on-disk unit carries setup's ownership marker, and
report status. This module formalizes that surface as a :class:`typing.Protocol`
(documentation/typing only — it does not rewrite ``systemd``/``launchd``/``winsched`` into
classes), provides a registry mapping a resolved ``service_type`` string to its backend
module, and centralizes the *selection* decision (:func:`select_service`) that used to live
inline in ``plan._resolve_service``.

Tier 2 (native Windows Task Scheduler support) registers ``winsched`` here and extends
``select_service`` with a dedicated ``windows`` branch (mirroring the macOS/linux branches
below); every other OS kind still resolves exactly as before this seams refactor.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from . import launchd, systemd, winsched


@runtime_checkable
class ServiceBackend(Protocol):
    """Structural contract a per-user service backend module should satisfy.

    ``systemd``, ``launchd`` and ``winsched`` already implement this shape, each under its
    own backend-specific names — this Protocol is the generic vocabulary they can be checked
    against. It is intentionally *not* applied via ``isinstance()``/type annotations to the
    real ``systemd``/``launchd``/``winsched`` modules anywhere in this codebase (their public
    functions are named ``render_unit``/``render_plist``/``render_task``,
    ``write_unit``/``write_plist``/``write_task``, ``unit_is_managed``/``plist_is_managed``/
    ``task_is_managed``, and they additionally expose backend-specific lifecycle verbs —
    ``daemon_reload``/``enable_now``/``disable_now``/``restart`` for systemd,
    ``bootstrap``/``bootout``/``kickstart`` for launchd, ``create``/``delete``/``query`` for
    winsched — that have no shared name across backends and so are deliberately left out of
    this Protocol). Concrete mapping today:

    =============== ================== =================== ===================
    concept          systemd.py         launchd.py           winsched.py
    =============== ================== =================== ===================
    name/label       SERVICE_NAME       LABEL                TASK_NAME
    marker comment   MARKER_COMMENT     MARKER_COMMENT       MARKER_COMMENT
    render(...)      render_unit(...)   render_plist(...)    render_task(...)
    write(text)      write_unit(text)   write_plist(text)    write_task(text)
    is_managed(...)  unit_is_managed    plist_is_managed     task_is_managed
    status()         status()          status()             status()
    =============== ================== =================== ===================
    """

    MARKER_COMMENT: str

    def render(self, *args: Any, **kwargs: Any) -> str:
        """Render the unit/plist/task text for a given runtime command/bind/port."""
        ...

    def write(self, text: str, *args: Any, **kwargs: Any) -> Any:
        """Persist rendered text to its on-disk path; returns the path written."""
        ...

    def is_managed(self, path: Any, marker_id: Optional[str] = None) -> bool:
        """Does the on-disk unit carry setup's ownership marker (optionally a specific id)?"""
        ...

    def status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """Current enabled/active snapshot as a dict."""
        ...


# Registry: resolved ``service_type`` string -> backend module. Tier 2 registers the
# native Windows Task Scheduler backend here (alongside its select_service() branch and
# the apply/uninstall dispatch in engine.py); this is the same extension point a further
# future backend would use.
SERVICE_BACKENDS: Dict[str, ModuleType] = {
    "systemd-user": systemd,
    "launchd": launchd,
    "winsched": winsched,
}


def backend_for(service_type: str) -> Optional[ModuleType]:
    """Look up the backend module for a resolved ``service_type``.

    Returns ``None`` for ``"none"`` and any unregistered/unknown type — matching current
    semantics where no backend module is dispatched to in that case.
    """
    return SERVICE_BACKENDS.get(service_type)


@dataclass
class ServiceSelection:
    """Outcome of :func:`select_service`.

    ``result`` is the ``{"type", "reason"}`` dict returned by (what used to be, and still
    is, externally) ``plan._resolve_service``. ``blockers``/``notes`` are the messages
    that call site must append to its own accumulating lists, in order, so the overall
    plan output is unchanged.
    """

    result: Dict[str, Any]
    blockers: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def select_service(
    want: str,
    os_kind: str,
    *,
    no_service: bool,
    systemd_available: bool,
    launchd_available: bool,
    winsched_available: bool = False,
) -> ServiceSelection:
    """Decide which service backend (if any) setup should configure.

    The macOS/linux/wsl/"other" branches are copied verbatim (branch logic and every message
    string) from the pre-refactor ``plan._resolve_service`` body and are UNCHANGED by Tier 2;
    ``opts``/``detection`` lookups are primitive parameters so this module has no dependency
    on :mod:`.plan` (avoiding a circular import). ``plan._resolve_service`` delegates here and
    threads ``result``/``blockers``/``notes`` back into its own arguments, so the externally
    observable behavior for every non-windows OS kind — the returned dict and every
    blocker/note string, verbatim, in the same order — is unchanged.

    ``windows`` gets its own dedicated branch (mirroring the macOS/linux-wsl shape): ``auto``
    or an explicit ``--service winsched`` resolves to the ``winsched`` backend when Task
    Scheduler is available, else falls back with a note (auto) or a blocker (explicit),
    exactly like launchd's "launchctl is unavailable" shape. An explicit
    ``--service systemd``/``--service launchd`` on Windows keeps the same
    ``f"--service {want} is not supported on {os_kind}."`` blocker as before. Any other
    (non-windows, non-macOS/linux/wsl) OS kind still resolves to ``{"type": "none", ...}``
    ("unsupported" for an explicit --service request, "deferred" for auto) — unchanged.
    ``winsched_available`` defaults to ``False`` so every pre-existing call site (which never
    passes it) is unaffected.
    """
    blockers: List[str] = []
    notes: List[str] = []

    if no_service or want == "none":
        return ServiceSelection({"type": "none", "reason": "requested"}, blockers, notes)

    # macOS -> launchd.
    if os_kind == "macos":
        if want in {"auto", "launchd"}:
            if launchd_available:
                return ServiceSelection({"type": "launchd", "reason": None}, blockers, notes)
            reason = "launchctl is unavailable"
            if want == "launchd":
                blockers.append(reason + "; re-run with --no-service or run `tokdash serve` yourself.")
            else:
                notes.append(reason + "; falling back to foreground guidance.")
            return ServiceSelection({"type": "none", "reason": reason}, blockers, notes)
        if want == "systemd":
            blockers.append("--service systemd is not supported on macOS; use --service launchd.")
            return ServiceSelection({"type": "none", "reason": "unsupported"}, blockers, notes)
        return ServiceSelection({"type": "none", "reason": "unknown"}, blockers, notes)

    # Linux / WSL -> systemd user service.
    if os_kind in {"linux", "wsl"}:
        if want in {"auto", "systemd"}:
            if systemd_available:
                return ServiceSelection({"type": "systemd-user", "reason": None}, blockers, notes)
            reason = (
                "systemd user services are unavailable"
                + (" (enable WSL systemd in /etc/wsl.conf)" if os_kind == "wsl" else "")
            )
            if want == "systemd":
                blockers.append(reason + "; re-run without --service systemd or use --no-service.")
            else:
                notes.append(reason + "; falling back to foreground guidance.")
            return ServiceSelection({"type": "none", "reason": reason}, blockers, notes)
        if want == "launchd":
            blockers.append("--service launchd is only supported on macOS.")
            return ServiceSelection({"type": "none", "reason": "unsupported"}, blockers, notes)
        return ServiceSelection({"type": "none", "reason": "unknown"}, blockers, notes)

    # Native Windows -> Task Scheduler.
    if os_kind == "windows":
        if want in {"auto", "winsched"}:
            if winsched_available:
                return ServiceSelection({"type": "winsched", "reason": None}, blockers, notes)
            reason = "Task Scheduler (schtasks) is unavailable"
            if want == "winsched":
                blockers.append(reason + "; re-run with --no-service or run `tokdash serve` yourself.")
            else:
                notes.append(reason + "; falling back to foreground guidance.")
            return ServiceSelection({"type": "none", "reason": reason}, blockers, notes)
        if want in {"systemd", "launchd"}:
            blockers.append(f"--service {want} is not supported on {os_kind}.")
            return ServiceSelection({"type": "none", "reason": "unsupported"}, blockers, notes)
        return ServiceSelection({"type": "none", "reason": "unknown"}, blockers, notes)

    # Other (unrecognized OS kind): no managed service yet.
    if want in {"systemd", "launchd", "winsched"}:
        blockers.append(f"--service {want} is not supported on {os_kind}.")
        return ServiceSelection({"type": "none", "reason": "unsupported"}, blockers, notes)
    notes.append(f"Background service setup for {os_kind} is not available yet; use `tokdash serve`.")
    return ServiceSelection({"type": "none", "reason": "deferred"}, blockers, notes)
