"""Bounded Overview buckets folded alongside the existing headline aggregation."""
from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from .model_normalization import normalize_model_name
except ImportError:  # pragma: no cover - OpenClaw's standalone import
    from model_normalization import normalize_model_name


def bucket_granularity(since: datetime | None, until: datetime | None) -> str | None:
    if since is None or until is None:
        return None
    # All-time uses a pre-epoch boundary Windows cannot convert to local time.
    # Reject wide windows first; the extra day allows for clock-offset changes.
    if until - since > timedelta(days=367):
        return None
    try:
        days = ((until - timedelta(microseconds=1)).astimezone().date() - since.astimezone().date()).days + 1
    except (OSError, OverflowError, ValueError):
        return None
    return "hour" if days == 1 else "day" if 1 < days <= 31 else "month" if 31 < days <= 366 else None


def next_month(local: datetime) -> datetime:
    """First midnight of the next local calendar month, resolving its UTC offset."""
    year, month = divmod(local.year * 12 + local.month, 12)
    return datetime(year, month + 1, 1).astimezone()


def bucket_key(timestamp_ms: int, granularity: str) -> str:
    local = datetime.fromtimestamp(timestamp_ms / 1000).astimezone()
    return local.strftime({"hour": "%Y-%m-%dT%H", "day": "%Y-%m-%d", "month": "%Y-%m"}[granularity])


def bucket_key_resolver(granularity: str):
    """Reuse a resolved calendar boundary for adjacent entries in a loaded source.

    Entries may arrive out of order; every timestamp is checked against both
    cached boundaries before reuse. Hourly keys use direct conversion so even
    clock changes within an hour cannot reuse a different local clock hour.
    """
    if granularity == "hour":
        return lambda timestamp_ms: bucket_key(timestamp_ms, "hour")
    first = last = 0
    key = ""

    def resolve(timestamp_ms: int) -> str:
        nonlocal first, last, key
        if first <= timestamp_ms < last:
            return key
        local = datetime.fromtimestamp(timestamp_ms / 1000).astimezone()
        if granularity == "month":
            floor = datetime(local.year, local.month, 1).astimezone()
            stop = next_month(floor)
        elif granularity == "day":
            floor = datetime.combine(local.date(), datetime.min.time()).astimezone()
            stop = datetime.combine(local.date() + timedelta(days=1), datetime.min.time()).astimezone()
        first, last = int(floor.timestamp() * 1000), int(stop.timestamp() * 1000)
        key = bucket_key(timestamp_ms, granularity)
        return key

    return resolve


def local_hour_keys(since: datetime) -> list[str]:
    """Clock hours that actually exist on this date; repeated hours share a key."""
    date = since.astimezone().date()
    start = datetime.combine(date, datetime.min.time()).astimezone()
    stop = datetime.combine(date + timedelta(days=1), datetime.min.time()).astimezone()
    return sorted({bucket_key(stamp, "hour") for stamp in range(
        int(start.timestamp() * 1000), int(stop.timestamp() * 1000), 3_600_000)})


def sql_bucket_expression(since: datetime | None, until: datetime | None, granularity: str) -> str:
    """Group by integer clock buckets when the window has one UTC offset.

    SQLite's local-time formatting per event is comparatively expensive. Most
    short windows have a constant offset; a window crossing a clock change uses
    SQLite's exact local-time conversion instead. No schema/index change needed.
    """
    if granularity not in ("hour", "day", "month"):
        raise ValueError(f"unsupported bucket granularity: {granularity}")
    if granularity == "month":
        if since is None or until is None:
            return "strftime('%Y-%m', timestamp / 1000, 'unixepoch', 'localtime')"
        local = since.astimezone()
        stop = until.astimezone()
        cursor = datetime(local.year, local.month, 1).astimezone()
        months = []
        while cursor < stop:
            months.append((int(cursor.timestamp() * 1000), cursor.year * 12 + cursor.month - 1))
            cursor = next_month(cursor)

        def expression(rows):
            if len(rows) == 1:
                return str(rows[0][1])
            mid = len(rows) // 2
            return (f"CASE WHEN timestamp < {rows[mid][0]} THEN {expression(rows[:mid])} "
                    f"ELSE {expression(rows[mid:])} END")

        # At most thirteen months; a balanced decision tree takes at most four
        # integer comparisons per event, with no per-row local-time formatting.
        return expression(months) if months else "NULL"
    if since is not None and until is not None:
        first = since.astimezone().date()
        last = (until - timedelta(microseconds=1)).astimezone().date()
        offsets = set()
        for day in range((last - first).days + 1):
            date = first + timedelta(days=day)
            for hour in (0, 12):
                offsets.add(datetime.combine(date, datetime.min.time()).replace(hour=hour).astimezone().utcoffset())
        if len(offsets) == 1:
            offset = int(next(iter(offsets)).total_seconds() * 1000)
            step = 3_600_000 if granularity == "hour" else 86_400_000
            return f"CAST((timestamp + {offset}) / {step} AS INTEGER)"
    pattern = "%Y-%m-%dT%H" if granularity == "hour" else "%Y-%m-%d"
    return f"strftime('{pattern}', timestamp / 1000, 'unixepoch', 'localtime')"


def sql_bucket_key(value: int | str, granularity: str) -> str:
    if isinstance(value, str):
        return value
    if granularity == "month":
        year, month = divmod(value, 12)
        return f"{year:04d}-{month + 1:02d}"
    step = 3600 if granularity == "hour" else 86400
    return datetime.fromtimestamp(value * step, timezone.utc).strftime(
        "%Y-%m-%dT%H" if granularity == "hour" else "%Y-%m-%d")


def add_bucket(buckets: dict[str, dict], key: str, *, tokens: int, cost: float,
               messages: int, tokens_in: int, tokens_cache: int, model: str,
               canonical_model: str | None = None) -> None:
    row = buckets.get(key)
    if row is None:
        row = buckets[key] = {"key": key, "tokens": 0, "cost": 0.0, "messages": 0,
                              "input": 0, "cache": 0, "models": {}}
    row["tokens"] += tokens
    row["cost"] += cost
    row["messages"] += messages
    row["input"] += tokens_in
    row["cache"] += tokens_cache
    canonical = normalize_model_name(model) if canonical_model is None else canonical_model
    row["models"][canonical] = row["models"].get(canonical, 0) + tokens


def merge_buckets(parts: list[dict[str, Any] | None]) -> dict[str, Any] | None:
    if not parts or any(not part or not part.get("granularity") for part in parts):
        return None
    granularity = parts[0]["granularity"]
    if any(part["granularity"] != granularity for part in parts):
        return None
    buckets: dict[str, dict] = {}
    for part in parts:
        for src in part.get("buckets", []):
            row = buckets.setdefault(src["key"], {"key": src["key"], "tokens": 0, "cost": 0.0,
                                                 "messages": 0, "input": 0, "cache": 0, "models": {}})
            for field in ("tokens", "cost", "messages", "input", "cache"):
                row[field] += src[field]
            for model, tokens in src["models"].items():
                row["models"][model] = row["models"].get(model, 0) + tokens
    return {"granularity": granularity, "buckets": [buckets[key] for key in sorted(buckets)]}


def interval_buckets(intervals: list[tuple[int, int]], granularity: str,
                     bounds: tuple[int, int] | None = None) -> dict[str, Any]:
    """Split additive agent intervals at local calendar boundaries, including DST."""
    if not intervals:
        return {"granularity": granularity, "buckets": []}
    first, last = bounds if bounds is not None else (
        min(start for start, _ in intervals), max(end for _, end in intervals))
    # Resolve the bounded hour/day/month boundaries once. Converting every
    # interval to local time is expensive for tools with thousands of events.
    local = datetime.fromtimestamp(first / 1000).astimezone()
    floor = local.replace(minute=0, second=0, microsecond=0) if granularity == "hour" else datetime.combine(local.date(), datetime.min.time()).astimezone()
    if granularity == "month":
        floor = datetime(local.year, local.month, 1).astimezone()
    edges = [int(floor.timestamp() * 1000)]
    keys = []
    while edges[-1] < last:
        local = datetime.fromtimestamp(edges[-1] / 1000).astimezone()
        keys.append(local.strftime({"hour": "%Y-%m-%dT%H", "day": "%Y-%m-%d", "month": "%Y-%m"}[granularity]))
        if granularity == "hour":
            boundary = edges[-1] + (3600 - local.minute * 60 - local.second) * 1000
        elif granularity == "month":
            boundary = int(next_month(local).timestamp() * 1000)
        else:
            tomorrow = local.date() + timedelta(days=1)
            boundary = int(datetime.combine(tomorrow, datetime.min.time()).astimezone().timestamp() * 1000)
        edges.append(boundary)
    totals = [0] * len(keys)
    step = edges[1] - edges[0] if len(edges) > 1 else None
    if step and any(right - left != step for left, right in zip(edges, edges[1:])):
        step = None
    origin = edges[0]
    index = 0
    for start, end in intervals:
        if end <= start:
            continue
        # Uniform real-time boundaries permit integer indexing, including DST
        # repeated/skipped clock hours. Unequal days or half-hour clock changes
        # still use the exact precomputed boundaries.
        if step:
            index = (start - origin) // step
        elif not edges[index] <= start < edges[index + 1]:
            index = bisect_right(edges, start) - 1
        # Nearly every capped activity interval fits within one bucket. Keep
        # that path to one binary search and an integer addition, without a
        # dictionary lookup or boundary-splitting loop for each event gap.
        if end <= edges[index + 1]:
            totals[index] += end - start
            continue
        while start < end:
            stop = min(end, edges[index + 1])
            totals[index] += stop - start
            start = stop
            if start < end:
                index += 1
    buckets: dict[str, int] = {}
    for key, total in zip(keys, totals):
        if total:
            buckets[key] = buckets.get(key, 0) + total
    return {"granularity": granularity,
            "buckets": [{"key": key, "agent_ms": buckets[key]} for key in sorted(buckets)]}
