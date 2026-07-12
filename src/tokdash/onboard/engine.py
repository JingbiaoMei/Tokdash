"""Lifecycle orchestration: ``tokdash setup`` / ``doctor`` / ``uninstall``.

The flow mirrors plan §6.1: detect -> plan -> (confirm) -> apply/revert -> record/report.
Every route runs the same engine; only input-gathering differs. ``apply`` and ``revert``
are mirror images keyed off the manifest, so setup is always reversible.

Mutation always requires an explicit non-interactive signal: with no TTY and no
``--auto``/``--yes`` the command prints its plan and exits non-zero instead of acting
(§8.1) — a command that wasn't explicitly non-interactive must never change the system
just because it happened to run without a terminal.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import detect, launchd, manifest, paths, plan, runtime, systemd, tailscale, updatecheck, winsched
from .plan import DEFAULT_PORT, Options

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_NEEDS_CONFIRM = 2


# --- CLI entry ------------------------------------------------------------------


def options_from_args(args) -> Options:
    """Translate the flat argparse namespace into normalized lifecycle Options."""
    return Options(
        action=args.command,
        auto=getattr(args, "auto", False),
        yes=getattr(args, "yes", False),
        json=getattr(args, "json", False),
        dry_run=getattr(args, "dry_run", False),
        bind=getattr(args, "bind", "127.0.0.1") or "127.0.0.1",
        port=getattr(args, "port", None),
        runtime=getattr(args, "runtime", "auto") or "auto",
        service=getattr(args, "service", "auto") or "auto",
        no_service=getattr(args, "no_service", False),
        purge=getattr(args, "purge", False),
        keep_runtime=getattr(args, "keep_runtime", False),
        force=getattr(args, "force", False),
    )


def run_lifecycle(args) -> int:
    opts = options_from_args(args)
    if opts.action == "setup":
        return cmd_setup(opts)
    if opts.action == "doctor":
        return cmd_doctor(opts)
    if opts.action == "update":
        return cmd_update(opts)
    if opts.action == "uninstall":
        return cmd_uninstall(opts)
    _err(f"unknown lifecycle command: {opts.action}")
    return EXIT_FAIL


# --- setup ----------------------------------------------------------------------


def cmd_setup(opts: Options) -> int:
    detection = detect.detect_all(opts.port or DEFAULT_PORT)
    p = plan.build_setup_plan(opts, detection)

    if p["blockers"]:
        _emit_plan(p, opts)
        for b in p["blockers"]:
            _err(b)
        return EXIT_FAIL

    if opts.dry_run:
        _emit_plan(p, opts)
        return EXIT_OK

    # Non-interactive guard: never mutate without an explicit non-interactive signal.
    if not opts.auto and not opts.yes and not detection["tty"]:
        _emit_plan(p, opts)
        _err("Not a terminal and neither --auto nor --yes was given; nothing was changed. "
             "Re-run with `tokdash setup --auto` to apply.")
        return EXIT_NEEDS_CONFIRM

    # Interactive confirmation (expert route). --auto/--yes skip the prompt.
    if not opts.auto and not opts.yes:
        if not opts.json:
            _print_setup_human_plan(p)
        if not _confirm("Apply this setup?", default=True):
            print("Aborted; nothing was changed.")
            return EXIT_FAIL

    result = _apply_setup(p, opts)

    # Remote exposure is interactive + explicitly confirmed only. --auto/--yes never run it
    # (they only print the command, via the plan note); here we offer it after a successful
    # setup of a real service on a loopback bind.
    if (
        result.get("ok")
        and not opts.auto
        and not opts.yes
        and detection["tty"]
        and detection.get("tailscale")
        and result.get("service", {}).get("type") in {"systemd-user", "launchd", "winsched"}
        and plan._is_loopback(result.get("bind", ""))
    ):
        _offer_tailscale(result)

    # Optional interactive-only steps: update-notice consent (default Yes) runs first, then
    # the quota step (per-provider network consent, default No, and the poll interval).
    # --auto/--yes/--json and non-tty runs skip both — scripted/CI setups must never block on
    # a prompt or silently enable a network-calling feature. They run last so neither steals
    # input meant for the earlier setup/tailscale confirmations. Browser opening happens after
    # these wizards so the dashboard does not interrupt the remaining terminal questions.
    if result.get("ok") and not opts.auto and not opts.yes and not opts.json and detection["tty"]:
        _update_check_setup_step()
        _quota_setup_wizard()

    _maybe_open_dashboard(result, opts, detection)

    _emit_result(result, opts)
    return EXIT_OK if result.get("ok") else EXIT_FAIL


def _has_display() -> bool:
    """Best-effort GUI detection for setup's optional browser open."""
    if os.environ.get("CI"):
        return False
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


def _maybe_open_dashboard(result: Dict[str, Any], opts: Options, detection: Dict[str, Any]) -> bool:
    if (
        not result.get("ok")
        or opts.auto
        or opts.yes
        or opts.json
        or not detection.get("tty")
        or not _has_display()
        or os.environ.get("TOKDASH_SETUP_NO_OPEN", "").strip().lower() in {"1", "true", "yes"}
    ):
        return False
    url = result.get("url")
    if not url:
        return False
    opened = _open_dashboard_url(url)
    if opened:
        result["opened_url"] = url
        result.setdefault("notes", []).append(f"Opened dashboard in your browser: {url}")
    return bool(opened)


def _open_dashboard_url(url: str) -> bool:
    """Open the setup URL without letting browser logs attach to this terminal."""
    commands: List[List[str]] = []
    if sys.platform == "darwin" and shutil.which("open"):
        commands.append(["open", url])
    elif os.name == "nt":
        commands.append(["cmd", "/c", "start", "", url])
    else:
        if detect.os_kind() == "wsl" and shutil.which("wslview"):
            commands.append(["wslview", url])
        if shutil.which("xdg-open"):
            commands.append(["xdg-open", url])
        if shutil.which("gio"):
            commands.append(["gio", "open", url])

    kwargs: Dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name != "nt":
        kwargs["start_new_session"] = True
    for cmd in commands:
        try:
            subprocess.Popen(cmd, **kwargs)
            return True
        except Exception:
            continue
    return False


_TAILSCALE_UNSET = {"configured_by_setup": False, "target": None, "teardown_command": None}


def _set_manifest_tailscale(block: Optional[Dict[str, Any]]) -> bool:
    """Merge ``block`` (or the unset shape when ``None``) into install.json's ``tailscale_serve``.

    Returns ``False`` when the manifest is unreadable or the write fails, so the caller can
    warn (or skip exposing) instead of crashing setup with a traceback after the user has
    already opted into Tailscale Serve. Never raises.
    """
    man = manifest.read_manifest()
    if man is None:
        return False
    man["tailscale_serve"] = block if block is not None else dict(_TAILSCALE_UNSET)
    try:
        manifest.write_manifest(man)
    except OSError:
        return False
    return True


def _offer_tailscale(result: Dict[str, Any]) -> None:
    """Expert-wizard opt-in: run `tailscale serve` and record it for uninstall (§8.3)."""
    port = result["port"]
    cmd = tailscale.serve_command(port)
    print(f"\nExpose Tokdash on your tailnet? This runs: {' '.join(cmd)}")
    print("  (The API is unauthenticated; proxied writes stay disabled by the loopback gate.)")
    if not _confirm("Run it now?", default=False):
        print("  Skipped — run the command above yourself to expose it later.")
        return

    # Record the teardown BEFORE activating Serve. `tailscale serve` exposes Tokdash to the
    # whole tailnet the instant it succeeds; if the process were killed between that success
    # and recording the block, uninstall would have no teardown command and the unauthenticated
    # exposure would be stranded. The teardown is the deterministic matched inverse of the serve
    # command, so pre-recording it is safe — if serve never activates we clear it again below,
    # and running the `off` teardown on a rule that was never created is a harmless no-op.
    if not _set_manifest_tailscale(tailscale.manifest_block(port, url=None)):
        _err(
            "Could not record a Tailscale exposure for revert (install.json is unreadable or "
            "not writable). Skipping Tailscale Serve; fix the manifest and re-run, or expose it "
            f"yourself with: {' '.join(cmd)}"
        )
        return

    out = tailscale.run_serve(port)
    if not out["ok"] and tailscale.needs_operator_permission(out.get("error")):
        print(_warn("Tailscale denied Serve configuration for this user."))
        op_cmd = tailscale.operator_command()
        print("  Tailscale can grant your user permission to manage Serve without sudo:")
        print(f"    {' '.join(op_cmd)}")
        if _confirm("Run this one-time sudo command and retry Tailscale Serve?", default=False):
            grant = tailscale.grant_operator()
            if not grant["ok"]:
                _err(f"tailscale operator grant failed: {grant['error']}")
                _err(f"Run manually: {' '.join(grant['command'])}")
                _set_manifest_tailscale(None)  # nothing was exposed; undo the pre-record
                return
            out = tailscale.run_serve(port)
    if not out["ok"]:
        _err(f"tailscale serve failed: {out['error']}")
        if tailscale.needs_operator_permission(out.get("error")):
            _err(f"Run manually once: {' '.join(tailscale.operator_command())}")
            _err(f"Then retry: {' '.join(tailscale.serve_command(port))}")
        _set_manifest_tailscale(None)  # serve never activated; keep uninstall exact
        return

    # Serve is live and already covered by the pre-recorded teardown; upgrade the block to the
    # full payload (with the resolved tailnet URL) now that we have it. If this write fails the
    # pre-recorded teardown still stands, so uninstall keeps working — just warn, never crash.
    if not _set_manifest_tailscale(out["block"]):
        _err(
            "Tailscale Serve started, but the URL could not be written to install.json "
            "(unreadable or not writable). `tokdash uninstall` still reverts it; if it misses, "
            f"run: {' '.join(tailscale.teardown_command())}"
        )
    result["tailscale_serve"] = out["block"]
    result.setdefault("changed", []).append("tailscale-serve")
    remote_url = out.get("url") or (out.get("block") or {}).get("url")
    if remote_url:
        result["tailscale_url"] = remote_url
        result.setdefault("notes", []).append(
            f"Tailscale URL: {remote_url} (tailnet only; write actions stay disabled through Serve)."
        )
        print(f"  {_ok('✓')} Tailscale Serve configured: {_accent(remote_url)} (`tokdash uninstall` will revert it).")
    else:
        print(f"  {_ok('✓')} Tailscale Serve configured; `tokdash uninstall` will revert it.")


def _same_path(a: str | Path, b: str | Path) -> bool:
    try:
        return Path(a).expanduser().resolve(strict=False) == Path(b).expanduser().resolve(strict=False)
    except OSError:
        return str(a) == str(b)


def _systemd_fragment_mismatch(name: str, unit_path: str | Path) -> Optional[str]:
    """Return the loaded unit path when ``name`` points somewhere other than ``unit_path``."""
    loaded = systemd.fragment_path(name)
    if loaded and not _same_path(loaded, unit_path):
        return loaded
    return None


def _probe_host_for_bind(bind: str) -> str:
    b = (bind or "").strip()
    if not b or b in {"0.0.0.0", "::", "localhost"} or b.startswith("127."):
        return "127.0.0.1"
    return b


def _wait_for_service_ready(bind: str, port: int, *, timeout: float = 8.0) -> Dict[str, Any]:
    """Wait until the configured port answers with Tokdash's /health fingerprint."""
    host = _probe_host_for_bind(bind)
    deadline = time.monotonic() + timeout
    last: Dict[str, Any] = {"port": port, "open": False, "is_tokdash": False, "version": None}
    while True:
        last = detect.probe_port(port, host=host, timeout=0.5)
        if last.get("is_tokdash"):
            return {"ok": True, "port": last}
        if time.monotonic() >= deadline:
            if last.get("open"):
                detail = f"port {port} is open but does not answer with Tokdash's /health fingerprint"
            else:
                detail = f"nothing answered on {host}:{port}"
            return {"ok": False, "port": last, "error": f"service did not become ready: {detail}"}
        time.sleep(0.25)


def _proc_failure_detail(proc: subprocess.CompletedProcess, fallback: str) -> str:
    detail = (proc.stderr or proc.stdout or "").strip()
    return detail or fallback


def _timeout_detail(action: str, exc: subprocess.TimeoutExpired) -> str:
    return f"{action} timed out after {exc.timeout} seconds"


def _apply_setup(p: Dict[str, Any], opts: Options) -> Dict[str, Any]:
    paths.data_dir().mkdir(parents=True, exist_ok=True)
    rt = dict(p["runtime"])
    changed: List[str] = []

    # 1. Create the managed runtime if the plan calls for it.
    if rt.get("needs_create"):
        try:
            py = runtime.create_managed_venv()
        except runtime.RuntimeError_ as exc:
            return {"ok": False, "action": "setup", "error": str(exc)}
        rt["command"] = [py, "-m", "tokdash"]
        rt["python"] = py
        changed.append("runtime:venv")

    # 2. Service (systemd on Linux/WSL, launchd on macOS). Both write a marked unit/plist
    #    and bring it up; both record an identical-shaped service_block for the manifest.
    service_block = None
    service_status = {"type": "none"}
    svc_type = p["service"]["type"]
    marker_id = p["marker_id"]
    if svc_type == "systemd-user":
        unit_text = systemd.render_unit(
            rt["command"], p["bind"], p["port"], marker_id=marker_id, env_data_dir=p["env_data_dir"]
        )
        unit_path = systemd.write_unit(unit_text)
        try:
            systemd.daemon_reload()
            proc = systemd.enable_now()
            if proc.returncode != 0:
                return {"ok": False, "action": "setup", "error": f"systemctl enable failed: {proc.stderr.strip()}"}
            # `enable --now` does not necessarily restart an already-running manual
            # tokdash.service after ExecStart changes. Restart so --force migrations and
            # re-runs actually pick up the newly-written unit before readiness probing.
            restart_error = None
            try:
                restart_proc = systemd.restart()
            except subprocess.TimeoutExpired as exc:
                # systemctl can outlive our client-side wait while the service still becomes
                # healthy. Keep the diagnostic, then let the /health fingerprint decide.
                restart_proc = None
                restart_error = _timeout_detail("systemctl restart", exc)
        except Exception as exc:  # pragma: no cover - environment dependent
            return {"ok": False, "action": "setup", "error": f"failed to start service: {exc}"}
        service_status = systemd.status()
        if restart_proc is not None and restart_proc.returncode != 0:
            restart_error = _proc_failure_detail(restart_proc, f"systemctl restart exited {restart_proc.returncode}")
        if restart_error:
            service_status["restart_error"] = restart_error
        service_block = {
            "type": "systemd-user", "unit": str(unit_path), "name": systemd.SERVICE_NAME,
            "created_by_setup": True, "marker": manifest.marker_token(marker_id),
        }
        changed.append("service:systemd-user")
    elif svc_type == "launchd":
        plist_text = launchd.render_plist(
            rt["command"], p["bind"], p["port"], marker_id=marker_id, env_data_dir=p["env_data_dir"]
        )
        plist_path = launchd.write_plist(plist_text)
        launchd_errors: List[str] = []
        try:
            # Non-zero is benign when the agent simply was not loaded yet. Bootstrap and
            # the health probe below decide whether setup reached the desired state.
            launchd.bootout()
        except subprocess.TimeoutExpired as exc:
            # The command may continue unloading asynchronously. Try bootstrap anyway, then
            # rely on the health probe instead of leaving a written plist with no manifest.
            launchd_errors.append(_timeout_detail("launchctl bootout", exc))
        except Exception as exc:  # pragma: no cover - environment dependent
            return {"ok": False, "action": "setup", "error": f"failed to unload launchd agent: {exc}"}
        try:
            proc = launchd.bootstrap(plist_path)
            if proc.returncode != 0:
                launchd_errors.append(
                    "launchctl bootstrap: "
                    + _proc_failure_detail(proc, f"exit {proc.returncode}")
                )
        except subprocess.TimeoutExpired as exc:
            launchd_errors.append(_timeout_detail("launchctl bootstrap", exc))
        except Exception as exc:  # pragma: no cover - environment dependent
            return {"ok": False, "action": "setup", "error": f"failed to start launchd agent: {exc}"}
        service_status = launchd.status()
        if launchd_errors:
            service_status["start_error"] = "; ".join(launchd_errors)
        service_block = {
            "type": "launchd", "unit": str(plist_path), "name": launchd.LABEL,
            "created_by_setup": True, "marker": manifest.marker_token(marker_id),
        }
        changed.append("service:launchd")
    elif svc_type == "winsched":
        task_text = winsched.render_task(
            rt["command"], p["bind"], p["port"], marker_id=marker_id, env_data_dir=p["env_data_dir"]
        )
        task_path = winsched.write_task(task_text)
        winsched_errors: List[str] = []
        try:
            # Non-zero is benign only in that `/F` already forces a clean replace; a genuine
            # failure here means the task never got registered at all.
            proc = winsched.create(task_path)
            if proc.returncode != 0:
                return {
                    "ok": False, "action": "setup",
                    "error": f"schtasks /Create failed: {_proc_failure_detail(proc, f'exit {proc.returncode}')}",
                }
        except subprocess.TimeoutExpired as exc:
            return {"ok": False, "action": "setup", "error": _timeout_detail("schtasks /Create", exc)}
        except Exception as exc:  # pragma: no cover - environment dependent
            return {"ok": False, "action": "setup", "error": f"failed to register Task Scheduler task: {exc}"}
        try:
            run_proc = winsched.run_now()
            if run_proc.returncode != 0:
                winsched_errors.append(
                    "schtasks /Run: " + _proc_failure_detail(run_proc, f"exit {run_proc.returncode}")
                )
        except subprocess.TimeoutExpired as exc:
            winsched_errors.append(_timeout_detail("schtasks /Run", exc))
        except Exception as exc:  # pragma: no cover - environment dependent
            return {"ok": False, "action": "setup", "error": f"failed to start Task Scheduler task: {exc}"}
        service_status = winsched.status()
        if winsched_errors:
            service_status["start_error"] = "; ".join(winsched_errors)
        service_block = {
            "type": "winsched", "unit": str(task_path), "name": winsched.TASK_NAME,
            "created_by_setup": True, "marker": manifest.marker_token(marker_id),
        }
        changed.append("service:winsched")

    # 3. Record the manifest (the revert contract).
    fit = detect.python_fitness(rt.get("python"))
    man = manifest.build_manifest(
        install_method=rt["install_method"],
        runtime_kind=rt["kind"],
        runtime_command=rt["command"],
        runtime_owned_by_setup=rt["owned_by_setup"],
        python_path=rt.get("python") or "",
        python_version=fit.get("version") or "",
        service=service_block,
        runtime_marker=str(paths.runtime_marker_path()) if rt["owned_by_setup"] else None,
        data_dir=p["data_dir"],
        bind=p["bind"],
        port=p["port"],
    )
    manifest.write_manifest(man)
    changed.append("manifest")

    result = {
        "ok": True,
        "action": "setup",
        "url": p["url"],
        "bind": p["bind"],
        "port": p["port"],
        "runtime_kind": rt["kind"],
        "runtime_command": rt["command"],
        "service": service_status,
        "manifest": str(paths.manifest_path()),
        "changed": changed,
        "notes": p.get("notes", []),
        "warnings": p.get("warnings", []),
    }
    if svc_type == "systemd-user":
        mismatch = _systemd_fragment_mismatch(systemd.SERVICE_NAME, service_block["unit"] if service_block else "")
        if mismatch:
            result.update({
                "ok": False,
                "error": (
                    f"systemd loaded {systemd.SERVICE_NAME}.service from {mismatch}, not "
                    f"{service_block['unit']}. This usually means another tokdash.service "
                    "is already installed; setup did not verify the new service."
                ),
            })
            return result
    if svc_type in {"systemd-user", "launchd", "winsched"}:
        readiness = _wait_for_service_ready(p["bind"], p["port"])
        result["readiness"] = readiness
        if not readiness.get("ok"):
            result.update({"ok": False, "error": readiness.get("error")})
    return result


# --- doctor ---------------------------------------------------------------------


def cmd_doctor(opts: Options) -> int:
    # Diagnose the port setup actually recorded, not the bare default — setup may have
    # auto-picked a free port (§11) and the service is bound there (Codex F3).
    pre = manifest.read_manifest()
    port = opts.port or (pre or {}).get("port") or DEFAULT_PORT
    detection = detect.detect_all(int(port))
    man = detection.get("manifest")
    if man and man.get("bind"):
        detection["port"] = detect.probe_port(int(port), host=_probe_host_for_bind(man["bind"]))
    fit = detection["python"]

    issues: List[str] = []
    # Data-dir / ExecStart mismatch: a unit still pointing at the wrong data dir (§10.1).
    if man and man.get("data_dir") and man["data_dir"] != detection["data_dir"]:
        issues.append(
            f"manifest data_dir {man['data_dir']} != resolved {detection['data_dir']} "
            "(TOKDASH_DATA_DIR changed since setup)."
        )
    if not fit.get("fit"):
        issues.append(f"Python is not fit: {fit.get('reason')}")

    service_info = _doctor_service(man, detection)
    _append_service_issues(issues, man, service_info, detection)

    from .. import __version__

    report = {
        "ok": not issues,
        "action": "doctor",
        "version": __version__,
        "os": detection["os"],
        "python": {"version": fit.get("version"), "fit": fit.get("fit"), "reason": fit.get("reason")},
        "systemd_user": detection["systemd_user"],
        "launchd": detection.get("launchd"),
        "winsched": detection.get("winsched"),
        "data_dir": detection["data_dir"],
        "manifest_present": man is not None,
        "install_method": (man or {}).get("install_method"),
        "runtime_kind": (man or {}).get("runtime_kind"),
        "service": service_info,
        "port": detection["port"],
        "update_check": _doctor_update_check(),
        "quota": _doctor_quota(),
        "issues": issues,
    }

    if opts.json:
        _print_json(report)
    else:
        _print_doctor_human(report)
    return EXIT_OK if report["ok"] else EXIT_FAIL


def _doctor_service(man: Optional[Dict[str, Any]], detection: Dict[str, Any]) -> Dict[str, Any]:
    service = (man or {}).get("service") or {}
    existing = detection["existing_service"]
    unit = (
        service.get("unit")
        or existing.get("systemd_unit")
        or existing.get("launchd_plist")
        or existing.get("winsched_task")
    )
    stype = service.get("type")
    info: Dict[str, Any] = {"type": stype, "unit": unit, "present": bool(unit and Path(unit).is_file())}
    if stype == "systemd-user" and detection["systemd_user"]:
        loaded = systemd.fragment_path()
        info.update({
            "enabled": systemd.is_enabled(),
            "active": systemd.is_active(),
            "fragment_path": loaded,
            "fragment_matches_unit": bool(loaded and unit and _same_path(loaded, unit)),
        })
    elif stype == "launchd" and detection.get("launchd"):
        loaded = launchd.is_loaded()
        info.update({"enabled": loaded, "active": loaded})
    elif stype == "winsched" and detection.get("winsched"):
        registered = winsched.is_registered()
        info.update({"enabled": registered, "active": winsched.is_running() if registered else False})
    return info


def _append_service_issues(issues: List[str], man: Optional[Dict[str, Any]], service: Dict[str, Any], detection: Dict[str, Any]) -> None:
    if not man:
        port = detection.get("port") or {}
        if service.get("present"):
            detail = f"{service.get('unit')} exists but no Tokdash setup manifest is present."
            if port.get("open") and not port.get("is_tokdash"):
                detail += (
                    " The default port is also occupied by a process that does not expose "
                    "Tokdash's /health fingerprint; restart your upgraded Tokdash service, "
                    "or run `tokdash setup --force` if you want onboarding to manage it."
                )
            else:
                detail += " `tokdash setup` will not overwrite it unless you pass --force."
            issues.append(detail)
        elif port.get("open") and not port.get("is_tokdash"):
            issues.append(
                f"port {port.get('port')} is occupied by something that does not expose "
                "Tokdash's /health fingerprint."
            )
        return
    stype = service.get("type")
    if stype not in {"systemd-user", "launchd", "winsched"}:
        return
    if not service.get("present"):
        issues.append(f"{stype} service unit is recorded in the manifest but the file is missing: {service.get('unit')}")
        return
    if stype == "systemd-user":
        if not detection.get("systemd_user"):
            issues.append("systemd user services are unavailable, but the manifest records a systemd service.")
            return
        loaded = service.get("fragment_path")
        if loaded and not service.get("fragment_matches_unit"):
            issues.append(
                f"systemd service name resolves to {loaded}, not the manifest unit {service.get('unit')}."
            )
    elif stype == "winsched" and not detection.get("winsched"):
        issues.append("Task Scheduler (schtasks) is unavailable, but the manifest records a winsched service.")
        return
    if "active" in service and not service.get("active"):
        issues.append(f"{stype} service is not active.")
    port = detection.get("port") or {}
    if service.get("active") and not port.get("is_tokdash"):
        if port.get("open"):
            issues.append(f"service is active, but port {port.get('port')} is occupied by something other than Tokdash.")
        else:
            issues.append(f"service is active, but Tokdash is not answering on port {port.get('port')}.")


def _doctor_update_check() -> Dict[str, Any]:
    # Opt-in only: never touches the network unless the user enabled update checks (§14).
    if not updatecheck.is_enabled():
        return {"enabled": False}
    from .. import __version__

    return {"enabled": True, **updatecheck.check(__version__)}


def _doctor_quota() -> Dict[str, Any]:
    """Quota tracking state for `tokdash doctor` (read-only; never touches the network)."""
    try:
        from ..sources.quota import config as quota_config
        from ..sources.quota import last_poll_at
    except Exception:
        return {"available": False}

    interval_seconds, interval_source = quota_config.effective_poll_interval()
    report: Dict[str, Any] = {
        "available": True,
        "enabled": quota_config.quota_tracking_enabled(),
        "config_enabled": quota_config.quota_config_enabled(),
        "kill_switch": quota_config.quota_poll_killed(),
        "consent": quota_config.read_quota_config(),
        "interval_seconds": interval_seconds,
        "interval_source": interval_source,
        "poll_interval_minutes": quota_config.read_poll_interval_minutes()
        or quota_config.DEFAULT_POLL_INTERVAL_MINUTES,
        "last_poll_at": None,
        "snapshots": None,
    }
    try:
        from ..usage_store import UsageEntryStore, persistent_usage_db_enabled

        if persistent_usage_db_enabled():
            store = UsageEntryStore()
            report["last_poll_at"] = last_poll_at(store)
            report["snapshots"] = int(store.status().get("quota_snapshots") or 0)
    except Exception:
        pass
    return report


# --- update ---------------------------------------------------------------------


def cmd_update(opts: Options) -> int:
    """Upgrade the runtime in place per the recorded install method (§14).

    Ownership does NOT gate update — the user explicitly asked to upgrade — but the method
    must be one we can drive in place (pipx / managed venv). An ``existing`` interpreter
    (unknown package manager) or a missing manifest only prints guidance and never mutates.
    """
    man = manifest.read_manifest()
    method = (man or {}).get("install_method")
    service = (man or {}).get("service") or {}
    venv_python = (man or {}).get("python_path") or str(paths.managed_venv_python())
    runtime_command = list((man or {}).get("runtime_command") or [])

    if method == "pipx":
        if not detect.find_pipx():
            return _update_guidance(opts, "pipx is recorded but not on PATH; run `pipx upgrade tokdash` yourself.")
        cmd = ["pipx", "upgrade", "tokdash"]
    elif method == "managed-venv":
        cmd = [venv_python, "-m", "pip", "install", "-U", "tokdash"]
    elif method is None:
        return _update_guidance(
            opts,
            "No setup manifest found; run `tokdash setup` first, or upgrade the way you installed "
            "Tokdash (e.g. `pipx upgrade tokdash` or `pip install -U tokdash`).",
        )
    else:
        # "existing" (unknown package manager) or "binary" (deferred): never upgrade an
        # interpreter/install setup did not create.
        return _update_guidance(
            opts,
            f"Tokdash runs from a {method!r} runtime that setup did not create; upgrade it the way "
            "you installed it (e.g. `pipx upgrade tokdash`, `pip install -U tokdash`, or conda).",
        )

    service_type = service.get("type")
    restart_managed = service_type in {"systemd-user", "launchd", "winsched"}
    # Fallback must be service-type aware: the launchd label is com.tokdash.tokdash, not
    # "tokdash" — a literal-"tokdash" fallback would print a remediation command targeting a
    # non-existent agent. `or` (not dict.get default) so a manifest with name present-but-None
    # (hand-edited/corrupt) also degrades to the safe default instead of "...restart None".
    if service_type == "launchd":
        default_name = launchd.LABEL
    elif service_type == "winsched":
        default_name = winsched.TASK_NAME
    else:
        default_name = systemd.SERVICE_NAME
    service_name = service.get("name") or default_name

    if opts.dry_run:
        # Use the SAME `has_managed_service` key as the live result below so a bundler reading
        # the --json output sees one stable schema across dry-run and apply.
        payload = {"ok": True, "action": "update", "install_method": method, "command": cmd,
                   "has_managed_service": restart_managed, "service_type": service_type, "dry_run": True}
        if opts.json:
            _print_json(payload)
        else:
            print(f"Would run: {' '.join(cmd)}")
            if restart_managed:
                if service_type == "launchd":
                    print(f"Would restart: launchctl kickstart -k gui/$(id -u)/{service_name}")
                elif service_type == "winsched":
                    print(f"Would restart: schtasks /End /TN {service_name} & schtasks /Run /TN {service_name}")
                else:
                    print(f"Would restart: systemctl --user restart {service_name}")
        return EXIT_OK

    version_before = _runtime_tokdash_version(runtime_command, venv_python)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception as exc:
        return _emit_update_result(
            opts,
            {
                "ok": False,
                "action": "update",
                "install_method": method,
                "version_before": version_before,
                "error": f"upgrade failed: {exc}",
            },
        )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-500:]
        return _emit_update_result(
            opts,
            {
                "ok": False,
                "action": "update",
                "install_method": method,
                "version_before": version_before,
                "error": detail or "upgrade failed",
            },
        )
    version_after = _runtime_tokdash_version(runtime_command, venv_python)
    updated = (version_before != version_after) if version_before and version_after else None

    # The upgrade landed, but a managed service is still running the OLD code until it
    # restarts. A failed restart must NOT be reported as success — otherwise the dashboard
    # silently serves the old version and a bundler thinks the update fully applied.
    restarted = False
    restart_failed = False
    if restart_managed:
        if service_type == "launchd":
            if detect.launchd_available():
                # A hung/missing launchctl raises (TimeoutExpired/FileNotFoundError); treat
                # ANY failure as a failed restart so the user gets the remediation command
                # via _emit_update_result, never a raw traceback (the upgrade already landed).
                try:
                    restarted = launchd.kickstart().returncode == 0
                except Exception:
                    restarted = False
                restart_failed = not restarted
            else:
                restart_failed = True  # managed agent exists but launchctl is unreachable
        elif service_type == "winsched":
            if detect.winsched_available():
                # Mirrors the launchd arm: any failure (including a hung/missing schtasks)
                # is a failed restart, never a raw traceback.
                try:
                    restarted = winsched.restart(service_name).returncode == 0
                except Exception:
                    restarted = False
                restart_failed = not restarted
            else:
                restart_failed = True  # managed task exists but schtasks is unreachable
        elif detect.systemd_user_available():
            try:
                restarted = systemd.restart(service_name).returncode == 0
            except Exception:
                restarted = False
            restart_failed = not restarted
        else:
            restart_failed = True  # a managed service exists but systemd is unreachable

    return _emit_update_result(
        opts,
        {
            "ok": not restart_failed,
            "action": "update",
            "install_method": method,
            "command": cmd,
            "version_before": version_before,
            "version_after": version_after,
            "updated": updated,
            "has_managed_service": restart_managed,
            "service_type": service_type,
            "service_name": service_name,
            "service_restarted": restarted,
            "restart_failed": restart_failed,
        },
    )


def _runtime_tokdash_version(runtime_command: List[str], python_path: str = "") -> Optional[str]:
    """Return the Tokdash version for a recorded runtime, or None if it cannot be read."""
    cmd = list(runtime_command or [])
    if not cmd and python_path:
        cmd = [python_path, "-m", "tokdash"]
    if not cmd:
        return None
    try:
        proc = subprocess.run(cmd + ["--version"], capture_output=True, text=True, timeout=20)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    text = (proc.stdout or proc.stderr or "").strip()
    match = re.search(r"\btokdash\s+([^\s]+)", text, flags=re.I)
    if match:
        return match.group(1)
    match = re.search(r"\b\d+(?:\.\d+)+(?:[a-zA-Z0-9_.+-]*)?\b", text)
    return match.group(0) if match else None


def _update_guidance(opts: Options, message: str) -> int:
    # Known-but-unmanageable or unknown method: report, never mutate. Exit 0 — nothing is
    # wrong, there is just nothing safe to upgrade automatically.
    if opts.json:
        _print_json({"ok": True, "action": "update", "updated": False, "reason": message})
    else:
        print(message)
    return EXIT_OK


def _emit_update_result(opts: Options, result: Dict[str, Any]) -> int:
    if opts.json:
        _print_json(result)
    elif result.get("ok"):
        before = result.get("version_before")
        after = result.get("version_after")
        method = result.get("install_method")
        if before and after and before == after:
            print(f"Tokdash is already at {after} via {method}.")
        elif before and after:
            print(f"Updated Tokdash via {method}: {before} -> {after}.")
        elif after:
            print(f"Updated Tokdash via {method}: now {after}.")
        else:
            print(f"Updated Tokdash via {method}.")
        if result.get("service_restarted"):
            print("  • restarted the background service")
        elif not result.get("has_managed_service"):
            print("  • no managed service to restart")
    elif result.get("restart_failed"):
        # The package upgraded but the long-running service did not restart. Give the
        # platform-correct restart command (systemctl on Linux, launchctl on macOS).
        name = result.get("service_name", "tokdash")
        if result.get("service_type") == "launchd":
            cmd = f"launchctl kickstart -k gui/$(id -u)/{name}"
        elif result.get("service_type") == "winsched":
            cmd = f"schtasks /End /TN {name} & schtasks /Run /TN {name}"
        else:
            cmd = f"systemctl --user restart {name}"
        before = result.get("version_before")
        after = result.get("version_after")
        version = f" ({before} -> {after})" if before and after and before != after else (
            f" (still {after})" if after else ""
        )
        print(f"Upgrade command completed via {result.get('install_method')}{version}, but the service restart FAILED —")
        print(f"  the service is still running the old code. Run: {cmd}")
    else:
        print(f"Update failed: {result.get('error')}")
    return EXIT_OK if result.get("ok") else EXIT_FAIL


# --- uninstall ------------------------------------------------------------------


def cmd_uninstall(opts: Options) -> int:
    detection = detect.detect_all(opts.port or DEFAULT_PORT)
    interactive = not opts.auto and not opts.yes and not opts.dry_run and detection["tty"]

    # Plan first so blockers (e.g. an unmarked unit needing --force) surface BEFORE we
    # ask the user any questions — no point prompting for decisions the plan won't reach.
    p = plan.build_uninstall_plan(opts, detection)

    if p["blockers"]:
        _emit_plan(p, opts)
        for b in p["blockers"]:
            _err(b)
        return EXIT_FAIL

    if opts.dry_run:
        _emit_plan(p, opts)
        return EXIT_OK

    # Interactive wizard: ask the two decisions §12 says matter, then re-plan with them.
    if interactive:
        _uninstall_wizard(opts, detection)
        p = plan.build_uninstall_plan(opts, detection)

    if not p["steps"]:
        if opts.json:
            _print_json({"ok": True, "action": "uninstall", "changed": [], "notes": p.get("notes", [])})
        else:
            print("Nothing to revert.")
            for n in p.get("notes", []):
                print(f"  • {n}")
        return EXIT_OK

    if not opts.auto and not opts.yes and not detection["tty"]:
        _emit_plan(p, opts)
        _err("Not a terminal and neither --auto nor --yes was given; nothing was changed.")
        return EXIT_NEEDS_CONFIRM

    if interactive:
        if not opts.json:
            _print_uninstall_human_plan(p)
        if not _confirm("Proceed with uninstall?", default=True):
            print("Aborted; nothing was changed.")
            return EXIT_FAIL

    result = _apply_uninstall(p, opts)
    _emit_result(result, opts)
    return EXIT_OK if result.get("ok") else EXIT_FAIL


def _uninstall_wizard(opts: Options, detection: Dict[str, Any]) -> None:
    """Ask the two decisions §12 mandates: keep the setup-owned runtime, and keep data."""
    man = detection.get("manifest")
    owned = bool((man or {}).get("runtime_owned_by_setup")) or (
        man is None and detect.managed_runtime_present()
    )
    if owned and not opts.keep_runtime:
        if not _confirm(f"Remove the setup-owned runtime at {paths.runtime_dir()}?", default=True):
            opts.keep_runtime = True
    if not opts.purge:
        if _confirm("Also delete usage history and config (cannot be undone)?", default=False):
            opts.purge = True


def _apply_uninstall(p: Dict[str, Any], opts: Options) -> Dict[str, Any]:
    changed: List[str] = []
    errors: List[str] = []
    kept_manifest = False

    for step in p["steps"]:
        kind = step["kind"]
        try:
            if kind == "tailscale":
                # Exact targeted `off` recorded by setup — never `tailscale serve reset`.
                # This network command IS the entire revert (no local artifact to remove), so
                # a non-zero exit must be reported as failure — otherwise uninstall claims
                # success while the unauthenticated Serve exposure is still live (mirror of
                # how setup's tailscale.run_serve treats a failed `serve`).
                proc = subprocess.run(step["command"], capture_output=True, text=True, timeout=20)
                if proc.returncode != 0:
                    detail = (proc.stderr or proc.stdout or "").strip()
                    errors.append(f"tailscale: teardown failed (exit {proc.returncode}): {detail} — exposure may still be live; run `{' '.join(step['command'])}`")
                else:
                    changed.append("tailscale")
            elif kind == "service":
                name = step.get("name", "tokdash")
                if step.get("service_type") == "launchd":
                    if detect.launchd_available():
                        # `launchctl bootout` returns non-zero when the agent simply isn't
                        # loaded (benign — nothing to stop), so treat it as a failure ONLY
                        # if the agent is STILL loaded afterwards. A genuine "couldn't stop
                        # it" must abort BEFORE we unlink the plist (so a retry can still
                        # find it) and surface as an error — otherwise uninstall would report
                        # success while the unauthenticated service keeps running.
                        proc = launchd.bootout()  # stop + unload the agent
                        if proc.returncode != 0:
                            # Confirm the load state with a STRICT probe: a hung launchctl
                            # makes the confirmation time out too, and an unconfirmable state
                            # must fail CLOSED (assume still loaded) rather than fall through
                            # to unlink — otherwise a correlated hang reports a false success
                            # and deletes the manifest while the agent keeps running.
                            try:
                                still_loaded = launchd.is_loaded_strict()
                            except Exception:
                                still_loaded = True
                            if still_loaded:
                                detail = (proc.stderr or proc.stdout or "").strip()
                                raise OSError(
                                    "launchctl bootout failed and the agent is still loaded "
                                    f"(or its state could not be confirmed): {detail}"
                                )
                    unit = Path(step["unit"])
                    if unit.is_file():
                        unit.unlink()
                elif step.get("service_type") == "winsched":
                    if detect.winsched_available():
                        # `schtasks /Delete` returns non-zero when the task simply isn't
                        # registered (benign — nothing to remove), so treat it as a failure
                        # ONLY if the task is STILL registered afterwards. A genuine
                        # "couldn't delete it" must abort BEFORE we unlink our recorded XML
                        # (so a retry can still find it) and surface as an error — mirroring
                        # the launchd arm above.
                        proc = winsched.delete(name)
                        if proc.returncode != 0:
                            try:
                                still_registered = winsched.is_registered_strict(name)
                            except Exception:
                                still_registered = True
                            if still_registered:
                                detail = (proc.stderr or proc.stdout or "").strip()
                                raise OSError(
                                    "schtasks /Delete failed and the task is still registered "
                                    f"(or its state could not be confirmed): {detail}"
                                )
                    unit = Path(step["unit"])
                    if unit.is_file():
                        unit.unlink()
                else:
                    if detect.systemd_user_available():
                        loaded = _systemd_fragment_mismatch(name, step["unit"])
                        if loaded:
                            # The name resolves to another unit (for example an older
                            # hand-written ~/.config/systemd/user/tokdash.service while a
                            # test wrote a temp XDG_CONFIG_HOME unit). Never run
                            # disable/restart operations against that foreign service name.
                            unit = Path(step["unit"])
                            if unit.is_file():
                                unit.unlink()
                            systemd.daemon_reload()
                            changed.append("service")
                            continue
                        # A non-zero exit is benign when there's simply nothing left to stop —
                        # e.g. the unit file was already removed by a prior partial uninstall
                        # (the planner still schedules this step to stop a possibly-loaded
                        # service), so `disable --now` returns "Unit file does not exist". Tell
                        # that apart from a real stop-job failure by confirming the active state
                        # with a STRICT probe; an unconfirmable probe (hung systemctl) fails
                        # CLOSED, mirroring the launchd arm above. Abort BEFORE unlinking so a
                        # still-running service is never left with its unit gone + manifest deleted.
                        proc = systemd.disable_now(name)
                        if proc.returncode != 0:
                            try:
                                still_active = systemd.is_active_strict(name)
                            except Exception:
                                still_active = True
                            if still_active:
                                detail = (proc.stderr or proc.stdout or "").strip()
                                raise OSError(
                                    f"systemctl --user disable --now {name} failed and the service is still "
                                    f"active (or its state could not be confirmed): {detail}"
                                )
                    unit = Path(step["unit"])
                    if unit.is_file():
                        unit.unlink()
                    if detect.systemd_user_available():
                        systemd.daemon_reload()
                changed.append("service")
            elif kind == "runtime":
                rt_dir = Path(step["path"])
                if rt_dir.is_dir():
                    # NOT ignore_errors: a failed removal must surface as an error (so ok is
                    # False and the manifest is preserved for retry) rather than be reported
                    # as success while the runtime tree survives on disk.
                    shutil.rmtree(rt_dir)
                    if rt_dir.exists():
                        raise OSError(f"runtime tree not fully removed: {rt_dir}")
                changed.append("runtime")
            elif kind == "data":
                _purge_data()
                changed.append("data")
            elif kind == "manifest":
                # The manifest is the ONLY record of how to retry failed steps (e.g. the exact
                # tailscale teardown command, which has no on-disk marker). If anything failed,
                # PRESERVE it so a re-run can retry — deleting it would strand the revert (the
                # exposure stays live and the next uninstall falsely reports success). Manifest
                # is the last planned step, so `errors` here reflects every prior step.
                if errors:
                    kept_manifest = True
                else:
                    manifest.remove_manifest()
                    changed.append("manifest")
        except Exception as exc:  # keep going; uninstall is partial-safe (§12.2)
            errors.append(f"{kind}: {exc}")

    return {
        "ok": not errors,
        "action": "uninstall",
        "changed": changed,
        "errors": errors,
        "manifest_kept_for_retry": kept_manifest,
        "kept": p.get("kept", []),
        "data_dir": p["data_dir"],
        "purged": opts.purge,
    }


def _purge_data() -> None:
    """Delete usage DB (+ sidecars), config, and the dashboard pricing override.

    The pricing override (``<data_dir>/pricing_db.json``) is user config that the round-2
    relocation moved under the data dir, so ``--purge`` must remove it too — otherwise
    edited pricing would survive a "delete usage history + config" teardown. Leaves a
    setup-owned runtime to its own step.
    """
    db = paths.usage_db_path()
    override = paths.pricing_db_override_path()
    targets = (
        db,
        Path(str(db) + "-wal"),
        Path(str(db) + "-shm"),
        Path(str(db) + ".lock"),
        paths.config_path(),
        override,
        override.with_suffix(override.suffix + ".tmp"),  # crashed-write sidecar from update_pricing_db
    )
    # Verify removal instead of swallowing failures: a target still present after the unlink
    # attempt (busy/locked/permission-denied) must surface as an error so the caller records
    # it (ok:false + manifest preserved), mirroring the runtime step's post-rmtree check —
    # otherwise --purge would claim "deleted usage history + config" while files survive.
    failed: List[str] = []
    for path in targets:
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass  # re-checked below; only a still-present file is a real failure
        if path.exists():  # follows symlinks; ENOENT (already gone) is the idempotent success
            failed.append(str(path))
    if failed:
        raise OSError("could not purge (still present): " + ", ".join(failed))


# --- output ---------------------------------------------------------------------


def _color_enabled() -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _style(text: str, code: str) -> str:
    if not _color_enabled():
        return text
    return f"\033[{code}m{text}\033[0m"


def _bold(text: str) -> str:
    return _style(text, "1")


def _ok(text: str) -> str:
    return _style(text, "32")


def _warn(text: str) -> str:
    return _style(text, "33")


def _bad(text: str) -> str:
    return _style(text, "31")


def _accent(text: str) -> str:
    return _style(text, "36")


def _emit_plan(p: Dict[str, Any], opts: Options) -> None:
    if opts.json:
        _print_json({**p, "dry_run": True})
    elif p["action"] == "setup":
        _print_setup_human_plan(p)
    else:
        _print_uninstall_human_plan(p)


def _emit_result(result: Dict[str, Any], opts: Options) -> None:
    if opts.json:
        _print_json(result)
    elif result["action"] == "setup":
        _print_setup_result(result)
    else:
        _print_uninstall_result(result)


def _print_json(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _print_setup_human_plan(p: Dict[str, Any]) -> None:
    print(_accent(_bold("Tokdash setup plan")) + "\n")
    print(f"  {_bold('URL:')}      {_accent(p['url'])}")
    print(f"  {_bold('Runtime:')}  {p['runtime']['kind']} ({'setup-owned' if p['runtime']['owned_by_setup'] else 'kept on uninstall'})")
    print(f"  {_bold('Service:')}  {p['service']['type']}")
    print(f"  {_bold('Data dir:')} {p['data_dir']}")
    if p["changes"]:
        print(f"\n  {_ok('Will:')}")
        for c in p["changes"]:
            print(f"    {_ok('•')} {c}")
    if p.get("blocked_changes"):
        print(f"\n  {_bad('Blocked')} (will NOT run until the issues below are resolved):")
        for c in p["blocked_changes"]:
            print(f"    {_bad('•')} {c}")
    _print_advisories(p)


def _print_setup_result(r: Dict[str, Any]) -> None:
    if not r.get("ok"):
        print(_bad(f"Setup failed: {r.get('error')}"))
        return
    svc = r.get("service", {})
    tailnet_url = r.get("tailscale_url")
    if svc.get("type") == "systemd-user":
        print(_ok(_bold("Tokdash is running as a user service.")) + "\n")
        _print_open_targets(r["url"], tailnet_url)
        print(f"\n  {_bold('Manage:')}")
        print(f"    Status:   systemctl --user status {svc['name']} --no-pager")
        print(f"    Logs:     journalctl --user -u {svc['name']} -f")
        print(f"    Restart:  systemctl --user restart {svc['name']}")
        print("    Remove:   tokdash uninstall")
    elif svc.get("type") == "launchd":
        print(_ok(_bold("Tokdash is running as a launchd user agent.")) + "\n")
        _print_open_targets(r["url"], tailnet_url)
        print(f"\n  {_bold('Manage:')}")
        print(f"    Status:   launchctl print gui/$(id -u)/{svc['name']}")
        print(f"    Restart:  launchctl kickstart -k gui/$(id -u)/{svc['name']}")
        print("    Remove:   tokdash uninstall")
    elif svc.get("type") == "winsched":
        print(_ok(_bold("Tokdash is running as a Windows Task Scheduler task.")) + "\n")
        _print_open_targets(r["url"], tailnet_url)
        print(f"\n  {_bold('Manage:')}")
        print(f"    Status:   schtasks /Query /TN {svc['name']} /V")
        print(f"    Restart:  schtasks /End /TN {svc['name']} & schtasks /Run /TN {svc['name']}")
        print("    Remove:   tokdash uninstall")
    else:
        # Match the recorded port/bind exactly — setup may have auto-picked a free port,
        # so a bare `tokdash serve` (which defaults to 55423) would bind elsewhere.
        serve_cmd = "tokdash serve"
        if r["bind"] != "127.0.0.1":
            serve_cmd += f" --bind {r['bind']}"
        if r["port"] != DEFAULT_PORT:
            serve_cmd += f" --port {r['port']}"
        print(_ok(_bold("Tokdash setup recorded.")) + "\n")
        _print_open_targets(r["url"], tailnet_url)
        print(f"\n  {_bold('Run:')}      {serve_cmd}")
        print(f"  {_bold('Remove:')}   tokdash uninstall")
    for n in _setup_result_notes(r):
        print(f"  {_ok('•')} {n}")
    for w in r.get("warnings", []):
        print(f"  {_warn('⚠')} {w}")


def _print_open_targets(local_url: str, tailnet_url: Optional[str] = None) -> None:
    print(f"  {_bold('Open:')}")
    print(f"    Local:   {_accent(local_url)}")
    if tailnet_url:
        print(f"    Tailnet: {_accent(tailnet_url)}")
    else:
        print("    Remote:  use the Tailscale Serve command below, or use SSH forwarding")


def _setup_result_notes(r: Dict[str, Any]) -> List[str]:
    notes = list(r.get("notes", []))
    if r.get("tailscale_url"):
        notes = [n for n in notes if not n.startswith("Remote access (optional, explicit):")]
    return notes


def _print_uninstall_human_plan(p: Dict[str, Any]) -> None:
    src = p["manifest_path"] if p["have_manifest"] else "no manifest (conservative fallback)"
    print(f"Reverting Tokdash setup ({src})\n")
    for r in p["removed"]:
        print(f"  ✗ {r}")
    for c in p.get("blocked_changes", []):
        print(f"  ⨯ BLOCKED: would remove {c}")
    for k in p["kept"]:
        print(f"  • keep {k}")
    for n in p.get("notes", []):
        print(f"  • {n}")


def _print_uninstall_result(r: Dict[str, Any]) -> None:
    if r.get("ok"):
        print("Tokdash background service removed. Python was not touched.")
    else:
        print("Uninstall finished with errors:")
        for e in r.get("errors", []):
            print(f"  ✗ {e}")
    for k in r.get("kept", []):
        print(f"  • kept {k}")


def _print_doctor_human(r: Dict[str, Any]) -> None:
    mark = _ok("✓") if r["ok"] else _bad("✗")
    title = _ok(_bold("Tokdash doctor")) if r["ok"] else _bad(_bold("Tokdash doctor"))
    print(f"{mark} {title}\n")
    print(f"  Version:      {r.get('version') or 'unknown'}")
    print(f"  OS:           {r['os']}")
    py = r["python"]
    print(f"  Python:       {py['version']} ({'fit' if py['fit'] else 'UNFIT: ' + str(py['reason'])})")
    if r["os"] == "macos":
        print(f"  launchd:      {'available' if r.get('launchd') else 'unavailable'}")
    elif r["os"] == "windows":
        print(f"  Task Scheduler: {'available' if r.get('winsched') else 'unavailable'}")
    else:
        print(f"  systemd user: {'available' if r['systemd_user'] else 'unavailable'}")
    print(f"  Data dir:     {r['data_dir']}")
    print(f"  Install:      {r['install_method'] or 'no manifest (setup not run)'}")
    svc = r["service"]
    if svc.get("unit"):
        state = []
        if "active" in svc:
            state.append("active" if svc["active"] else "inactive")
        if "enabled" in svc:
            state.append("enabled" if svc["enabled"] else "disabled")
        print(f"  Service:      {svc['unit']} ({', '.join(state) or ('present' if svc['present'] else 'missing')})")
    port = r["port"]
    if port.get("open"):
        who = "Tokdash" if port.get("is_tokdash") else "another app"
        print(f"  Port {port['port']}:    in use by {who}")
    else:
        print(f"  Port {port['port']}:    free")
    uc = r.get("update_check", {})
    if uc.get("enabled"):
        if uc.get("error"):
            print(f"  Updates:      check failed ({uc['error']})")
        elif uc.get("update_available"):
            print(f"  Updates:      {uc.get('latest')} available (you have {uc.get('current')}) — run `tokdash update`")
        else:
            print(f"  Updates:      up to date ({uc.get('current')})")
    q = r.get("quota", {})
    if q.get("available"):
        state = "on" if q.get("enabled") else "off"
        if q.get("kill_switch"):
            state += ", TOKDASH_QUOTA_POLL=0 kill switch"
        elif not q.get("config_enabled"):
            state += ", disabled in config"
        print(f"  Quota:        {state}")
        consent = q.get("consent") or {}
        on = [key for key, value in consent.items() if value]
        print(f"  Quota network:{' ' + ', '.join(on) if on else ' none (local-only)'}")
        minutes = q.get("interval_seconds", 0) // 60
        print(f"  Quota poll:   every {minutes} min (from {q.get('interval_source')})")
        last = q.get("last_poll_at")
        snaps = q.get("snapshots")
        last_str = "never" if not last else time.strftime("%Y-%m-%d %H:%M", time.localtime(int(last)))
        print(f"  Quota data:   {snaps if snaps is not None else '—'} snapshots, last poll {last_str}")
    for issue in r["issues"]:
        print(f"  {_warn('⚠')} {issue}")


def _print_advisories(p: Dict[str, Any]) -> None:
    for n in p.get("notes", []):
        print(f"  {_ok('•')} {n}")
    for w in p.get("warnings", []):
        print(f"  {_warn('⚠')} {w}")


def _update_check_setup_step() -> None:
    """Ask whether to enable opt-in PyPI update-check notices (§14) — default YES.

    Unlike quota tracking this is a small, fully read-only check (no credentials, one cached
    PyPI GET) that only ever *reports* availability and never auto-upgrades anything, so
    consenting by default keeps the dashboard's "update available" badge useful out of the
    box while still requiring an explicit accept. Skipped entirely when the
    ``TOKDASH_UPDATE_CHECK`` kill switch (``0``/``false``/``no``/``off``) is set (offering to
    enable something the env is force-disabling would be misleading), or when update checks
    are already enabled.
    """
    if updatecheck.kill_switched():
        return
    if updatecheck.is_enabled():
        return

    print("\n" + _bold("Update notices (optional)"))
    print("  Tokdash can do a one-time, opt-in PyPI version check when the dashboard loads")
    print("  and show an \"update available\" badge. Read-only; it never auto-upgrades")
    print("  anything. Toggle it later from the dashboard's \"Enable update notices\" link.")

    # Any input failure (exhausted stdin / EOF / interrupt) means "accept the default and
    # stop asking" — matches the quota wizard so a broken terminal never blocks setup.
    try:
        enable = _confirm("  Enable update notices?", default=True)
    except (EOFError, KeyboardInterrupt, StopIteration):
        print()
        return
    if not enable:
        print("  Update notices disabled. Enable them any time from the dashboard.")
        return
    try:
        updatecheck.enable()
    except Exception:
        _err("  Could not save update-check setting; enable it later from the dashboard.")


def _quota_setup_wizard() -> None:
    """Ask the optional quota decisions §4 wants — the SAME settings the Quota tab exposes:
    the master on/off switch, per-provider network consent, and the poll interval.

    The three writes here (:func:`set_quota_enabled`, :func:`set_quota_consent`,
    :func:`set_poll_interval_minutes`) are exactly the ones the web UI's Quota-tracking
    toggle, per-provider "Enable quota API" buttons, and interval selector call, so the two
    entry points stay in lockstep. Any failure to persist is non-fatal — setup already
    succeeded, so we warn and move on.
    """
    try:
        from ..sources.quota import config as quota_config
    except Exception:
        return

    print("\n" + _bold("Quota tracking (optional)"))
    print("  The Quota tab shows how much of each subscription window (5-hour / weekly) is")
    print("  used and when it resets. Two data sources:")
    print("    - Local logs (no network): Codex records its own quota in session files;")
    print("      works out of the box, Codex only, updates only when you use Codex.")
    print("    - Live polling (off by default): asks each provider's quota endpoint using")
    print("      the sign-in your CLI already has. Fresher, adds Codex reset credits, and")
    print("      is the ONLY source for Claude Code and Antigravity. Read-only; Tokdash")
    print("      never refreshes or writes credentials.")

    # Master switch — identical to the Quota tab's "Quota tracking" toggle (config
    # quota.enabled). Off disables ALL quota work (including local session scanning); on
    # (the default) proceeds to the per-provider network + interval questions. Any input
    # failure (exhausted stdin / EOF / interrupt) means "accept the defaults and stop".
    try:
        enable = _confirm("  Enable quota tracking?", default=True)
    except (EOFError, KeyboardInterrupt, StopIteration):
        print()
        return
    try:
        quota_config.set_quota_enabled(enable)
    except Exception:
        _err("  Could not save quota setting; change it later from the Quota tab.")
    if not enable:
        print("  Quota tracking disabled. Enable it any time from the Quota tab.")
        return

    # Any input failure (exhausted stdin / EOF / interrupt) means "accept the defaults and
    # stop asking" — the wizard never blocks or crashes an otherwise-successful setup.
    try:
        consent_updates: Dict[str, Any] = {}
        for key, label, hint in (
            ("codex_api", "Codex", "fresher than local logs; adds reset credits"),
            ("claude_api", "Claude Code", "required for any Claude quota data"),
            ("antigravity_api", "Antigravity", "required for any Antigravity quota data"),
        ):
            consent_updates[key] = _confirm(f"  Enable live quota polling for {label} ({hint})?", default=False)
        try:
            quota_config.set_quota_consent(consent_updates)
        except Exception:
            _err("  Could not save quota consent; enable it later from the Quota tab.")
        if consent_updates.get("claude_api") and detect.os_kind() == "macos":
            # Claude Code keeps its sign-in in the Keychain on macOS; Tokdash reads it
            # (read-only), but the first access may need a one-time Keychain approval.
            print("  macOS note: Tokdash reads Claude Code's sign-in from the Keychain. If a")
            print("  Keychain permission prompt appears on the first poll, choose Always Allow.")
            print("  Headless/locked-Keychain sessions can set CLAUDE_CODE_OAUTH_TOKEN instead")
            print("  (create one with `claude setup-token`).")

        choices = quota_config.POLL_INTERVAL_CHOICES
        default_minutes = quota_config.DEFAULT_POLL_INTERVAL_MINUTES
        answer = input(f"  Poll interval in minutes {list(choices)} [{default_minutes}]: ").strip()
    except (EOFError, KeyboardInterrupt, StopIteration):
        print()
        return
    if answer:
        try:
            minutes = int(answer)
        except ValueError:
            minutes = 0
        if minutes in quota_config.POLL_INTERVAL_CHOICES:
            try:
                quota_config.set_poll_interval_minutes(minutes)
            except Exception:
                pass
        else:
            # Unrecognized input leaves the *existing* setting untouched — report whatever
            # is actually in effect (a previously-saved value, else the default), not a
            # blanket "default".
            current = quota_config.read_poll_interval_minutes() or quota_config.DEFAULT_POLL_INTERVAL_MINUTES
            print(f"  Unrecognized value; keeping the current interval ({current} min).")


def _confirm(prompt: str, default: bool = True) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        answer = input(prompt + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not answer:
        return default
    return answer in {"y", "yes"}


def _err(msg: str) -> None:
    print(_bad(msg), file=sys.stderr)
