"""Tests for tokdash.filelock.process_lock (Tier 1 Windows support).

Covers:
  - The real POSIX fcntl.flock path (this box is Linux) still excludes a
    concurrent holder and releases correctly.
  - process_lock's reentrant-safety story with usage_store's in-process
    threading.RLock (``usage_store._WRITE_LOCK``), which is the only "caller
    lock" wrapping process_lock in this codebase.
  - The Windows (``os.name == "nt"``) branch, exercised on this Linux box by
    injecting a fake ``msvcrt`` module into ``sys.modules`` and forcing the
    nt code path via monkeypatching ``filelock.os.name``. This validates the
    lock/unlock call sequence and the graceful no-op degrade on OSError /
    missing msvcrt, but cannot validate real Windows locking semantics.
"""
from __future__ import annotations

import sys
import threading

import pytest

from tokdash import filelock
from tokdash import usage_store
from tokdash.filelock import process_lock

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows collection path
    fcntl = None


# ---------------------------------------------------------------------------
# POSIX (real fcntl) behavior
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="exercises the real POSIX fcntl.flock code path; Windows takes the msvcrt branch instead",
)
def test_posix_process_lock_excludes_concurrent_holder_and_releases(tmp_path):
    lock_path = tmp_path / "usage.sqlite3.lock"

    lock_taken = threading.Event()
    release_lock = threading.Event()

    def hold_lock():
        with process_lock(lock_path):
            lock_taken.set()
            release_lock.wait(timeout=5)

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()
    assert lock_taken.wait(timeout=5), "background thread never acquired the lock"

    # While the background thread holds the lock via process_lock, a second,
    # independent file descriptor attempting a non-blocking exclusive flock on
    # the same file must fail with EWOULDBLOCK/EAGAIN.
    with lock_path.open("a+") as contender:
        with pytest.raises(BlockingIOError):
            fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    release_lock.set()
    holder.join(timeout=5)
    assert not holder.is_alive()

    # After process_lock's context exits, the lock must be released: a fresh
    # non-blocking exclusive flock attempt now succeeds.
    with lock_path.open("a+") as contender:
        fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(contender.fileno(), fcntl.LOCK_UN)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="asserts the POSIX-only fcntl module is used; Windows has no fcntl",
)
def test_posix_process_lock_uses_fcntl_flock(tmp_path):
    assert filelock.fcntl is not None
    lock_path = tmp_path / "usage.sqlite3.lock"
    with process_lock(lock_path):
        pass  # must not raise; exercises the real fcntl.flock/LOCK_UN pair


# ---------------------------------------------------------------------------
# Reentrant-safety with usage_store's in-process lock
# ---------------------------------------------------------------------------


def test_process_lock_reentrant_safe_with_usage_store_write_lock(tmp_path):
    """Mirrors usage_store.usage_db_process_lock's actual locking shape.

    ``usage_db_process_lock`` wraps ``process_lock`` in the module-level
    ``_WRITE_LOCK`` (a ``threading.RLock``). No call site in usage_store.py
    nests ``usage_db_process_lock`` within itself, but a caller *can* already
    hold ``_WRITE_LOCK`` reentrantly (same thread) before entering it — e.g. a
    future nested call path. That must not deadlock: the RLock allows
    reentry, and process_lock itself is only acquired once at a time.
    """
    db_path = tmp_path / "usage.sqlite3"

    usage_store._WRITE_LOCK.acquire()
    try:
        with usage_store.usage_db_process_lock(db_path):
            pass
    finally:
        usage_store._WRITE_LOCK.release()

    # The RLock must be back to "unheld" and process_lock's fcntl state must
    # not have leaked, so a normal (non-reentrant) call still works.
    with usage_store.usage_db_process_lock(db_path):
        pass

    # And back-to-back sequential calls (the real usage_store call pattern)
    # must keep working repeatedly without deadlocking or leaking lock state.
    for _ in range(3):
        with usage_store.usage_db_process_lock(db_path):
            pass


# ---------------------------------------------------------------------------
# Windows (os.name == "nt") branch, exercised via an injected fake msvcrt
# ---------------------------------------------------------------------------


class _FakeMsvcrt:
    """Stand-in for the ``msvcrt`` module, sufficient for process_lock's use."""

    LK_LOCK = 1
    LK_UNLCK = 0

    def __init__(self, fail_locking: bool = False):
        self.calls: list[tuple[int, int]] = []
        self.fail_locking = fail_locking

    def locking(self, fd, mode, nbytes):  # noqa: D401 - mirrors msvcrt.locking signature
        self.calls.append((mode, nbytes))
        if mode == self.LK_LOCK and self.fail_locking:
            raise OSError("fake: region already locked")
        # LK_UNLCK (or a successful LK_LOCK) always "succeeds".


def _force_nt_branch(monkeypatch, fake_msvcrt):
    monkeypatch.setattr(filelock.os, "name", "nt")
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)


def test_windows_branch_locks_then_unlocks_around_with_body(tmp_path, monkeypatch):
    fake = _FakeMsvcrt()
    _force_nt_branch(monkeypatch, fake)

    lock_path = tmp_path / "usage.sqlite3.lock"
    with process_lock(lock_path):
        # Exactly one LK_LOCK call must have happened before the body runs,
        # and no unlock yet.
        assert fake.calls == [(fake.LK_LOCK, 1)]
    # After the with-block exits, LK_UNLCK must have been issued.
    assert fake.calls == [(fake.LK_LOCK, 1), (fake.LK_UNLCK, 1)]


def test_windows_branch_unlocks_even_when_body_raises(tmp_path, monkeypatch):
    fake = _FakeMsvcrt()
    _force_nt_branch(monkeypatch, fake)

    lock_path = tmp_path / "usage.sqlite3.lock"
    with pytest.raises(ValueError):
        with process_lock(lock_path):
            raise ValueError("boom")
    assert fake.calls == [(fake.LK_LOCK, 1), (fake.LK_UNLCK, 1)]


def test_windows_branch_degrades_gracefully_on_oserror(tmp_path, monkeypatch):
    """OSError from msvcrt.locking must never crash the caller.

    Matches the POSIX ``fcntl is None`` no-op fallback: locking is a
    best-effort convenience. The bounded retry timeout/delay are shrunk so
    the test runs fast instead of spending the real (Windows-sized) timeout.
    """
    fake = _FakeMsvcrt(fail_locking=True)
    _force_nt_branch(monkeypatch, fake)
    monkeypatch.setattr(filelock, "_WINDOWS_LOCK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(filelock, "_WINDOWS_LOCK_RETRY_DELAY_SECONDS", 0.01)

    lock_path = tmp_path / "usage.sqlite3.lock"
    with process_lock(lock_path):
        pass  # must not raise despite every LK_LOCK attempt failing

    assert fake.calls, "expected at least one LK_LOCK retry attempt"
    assert all(mode == fake.LK_LOCK for mode, _ in fake.calls), (
        "since acquisition never succeeded, LK_UNLCK must never be called"
    )


def test_windows_branch_degrades_gracefully_when_msvcrt_missing(tmp_path, monkeypatch):
    """If msvcrt can't be imported at all (e.g. a broken embed), no-op like POSIX."""
    monkeypatch.setattr(filelock.os, "name", "nt")
    monkeypatch.delitem(sys.modules, "msvcrt", raising=False)
    # msvcrt genuinely does not exist on this POSIX box, so the lazy `import
    # msvcrt` inside the nt branch raises ImportError for real (not faked).

    lock_path = tmp_path / "usage.sqlite3.lock"
    with process_lock(lock_path):
        pass  # must not raise


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="validates the real msvcrt module shape only meaningful on actual Windows",
)
def test_real_msvcrt_has_expected_constants():  # pragma: no cover - real Windows only
    import msvcrt

    assert hasattr(msvcrt, "locking")
    assert hasattr(msvcrt, "LK_LOCK")
    assert hasattr(msvcrt, "LK_UNLCK")
