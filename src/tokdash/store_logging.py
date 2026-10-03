"""How a failed persistent-store read is reported.

The store is a cache. When it cannot be read the request fails open to the live
parsers and the answer is complete, so the failure must not reach the response:
``source_errors`` means "this source could not answer", and the dashboard reads a
non-empty list as "this server's answer is unusable" -- it keeps that server's
last complete snapshot, or marks the row partial when there is none. Reporting a
failed cache there would pin the view to stale numbers for as long as the
database stays broken, worse than the silence #151 was filed about. The failure
goes to the log instead, which is the only place a fallback that ran on every
request is still distinguishable from one that never ran.

One copy of that policy lives here, and this module imports nothing from the rest
of the package so every caller can import it unconditionally.
"""

from __future__ import annotations

import logging
from typing import Set, Tuple

# A corrupt, locked or unreadable store stays that way until somebody repairs it,
# and every request that touches it fails the same way -- so the report is per
# distinct problem, not per occurrence.
_REPORTED_STORE_FAILURES: Set[Tuple[str, type[BaseException]]] = set()


def log_store_failure(
    logger: logging.Logger, message: str, exc: BaseException, *, site: str
) -> None:
    """Report a failed store read once per process, loudly, then quietly.

    The first occurrence of a given failure -- the same ``site`` raising the same
    exception type -- warns with ``exc_info``. Repeats log at debug with the
    traceback still attached, so ``--log-level=DEBUG`` keeps the full story: one
    Overview refresh makes several store reads, and under the systemd service a
    persistent failure would otherwise put the same traceback in the journal
    forever.

    ``site`` is required rather than read off the call stack. Reading it silently
    is correct only while every caller invokes this function directly; one
    wrapper, lambda or partial would file a site's failures under the wrapper's
    name and merge two unrelated failures into one line.

    Only the site and the exception type are keyed. The message is excluded on
    purpose: a caller interpolating a path, a row count or the exception's own
    text would mint a fresh key per occurrence and re-flood the journal. A caller
    with genuinely distinct problems in one place passes a distinct ``site`` for
    each -- the session cache reports one failure per tool from a single handler,
    which is what keeps its five unreadable stores as five lines.
    """
    key = (site, type(exc))
    if key in _REPORTED_STORE_FAILURES:
        logger.debug(message, exc_info=exc)
        return
    _REPORTED_STORE_FAILURES.add(key)
    logger.warning(message, exc_info=exc)


def reset_store_failure_reports() -> None:
    """Forget every failure this process has already reported.

    The policy is process-wide, so a test that exercises it needs a way back to a
    process that has reported nothing.
    """
    _REPORTED_STORE_FAILURES.clear()
