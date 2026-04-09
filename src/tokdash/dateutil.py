"""Shared date-range parsing utilities."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Tuple


def parse_date_range(date_from: str, date_to: str) -> Tuple[datetime, datetime]:
    """Parse YYYY-MM-DD strings into a (since, until) datetime pair.

    ``until`` is set to the *start* of the day after ``date_to`` so that the
    range is inclusive of the full final day.

    Raises ``ValueError`` on malformed input.
    """
    local_tz = datetime.now().astimezone().tzinfo or timezone.utc
    since = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=local_tz)
    until = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=local_tz) + timedelta(days=1)
    if since >= until:
        raise ValueError("date_from must be on or before date_to")
    return since, until
