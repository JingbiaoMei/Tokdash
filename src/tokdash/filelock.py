"""Cross-platform advisory file lock seam (Tier 0 seams refactor; Tier 1 Windows support).

Extracted from ``usage_store.usage_db_process_lock``. ``process_lock()`` is the
single place that knows how to take an advisory, exclusive, blocking lock on a
sidecar lock file. POSIX uses ``fcntl.flock``; when ``fcntl`` is unavailable
(e.g. not implemented on the platform) the lock degrades to a no-op, exactly as
today's ``usage_store`` behavior did before this extraction.

Windows (Tier 1) uses ``msvcrt.locking`` on an ``os.name == "nt"`` branch. This
is intentionally kept separate from the POSIX branch rather than unified: the
two OS locking primitives have different semantics (see caveat below), and the
POSIX path must stay byte-identical for Linux/macOS. If ``msvcrt`` is ever
unavailable on a "nt" platform, or the lock cannot be acquired even after
retrying, this degrades to the same no-op fallback as the POSIX
"fcntl unavailable" case — a locking failure must never crash a caller.

Caveat (Windows vs POSIX locking semantics): ``msvcrt.locking`` locks a
byte-range of the *open file* and is enforced by the OS as a *mandatory*
(not just advisory) lock on Windows, whereas POSIX ``flock`` is purely
advisory and locks the whole file via the inode. ``msvcrt.locking(LK_LOCK, ...)``
is also not truly blocking: internally it retries acquisition roughly 10 times
over ~1 second and then raises ``OSError`` if it still can't get the lock. To
approximate ``flock``'s indefinite blocking behavior, the Windows branch below
wraps that call in its own bounded retry loop. This is best-effort, not a
perfect emulation of POSIX advisory semantics.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
import os
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]


# How long the Windows branch will keep retrying ``msvcrt.locking`` after it
# raises ``OSError`` (each internal msvcrt attempt already spends ~1s retrying
# before raising), before giving up and degrading to the no-op fallback.
_WINDOWS_LOCK_TIMEOUT_SECONDS = 30.0
_WINDOWS_LOCK_RETRY_DELAY_SECONDS = 0.05


@contextmanager
def process_lock(lock_path: Path) -> Iterator[None]:
    """Take an advisory, exclusive, blocking lock on ``lock_path`` for the ``with`` block.

    Creates ``lock_path``'s parent directory if needed. When the platform has no
    locking support (``fcntl`` unavailable on POSIX, ``msvcrt`` unavailable or
    non-functional on Windows), this is a no-op — callers still get the rest of
    their concurrency story (e.g. an in-process lock) but not cross-process
    exclusion. Preserves the exact behavior of the original
    ``usage_store.usage_db_process_lock`` POSIX/no-op fallback.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if os.name == "nt":
        with _windows_process_lock(lock_path):
            yield
        return

    if fcntl is None:
        yield
        return
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _windows_process_lock(lock_path: Path) -> Iterator[bool]:
    """``os.name == "nt"`` locking implementation using ``msvcrt.locking``.

    Imported lazily because ``msvcrt`` does not exist on POSIX. Any failure to
    import or use it (module missing, attribute missing, or the bounded retry
    loop below exhausting its budget) degrades to a no-op, exactly like the
    POSIX ``fcntl is None`` fallback — locking is a best-effort convenience,
    never a hard dependency for callers.
    """
    try:
        import msvcrt
    except ImportError:  # pragma: no cover - exercised via injected fake on non-Windows
        yield False
        return

    try:
        handle = lock_path.open("a+b")
    except OSError:
        yield False
        return

    with handle:
        try:
            fd = handle.fileno()
        except (OSError, ValueError):
            yield False
            return

        locked = _acquire_windows_lock(msvcrt, fd)
        try:
            yield locked
        finally:
            if locked:
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass


def _acquire_windows_lock(msvcrt, fd: int) -> bool:
    """Retry ``msvcrt.locking(LK_LOCK)`` to approximate blocking ``flock(LOCK_EX)``.

    ``msvcrt.locking(fd, LK_LOCK, 1)`` already retries internally (~10 times
    over ~1 second) before raising ``OSError`` if the region is still locked.
    We wrap that in an outer bounded loop so a lock held by another process for
    longer than msvcrt's internal retry window doesn't immediately give up.
    After ``_WINDOWS_LOCK_TIMEOUT_SECONDS`` of total retrying we give up and
    return ``False`` (no-op degrade) rather than block forever or raise.
    """
    deadline = time.monotonic() + _WINDOWS_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(_WINDOWS_LOCK_RETRY_DELAY_SECONDS)
