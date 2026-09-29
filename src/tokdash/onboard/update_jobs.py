"""Durable update-job journal shared by the dashboard updater and the CLI (feasibility §6).

The upgrade crosses a planned service outage, so its progress cannot live in a request
handler's memory: the record lives in ``<data_dir>/update_jobs.json`` behind the same
``<data_dir>/update.lock`` flock the CLI and the staged helper use, and the helper
refreshes ``updated_at`` as its heartbeat. That yields the three protocol guarantees the
UI depends on:

- **Attach, don't double-apply.** Repeated clicks / retried requests find the in-flight
  job and return its id; a second apply never starts.
- **Result survives the outage.** The new server exposes the journal through status
  reads; a reopened page recovers the most recent job after authorization.
- **No endless "Updating".** Jobs whose heartbeat goes stale (helper died, host
  rebooted) are reconciled to ``failed/interrupted`` at boot and on read, with bounded
  staleness instead of a forever-pending state.

Only sanitized views leave this module over the API: records keep local paths out of
:func:`public_view` so the journal can never leak private paths or installer output.
"""
from __future__ import annotations

import contextlib
import json
import os
import secrets
import socket
import tempfile
import threading
from contextlib import contextmanager

try:
    import fcntl
except ImportError:  # non-POSIX: reconcile stays lock-free (best effort)
    fcntl = None
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import paths, manifest
from ..filelock import process_lock

SCHEMA = 1

# Ordered phases of the managed apply. ``succeeded``/``failed`` are terminal.
PHASES = ("accepted", "preflight", "stopping", "backing_up", "installing", "starting", "ready", "succeeded", "failed")
TERMINAL_PHASES = {"succeeded", "failed"}

# A job whose heartbeat is older than this is not alive anymore (the helper refreshes
# every few seconds while it works, and its outer deadline is well inside this).
STALE_AFTER_SECONDS = 180
# Keep the journal bounded; the browser only ever cares about the newest job.
MAX_JOBS_KEPT = 20


def journal_path() -> Path:
    return paths.data_dir() / "update_jobs.json"


def update_lock_path() -> Path:
    """The single lock shared by the CLI update, the dashboard control, and the helper."""
    return paths.data_dir() / "update.lock"


def staging_dir() -> Path:
    """Staging lives OUTSIDE the package tree being replaced (feasibility §6.5)."""
    return paths.data_dir() / "update-staging"


def backups_dir() -> Path:
    return paths.data_dir() / "backups"


def log_dir() -> Path:
    return paths.data_dir() / "update-jobs"


def _now() -> str:
    return manifest.utc_now_iso()


def _age_seconds(iso: Optional[str]) -> float:
    if not iso:
        return float("inf")
    try:
        then = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    return (datetime.now(timezone.utc) - then).total_seconds()


def _load() -> Dict[str, Any]:
    try:
        data = json.loads(journal_path().read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("jobs"), dict):
            return data
    except Exception:
        pass
    return {"schema": SCHEMA, "latest": None, "jobs": {}}


def _store(data: Dict[str, Any]) -> None:
    p = journal_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp name: two writers must never share one scratch file.
    fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp_name, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


# Thread-local: a second thread in the same process (e.g. two concurrent API writes on
# the server's threadpool) must still queue on the real flock — only re-entry on the
# HOLDING thread may skip it. A cross-thread counter would wrongly vouch for that.
_tls = threading.local()


def _held_here() -> int:
    return getattr(_tls, "depth", 0)


@contextmanager
def with_update_lock():
    """Take the shared update lock; reentrant on the holding thread.

    ``flock`` is not reentrant and every nesting level would open a fresh file
    description — so the CLI, which holds this lock across the whole update while the
    journal functions inside it take it again, would deadlock against itself. The
    per-thread depth keeps that nesting honest; cross-process (and cross-thread)
    exclusion still gates every other entry.
    """
    if _held_here():
        _tls.depth += 1
        try:
            yield
        finally:
            _tls.depth -= 1
        return
    with process_lock(update_lock_path()):
        _tls.depth = 1
        try:
            yield
        finally:
            _tls.depth = 0


def is_stale(job: Optional[Dict[str, Any]]) -> bool:
    if not job or job.get("phase") in TERMINAL_PHASES:
        return False
    return _age_seconds(job.get("updated_at")) > STALE_AFTER_SECONDS


def create_job(
    *,
    to_version: str,
    from_version: Optional[str],
    trigger: str,
    install_argv: list,
    service_type: Optional[str],
    service_name: Optional[str],
    service_marker: Optional[str],
    python_path: str,
    usage_db: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Atomically admit one job. Returns ``(job, created)``.

    ``created`` is False (with the existing job) when an in-flight job still has a live
    heartbeat — that is the attach path for repeated clicks, retried requests, and CLI
    overlap. A stale in-flight job is reconciled to ``failed/interrupted`` first, so a
    dead helper can never wedge the queue.

    The live job is first spotted with a lock-free read: its runner holds the update
    lock for the whole apply, and an attach must answer now, not when that run ends.
    """
    live = latest_job()
    if live is not None and live.get("phase") not in TERMINAL_PHASES and not is_stale(live):
        return live, False
    with with_update_lock():
        data = _load()
        latest = data["jobs"].get(data.get("latest") or "")
        if latest is not None and latest.get("phase") not in TERMINAL_PHASES:
            if not is_stale(latest):
                return latest, False
            _reconcile_one(data, latest, failed_phase="interrupted",
                           message="A previous updater stopped reporting; it was not resumed.")
        job_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)
        job = {
            "id": job_id,
            "host": socket.gethostname(),
            "trigger": trigger,
            "from_version": from_version,
            "to_version": to_version,
            "phase": "accepted",
            "failed_phase": None,
            "message": None,
            "result_version": None,
            "created_at": _now(),
            "updated_at": _now(),
            # Internal fields — never surfaced through public_view():
            "install_argv": list(install_argv),
            "service_type": service_type,
            "service_name": service_name,
            "service_marker": service_marker,
            "python_path": python_path,
            "usage_db": usage_db,
            "backup_path": None,
            "log_path": str(log_dir() / f"{job_id}.log"),
        }
        data["jobs"][job_id] = job
        data["latest"] = job_id
        _prune(data)
        _store(data)
        return job, True


def set_phase(job_id: str, phase: str, **fields: Any) -> Optional[Dict[str, Any]]:
    """Record a phase transition (and optional extra fields) under the lock."""
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}")
    with with_update_lock():
        data = _load()
        job = data["jobs"].get(job_id)
        if not job:
            return None
        job["phase"] = phase
        job["updated_at"] = _now()
        for key, value in fields.items():
            if key in job:
                job[key] = value
        _store(data)
        return job


def heartbeat(job_id: str) -> None:
    """Refresh the liveness timestamp without changing the phase."""
    with with_update_lock():
        data = _load()
        job = data["jobs"].get(job_id)
        if job:
            job["updated_at"] = _now()
            _store(data)


def touch_heartbeat(job_id: str) -> None:
    """Refresh the liveness stamp WITHOUT taking the lock.

    For the update's HOLDING process only, from a thread the lock-holder is parked
    behind: the CLI's main thread sits in the installer holding update.lock while its
    heartbeat thread ticks. That thread must neither wait on the lock (it would never
    tick) nor race another writer (there is none: the holder is parked). Never call
    this from a process that does not hold update.lock — the helper has its own
    lock_free journal client for exactly this pattern.
    """
    data = _load()
    job = data["jobs"].get(job_id)
    if job is not None:
        job["updated_at"] = _now()
        _store(data)


@contextmanager
def _try_reconcile_lock():
    """Try (never wait) to hold the update lock for a reconcile write.

    A caller who cannot grab the lock has the answer already: the lock's holder is a
    LIVE updater (CLI or helper), so its "stale" heartbeat proves nothing and the job
    is not interrupted. Waiting here would put a blocking flock inside async request
    handlers — the server's event loop must never wait on an update in flight.
    """
    if _held_here() or fcntl is None:
        yield True
        return
    fd = None
    got = False
    try:
        path = update_lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        got = True
    except OSError:
        got = False
    try:
        yield got
    finally:
        if got and fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


def _reconcile_stale(job_id: str, job: Dict[str, Any]) -> Dict[str, Any]:
    """Re-check a stale job under a non-blocking lock; return the job to show."""
    with _try_reconcile_lock() as got:
        if not got:
            return job  # a live updater holds the lock; this job is not interrupted
        data = _load()
        fresh = data["jobs"].get(job_id)
        if fresh and is_stale(fresh):
            _reconcile_one(data, fresh, failed_phase="interrupted",
                           message="The updater stopped reporting; it was not resumed.")
            _store(data)
        return data["jobs"].get(job_id) or job


def get_job(job_id: str, *, reconcile: bool = True) -> Optional[Dict[str, Any]]:
    """Read one job without taking the update lock.

    The helper holds that lock for the whole apply, and status polling must answer
    while an update runs; ``_store`` is an atomic replace, so a plain read is always a
    complete record. Only a stale job needs a write, and that attempt is non-blocking
    (see :func:`_try_reconcile_lock`).
    """
    job = _load()["jobs"].get(job_id)
    if job and reconcile and is_stale(job):
        return _reconcile_stale(job_id, job)
    return job


def latest_job(*, reconcile: bool = True) -> Optional[Dict[str, Any]]:
    """Read the newest job; lock-free unless a stale job needs reconciling (see get_job)."""
    data = _load()
    latest_id = data.get("latest") or ""
    job = data["jobs"].get(latest_id)
    if job and reconcile and is_stale(job):
        return _reconcile_stale(latest_id, job)
    return job


def reconcile_boot() -> Optional[Dict[str, Any]]:
    """Boot-time reconciliation of interrupted jobs (feasibility §6.8).

    Called when a fresh server process starts. A job still in flight whose heartbeat is
    stale can never complete (its helper died with the outage, or the host rebooted);
    mark it failed. A job with a fresh heartbeat belongs to a live helper (for example a
    CLI update running while this server started) and is left alone.
    """
    return latest_job(reconcile=True)


def _reconcile_one(data: Dict[str, Any], job: Dict[str, Any], *, failed_phase: str, message: str) -> None:
    job["phase"] = "failed"
    job["failed_phase"] = failed_phase
    job["message"] = message
    job["updated_at"] = _now()


def _prune(data: Dict[str, Any]) -> None:
    keep = sorted(data["jobs"].values(), key=lambda j: str(j.get("created_at") or ""))[-MAX_JOBS_KEPT:]
    data["jobs"] = {j["id"]: j for j in keep}


def public_view(job: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Sanitized projection for the browser: phases and verdicts, never local paths."""
    if not job:
        return None
    return {
        "id": job.get("id"),
        "host": job.get("host"),
        "trigger": job.get("trigger"),
        "from_version": job.get("from_version"),
        "to_version": job.get("to_version"),
        "phase": job.get("phase"),
        "failed_phase": job.get("failed_phase"),
        "message": job.get("message"),
        "result_version": job.get("result_version"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "terminal": job.get("phase") in TERMINAL_PHASES,
    }
