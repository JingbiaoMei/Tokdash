"""Service-backend selection matrix (Tier 0 seams refactor, service_base.select_service).

Locks in that `service_base.select_service` — and the `plan._resolve_service` adapter
that delegates to it — return byte-identical `{"type", "reason"}` results and
blocker/note strings to the pre-refactor inline implementation, for every non-windows OS
kind x `--service` request combination. Also covers the `service_base` registry
(`SERVICE_BACKENDS` / `backend_for`), and (Tier 2) the native Windows Task Scheduler
`winsched` backend now registered there and dispatched to from the dedicated `windows`
branch of `select_service`.
"""
from __future__ import annotations

from tokdash.onboard import launchd, plan, service_base, systemd, winsched


# --- select_service: auto ---------------------------------------------------------


def test_auto_macos_launchd_available():
    sel = service_base.select_service(
        "auto", "macos", no_service=False, systemd_available=False, launchd_available=True
    )
    assert sel.result == {"type": "launchd", "reason": None}
    assert sel.blockers == []
    assert sel.notes == []


def test_auto_macos_launchd_unavailable():
    sel = service_base.select_service(
        "auto", "macos", no_service=False, systemd_available=False, launchd_available=False
    )
    assert sel.result == {"type": "none", "reason": "launchctl is unavailable"}
    assert sel.blockers == []
    assert sel.notes == ["launchctl is unavailable; falling back to foreground guidance."]


def test_auto_linux_systemd_available():
    sel = service_base.select_service(
        "auto", "linux", no_service=False, systemd_available=True, launchd_available=False
    )
    assert sel.result == {"type": "systemd-user", "reason": None}
    assert sel.blockers == []
    assert sel.notes == []


def test_auto_linux_systemd_unavailable_no_wsl_hint():
    sel = service_base.select_service(
        "auto", "linux", no_service=False, systemd_available=False, launchd_available=False
    )
    assert sel.result == {"type": "none", "reason": "systemd user services are unavailable"}
    assert sel.blockers == []
    assert sel.notes == ["systemd user services are unavailable; falling back to foreground guidance."]


def test_auto_wsl_systemd_available():
    sel = service_base.select_service(
        "auto", "wsl", no_service=False, systemd_available=True, launchd_available=False
    )
    assert sel.result == {"type": "systemd-user", "reason": None}
    assert sel.blockers == []
    assert sel.notes == []


def test_auto_wsl_systemd_unavailable_includes_wsl_conf_hint():
    sel = service_base.select_service(
        "auto", "wsl", no_service=False, systemd_available=False, launchd_available=False
    )
    expected_reason = "systemd user services are unavailable (enable WSL systemd in /etc/wsl.conf)"
    assert sel.result == {"type": "none", "reason": expected_reason}
    assert sel.blockers == []
    assert sel.notes == [expected_reason + "; falling back to foreground guidance."]


def test_auto_windows_winsched_available():
    sel = service_base.select_service(
        "auto", "windows", no_service=False, systemd_available=False, launchd_available=False,
        winsched_available=True,
    )
    assert sel.result == {"type": "winsched", "reason": None}
    assert sel.blockers == []
    assert sel.notes == []


def test_auto_windows_winsched_unavailable_falls_back():
    # winsched_available defaults to False when the caller doesn't pass it, so this also
    # covers "the caller never even asked" the same way the pre-Tier-2 callers didn't.
    sel = service_base.select_service(
        "auto", "windows", no_service=False, systemd_available=False, launchd_available=False
    )
    assert sel.result == {"type": "none", "reason": "Task Scheduler (schtasks) is unavailable"}
    assert sel.blockers == []
    assert sel.notes == ["Task Scheduler (schtasks) is unavailable; falling back to foreground guidance."]


def test_auto_other_os_kind_deferred():
    # Any unrecognized os_kind (not just "windows") takes the same deferred path, and the
    # note interpolates the given os_kind verbatim.
    sel = service_base.select_service(
        "auto", "freebsd", no_service=False, systemd_available=False, launchd_available=False
    )
    assert sel.result == {"type": "none", "reason": "deferred"}
    assert sel.blockers == []
    assert sel.notes == ["Background service setup for freebsd is not available yet; use `tokdash serve`."]


# --- select_service: explicit --service on the "wrong" OS -------------------------


def test_explicit_systemd_on_macos_is_blocked():
    sel = service_base.select_service(
        "systemd", "macos", no_service=False, systemd_available=False, launchd_available=True
    )
    assert sel.result == {"type": "none", "reason": "unsupported"}
    assert sel.blockers == ["--service systemd is not supported on macOS; use --service launchd."]
    assert sel.notes == []


def test_explicit_launchd_on_linux_is_blocked():
    sel = service_base.select_service(
        "launchd", "linux", no_service=False, systemd_available=True, launchd_available=False
    )
    assert sel.result == {"type": "none", "reason": "unsupported"}
    assert sel.blockers == ["--service launchd is only supported on macOS."]
    assert sel.notes == []


def test_explicit_launchd_on_wsl_is_blocked():
    sel = service_base.select_service(
        "launchd", "wsl", no_service=False, systemd_available=True, launchd_available=False
    )
    assert sel.result == {"type": "none", "reason": "unsupported"}
    assert sel.blockers == ["--service launchd is only supported on macOS."]
    assert sel.notes == []


def test_explicit_systemd_on_windows_is_blocked():
    sel = service_base.select_service(
        "systemd", "windows", no_service=False, systemd_available=False, launchd_available=False
    )
    assert sel.result == {"type": "none", "reason": "unsupported"}
    assert sel.blockers == ["--service systemd is not supported on windows."]
    assert sel.notes == []


def test_explicit_launchd_on_windows_is_blocked():
    sel = service_base.select_service(
        "launchd", "windows", no_service=False, systemd_available=False, launchd_available=False
    )
    assert sel.result == {"type": "none", "reason": "unsupported"}
    assert sel.blockers == ["--service launchd is not supported on windows."]
    assert sel.notes == []


# --- select_service: explicit --service requested + available/unavailable --------


def test_explicit_systemd_available_on_linux():
    sel = service_base.select_service(
        "systemd", "linux", no_service=False, systemd_available=True, launchd_available=False
    )
    assert sel.result == {"type": "systemd-user", "reason": None}
    assert sel.blockers == []
    assert sel.notes == []


def test_explicit_systemd_unavailable_on_linux_blocks_not_notes():
    sel = service_base.select_service(
        "systemd", "linux", no_service=False, systemd_available=False, launchd_available=False
    )
    assert sel.result == {"type": "none", "reason": "systemd user services are unavailable"}
    assert sel.blockers == [
        "systemd user services are unavailable; re-run without --service systemd or use --no-service."
    ]
    assert sel.notes == []


def test_explicit_systemd_unavailable_on_wsl_blocks_with_wsl_hint():
    sel = service_base.select_service(
        "systemd", "wsl", no_service=False, systemd_available=False, launchd_available=False
    )
    expected_reason = "systemd user services are unavailable (enable WSL systemd in /etc/wsl.conf)"
    assert sel.result == {"type": "none", "reason": expected_reason}
    assert sel.blockers == [
        expected_reason + "; re-run without --service systemd or use --no-service."
    ]
    assert sel.notes == []


def test_explicit_launchd_available_on_macos():
    sel = service_base.select_service(
        "launchd", "macos", no_service=False, systemd_available=False, launchd_available=True
    )
    assert sel.result == {"type": "launchd", "reason": None}
    assert sel.blockers == []
    assert sel.notes == []


def test_explicit_launchd_unavailable_on_macos_blocks_not_notes():
    sel = service_base.select_service(
        "launchd", "macos", no_service=False, systemd_available=False, launchd_available=False
    )
    assert sel.result == {"type": "none", "reason": "launchctl is unavailable"}
    assert sel.blockers == [
        "launchctl is unavailable; re-run with --no-service or run `tokdash serve` yourself."
    ]
    assert sel.notes == []


def test_explicit_winsched_available_on_windows():
    sel = service_base.select_service(
        "winsched", "windows", no_service=False, systemd_available=False, launchd_available=False,
        winsched_available=True,
    )
    assert sel.result == {"type": "winsched", "reason": None}
    assert sel.blockers == []
    assert sel.notes == []


def test_explicit_winsched_unavailable_on_windows_blocks_not_notes():
    sel = service_base.select_service(
        "winsched", "windows", no_service=False, systemd_available=False, launchd_available=False,
        winsched_available=False,
    )
    assert sel.result == {"type": "none", "reason": "Task Scheduler (schtasks) is unavailable"}
    assert sel.blockers == [
        "Task Scheduler (schtasks) is unavailable; re-run with --no-service or run `tokdash serve` yourself."
    ]
    assert sel.notes == []


# --- select_service: --no-service / --service none --------------------------------


def test_no_service_flag_requested_regardless_of_os():
    for os_kind in ("macos", "linux", "wsl", "windows"):
        sel = service_base.select_service(
            "auto", os_kind, no_service=True, systemd_available=True, launchd_available=True
        )
        assert sel.result == {"type": "none", "reason": "requested"}
        assert sel.blockers == []
        assert sel.notes == []


def test_service_none_requested_regardless_of_os():
    for os_kind in ("macos", "linux", "wsl", "windows"):
        sel = service_base.select_service(
            "none", os_kind, no_service=False, systemd_available=True, launchd_available=True
        )
        assert sel.result == {"type": "none", "reason": "requested"}
        assert sel.blockers == []
        assert sel.notes == []


# --- select_service: unknown --service value on a recognized OS -------------------


def test_unknown_service_value_on_macos():
    sel = service_base.select_service(
        "bogus", "macos", no_service=False, systemd_available=False, launchd_available=True
    )
    assert sel.result == {"type": "none", "reason": "unknown"}
    assert sel.blockers == []
    assert sel.notes == []


def test_unknown_service_value_on_linux():
    sel = service_base.select_service(
        "bogus", "linux", no_service=False, systemd_available=True, launchd_available=False
    )
    assert sel.result == {"type": "none", "reason": "unknown"}
    assert sel.blockers == []
    assert sel.notes == []


def test_winsched_requested_on_macos_is_unknown_not_dispatched():
    # `winsched` only means anything inside the dedicated `windows` branch; on any other OS
    # kind it falls through exactly like any other unrecognized --service value (the
    # macOS/linux/wsl branches are untouched by Tier 2).
    sel = service_base.select_service(
        "winsched", "macos", no_service=False, systemd_available=False, launchd_available=True
    )
    assert sel.result == {"type": "none", "reason": "unknown"}
    assert sel.blockers == []
    assert sel.notes == []


def test_winsched_requested_on_linux_is_unknown_not_dispatched():
    sel = service_base.select_service(
        "winsched", "linux", no_service=False, systemd_available=True, launchd_available=False
    )
    assert sel.result == {"type": "none", "reason": "unknown"}
    assert sel.blockers == []
    assert sel.notes == []


def test_unknown_service_value_on_windows():
    sel = service_base.select_service(
        "bogus", "windows", no_service=False, systemd_available=False, launchd_available=False,
        winsched_available=True,
    )
    assert sel.result == {"type": "none", "reason": "unknown"}
    assert sel.blockers == []
    assert sel.notes == []


# --- registry: SERVICE_BACKENDS / backend_for --------------------------------------


def test_backend_for_registered_types():
    assert service_base.backend_for("systemd-user") is systemd
    assert service_base.backend_for("launchd") is launchd
    assert service_base.backend_for("winsched") is winsched


def test_backend_for_unregistered_types_returns_none():
    # "none" (today's no-service outcome), the deferred/unsupported other-OS outcome, and
    # any not-yet-registered future backend name all resolve to None -- there is no backend
    # module to dispatch to.
    assert service_base.backend_for("none") is None
    assert service_base.backend_for("windows") is None
    assert service_base.backend_for("some-future-backend") is None


def test_service_backends_registry_contents():
    assert service_base.SERVICE_BACKENDS == {"systemd-user": systemd, "launchd": launchd, "winsched": winsched}


# --- plan._resolve_service back-compat adapter -------------------------------------
#
# The factory above is exercised directly; these confirm the thin plan.py adapter
# (opts/detection -> primitives -> service_base.select_service -> blockers/notes
# threaded back) produces the identical externally observable result for a
# representative sample of the matrix, using the same Options/detection shape the
# rest of the onboarding engine uses.


def _detection(os_kind, *, systemd_user=False, launchd_avail=False):
    return {"os": os_kind, "systemd_user": systemd_user, "launchd": launchd_avail}


def test_resolve_service_delegates_auto_macos_launchd():
    opts = plan.Options(service="auto")
    blockers, notes = [], []
    result = plan._resolve_service(opts, _detection("macos", launchd_avail=True), blockers, notes)
    assert result == {"type": "launchd", "reason": None}
    assert blockers == []
    assert notes == []


def test_resolve_service_delegates_auto_wsl_systemd_unavailable():
    opts = plan.Options(service="auto")
    blockers, notes = [], []
    result = plan._resolve_service(opts, _detection("wsl", systemd_user=False), blockers, notes)
    expected_reason = "systemd user services are unavailable (enable WSL systemd in /etc/wsl.conf)"
    assert result == {"type": "none", "reason": expected_reason}
    assert blockers == []
    assert notes == [expected_reason + "; falling back to foreground guidance."]


def test_resolve_service_delegates_explicit_launchd_wrong_os():
    opts = plan.Options(service="launchd")
    blockers, notes = [], []
    result = plan._resolve_service(opts, _detection("linux", systemd_user=True), blockers, notes)
    assert result == {"type": "none", "reason": "unsupported"}
    assert blockers == ["--service launchd is only supported on macOS."]
    assert notes == []


def test_resolve_service_delegates_no_service_requested():
    opts = plan.Options(no_service=True)
    blockers, notes = [], []
    result = plan._resolve_service(opts, _detection("linux", systemd_user=True), blockers, notes)
    assert result == {"type": "none", "reason": "requested"}
    assert blockers == []
    assert notes == []


def test_resolve_service_delegates_appends_to_existing_blockers_and_notes():
    # blockers/notes threading must extend (not replace) whatever the caller already
    # accumulated -- mirroring how build_setup_plan calls it mid-way through building
    # its own blockers/notes lists.
    opts = plan.Options(service="systemd")
    blockers = ["earlier blocker"]
    notes = ["earlier note"]
    result = plan._resolve_service(opts, _detection("macos", launchd_avail=True), blockers, notes)
    assert result == {"type": "none", "reason": "unsupported"}
    assert blockers == ["earlier blocker", "--service systemd is not supported on macOS; use --service launchd."]
    assert notes == ["earlier note"]
