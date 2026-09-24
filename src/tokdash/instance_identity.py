"""Stable per-daemon identity (issue #108).

One Tokdash daemon is often reachable under several URLs -- loopback, a Tailscale Serve
name, an SSH forward, a LAN address. A dashboard looking at two of them has to know
whether it is looking at one machine twice or at two machines, and no URL, port or label
can tell it: two daemons on one machine over two data dirs share everything but the data,
and two routes to one daemon share the data. Only the daemon can answer, so it answers
with this id.

The id is a UUID4 created once per data directory and kept in ``instance.json``. That
gives the three properties the dashboard needs: it survives a restart, two daemons over
one data dir agree on it (they read the same session logs, so counting them separately
double-counts), and two daemons over two data dirs differ even on one machine.

Never substitute a hash of the data-dir path. The same path string on two machines is two
daemons, and a wrong merge quietly hides a machine -- the one failure this field exists to
prevent. When the file can be neither written nor read the id is absent and the caller
says so instead of guessing, which costs de-duplication and nothing else.

GUARDRAIL: this value must never become readable cross-origin by loosening CORS on
``/health`` or any other unauthenticated endpoint. Any website that could read it would
hold a durable tracking identifier for this user. The stock policy in ``api.py`` stays as
it is; a route the browser is not allowed to read is reported as blocked, not unblocked.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Dict, NamedTuple, Optional, Set

from .clientpaths import tokdash_data_dir

logger = logging.getLogger(__name__)

#: Filename under ``TOKDASH_DATA_DIR``.
INSTANCE_FILENAME = "instance.json"

#: Bumped only if the file's shape changes. Readers skip files they cannot parse rather
#: than guessing at a layout they have never seen.
SCHEMA_VERSION = 1

#: A file that exists but is empty is almost certainly a sibling daemon mid-publish. Two
#: services coming up together at boot hit that window constantly, so read twice more
#: before concluding anything.
_READ_RETRIES = 2
_READ_RETRY_DELAY_S = 0.05

#: How long to sit out after a failure that could heal on its own: a full disk, a
#: read-only mount, a directory that was not there yet. Caching the failure is what keeps
#: ``/health`` free of file I/O on a permanently broken state dir; caching it forever
#: would let one bad boot cost identity for the life of the process.
_FAILURE_RETRY_S = 60.0

_LOCK = threading.Lock()
#: Guards ``_IN_FLIGHT`` alone, and never across file I/O. It cannot be ``_LOCK``: that
#: one is held for the whole of a resolution, and taking it here would block the event
#: loop on the very lookup this function exists to keep off the loop.
_IN_FLIGHT_LOCK = threading.Lock()
_IDS: Dict[Path, str] = {}
_NEXT_TRY: Dict[Path, float] = {}
#: Paths whose file exists but cannot be parsed. ``_create`` never replaces a file it did
#: not write, so no amount of retrying can fix these, and retrying forever just churns
#: write, fsync and unlink against a file that will never read. Cleared on restart.
_PERMANENT: Set[Path] = set()
#: One warning per path per process: a broken state dir should reach the log once, not on
#: every retry of every minute the daemon is up.
_WARNED: Set[Path] = set()
#: Paths with a resolution running on a worker thread, so a cold caller that arrives while
#: one is already going answers without starting a second one.
_IN_FLIGHT: Set[Path] = set()


class _Outcome(Enum):
    """Why a resolution attempt ended the way it did.

    Collapsing all of these into ``None`` is what made the failures both silent and
    indistinguishable, so the distinction is kept all the way out of ``_read``.
    """

    OK = "ok"
    ABSENT = "absent"
    #: Present but empty: a sibling is mid-publish. Worth another look later.
    EMPTY = "empty"
    #: Open or write failed. Permissions and disk space can come back.
    UNREADABLE = "unreadable"
    #: Present, non-empty, and not a usable id. Cannot heal without a human.
    MALFORMED = "malformed"


class _Result(NamedTuple):
    outcome: _Outcome
    value: Optional[str] = None
    detail: str = ""


def instance_json_path() -> Path:
    """``<TOKDASH_DATA_DIR>/instance.json``, resolved on every call."""
    return tokdash_data_dir() / INSTANCE_FILENAME


def get_instance_id(path: Optional[Path] = None) -> Optional[str]:
    """This daemon's identity, or ``None`` when it cannot be established.

    Blocks on file I/O whenever the answer is not already cached, so an async caller
    wants :func:`get_instance_id_async` instead.

    Cached per resolved path, so ``/health`` does no file I/O after the first call and the
    lifespan warm-up only has to win once. Resolution is lazy rather than startup-only
    because ``TestClient(app)`` built without a context manager skips the lifespan, and
    ``/health`` has to answer the same either way.
    """
    target = _target(path)
    hit = _IDS.get(target)
    if hit is not None:
        return hit

    with _LOCK:
        hit = _IDS.get(target)
        if hit is not None:
            return hit
        if _waiting(target):
            return None

        result = _resolve(target)
        if result.outcome is _Outcome.OK:
            _IDS[target] = result.value or ""
            _NEXT_TRY.pop(target, None)
            _PERMANENT.discard(target)
            return result.value

        _record_failure(target, result)
        return None


async def get_instance_id_async(path: Optional[Path] = None) -> Optional[str]:
    """:func:`get_instance_id` without blocking the event loop.

    ``/health`` is async specifically so it answers while every worker thread is busy, and
    an uncached resolution is not cheap: ``mkdir``, a write, an ``fsync`` and an
    ``os.link``, plus up to two sleeps on an empty file. On a slow or broken state dir
    that would stall the whole server once a minute, which is the opposite of the point,
    so the settled answer is returned inline and anything that might touch the disk goes
    to a worker thread. One per path at most: a lookup that cannot finish should cost one
    caller a missing id, and not a fresh stuck thread on every probe.
    """
    target = _target(path)
    if _is_settled(target):
        return _IDS.get(target)

    # Only ever a quick set membership test here. Anything that could wait -- the
    # resolution itself, or ``_LOCK`` -- belongs on the worker thread, or a second probe
    # arriving mid-lookup stalls the loop for exactly as long as the first one takes.
    with _IN_FLIGHT_LOCK:
        if target in _IN_FLIGHT:
            return None
        _IN_FLIGHT.add(target)
    try:
        return await asyncio.to_thread(get_instance_id, target)
    finally:
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT.discard(target)


def clear_instance_id_cache() -> None:
    """Forget every cached id and failure. A test seam; production never calls this."""
    with _LOCK:
        _IDS.clear()
        _NEXT_TRY.clear()
        _PERMANENT.clear()
        _WARNED.clear()
    with _IN_FLIGHT_LOCK:
        _IN_FLIGHT.clear()


def _target(path: Optional[Path]) -> Path:
    return Path(path) if path is not None else instance_json_path()


def _now() -> float:
    """The monotonic clock, in one seam so the retry backoff is testable."""
    return time.monotonic()


def _waiting(target: Path) -> bool:
    """True while a retryable failure is still inside its backoff."""
    return target in _PERMANENT or _NEXT_TRY.get(target, 0.0) > _now()


def _is_settled(target: Path) -> bool:
    """True when answering needs no file I/O: cached, backing off, or given up on."""
    return target in _IDS or target in _PERMANENT or _NEXT_TRY.get(target, 0.0) > _now()


def _record_failure(target: Path, result: _Result) -> None:
    if result.outcome is _Outcome.MALFORMED:
        # Never retried: the file is only fixable by whoever put it there.
        _PERMANENT.add(target)
        _NEXT_TRY.pop(target, None)
    else:
        _NEXT_TRY[target] = _now() + _FAILURE_RETRY_S
    _warn_once(target, result)


def _warn_once(target: Path, result: _Result) -> None:
    """Tell the user once, and tell them what to do about it.

    A missing id is invisible in the dashboard: the merge simply never happens, so two
    rows keep double-counting and nothing says why. Everything else here logs at DEBUG.
    """
    if target in _WARNED:
        return
    _WARNED.add(target)
    detail = f" ({result.detail})" if result.detail else ""
    if result.outcome is _Outcome.MALFORMED:
        logger.warning(
            "tokdash could not read its identity from %s%s. It is never overwritten, so "
            "delete the file to let Tokdash write a new one; until then this daemon "
            "reports no identity and a dashboard reaching it over several URLs cannot "
            "tell that they are one server.",
            target,
            detail,
        )
    else:
        logger.warning(
            "tokdash could not establish its identity at %s%s. Multi-server de-duplication "
            "is disabled until it can; check that the data directory is writable and has "
            "free space.",
            target,
            detail,
        )


def _resolve(target: Path) -> _Result:
    """Read the id, publishing one if there is nothing there.

    Public-ish on purpose: the boot-race test drives this from threads, because
    ``get_instance_id`` serialises on ``_LOCK`` and so cannot show a publish race that two
    separate daemon processes would hit.
    """
    read = _read(target)
    if read.outcome is _Outcome.OK or read.outcome is _Outcome.MALFORMED:
        return read
    if read.outcome is _Outcome.UNREADABLE:
        # The file exists and is not readable; creating over it is not on the table.
        return read
    return _create(target)


def _read(target: Path) -> _Result:
    """The stored id, or why there is not one.

    An empty read is retried, because the daemon that published this file a moment ago may
    still be filling it. A file that is present and unparseable is reported as
    ``MALFORMED`` rather than ``ABSENT`` so the caller does not go hunting for a
    ``FileExistsError`` it will only hit again.
    """
    for attempt in range(_READ_RETRIES + 1):
        try:
            raw = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _Result(_Outcome.ABSENT)
        except OSError as exc:
            logger.debug("tokdash instance identity unreadable at %s: %s", target, exc)
            return _Result(_Outcome.UNREADABLE, detail=str(exc))
        if raw.strip():
            value = _parse(raw, target)
            if value is not None:
                return _Result(_Outcome.OK, value)
            return _Result(_Outcome.MALFORMED, detail="not a UUID4 identity object")
        if attempt < _READ_RETRIES:
            time.sleep(_READ_RETRY_DELAY_S)
    logger.debug("tokdash instance identity file is empty at %s", target)
    return _Result(_Outcome.EMPTY)


def _parse(raw: str, target: Path) -> Optional[str]:
    try:
        payload = json.loads(raw)
    except ValueError:
        logger.debug("tokdash instance identity is not JSON at %s", target)
        return None
    if not isinstance(payload, dict):
        logger.debug("tokdash instance identity is not an object at %s", target)
        return None
    value = payload.get("instance_id")
    if not isinstance(value, str) or not value.strip():
        logger.debug("tokdash instance identity has no instance_id at %s", target)
        return None
    try:
        # Canonical round-trip, so a stored id with different casing or braces cannot
        # make one daemon look like two.
        return str(uuid.UUID(value.strip()))
    except ValueError:
        logger.debug("tokdash instance identity is not UUID-shaped at %s", target)
        return None


def _create(target: Path) -> _Result:
    """Publish a fresh id, or adopt the one a sibling daemon published first.

    ``os.link`` is the whole point: it is the one primitive that puts the final name in
    place already filled *and* refuses to step on a file that exists, so two daemons
    starting together over one data dir converge on a single id. ``open(path, "x")``
    creates the file empty, which is how a daemon ends up reading nothing, giving up on
    identity for the rest of its life, and never merging with the twin it was written to
    agree with.
    """
    value = str(uuid.uuid4())
    payload = json.dumps({"schema_version": SCHEMA_VERSION, "instance_id": value}) + "\n"
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        # Someone else created it between the read and here, empty at that instant.
        return _read(target)
    except OSError as exc:
        logger.debug("tokdash data dir cannot hold instance identity: %s", exc)
        return _Result(_Outcome.UNREADABLE, detail=str(exc))

    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, target)
        except FileExistsError:
            adopted = _read(target)
            if adopted.outcome is _Outcome.OK:
                return adopted
            # Somebody else hit the same empty-file window. Leave their file alone; the
            # next attempt retries rather than inventing a second id for this data dir.
            logger.debug("tokdash instance identity publish raced at %s", target)
            return adopted if adopted.outcome is not _Outcome.ABSENT else _Result(
                _Outcome.EMPTY, detail="publish raced"
            )
        return _Result(_Outcome.OK, value)
    except OSError as exc:
        # This also covers a filesystem with no hardlink support: ``os.link`` raises
        # ``OSError`` there rather than working halfway. Falling back to an exclusive
        # create of the final name would put the empty-file window back and let two
        # daemons over one data dir disagree, so identity is simply absent here, which
        # the dashboard shows as "identity unknown" rather than as two machines.
        logger.debug("tokdash instance identity not persistable at %s: %s", target, exc)
        return _Result(_Outcome.UNREADABLE, detail=str(exc))
    finally:
        # ``os.link`` leaves the content reachable through ``target`` whatever happens to
        # the temp name, so on every way out of here the temp file is only litter.
        try:
            tmp.unlink()
        except OSError:
            pass
