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
prevent. When the file can be neither written nor read, the id is simply absent and the
caller says so instead of guessing.

GUARDRAIL: this value must never become readable cross-origin by loosening CORS on
``/health`` or any other unauthenticated endpoint. Any website that could read it would
hold a durable tracking identifier for this user. The stock policy in ``api.py`` stays as
it is; a route the browser is not allowed to read is reported as blocked, not unblocked.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, Optional

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

#: How long to sit out after a failed read or create. Caching the failure is what keeps
#: ``/health`` free of file I/O on a permanently broken state dir; caching it forever
#: would let one full disk at boot cost identity for the life of the process.
_FAILURE_RETRY_S = 60.0

_LOCK = threading.Lock()
_IDS: Dict[Path, str] = {}
_NEXT_TRY: Dict[Path, float] = {}


def instance_json_path() -> Path:
    """``<TOKDASH_DATA_DIR>/instance.json``, resolved on every call."""
    return tokdash_data_dir() / INSTANCE_FILENAME


def get_instance_id(path: Optional[Path] = None) -> Optional[str]:
    """This daemon's identity, or ``None`` when it cannot be established.

    Cached per resolved path, so ``/health`` does no file I/O after the first call and
    the lifespan warm-up only has to win once. Resolution is lazy rather than
    startup-only because ``TestClient(app)`` built without a context manager skips the
    lifespan, and ``/health`` has to answer the same either way.
    """
    target = Path(path) if path is not None else instance_json_path()
    hit = _IDS.get(target)
    if hit is not None:
        return hit
    if _waiting(target):
        return None

    with _LOCK:
        hit = _IDS.get(target)
        if hit is not None:
            return hit
        if _waiting(target):
            return None
        value = _resolve(target)
        if value is None:
            _NEXT_TRY[target] = time.monotonic() + _FAILURE_RETRY_S
            return None
        _IDS[target] = value
        _NEXT_TRY.pop(target, None)
        return value


def clear_instance_id_cache() -> None:
    """Forget every cached id. A test seam; production code never calls this."""
    with _LOCK:
        _IDS.clear()
        _NEXT_TRY.clear()


def _waiting(target: Path) -> bool:
    """True while a failed read or create is still inside its retry backoff."""
    return _NEXT_TRY.get(target, 0.0) > time.monotonic()


def _resolve(target: Path) -> Optional[str]:
    value = _read(target)
    if value:
        return value
    return _create(target)


def _read(target: Path) -> Optional[str]:
    """The stored id, or ``None`` when the file is absent, unreadable or malformed.

    An empty read is retried, because the daemon that published this file a moment ago
    may still be filling it. A file that is present but unparseable reads as absent,
    which is safe here only because ``_create`` publishes with ``os.link`` and so never
    has to truncate or replace one.
    """
    for attempt in range(_READ_RETRIES + 1):
        try:
            raw = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.debug("tokdash instance identity unreadable at %s: %s", target, exc)
            return None
        if raw.strip():
            return _parse(raw, target)
        if attempt < _READ_RETRIES:
            time.sleep(_READ_RETRY_DELAY_S)
    logger.debug("tokdash instance identity file is empty at %s", target)
    return None


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


def _create(target: Path) -> Optional[str]:
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
    except OSError as exc:
        logger.debug("tokdash data dir cannot hold instance identity: %s", exc)
        return None

    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, target)
        except FileExistsError:
            adopted = _read(target)
            if adopted:
                return adopted
            # Somebody else hit the same empty-file window. Leave their file alone; the
            # next request retries instead of inventing a second id for this data dir.
            logger.debug("tokdash instance identity publish raced at %s", target)
            return None
        return value
    except OSError as exc:
        # This also covers a filesystem with no hardlink support: ``os.link`` raises
        # ``OSError`` there rather than working halfway. Falling back to an exclusive
        # create of the final name would put the empty-file window back and let two
        # daemons over one data dir disagree, so identity is simply absent here, which
        # the dashboard shows as "identity unknown" rather than as two machines.
        logger.debug("tokdash instance identity not persistable at %s: %s", target, exc)
        return None
    finally:
        # ``os.link`` leaves the content reachable through ``target`` whatever happens to
        # the temp name, so on every way out of here the temp file is only litter.
        try:
            tmp.unlink()
        except OSError:
            pass
