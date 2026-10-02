"""Tests for concurrent config writers and file locking (issue #143).

Validates:
- High-contention multi-threaded writes on Windows and POSIX (0% failure rate vs baseline 80%).
- Multi-process concurrent updates with no lost updates and no PermissionError crashes.
- Mutual exclusion under config_process_lock.
- Clean temp file removal (no orphaned *.tmp* files) even under error conditions.
- Update-check consent safely coexisting with quota consent updates.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import List

# Ensure local worktree src takes precedence over any editable-installed version
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from tokdash.onboard import paths, updatecheck
from tokdash.sources.quota import config as quota_config


def test_multithreaded_concurrent_config_mutations(tmp_path, monkeypatch):
    """Stress test: 8 threads executing 20 writes each under simultaneous contention.

    On Windows without file locking and unique temp names, this fails with
    PermissionError at ~80% crash rate. With two-tier locking and per-write temp
    files, all 160 writes must complete with 0 errors and zero orphaned temp files.
    """
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TOKDASH_QUOTA_POLL", raising=False)

    n_threads = 8
    n_writes = 20
    barrier = threading.Barrier(n_threads)
    errors: List[str] = []

    def worker(tid: int):
        barrier.wait()
        for i in range(n_writes):
            try:
                if tid == 0:
                    quota_config.set_quota_consent({"claude_api": (i % 2 == 0)})
                elif tid == 1:
                    quota_config.set_poll_interval_minutes(15 if i % 2 == 0 else 60)
                elif tid == 2:
                    quota_config.set_quota_consent({"codex_api": (i % 2 == 0)})
                elif tid == 3:
                    quota_config.set_quota_enabled(i % 2 == 0)
                elif tid == 4:
                    quota_config.set_quota_consent({"antigravity_api": True, "credential_scan": True})
                elif tid == 5:
                    updatecheck.enable()
                elif tid == 6:
                    quota_config.set_quota_consent({"minimax_api": True, "kimi_api": (i % 2 == 0)})
                else:
                    quota_config.set_quota_consent({"grok_api": (i % 2 == 0), "zai_api": True})
            except Exception as exc:
                errors.append(f"Thread {tid} write {i}: {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Encountered {len(errors)} concurrent write errors:\n" + "\n".join(errors[:10])

    # Validate final file integrity and retention of fields
    p = paths.config_path()
    assert p.is_file()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert "quota" in data
    assert data.get("update_check") is True
    quota = data["quota"]
    assert "antigravity_api" in quota
    assert quota["antigravity_api"] is True
    assert quota["credential_scan"] is True
    assert "poll_interval_minutes" in quota
    assert "enabled" in quota

    # Assert no temp files left behind
    leftover_tmps = list(tmp_path.glob("*.tmp*"))
    assert leftover_tmps == [], f"Found orphaned temp files: {leftover_tmps}"


def test_multiprocess_concurrent_config_mutations(tmp_path, monkeypatch):
    """Stress test: 4 separate OS processes executing concurrent RMW writes.

    Simulates the CLI `tokdash quota consent`, background poller, and dashboard
    writing simultaneously to TOKDASH_DATA_DIR. All processes must succeed and
    their updates must not clobber each other.
    """
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path))

    child_script = tmp_path / "_child_writer.py"
    child_code = r'''
import os
import sys
from pathlib import Path

data_dir = sys.argv[1]
proc_id = int(sys.argv[2])
n_writes = int(sys.argv[3])

os.environ["TOKDASH_DATA_DIR"] = data_dir

from tokdash.sources.quota import config as qc
from tokdash.onboard import updatecheck

errs = 0
for i in range(n_writes):
    try:
        if proc_id == 0:
            qc.set_quota_consent({"codex_api": True, "claude_api": (i % 2 == 0)})
        elif proc_id == 1:
            qc.set_poll_interval_minutes(30 if i % 2 == 0 else 120)
        elif proc_id == 2:
            qc.set_quota_consent({"antigravity_api": True, "credential_scan": True})
        else:
            updatecheck.enable()
    except Exception as exc:
        print(f"Proc {proc_id} error at write {i}: {type(exc).__name__}: {exc}", file=sys.stderr)
        errs += 1

sys.exit(errs)
'''
    child_script.write_text(child_code, encoding="utf-8")

    n_procs = 4
    n_writes = 15
    src_dir = str(Path(__file__).resolve().parent.parent / "src")

    env = os.environ.copy()
    env["PYTHONPATH"] = src_dir + (os.pathsep + env["PYTHONPATH"] if "PYTHONPATH" in env else "")

    processes = [
        subprocess.Popen(
            [sys.executable, str(child_script), str(tmp_path), str(pid), str(n_writes)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for pid in range(n_procs)
    ]

    for p in processes:
        stdout, stderr = p.communicate(timeout=30)
        assert p.returncode == 0, f"Child process failed with rc {p.returncode}:\nstderr: {stderr}\nstdout: {stdout}"

    # Verify merged contents
    cfg = json.loads(paths.config_path().read_text(encoding="utf-8"))
    assert cfg.get("update_check") is True
    quota = cfg.get("quota", {})
    assert quota.get("codex_api") is True
    assert quota.get("antigravity_api") is True
    assert quota.get("credential_scan") is True
    assert quota.get("poll_interval_minutes") in (30, 120)

    # No temp files left
    leftover_tmps = list(tmp_path.glob("*.tmp*"))
    assert leftover_tmps == []


def test_config_process_lock_mutual_exclusion(tmp_path, monkeypatch):
    """Verify that config_process_lock serializes in-process callers."""
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path))

    lock_entered = threading.Event()
    release_event = threading.Event()
    contender_acquired = False

    def holder():
        with quota_config.config_process_lock():
            lock_entered.set()
            release_event.wait(timeout=5)

    t = threading.Thread(target=holder)
    t.start()
    assert lock_entered.wait(timeout=5)

    # Inside the main thread, attempting to take the in-process lock without blocking must fail
    acquired_immediately = quota_config._CONFIG_WRITE_LOCK.acquire(blocking=False)
    if acquired_immediately:
        quota_config._CONFIG_WRITE_LOCK.release()
        pytest.fail("_CONFIG_WRITE_LOCK was acquired while background thread held config_process_lock")

    release_event.set()
    t.join(timeout=5)

    # Now the lock can be acquired
    assert quota_config._CONFIG_WRITE_LOCK.acquire(timeout=2)
    quota_config._CONFIG_WRITE_LOCK.release()


def test_config_write_cleans_up_temp_on_failure(tmp_path, monkeypatch):
    """Verify that if an error occurs during writing or atomic replace, temp file is deleted."""
    cfg_file = tmp_path / "config.json"

    # Simulate an error during atomic replacement by monkeypatching Path.replace to raise
    original_replace = Path.replace

    def broken_replace(self, target):
        raise OSError("simulated disk error during replace")

    monkeypatch.setattr(Path, "replace", broken_replace)

    with pytest.raises(OSError, match="simulated disk error"):
        quota_config._write_config_unlocked({"test": 123}, cfg_file)

    # Assert no .tmp file was left behind
    temp_files = list(tmp_path.glob("*.tmp*"))
    assert temp_files == [], f"Expected temp file cleanup on failure, but found: {temp_files}"


def test_updatecheck_and_quota_consent_interleaved(tmp_path, monkeypatch):
    """Verify updatecheck.enable and set_quota_consent do not clobber each other's keys."""
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path))

    # Pre-populate quota block
    quota_config.set_quota_consent({"claude_api": True})

    # Enable updatecheck
    updatecheck.enable()

    # Re-read: both keys must exist
    cfg = json.loads(paths.config_path().read_text(encoding="utf-8"))
    assert cfg["update_check"] is True
    assert cfg["quota"]["claude_api"] is True

    # Mutate quota again: update_check must not be dropped
    quota_config.set_quota_consent({"codex_api": True})
    cfg2 = json.loads(paths.config_path().read_text(encoding="utf-8"))
    assert cfg2["update_check"] is True
    assert cfg2["quota"]["claude_api"] is True
    assert cfg2["quota"]["codex_api"] is True


def test_purge_cleans_config_lock_file(tmp_path, monkeypatch):
    """Verify that uninstall --purge deletes config.json.lock alongside config.json."""
    from tokdash.onboard import engine
    from tokdash.onboard.plan import Options

    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path))

    db = paths.usage_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_text("dummy db", encoding="utf-8")

    cfg = paths.config_path()
    cfg.write_text("{}", encoding="utf-8")

    lock = Path(str(cfg) + ".lock")
    lock.write_text("dummy lock", encoding="utf-8")

    opts = Options(
        action="uninstall",
        auto=True,
        purge=True,
        yes=True,
    )
    rc = engine.cmd_uninstall(opts)
    assert rc == 0
    assert not cfg.exists()
    assert not lock.exists()
