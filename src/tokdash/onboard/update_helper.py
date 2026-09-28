#!/usr/bin/env python3
"""Independent updater helper — staged OUTSIDE the package tree before it runs.

The dashboard updater cannot be a thread or ordinary child of the server: the upgrade
replaces the very package this process tree serves (mixed-version hazard, feasibility
§8), and inside systemd ``setsid``/double-fork do NOT escape the service's cgroup, so a
child dies with the stopped service. The control plane therefore copies THIS file
verbatim into ``<data_dir>/update-staging/`` and launches it as a separate transient
systemd user unit (``systemd-run --user``), which the user manager keeps alive across
the tokdash service's stop/start.

Because it runs against a package tree that is about to be replaced, it must not import
anything from ``tokdash``. The journal format it updates is defined in
:mod:`tokdash.onboard.update_jobs`; the compact reader/writer below mirrors it.

Operation ordering is the feasibility plan's §6: verify ownership again → stop the
managed server → wait for the port to release → consistent usage-DB backup (writers
provably stopped) → exact-version install with validated structured argv (no shell) →
start → bounded application-readiness probe → atomically record the result. Failures
leave the package untouched when possible, attempt service recovery, and never claim
success. Every step is individually bounded, so the journal's staleness reconciliation
can never wait forever on a wedged helper.

Usage: ``python update_helper.py <data_dir> <job_id>``
"""
from __future__ import annotations

import fcntl
import json
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TERMINAL = {"succeeded", "failed"}
ACCEPT_PHASES = {"accepted", "staged"}
INSTALL_TIMEOUT = 600
SERVICE_OP_TIMEOUT = 60
READY_TIMEOUT = 90
HEARTBEAT_SECONDS = 10
LOCK_ACQUIRE_TIMEOUT = 60

_TARGET_RE = re.compile(r"\d+(\.\d+)+(?:[a-zA-Z0-9_.+-]+)?")


def safe_install_argv(argv):
    """Mirror of update_mechanics.safe_install_argv (the staged copy cannot import it):
    plain strings, execve-style, ``pip install`` semantics, one ``tokdash==<version>``."""
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
        return False
    if argv[0] != "pipx" and (len(argv) < 4 or argv[1] != "-m" or argv[2] != "pip" or argv[3] != "install"):
        return False
    specs = [a for a in argv[4:] if a.startswith("tokdash")]
    return len(specs) == 1 and specs[0].startswith("tokdash==") and bool(
        _TARGET_RE.fullmatch(specs[0].split("==", 1)[1])
    )


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Journal:
    """Compact, format-compatible client for ``<data_dir>/update_jobs.json``.

    ``lock_free`` selects the locking mode: unset (the default), each mutation takes and
    releases the ``update.lock`` flock itself; the helper's mutations run ``lock_free``
    because :func:`main` holds ONE exclusive flock on that file for the whole run (so the
    CLI ``tokdash update`` can never overlap a live apply) and mutations must not
    re-lock a file the same process already holds.
    """

    def __init__(self, data_dir, lock_free=False):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "update_jobs.json"
        self.lock_path = self.data_dir / "update.lock"
        self.lock_free = lock_free
        self._handle = None

    def acquire(self, timeout=LOCK_ACQUIRE_TIMEOUT):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+", encoding="utf-8")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._handle = handle
                return
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise RuntimeError("another updater is running (update lock is held)")
                time.sleep(0.5)

    def release(self):
        if self._handle is not None:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self._handle.close()
            self._handle = None

    def _locked(self):
        handle = self.lock_path.open("a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    def read(self, job_id):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data.get("jobs", {}).get(job_id)
        except Exception:
            return None

    def update(self, job_id, **fields):
        handle = None if self.lock_free else self._locked()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            job = data.get("jobs", {}).get(job_id)
            if not job:
                return None
            job.update(fields)
            job["updated_at"] = _now_iso()
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self.path)
            return job
        finally:
            if handle is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()


class Runner:
    """Everything the helper does, with the test seams on one object."""

    def __init__(self, data_dir, job_id, log):
        self.data_dir = Path(data_dir)
        self.job_id = job_id
        self.journal = Journal(data_dir, lock_free=True)
        self.log = log
        self.service_stopped = False
        self._hb = None

    # -- plumbing ---------------------------------------------------------------
    def run(self, args, timeout=60):
        self.log(f"exec: {args}")
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

    def systemctl(self, *args, timeout=SERVICE_OP_TIMEOUT):
        proc = self.run(["systemctl", "--user", *args], timeout=timeout)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[-300:]
            raise RuntimeError(f"systemctl --user {' '.join(args)} failed: {detail}")
        return proc

    def http_json(self, url, timeout=5):
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            return json.loads(resp.read().decode("utf-8"))

    def port_open(self, host, port):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            try:
                sock.connect((host, port))
                return True
            finally:
                sock.close()
        except OSError:
            return False

    def heartbeat_start(self):
        stop = threading.Event()

        def loop():
            while not stop.wait(HEARTBEAT_SECONDS):
                try:
                    self.journal.update(self.job_id)
                except Exception:
                    pass

        self._hb = stop
        thread = threading.Thread(target=loop, daemon=True)
        thread.start()

    def heartbeat_stop(self):
        if self._hb:
            self._hb.set()

    # -- phases -----------------------------------------------------------------
    def preflight(self, job):
        """Re-verify (against the helper's own eyes) everything the server checked."""
        if job.get("phase") not in ACCEPT_PHASES:
            raise RuntimeError(f"job already in phase {job.get('phase')!r}; refusing to run")
        if job.get("host") != socket.gethostname():
            raise RuntimeError("job belongs to a different host")
        if not safe_install_argv(job.get("install_argv")):
            raise RuntimeError("install command failed validation")
        man = json.loads((self.data_dir / "install.json").read_text(encoding="utf-8"))
        if not isinstance(man, dict):
            raise RuntimeError("manifest is not an object")
        marker = str((man.get("service") or {}).get("marker") or "")
        if not marker or marker != str(job.get("service_marker") or ""):
            raise RuntimeError("manifest service marker no longer matches the accepted job")
        python = str(man.get("python_path") or "")
        if not python or not Path(python).is_file():
            raise RuntimeError("recorded interpreter is gone")
        unit = str((man.get("service") or {}).get("unit") or "")
        try:
            unit_text = Path(unit).read_text(encoding="utf-8")
        except OSError:
            raise RuntimeError("service unit file is missing")
        if marker not in unit_text:
            raise RuntimeError("service unit lost setup's ownership marker")
        return man

    def stop_service(self, name, bind, port):
        self.systemctl("stop", name)
        self.service_stopped = True
        probe_host = "127.0.0.1" if bind in {"0.0.0.0", "::", ""} else bind
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if not self.port_open(probe_host, port):
                return
            time.sleep(0.5)
        raise RuntimeError(f"service port {port} did not release after stop")

    def backup_db(self, db_path, backups_dir, keep=3):
        if not db_path or not Path(db_path).is_file():
            self.log("no usage DB on disk; nothing to back up")
            return None
        # Shared-writer guard: refuse before replacement if another Tokdash process
        # still holds the usage-DB write lock (feasibility §7).
        lock_path = Path(str(db_path) + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+", encoding="utf-8")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError("another Tokdash process holds the usage-DB lock; not replacing the package")
            # Server is stopped (writers quiesced); SQLite's online backup still gives a
            # consistent snapshot including any leftover WAL.
            backups_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            dest = backups_dir / f"pre-update-{stamp}.sqlite3"
            src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            dst = sqlite3.connect(str(dest))
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            self.log(f"usage DB backed up to {dest}")
            for old in sorted(backups_dir.glob("pre-update-*.sqlite3"))[:-keep]:
                try:
                    old.unlink()
                except OSError:
                    pass
            return str(dest)
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            handle.close()

    def install(self, argv):
        proc = self.run(list(argv), timeout=INSTALL_TIMEOUT)
        if proc.returncode != 0:
            raise RuntimeError("package install command failed")

    def start_service(self, name, bind, port):
        self.systemctl("start", name)
        self.service_stopped = False

    def readiness(self, to_version, bind, port):
        probe_host = "127.0.0.1" if bind in {"0.0.0.0", "::", ""} else bind
        base = f"http://{probe_host}:{port}"
        deadline = time.monotonic() + READY_TIMEOUT
        last_error = "server never answered"
        while time.monotonic() < deadline:
            try:
                health = self.http_json(f"{base}/health")
                if health.get("service") != "tokdash":
                    last_error = "port answered but is not Tokdash"
                elif health.get("version") != to_version:
                    last_error = f"running version {health.get('version')!r} != target {to_version!r}"
                else:
                    version_info = self.http_json(f"{base}/api/version")
                    if version_info.get("runtime_version") != to_version:
                        last_error = "runtime_version mismatch"
                    else:
                        # Application-readiness beyond /health: exercise the usage DB.
                        self.http_json(f"{base}/api/tools", timeout=15)
                        return
            except Exception as exc:
                last_error = str(exc)
            time.sleep(2)
        raise RuntimeError(f"readiness check failed: {last_error}")

    def recover_service(self, name):
        try:
            self.systemctl("start", name)
            self.service_stopped = False
            self.log("service recovery: started")
        except Exception as exc:
            self.log(f"service recovery FAILED: {exc}")


def main(data_dir, job_id, *, runner=None, opener=None):
    """Run the full managed apply for one accepted job. Returns True on success."""
    journal = Journal(data_dir)
    job = journal.read(job_id)
    log_path = None
    if job and job.get("log_path"):
        try:
            log_path = Path(job["log_path"])
            log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            log_path = None

    def log(msg):
        line = f"{_now_iso()} {msg}"
        print(line, flush=True)
        if log_path:
            try:
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass

    r = runner or Runner(data_dir, job_id, log)
    if opener is not None:
        r.http_json = opener
    name = str((job or {}).get("service_name") or "tokdash")
    acquired = False
    try:
        try:
            journal.acquire()
            acquired = True
        except RuntimeError as exc:
            log(f"FAILED: {exc}")
            return False

        job = r.journal.read(job_id) or job
        if not job:
            log("FAILED: job record is gone")
            return False
        # Validate BEFORE touching the phase: preflight rejects anything not in
        # accepted/staged, so stamping preflight first would validate its own write.
        man = r.preflight(job)
        r.journal.update(job_id, phase="preflight")
        bind = str(man.get("bind") or "127.0.0.1")
        port = int(man.get("port") or 55423)
        name = str((man.get("service") or {}).get("name") or name)

        r.journal.update(job_id, phase="stopping")
        r.heartbeat_start()
        try:
            r.stop_service(name, bind, port)

            r.journal.update(job_id, phase="backing_up")
            backup = r.backup_db(job.get("usage_db"), Path(data_dir) / "backups")
            r.journal.update(job_id, backup_path=backup)

            r.journal.update(job_id, phase="installing")
            try:
                r.install(job["install_argv"])
            except Exception as exc:
                # Package replacement failed (possibly partially): the environment can
                # NOT be assumed intact. Attempt recovery, report honestly.
                r.recover_service(name)
                r.journal.update(
                    job_id, phase="failed", failed_phase="install",
                    message=(
                        f"Upgrade to v{job.get('to_version')} failed during install ({exc}); "
                        "attempted to restart the service. If it stays down, repair from a terminal "
                        "with `tokdash update` (a usage-DB backup is on this host)."
                    ),
                )
                log("FAILED during install")
                return False

            r.journal.update(job_id, phase="starting")
            try:
                r.start_service(name, bind, port)
                r.journal.update(job_id, phase="ready")
                r.readiness(str(job["to_version"]), bind, port)
            except Exception as exc:
                r.journal.update(
                    job_id, phase="failed", failed_phase="starting",
                    message=(
                        f"Installed v{job.get('to_version')} but the service did not come up ready "
                        f"({exc}). The usage-DB backup is kept; if the new version is broken, restore "
                        "it and the matching older package from a terminal."
                    ),
                )
                log("FAILED during start/readiness")
                return False

            r.journal.update(
                job_id, phase="succeeded", failed_phase=None,
                result_version=str(job["to_version"]), message=None,
            )
            log(f"SUCCESS: v{job['to_version']} ready")
            return True
        finally:
            r.heartbeat_stop()
    except Exception as exc:
        log(f"FAILED: {exc}")
        try:
            # Failed before/around replacement with the old package intact: restore
            # availability whenever we had taken the service down.
            if r.service_stopped:
                r.recover_service(name)
        finally:
            try:
                # Never overwrite a terminal job (a second runner racing an already
                # finished/succeeded job loses, and losing must not rewrite history).
                current = r.journal.read(job_id) or {}
                if current.get("phase") not in TERMINAL:
                    r.journal.update(
                        job_id, phase="failed", failed_phase="preflight",
                        message=(
                            f"Update aborted before replacing the package: {exc}. Nothing was changed "
                            "beyond the service restart; run `tokdash update` from a terminal."
                        ),
                    )
            except Exception:
                pass
        return False
    finally:
        if acquired:
            journal.release()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: update_helper.py <data_dir> <job_id>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(0 if main(sys.argv[1], sys.argv[2]) else 1)
