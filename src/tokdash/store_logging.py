"""How a failed persistent-store read is reported.

The persistent store is a cache. When it cannot be read, the request fails open
to the live parsers and the answer is complete -- so the failure must not reach
the response (the dashboard reads a non-empty ``source_errors`` as "this server's
answer is unusable" and falls back to its last snapshot), and it must still be
visible somewhere, because otherwise a fallback that ran on every request looks
exactly like one that never ran (#151).

This module owns that policy so there is one copy of it. It deliberately has no
imports from the rest of the package, so every caller can import it
unconditionally rather than standing in a local re-implementation.
"""

from __future__ import annotations

import logging
from typing import Set, Tuple

# Failures this process has already reported. A store that is corrupt, locked or
# unreadable stays that way until somebody repairs it, and every request that
# touches it fails the same way, so the report is per distinct problem rather
# than per occurrence.
_REPORTED_STORE_FAILURES: Set[Tuple[str, type[BaseException]]] = set()


def log_store_failure(
    logger: logging.Logger, message: str, exc: BaseException, *, site: str
) -> None:
    """Report a failed store read, loudly once and quietly thereafter.

    The first occurrence of a given failure -- the same ``site`` raising the same
    exception type -- is a warning with ``exc_info``. Repeats are logged at debug,
    with the traceback still attached, so ``--log-level=DEBUG`` keeps the full
    story. One Overview refresh makes several requests that touch the store, and
    under the systemd service a persistent failure would otherwise put the same
    traceback in the journal forever.

    ``site`` is passed explicitly rather than read off the call stack. Reading it
    silently would be correct only while every caller invokes this function
    directly; one wrapper, lambda or partial would file a site's failures under
    the wrapper's name and merge two unrelated failures back into one line.

    The key is the site and the exception type only. The message is deliberately
    excluded: a caller that interpolates a path, a row count or the exception's
    own text would otherwise mint a fresh key per occurrence and re-flood the
    journal, which is the one thing this function exists to prevent. A caller
    that reports genuinely distinct problems from one place -- the session cache
    reports one failure per tool from a single handler -- passes a distinct
    ``site`` for each, which is what keeps them apart.
    """
    key = (site, type(exc))
    if key in _REPORTED_STORE_FAILURES:
        logger.debug(message, exc_info=exc)
        return
    _REPORTED_STORE_FAILURES.add(key)
    logger.warning(message, exc_info=exc)


def reset_store_failure_reports() -> None:
    """Forget every failure this process has already reported.

    The report-once policy is deliberately process-wide, so a test that exercises
    it needs a way back to a process that has reported nothing. Clearing the set
    is the whole of that: the next occurrence of a given failure warns again.
    """
    _REPORTED_STORE_FAILURES.clear()
