"""Pure planners: turn (options + detection/manifest) into a concrete action list.

These never touch the system — they compute what *would* change. ``apply``/``revert``
in :mod:`.engine` execute the result, and ``--dry-run`` prints it. Keeping planning pure
is what makes the whole engine unit-testable without a real systemd/venv (plan §6.1).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import detect, launchd, manifest, paths, runtime, service_base, systemd, tailscale, winsched

DEFAULT_PORT = 55423
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@dataclass
class Options:
    """Normalized lifecycle options parsed from the CLI namespace."""

    action: str = "setup"
    auto: bool = False
    yes: bool = False
    json: bool = False
    dry_run: bool = False
    bind: str = "127.0.0.1"
    port: Optional[int] = None
    runtime: str = "auto"
    service: str = "auto"
    no_service: bool = False
    purge: bool = False
    keep_runtime: bool = False
    force: bool = False


def _is_loopback(bind: str) -> bool:
    b = (bind or "").strip().lower()
    return b in LOOPBACK or b.startswith("127.")


# --- setup ----------------------------------------------------------------------


def build_setup_plan(opts: Options, detection: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the full setup plan (no side effects)."""
    blockers: List[str] = []
    warnings: List[str] = []
    notes: List[str] = []
    changes: List[str] = []

    bind = (opts.bind or "127.0.0.1").strip()
    loopback = _is_loopback(bind)

    # --auto stays strictly local-only: it must never configure a non-loopback exposure.
    if opts.auto and not loopback:
        blockers.append(
            f"--auto refuses to bind {bind} (non-loopback). Re-run interactively to confirm remote exposure."
        )
    elif not loopback:
        warnings.append(
            f"Binding {bind} exposes an UNAUTHENTICATED API. Write endpoints stay disabled unless "
            "bound to loopback; prefer `tailscale serve` or `ssh -L` for remote access."
        )

    # Runtime.
    rt = runtime.resolve(opts.runtime, detection)
    if rt["error"]:
        blockers.append(rt["error"])
    if rt.get("needs_create"):
        changes.append(f"create managed venv at {paths.managed_venv_dir()} and install Tokdash")
    elif rt.get("kind") == "existing" and not rt.get("owned_by_setup"):
        # The service will be pinned to the launching interpreter. Warn when that looks
        # like a churn-prone env (conda/virtualenv), where `--runtime venv` is safer.
        py = (rt.get("python") or "").lower()
        if any(s in py for s in ("conda", "miniforge", "mamba", "/envs/", "virtualenv")) or "/.venv/" in py:
            notes.append(
                f"The service will use the current interpreter ({rt.get('python')}). If that is a "
                "throwaway/conda env, prefer `--runtime venv` so the service survives env changes."
            )

    # Service type (resolved first so the port check knows whether a service will bind).
    service = _resolve_service(opts, detection, blockers, notes)
    existing_systemd_unit = (
        detection["existing_service"].get("systemd_unit")
        if service["type"] == "systemd-user"
        else None
    )
    force_replacing_unmarked_systemd = bool(
        existing_systemd_unit
        and opts.force
        and not systemd.unit_is_managed(Path(existing_systemd_unit))
    )

    # Port.
    requested_port = opts.port or DEFAULT_PORT
    port = requested_port
    port_info = detection.get("port") or {}
    if port_info.get("open"):
        if port_info.get("is_tokdash"):
            notes.append(f"Port {requested_port} already serves Tokdash; setup will point the service at it.")
        elif force_replacing_unmarked_systemd:
            notes.append(
                f"Port {requested_port} is busy, but --force will replace the existing unmarked "
                f"tokdash.service at {existing_systemd_unit} and restart it."
            )
        elif opts.auto:
            free = detect.find_free_port(requested_port + 1)
            if free is None:
                blockers.append(f"port {requested_port} is busy and no free port was found nearby.")
            else:
                port = free
                notes.append(f"Port {requested_port} is busy; auto-picked {free}.")
        elif service["type"] != "none":
            # A service we're about to enable+start would crash-loop on a taken port, and
            # `systemctl enable --now` often still returns 0 — so refuse rather than write a
            # unit that can't bind and mis-report success.
            blockers.append(
                f"Port {requested_port} is busy (not Tokdash); re-run with --port <free> or --auto to auto-pick."
            )
        else:
            warnings.append(f"Port {requested_port} is busy (not Tokdash); `tokdash serve` will need a free --port.")

    data_dir = paths.data_dir()
    env_data_dir = None if paths.is_default_data_dir() else str(data_dir)

    unit_text = None
    marker_id = None
    service_block: Optional[Dict[str, Any]] = None
    if service["type"] == "systemd-user" and rt["command"]:
        # Never silently clobber a unit setup did not create. Overwriting our own
        # (marked) unit is fine and keeps re-running setup idempotent; an unmarked,
        # hand-installed tokdash.service requires --force.
        existing_unit = existing_systemd_unit
        if existing_unit and not systemd.unit_is_managed(Path(existing_unit)) and not opts.force:
            blockers.append(
                f"{existing_unit} already exists and was not created by tokdash setup; refusing to "
                "overwrite it. Re-run with --force to replace it, or remove it first."
            )
        marker_id = manifest.new_marker_id()
        unit_text = systemd.render_unit(
            rt["command"], bind, port, marker_id=marker_id, env_data_dir=env_data_dir
        )
        unit_path = paths.systemd_unit_path()
        changes.append(f"write {unit_path}")
        changes.append("enable + start the systemd user service")
        service_block = {
            "type": "systemd-user",
            "unit": str(unit_path),
            "name": systemd.SERVICE_NAME,
            "created_by_setup": True,
            "marker": manifest.marker_token(marker_id),
        }
    elif service["type"] == "launchd" and rt["command"]:
        # Same overwrite guard as systemd: refuse an unmarked, hand-installed plist.
        existing_plist = detection["existing_service"].get("launchd_plist")
        if existing_plist and not launchd.plist_is_managed(Path(existing_plist)) and not opts.force:
            blockers.append(
                f"{existing_plist} already exists and was not created by tokdash setup; refusing to "
                "overwrite it. Re-run with --force to replace it, or remove it first."
            )
        marker_id = manifest.new_marker_id()
        unit_text = launchd.render_plist(
            rt["command"], bind, port, marker_id=marker_id, env_data_dir=env_data_dir
        )
        plist_path = paths.launchd_plist_path()
        changes.append(f"write {plist_path}")
        changes.append("load + start the launchd user agent")
        service_block = {
            "type": "launchd",
            "unit": str(plist_path),
            "name": launchd.LABEL,
            "created_by_setup": True,
            "marker": manifest.marker_token(marker_id),
        }
    elif service["type"] == "winsched" and rt["command"]:
        # Same overwrite guard as systemd/launchd: refuse an unmarked, hand-installed task.
        existing_task = detection["existing_service"].get("winsched_task")
        if existing_task and not winsched.task_is_managed(Path(existing_task)) and not opts.force:
            blockers.append(
                f"{existing_task} already exists and was not created by tokdash setup; refusing to "
                "overwrite it. Re-run with --force to replace it, or remove it first."
            )
        marker_id = manifest.new_marker_id()
        unit_text = winsched.render_task(
            rt["command"], bind, port, marker_id=marker_id, env_data_dir=env_data_dir
        )
        task_path = paths.winsched_task_path()
        changes.append(f"write {task_path}")
        changes.append("register + start the Windows Task Scheduler task")
        service_block = {
            "type": "winsched",
            "unit": str(task_path),
            "name": winsched.TASK_NAME,
            "created_by_setup": True,
            "marker": manifest.marker_token(marker_id),
        }
    elif service["type"] == "none":
        notes.append("No background service will be created; start Tokdash with `tokdash serve` (see the URL above for the bind/port).")

    # Remote access is an explicit, opt-in choice; print the info in every mode (the
    # interactive wizard additionally offers to run Tailscale Serve). Writes stay disabled
    # while proxied because the forwarded Host is not in the loopback allowlist.
    if service["type"] != "none":
        ts_hint = (
            f"`{' '.join(tailscale.serve_command(port))}`"
            if detection.get("tailscale")
            else f"install Tailscale, then `{' '.join(tailscale.serve_command(port))}`"
        )
        notes.append(
            f"Remote access (optional, explicit): {ts_hint}, or `ssh -L {port}:127.0.0.1:{port} <host>`. "
            "Loopback writes stay protected; proxied writes are disabled by design."
        )

    changes.append(f"write manifest {paths.manifest_path()}")

    url_host = "localhost" if bind in {"0.0.0.0", "::"} else bind
    # When blocked, the actions won't run — present them as blocked_changes, not changes,
    # so a bundler reading the JSON never confuses "would do" with "did/will do".
    blocked = bool(blockers)
    return {
        "ok": not blocked,
        "action": "setup",
        "os": detection["os"],
        "interactive": not opts.auto,
        "bind": bind,
        "port": port,
        "url": f"http://{url_host}:{port}",
        "runtime": rt,
        "service": service,
        "service_block": service_block,
        "marker_id": marker_id,
        "unit_text": unit_text,
        "env_data_dir": env_data_dir,
        "data_dir": str(data_dir),
        "manifest_path": str(paths.manifest_path()),
        "changes": [] if blocked else changes,
        "blocked_changes": changes if blocked else [],
        "warnings": warnings,
        "notes": notes,
        "blockers": blockers,
    }


def _resolve_service(opts: Options, detection: Dict[str, Any], blockers: List[str], notes: List[str]) -> Dict[str, Any]:
    """Decide the service backend; delegates to :func:`service_base.select_service`.

    Kept as a thin adapter (extract primitives from ``opts``/``detection``, call the
    centralized selection factory, thread its blockers/notes back into the caller's
    accumulating lists) so this function's name/signature — and therefore its single
    caller in :func:`build_setup_plan` plus any external import of it — stays unchanged.
    The actual branch logic and every message string now live in
    :mod:`.service_base` (moved verbatim; see that module's docstring).
    """
    sel = service_base.select_service(
        opts.service,
        detection["os"],
        no_service=opts.no_service,
        systemd_available=bool(detection.get("systemd_user")),
        launchd_available=bool(detection.get("launchd")),
        winsched_available=bool(detection.get("winsched")),
    )
    blockers.extend(sel.blockers)
    notes.extend(sel.notes)
    return sel.result


# --- uninstall ------------------------------------------------------------------


def _plan_service_removal(
    steps, removed, blockers, opts, *, unit_path, service_type, marker_id, name,
    have_manifest, created_by_setup, block_unmarked,
):
    """Append a service-removal step iff the on-disk unit is provably setup-owned.

    Shared by the manifest-recorded-unit path and the on-disk marker scan, so both apply
    the identical ownership-marker gate (§12.3).
    """
    file_exists = unit_path.is_file()
    if service_type == "launchd":
        is_ours = launchd.plist_is_managed(unit_path, marker_id) if file_exists else False
    elif service_type == "winsched":
        is_ours = winsched.task_is_managed(unit_path, marker_id) if file_exists else False
    else:
        is_ours = systemd.unit_is_managed(unit_path, marker_id) if file_exists else False
    step = {"kind": "service", "unit": str(unit_path), "name": name, "service_type": service_type}

    if not file_exists:
        # Manifest records a setup unit but the file is gone: still stop/disable any loaded
        # service (the apply step's unlink is a no-op when absent).
        if have_manifest and created_by_setup:
            steps.append(step)
            removed.append(f"stop + disable service ({unit_path}; unit file already gone)")
        return
    if is_ours:
        steps.append(step)
        removed.append(f"stop + disable + remove service ({unit_path})")
    elif opts.force:
        steps.append(step)
        removed.append(f"stop + disable + remove (adopted) service ({unit_path})")
    elif block_unmarked:
        why = (
            "is no longer the unit tokdash setup created (marker missing/changed)"
            if have_manifest
            else "is not marked as setup-created"
        )
        blockers.append(
            f"{unit_path} {why}; refusing to remove it. "
            "Re-run with --force to remove it anyway, or remove it manually."
        )
    # else: present but not ours and not blocking -> leave the user's own unit untouched


def build_uninstall_plan(opts: Options, detection: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the revert plan from the manifest (or a conservative fallback)."""
    man = detection.get("manifest")
    removed: List[str] = []
    kept: List[str] = []
    notes: List[str] = []
    blockers: List[str] = []
    steps: List[Dict[str, Any]] = []

    have_manifest = man is not None

    # 1. Tailscale teardown — only if setup configured it (Phase 1 never does).
    ts = (man or {}).get("tailscale_serve") or {}
    if ts.get("configured_by_setup") and ts.get("teardown_command"):
        steps.append({"kind": "tailscale", "command": ts["teardown_command"]})
        removed.append(f"revert Tailscale Serve: {' '.join(ts['teardown_command'])}")

    # 2/3. Service: stop + disable + remove the unit setup wrote. The on-disk file must
    # STILL carry our ownership marker before we delete it — even with a manifest (a user
    # can replace a setup-created unit with a hand-written one at the same path).
    service = (man or {}).get("service") or {}
    manifest_unit = service.get("unit") if have_manifest else None
    manifest_type = service.get("type") if have_manifest else None
    marker_id = _marker_id_from(service.get("marker"))

    if manifest_unit:
        _plan_service_removal(
            steps, removed, blockers, opts,
            unit_path=Path(manifest_unit), service_type=manifest_type, marker_id=marker_id,
            name=service.get("name", _default_service_name(manifest_type)),
            have_manifest=have_manifest, created_by_setup=service.get("created_by_setup"),
            block_unmarked=True,  # a recorded-but-replaced unit must warn, not be deleted
        )
    else:
        # No service unit recorded in the manifest: either the manifest is ABSENT
        # (conservative fallback) OR it records service:None while a setup-owned, MARKED unit
        # is still on disk (e.g. `setup --service systemd` then `setup --no-service`). Scan
        # disk and remove only marker-carrying (provably ours) units, so a single uninstall
        # truly removes the service it owns instead of leaving it running and lying.
        es = detection["existing_service"]
        for stype, ustr in (
            ("systemd-user", es.get("systemd_unit")),
            ("launchd", es.get("launchd_plist")),
            ("winsched", es.get("winsched_task")),
        ):
            if not ustr:
                continue
            _plan_service_removal(
                steps, removed, blockers, opts,
                unit_path=Path(ustr), service_type=stype, marker_id=None,
                name=_default_service_name(stype),
                have_manifest=have_manifest, created_by_setup=False,
                # Only the truly-unknown (no-manifest) case blocks an unmarked unit. When the
                # manifest says we created no service, an unmarked unit is provably NOT ours —
                # leave the user's own unit alone rather than blocking uninstall on it.
                block_unmarked=not have_manifest,
            )

    # 4. Runtime: remove only when setup owns it AND the .tokdash-managed marker is
    #    present. The marker is what makes this safe even when the manifest is gone —
    #    e.g. a `--runtime venv` setup that crashed after building the venv but before
    #    writing install.json (§12.2/§12.3 partial-safe guarantee).
    owned = bool((man or {}).get("runtime_owned_by_setup"))
    if opts.keep_runtime:
        if owned or (not have_manifest and detect.managed_runtime_present()):
            kept.append(f"managed runtime ({paths.runtime_dir()}) (--keep-runtime)")
    elif owned:
        if detect.managed_runtime_present():
            steps.append({"kind": "runtime", "path": str(paths.runtime_dir())})
            removed.append(f"remove setup-owned runtime ({paths.runtime_dir()})")
        else:
            notes.append("setup-owned runtime not found (already removed); skipping.")
    elif not have_manifest and detect.managed_runtime_present():
        steps.append({"kind": "runtime", "path": str(paths.runtime_dir())})
        removed.append(
            f"remove marked setup-owned runtime ({paths.runtime_dir()}) "
            "[manifest missing; .tokdash-managed present]"
        )
    elif (man or {}).get("runtime_kind") in {"existing", "pipx"}:
        kept.append("the Tokdash runtime you installed yourself (not setup-owned)")

    # 5. Data: kept unless --purge.
    if opts.purge:
        steps.append({"kind": "data"})
        removed.append(f"DELETE usage history + config under {paths.data_dir()}")
    else:
        kept.append(f"usage history + config under {paths.data_dir()} (use --purge to delete)")

    # 6. Manifest removed last.
    if have_manifest:
        steps.append({"kind": "manifest"})
        removed.append(f"remove manifest ({paths.manifest_path()})")
    if not steps:
        notes.append("No manifest and nothing setup-owned was found; nothing to revert.")

    blocked = bool(blockers)
    return {
        "ok": not blocked,
        "action": "uninstall",
        "have_manifest": have_manifest,
        "data_dir": str(paths.data_dir()),
        "manifest_path": str(paths.manifest_path()),
        "purge": opts.purge,
        "keep_runtime": opts.keep_runtime,
        # When blocked, no steps run — surface the would-remove list as blocked_changes
        # rather than as `removed` so the JSON never implies the revert happened.
        "steps": [] if blocked else steps,
        "removed": [] if blocked else removed,
        "blocked_changes": removed if blocked else [],
        "kept": kept,
        "notes": notes,
        "blockers": blockers,
    }


def _marker_id_from(marker: Optional[str]) -> Optional[str]:
    if not marker:
        return None
    for token in marker.split():
        if token.startswith("id="):
            return token[3:]
    return None


def _default_service_name(service_type: Optional[str]) -> str:
    """Fallback service ``name`` when a (possibly hand-edited/legacy) manifest omits it."""
    if service_type == "launchd":
        return launchd.LABEL
    if service_type == "winsched":
        return winsched.TASK_NAME
    return "tokdash"
