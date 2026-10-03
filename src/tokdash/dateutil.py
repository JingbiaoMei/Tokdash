"""Shared date-range parsing utilities."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Tuple


def local_midnight(dt: datetime) -> datetime:
    """A naive local midnight, aware in the offset that date actually has.

    ``datetime.astimezone()`` on a naive value asks the OS for the offset in
    force at *that* moment, so a January boundary resolves to UTC+0 in London
    and a July one to UTC+1. Anchoring every date to a single offset captured
    once -- ``datetime.now().astimezone().tzinfo`` -- is the bug this replaced
    (#145): it dated a January range an hour early, and moved it again a week
    either side of a clock change.

    This is also the only rule that keeps the windows and the day buckets
    together. Entries are bucketed by the OS's local time and the heatmap
    buckets in SQLite with ``'localtime'``; a window built from any other
    source of truth -- ``/etc/timezone``, the registry, a ``TZ`` override --
    can disagree with both, which is the mismatch #145 is about.

    Aware input is returned unchanged: the caller already knows which instant it
    means, and re-resolving it would move it.
    """
    if dt.tzinfo is not None:
        return dt
    try:
        return dt.astimezone()
    except (OSError, OverflowError, ValueError):
        # Windows cannot convert a date before 1970 to a POSIX timestamp. The
        # offset for today is the closest thing available, and a pre-epoch
        # range is not one any source log reaches.
        return dt.replace(tzinfo=datetime.now().astimezone().tzinfo or timezone.utc)


def parse_date_range(date_from: str, date_to: str) -> Tuple[datetime, datetime]:
    """Parse YYYY-MM-DD strings into a (since, until) datetime pair.

    ``until`` is set to the *start* of the day after ``date_to`` so that the
    range is inclusive of the full final day. Each boundary resolves to local
    midnight in its own date's offset, so a range that spans a clock change is
    still exactly as many local days long.

    Raises ``ValueError`` on malformed input or if date_from is after date_to.
    """
    since = datetime.strptime(date_from, "%Y-%m-%d")
    until = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
    if since >= until:
        raise ValueError("date_from must be on or before date_to")
    return local_midnight(since), local_midnight(until)