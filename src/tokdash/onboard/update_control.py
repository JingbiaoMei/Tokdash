"""Server-side control plane for dashboard click-to-update (feasibility §6.1-6.3).

One entry per API concern — capability, start, status — each re-checking eligibility and
authorization state server-side (the UI's claims prove nothing). ``start_update``
durably admits exactly one job (attaching to a live one instead of double-applying),
stages the dependency-free helper OUTSIDE the package tree, and hands it to
``systemd-run --user`` so the apply survives the service it is about to stop. The
accepted-job response is written to the journal before launch; if the response never
reaches the browser, the durable job id/idempotency still prevents a second install.

Only Linux/systemd has a validated adapter today (feasibility §9): every other platform
reports capability as ineligible with the terminal command, never a partial attempt.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import manifest, paths, update_auth, update_eligibility, update_jobs, update_mechanics, updatecheck

SERVICE_OP_TIMEOUT = 30


def _helper_source_path() -> Path:
    """Locate the helper's source WITHOUT importing it.

    The helper imports Linux-only ``fcntl`` at run time; a Windows server must be able
    to answer ``capability``/``start`` with manual guidance, which means loading the
    control module there must never crash. A sibling-path read needs no interpreter.
    """
    return Path(__file__).with_name("update_helper.py")


def capability(current_version: str) -> Dict[str, Any]:
    """What the dashboard may do here: eligibility, target, remote config, live job."""
    man = manifest.read_manifest()
    elig = update_eligibility.check_eligibility(man)
    enabled = updatecheck.is_enabled()
    target = None
    if enabled:
        check = updatecheck.check(current_version)
        if check.get("update_available"):
            target = check.get("latest")
    job = update_jobs.latest_job()
    return {
        "eligible": bool(elig["eligible"]),
        "reason": elig["reason"],
        "method": elig["method"],
        "current": current_version,
        "target": target,
        "update_check_enabled": enabled,
        "remote_updates_enabled": update_auth.configured_origin() is not None,
        "manual_command": update_mechanics.MANUAL_COMMAND,
        "latest_job": update_jobs.public_view(job),
    }


def start_update(
    to_version: str, *, current_version: str, trigger: str = "dashboard"
) -> Tuple[int, Dict[str, Any]]:
    """Admit (or attach to) exactly one update job and launch the helper.

    Returns an ``(http_status, payload)`` pair: 202 + job when newly accepted, 200 +
    job when attaching to a live one, 4xx with a sanitized reason otherwise.
    """
    man = manifest.read_manifest()
    elig = update_eligibility.check_eligibility(man)
    if not elig["eligible"]:
        return 403, {"detail": elig["reason"] or "This installation is not eligible for dashboard updates.",
                     "manual_command": update_mechanics.MANUAL_COMMAND}

    latest = None
    if updatecheck.is_enabled():
        check = updatecheck.check(current_version)
        if check.get("update_available"):
            latest = check.get("latest")
    error = update_mechanics.validate_target(to_version, current=current_version, latest=latest)
    if error:
        return 400, {"detail": error}

    if shutil.which("systemd-run") is None:
        return 403, {"detail": "systemd-run is unavailable on this host.",
                     "manual_command": update_mechanics.MANUAL_COMMAND}

    method = elig["method"]
    python_path = str(man.get("python_path") or "")
    argv = update_mechanics.install_argv(method, python_path, str(to_version).strip())
    if not argv:
        return 403, {"detail": f"No in-place upgrade command for install method {method!r}.",
                     "manual_command": update_mechanics.MANUAL_COMMAND}

    service = man.get("service") or {}
    job, created = update_jobs.create_job(
        to_version=str(to_version).strip(),
        from_version=current_version,
        trigger=trigger,
        install_argv=argv,
        service_type=service.get("type"),
        service_name=service.get("name"),
        service_marker=service.get("marker"),
        python_path=python_path,
        usage_db=str(paths.usage_db_path()),
    )
    if not created:
        # Attach semantics: this click (or a retried one) belongs to a live job.
        return 200, {"job": update_jobs.public_view(job), "attached": True}

    job_id = job["id"]
    try:
        _stage_and_launch(job)
    except Exception as exc:
        update_jobs.set_phase(
            job_id, "failed", failed_phase="preflight",
            message=f"Could not start the updater: {exc}. Nothing was changed; run `{update_mechanics.MANUAL_COMMAND}` from a terminal.",
        )
        return 500, {"detail": f"Could not start the updater: {exc}",
                     "manual_command": update_mechanics.MANUAL_COMMAND}

    return 202, {"job": update_jobs.public_view(update_jobs.get_job(job_id, reconcile=False)), "attached": False}


def _stage_and_launch(job: Dict[str, Any]) -> None:
    job_id = job["id"]
    data_dir = paths.data_dir()
    staging = update_jobs.staging_dir()
    staging.mkdir(parents=True, exist_ok=True)
    # Sweep leftover staged scripts from terminal jobs (§6.9) before staging ours.
    for old in staging.glob("update-helper-*.py"):
        try:
            if old.stem.endswith(job_id):
                continue
            old.unlink()
        except OSError:
            pass
    source = _helper_source_path()
    if not source.is_file():
        raise RuntimeError("the updater helper script is missing from this installation")
    staged = staging / f"update-helper-{job_id}.py"
    shutil.copyfile(source, staged)

    env_args = []
    for var in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
        value = os.environ.get(var)
        if value:
            env_args.append(f"--setenv={var}={value}")
    unit = f"tokdash-update-{job_id}"
    cmd = [
        "systemd-run", "--user",
        f"--unit={unit}",
        "--collect",
        "--quiet",
        *env_args,
        job["python_path"], str(staged), str(data_dir), job_id,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=SERVICE_OP_TIMEOUT)
    except Exception as exc:
        raise RuntimeError(f"systemd-run could not be launched: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-300:]
        raise RuntimeError(f"systemd-run failed: {detail}")
