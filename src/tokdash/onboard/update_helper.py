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

import json
import os
import re
import shlex
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # the helper only RUNS on Linux/systemd; importing it must not crash
    fcntl = None

TERMINAL = {"succeeded", "failed"}
ACCEPT_PHASES = {"accepted", "staged"}
INSTALL_TIMEOUT = 600
SERVICE_OP_TIMEOUT = 60
READY_TIMEOUT = 90
HEARTBEAT_SECONDS = 10
LOCK_ACQUIRE_TIMEOUT = 60

_TARGET_RE = re.compile(r"\d+(\.\d+)+(?:[a-zA-Z0-9_.+-]+)?")

# Snapshot names are `pre-update-%Y%m%d%H%M%S.sqlite3`; this marker (inside the
# backups dir, outside that glob so rotation never touches it) holds the stamp of
# the most recent update that passed readiness — see _prune_backups.
VALIDATED_MARKER = ".last-readiness-passed"
_STAMP_START = len("pre-update-")

# The journal is one read-modify-write file and the helper touches it from two threads
# (the heartbeat loop and the phase machine). flock only covers OTHER processes, so
# every in-process mutation serializes on this lock: an unsynchronized heartbeat could
# interleave with a phase write and resurrect a stale phase (e.g. overwrite a finished
# job back to "installing").
_WRITE_LOCK = threading.Lock()


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


def _looks_like_tokdash(argv):
    """Conservative argv test for "this process is a tokdash invocation".

    Covers every supported launch shape, including the one a venv console script
    produces under ``python -m``-style parents — ``[/venv/bin/python,
    /home/u/.local/bin/tokdash, serve]`` — the repo's ``main.py`` dev runner, and the
    bare ``tokdash`` script resolved from PATH. Over-matching is safe (it refuses an
    update); ``pip install tokdash==X`` deliberately does NOT match (our own child).
    """
    for i, a in enumerate(argv):
        if i == 0 and a == "tokdash":
            return True
        if a.endswith("/tokdash") and a.startswith("/"):
            return True  # absolute console-script path at any position
        if a == "-m" and i + 1 < len(argv) and argv[i + 1] == "tokdash":
            return True
        if a == "main.py" or a.endswith("/main.py"):
            return True
    return False


def _parse_execstart_line(line):
    """Parse one loaded ExecStart line into argv — mirror of update_eligibility's
    parser (the staged copy cannot import it). Handles both the modern structured
    ``{ path=… ; argv[]=… ; … }`` form (field separator ``;``, backslash escapes
    inside values) and the legacy bare command line."""
    line = line.strip()
    if not line:
        return None
    if line.startswith("{") and "argv[]=" in line:
        m = re.search(r"argv\[\]=(.*)$", line)
        if not m:
            return None
        rest = m.group(1)
        chars = []
        i = 0
        while i < len(rest):
            c = rest[i]
            if c == "\\" and i + 1 < len(rest):
                chars.append(rest[i + 1])
                i += 2
                continue
            if c in (";", "}"):
                break
            chars.append(c)
            i += 1
        raw = "".join(chars).strip()
    else:
        while line[:1] in ("+", "-", "!", "@", ":"):
            line = line[1:].lstrip()
        raw = line
    try:
        return shlex.split(raw) or None
    except ValueError:
        return None


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
        # One lock for the whole read-modify-write: the heartbeat thread and the phase
        # machine must never interleave (a lost update could undo a terminal phase).
        with _WRITE_LOCK:
            handle = None if self.lock_free else self._locked()
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                job = data.get("jobs", {}).get(job_id)
                if not job:
                    return None
                job.update(fields)
                job["updated_at"] = _now_iso()
                fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        fh.write(json.dumps(data, indent=2) + "\n")
                    os.replace(tmp, self.path)
                except BaseException:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
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
        self._hb_thread = None
        self._db_lock_handle = None

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
        self._hb_thread = threading.Thread(target=loop, daemon=True)
        self._hb_thread.start()

    def heartbeat_stop(self):
        """Stop the heartbeat AND join the thread.

        Stopping is not enough: a thread already inside its update would still land its
        write AFTER a terminal write and un-finish the job. Callers quiesce (this)
        BEFORE writing any terminal phase, so the last writer is always the phase
        machine. Idempotent.
        """
        thread = getattr(self, "_hb_thread", None)
        if thread is not None:
            self._hb_thread = None
            self._hb.set()
            thread.join(timeout=HEARTBEAT_SECONDS + 5)

    # -- phases -----------------------------------------------------------------
    def _loaded_execstart(self, unit_name):
        """First LOADED ExecStart argv for the unit (mirror of update_eligibility,
        including the structured ``{ path=… ; argv[]=… }`` modern systemd emits)."""
        proc = self.systemctl("show", unit_name, "-p", "ExecStart", "--value")
        for line in (proc.stdout or "").splitlines():
            argv = _parse_execstart_line(line)
            if argv:
                return argv
        return None

    @staticmethod
    def _check_loaded_argv(loaded, python, man):
        """Same proof update_eligibility._loaded_argv_mismatch gives, stdlib-only.
        normpath (not realpath) preserves virtualenv identity — venv bin/python is
        normally a symlink to a shared base interpreter."""
        if os.path.normpath(loaded[0]) != os.path.normpath(str(python)):
            raise RuntimeError("loaded service does not run the recorded interpreter")
        if loaded[1:3] != ["-m", "tokdash"] or "serve" not in loaded[3:]:
            raise RuntimeError("loaded service does not run the recorded Tokdash service")
        for flag, key in (("--bind", "bind"), ("--port", "port")):
            want = man.get(key)
            if want in (None, ""):
                continue
            want_s = str(want)
            try:
                i = loaded.index(flag)
            except ValueError:
                raise RuntimeError(f"loaded service does not pin {flag} as setup recorded")
            if i + 1 >= len(loaded) or loaded[i + 1] != want_s:
                raise RuntimeError(f"loaded service {flag} differs from setup's record")

    def _proc_snapshot(self):
        """Yield (pid, argv, environ) for every readable /proc entry (empty off-Linux)."""
        try:
            entries = os.listdir("/proc")
        except OSError:
            return
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                raw = Path(f"/proc/{entry}/cmdline").read_bytes()
                env_raw = Path(f"/proc/{entry}/environ").read_bytes()
            except OSError:
                continue
            argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
            if not argv:
                continue
            env = {}
            for pair in env_raw.split(b"\0"):
                chunk = pair.decode("utf-8", "replace")
                if "=" in chunk:
                    k, _, v = chunk.partition("=")
                    env[k] = v
            yield int(entry), argv, env

    def _sibling_tokdash_pids(self, exclude, usage_db=None):
        """PIDs of OTHER live processes that can write THIS update's usage DB.

        The DB lock covers individual writes, not process lifetimes, so a tokdash that
        is idle at backup time can resume writing mid-install. The helper cannot order
        such a process around — the honest move is to refuse the update while it runs.
        Matching follows BOTH bindings a process can have to our database: its data
        directory AND an explicit shared ``TOKDASH_USAGE_DB_PATH`` (the plan supports
        that override, so a different data dir does not prove a different database).
        """
        found = []
        me = os.getpid()
        want_dir = os.path.realpath(str(self.data_dir))
        want_db = os.path.realpath(usage_db) if usage_db else os.path.join(want_dir, "usage.sqlite3")
        for pid, argv, env in self._proc_snapshot():
            if pid == me or pid in exclude:
                continue
            if not _looks_like_tokdash(argv):
                continue
            dd = env.get("TOKDASH_DATA_DIR") or os.path.join(env.get("HOME", ""), ".tokdash")
            db = env.get("TOKDASH_USAGE_DB_PATH") or (os.path.join(dd, "usage.sqlite3") if dd else "")
            try:
                if (dd and os.path.realpath(dd) == want_dir) or (db and os.path.realpath(db) == want_db):
                    found.append(pid)
            except OSError:
                continue
        return found

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

        # Loaded configuration, complete arguments (mirrors update_eligibility): what
        # systemd would ACTUALLY restart must be the recorded Tokdash service.
        unit_name = f"{(man.get('service') or {}).get('name') or job.get('service_name') or 'tokdash'}.service"
        loaded = self._loaded_execstart(unit_name)
        if not loaded:
            raise RuntimeError(f"loaded configuration for {unit_name} is unavailable")
        self._check_loaded_argv(loaded, python, man)

        # No compatible co-tenant: another tokdash on this data dir would survive the
        # service stop and keep writing the database the replacement migrates.
        main_pid = 0
        try:
            shown = self.systemctl("show", unit_name, "-p", "MainPID", "--value")
            main_pid = int((shown.stdout or "0").strip() or 0)
        except Exception:
            main_pid = 0
        others = self._sibling_tokdash_pids(
            exclude={main_pid} if main_pid else set(), usage_db=job.get("usage_db"),
        )
        if others:
            raise RuntimeError(
                "another Tokdash process is using this data directory (pid "
                f"{', '.join(str(p) for p in others[:4])}); close it and retry the update"
            )
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
        """Snapshot the usage DB and HOLD the DB write lock until released.

        The lock is Tokdash's own write-serialization point. Taking it exclusively for
        the WHOLE replacement — not just the snapshot — means any tokdash process that
        tries to write during the install waits (it cannot write a half-replaced,
        mid-migration database). Processes that are merely IDLE at snapshot time are
        excluded the other way: preflight refused the update if any existed.
        """
        if not db_path or not Path(db_path).is_file():
            self.log("no usage DB on disk; nothing to back up")
            return None
        lock_path = Path(str(db_path) + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+", encoding="utf-8")
        self._db_lock_handle = handle
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            self._db_lock_handle = None
            raise RuntimeError("another Tokdash process holds the usage-DB lock; not replacing the package")
        try:
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
            self._prune_backups(backups_dir, keep)
            return str(dest)
        except BaseException:
            self.release_db_lock()
            raise

    def _prune_backups(self, backups_dir, keep):
        """Rotate old snapshots — but never past the recovery baseline.

        Pure count-based rotation is unsafe across a FAILED update: the failed new
        version may already have migrated the live DB to a schema the previously
        working version cannot read, so every snapshot the next attempts take is
        in the NEW schema and rotating to the newest three would delete the last
        snapshot that could still roll back. The baseline is the OLDEST snapshot
        taken since the last readiness-passed stamp — exactly the streak of failed
        attempts protecting the pre-failure state — and it is pinned regardless of
        age. :meth:`mark_readiness_passed` stamps the release; until then, count
        rotation applies to everything else.
        """
        all_backups = sorted(backups_dir.glob("pre-update-*.sqlite3"))
        if len(all_backups) <= keep:
            return
        try:
            validated = (backups_dir / VALIDATED_MARKER).read_text(encoding="utf-8").split()[0]
        except (OSError, IndexError):
            validated = ""  # nothing ever passed readiness: the oldest snapshot is the baseline
        baseline = None
        for path in all_backups:  # names embed %Y%m%d%H%M%S, so sorted order is time order
            if path.name[_STAMP_START:-len(".sqlite3")] > validated:
                baseline = path
                break
        for old in all_backups[:-keep]:
            if old == baseline:
                continue
            try:
                old.unlink()
            except OSError:
                pass

    def mark_readiness_passed(self, backups_dir):
        """Stamp that an update reached readiness, releasing the pinned baseline.

        Written the moment :meth:`readiness` passes, so the NEXT backup's rotation
        no longer pins the failed streak that preceded this success. The content
        is the same %Y%m%d%H%M%S stamp the snapshot names carry (plus the job id
        for humans), which makes the comparison in :meth:`_prune_backups` a plain
        lexicographic one.
        """
        try:
            backups_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            (backups_dir / VALIDATED_MARKER).write_text(f"{stamp} {self.job_id}\n", encoding="utf-8")
        except OSError:
            pass  # a missing marker only keeps the (safe) baseline pinned one round longer

    def release_db_lock(self):
        """Give the usage-DB lock back — BEFORE the new service starts, so its own
        startup writes are not blocked by us, and on every failure path."""
        handle = getattr(self, "_db_lock_handle", None)
        if handle is not None:
            self._db_lock_handle = None
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
    if fcntl is None:
        # Imported (so capability paths work anywhere) but can only run where flock does.
        print("update helper requires POSIX flock (Linux); aborting without touching anything", flush=True)
        return False
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
                r.release_db_lock()  # recovered service must be able to write
                r.recover_service(name)
                r.heartbeat_stop()  # last writer must be us, not a late heartbeat
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

            # The replacement is done: give the usage-DB lock back BEFORE starting the
            # new server, whose startup writes take the very same lock.
            r.release_db_lock()
            r.journal.update(job_id, phase="starting")
            try:
                r.start_service(name, bind, port)
                r.journal.update(job_id, phase="ready")
                r.readiness(str(job["to_version"]), bind, port)
                # Readiness passed: the new version is the working baseline now, so
                # the failed-streak snapshot that rotation pins may be released.
                r.mark_readiness_passed(Path(data_dir) / "backups")
            except Exception as exc:
                r.heartbeat_stop()
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

            r.heartbeat_stop()
            r.journal.update(
                job_id, phase="succeeded", failed_phase=None,
                result_version=str(job["to_version"]), message=None,
            )
            log(f"SUCCESS: v{job['to_version']} ready")
            return True
        finally:
            # Idempotent safety nets for paths that jumped out mid-phase.
            r.heartbeat_stop()
            r.release_db_lock()
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
