"""Fine-grained usage analytics for report-style consumers.

One endpoint, many facets, one scan. An endpoint per metric would repeat the
/api/sessions shape -- a serialised fan-out that already sheds load at 503 under
parallelism -- and a yearly report opening with eight cold round trips is the
thing this module exists to avoid. Instead the store is asked once, at a
grouping fine enough to fold into every time-shaped facet, and the caller picks
which folds it wants.

The expensive facet (``projects``) is opt-in and pays for itself only when
requested; everything else comes out of the single composite scan.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

from .compute import (
    _collect_live_coding_entries,
    _sync_usage_store,
    _usage_store_live_sources,
    assign_contribution_intensity,
    contribution_streaks,
    resolve_period,
)
from .dateutil import parse_date_range
from .sources.coding_tools import CodingToolsUsageTracker
from .usage_store import UsageEntryStore, persistent_usage_db_enabled

INSIGHTS_SCHEMA_VERSION = 1

ALL_FACETS = (
    "hourly",
    "weekday",
    "heatmap",
    "daily",
    "models",
    "tools",
    "projects",
    "streaks",
    "firsts",
)

# Everything the composite scan already pays for. `projects` and `daily` are
# left out: the first needs a second scan plus a session-record read, and the
# second is the largest payload in the set and duplicates /api/stats.
DEFAULT_FACETS = ("hourly", "weekday", "heatmap", "models", "tools", "streaks", "firsts")

WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# The 夜猫子指数 window: tokens produced between 22:00 and 02:00 local.
NIGHT_HOURS = frozenset({22, 23, 0, 1})

_TOKEN_FIELDS = ("input", "output", "cacheRead", "cacheWrite", "reasoning")


class UnknownFacetError(ValueError):
    """Raised when a caller asks for a facet that does not exist."""


def parse_facets(raw: Optional[str]) -> tuple[str, ...]:
    """Facet list from the query string, or the default set.

    An unknown name is refused rather than dropped. Silently ignoring it is how
    a consumer ends up rendering a blank section labelled as data -- the same
    failure mode as an unrecognised ``period``.
    """
    if raw is None or not str(raw).strip():
        return DEFAULT_FACETS

    requested = [token.strip().lower() for token in str(raw).split(",") if token.strip()]
    if not requested:
        return DEFAULT_FACETS

    unknown = [name for name in requested if name not in ALL_FACETS]
    if unknown:
        raise UnknownFacetError(
            f"unknown facet(s): {', '.join(sorted(set(unknown)))}. "
            f"Accepted: {', '.join(ALL_FACETS)}"
        )
    # Preserve caller order but drop repeats.
    return tuple(dict.fromkeys(requested))


def _local_day_hour(timestamp_ms: Any) -> Optional[tuple[str, int]]:
    """(YYYY-MM-DD, hour) in local time for an epoch-ms stamp."""
    try:
        stamp = int(timestamp_ms)
    except (TypeError, ValueError):
        return None
    if stamp <= 0:
        return None
    moment = datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).astimezone()
    return moment.strftime("%Y-%m-%d"), moment.hour


def _live_insight_rows(
    since: Optional[datetime], until: Optional[datetime]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Rows for sources that keep their own database and never reach usage_entries.

    Without these the facets would quietly omit whole tools -- on a machine with
    OpenCode installed that is tens of thousands of entries -- while still
    reporting a total that looks complete.
    """
    tracker = CodingToolsUsageTracker()
    live_sources = _usage_store_live_sources(tracker)
    if not live_sources:
        return [], []

    entries = _collect_live_coding_entries(tracker, since, until, live_sources)
    grouped: dict[tuple[str, int, str, str, str], dict[str, Any]] = {}
    for entry in entries:
        placed = _local_day_hour(entry.get("timestamp"))
        if placed is None:
            continue
        day, hour = placed
        source = str(entry.get("source") or "unknown")
        model = str(entry.get("model") or "unknown")
        provider = str(entry.get("provider") or "")
        key = (day, hour, source, model, provider)
        bucket = grouped.get(key)
        if bucket is None:
            bucket = grouped[key] = {
                "day": day,
                "hour": hour,
                "source": source,
                "model": model,
                "provider": provider,
                "tokens": 0,
                "cost": 0.0,
                "messages": 0,
                "entries": 0,
            }
        bucket["tokens"] += sum(int(entry.get(field, 0) or 0) for field in _TOKEN_FIELDS)
        bucket["cost"] += float(entry.get("cost", 0.0) or 0.0)
        bucket["messages"] += int(entry.get("messageCount", 1) or 1)
        bucket["entries"] += 1

    return list(grouped.values()), sorted({str(e.get("source") or "unknown") for e in entries})


def _empty_bucket(**extra: Any) -> dict[str, Any]:
    return {"tokens": 0, "cost": 0.0, "messages": 0, "entries": 0, **extra}


def _accumulate(bucket: dict[str, Any], row: dict[str, Any]) -> None:
    bucket["tokens"] += int(row.get("tokens", 0) or 0)
    bucket["cost"] += float(row.get("cost", 0.0) or 0.0)
    bucket["messages"] += int(row.get("messages", 0) or 0)
    bucket["entries"] += int(row.get("entries", 0) or 0)


def _rounded(bucket: dict[str, Any]) -> dict[str, Any]:
    out = dict(bucket)
    out["cost"] = round(float(out.get("cost", 0.0)), 6)
    return out


def _ranked(buckets: dict[str, dict[str, Any]], key: str) -> list[dict[str, Any]]:
    rows = [_rounded({key: name, **values}) for name, values in buckets.items()]
    rows.sort(key=lambda row: (-row["tokens"], -row["cost"], str(row[key])))
    return rows


def _fold_hourly(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    hours = {hour: _empty_bucket(hour=hour) for hour in range(24)}
    for row in rows:
        _accumulate(hours[int(row["hour"]) % 24], row)

    buckets = [_rounded(hours[hour]) for hour in range(24)]
    total = sum(bucket["tokens"] for bucket in buckets)
    night = sum(bucket["tokens"] for bucket in buckets if bucket["hour"] in NIGHT_HOURS)
    busiest = max(buckets, key=lambda bucket: bucket["tokens"]) if total else None

    return {
        "buckets": buckets,
        "peak_hour": busiest["hour"] if busiest and busiest["tokens"] else None,
        # The share a 年度报告 leads with; None rather than 0.0 when there is no
        # data at all, so "no usage" cannot be read as "never works at night".
        "night_share": round(night / total, 4) if total else None,
        "night_hours": sorted(NIGHT_HOURS),
    }


def _fold_weekday(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    days = {index: _empty_bucket(weekday=index, name=WEEKDAY_NAMES[index]) for index in range(7)}
    for row in rows:
        weekday = datetime.strptime(row["day"], "%Y-%m-%d").date().weekday()
        _accumulate(days[weekday], row)

    buckets = [_rounded(days[index]) for index in range(7)]
    total = sum(bucket["tokens"] for bucket in buckets)
    busiest = max(buckets, key=lambda bucket: bucket["tokens"]) if total else None
    return {
        "buckets": buckets,
        "peak_weekday": busiest["weekday"] if busiest and busiest["tokens"] else None,
    }


def _fold_heatmap(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """The full 7x24 grid, zeros included.

    Emitted dense on purpose: a heatmap consumer needs every cell to draw, and
    filling gaps client-side is where off-by-one weekday bugs come from.
    """
    cells = {
        (weekday, hour): _empty_bucket(weekday=weekday, hour=hour)
        for weekday in range(7)
        for hour in range(24)
    }
    for row in rows:
        weekday = datetime.strptime(row["day"], "%Y-%m-%d").date().weekday()
        _accumulate(cells[(weekday, int(row["hour"]) % 24)], row)

    grid = [_rounded(cells[(weekday, hour)]) for weekday in range(7) for hour in range(24)]
    return {"cells": grid, "max_tokens": max((cell["tokens"] for cell in grid), default=0)}


def _fold_daily(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    days: dict[str, dict[str, Any]] = {}
    for row in rows:
        bucket = days.get(row["day"])
        if bucket is None:
            bucket = days[row["day"]] = _empty_bucket(date=row["day"])
        _accumulate(bucket, row)

    ordered = [_rounded(days[day]) for day in sorted(days)]
    # Reuse the Stats intensity ranking so a calendar drawn from either surface
    # shades identically.
    shaped = [{"totals": {"tokens": entry["tokens"]}} for entry in ordered]
    assign_contribution_intensity(shaped)
    for entry, shape in zip(ordered, shaped):
        entry["intensity"] = shape["intensity"]
    return ordered


def _fold_projects(
    store: UsageEntryStore,
    *,
    since: Optional[datetime],
    until: Optional[datetime],
    include_names: bool,
) -> dict[str, Any]:
    """Token totals per project, via file_path -> session_records.

    usage_entries carries no project column, but it does carry the transcript
    path, and that is the same path session_records indexes. So attribution is a
    lookup rather than a schema migration. Sources whose rows have no usable
    path land in ``unattributed`` rather than being dropped.
    """
    usage_rows = store.project_usage_rows(since=since, until=until)
    project_by_path = store.session_project_map()

    projects: dict[str, dict[str, Any]] = {}
    unattributed = _empty_bucket()
    for row in usage_rows:
        project = project_by_path.get(row["file_path"])
        target = projects.setdefault(project, _empty_bucket()) if project else unattributed
        _accumulate(target, row)

    ranked = _ranked(projects, "project")
    if not include_names:
        # The shareable tier: keep the shape and the distribution, drop the
        # identities. Ranks stay meaningful, names never leave the machine.
        for index, entry in enumerate(ranked, start=1):
            entry["project"] = f"project-{index}"

    return {
        "projects": ranked,
        "unattributed": _rounded(unattributed),
        "attributed_project_count": len(ranked),
        "names_included": bool(include_names),
    }


def compute_insights(
    period: str = "year",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    facets: Optional[str] = None,
    include_project_names: bool = True,
) -> Dict[str, Any]:
    """Facet-selected analytics over one composite scan."""
    selected = parse_facets(facets)
    window = resolve_period(period, date_from, date_to)

    if date_from and date_to:
        since, until = parse_date_range(date_from, date_to)
    else:
        since = datetime.strptime(window["from"], "%Y-%m-%d").astimezone() if window.get("from") else None
        until = None
        if window.get("to"):
            end = datetime.strptime(window["to"], "%Y-%m-%d").astimezone()
            until = end.replace(hour=23, minute=59, second=59, microsecond=999000)

    rows: list[dict[str, Any]] = []
    store: Optional[UsageEntryStore] = None
    stored_sources: list[str] = []
    if persistent_usage_db_enabled():
        store, stored_sources = _sync_usage_store(CodingToolsUsageTracker())
        rows.extend(store.insight_rows(since=since, until=until))

    live_rows, live_sources = _live_insight_rows(since, until)
    rows.extend(live_rows)

    result: Dict[str, Any] = {
        "schema_version": INSIGHTS_SCHEMA_VERSION,
        "range": window,
        "facets": list(selected),
        # Hour buckets are cut in the server's local zone, so a machine that
        # moves re-buckets its own history. Say which zone produced these.
        "timezone": str(datetime.now().astimezone().tzinfo),
        "coverage": {
            "stored_sources": sorted(stored_sources),
            "live_sources": sorted(live_sources),
            "group_count": len(rows),
        },
        "totals": _rounded(
            {
                "tokens": sum(int(row.get("tokens", 0) or 0) for row in rows),
                "cost": sum(float(row.get("cost", 0.0) or 0.0) for row in rows),
                "messages": sum(int(row.get("messages", 0) or 0) for row in rows),
                "entries": sum(int(row.get("entries", 0) or 0) for row in rows),
            }
        ),
        "timestamp": datetime.now().isoformat(),
    }

    if "hourly" in selected:
        result["hourly"] = _fold_hourly(rows)
    if "weekday" in selected:
        result["weekday"] = _fold_weekday(rows)
    if "heatmap" in selected:
        result["heatmap"] = _fold_heatmap(rows)
    if "daily" in selected:
        result["daily"] = _fold_daily(rows)

    if "models" in selected or "tools" in selected:
        models: dict[str, dict[str, Any]] = defaultdict(_empty_bucket)
        tools: dict[str, dict[str, Any]] = defaultdict(_empty_bucket)
        for row in rows:
            _accumulate(models[row["model"]], row)
            _accumulate(tools[row["source"]], row)
        if "models" in selected:
            ranked = _ranked(models, "model")
            result["models"] = {
                "ranked": ranked,
                # Both rankings, so nothing downstream has to guess which one
                # "favorite" meant (D4).
                "most_used": ranked[0]["model"] if ranked else None,
                "highest_cost": (
                    max(ranked, key=lambda row: row["cost"])["model"] if ranked else None
                ),
            }
        if "tools" in selected:
            result["tools"] = {"ranked": _ranked(tools, "tool")}

    if "streaks" in selected or "firsts" in selected:
        per_day: dict[str, int] = defaultdict(int)
        for row in rows:
            per_day[row["day"]] += int(row.get("tokens", 0) or 0)
        active = sorted(per_day)

        if "streaks" in selected:
            current, longest = contribution_streaks([{"date": day} for day in active])
            span = 0
            if active:
                first = datetime.strptime(active[0], "%Y-%m-%d").date()
                last = datetime.strptime(active[-1], "%Y-%m-%d").date()
                span = (last - first).days + 1
            result["streaks"] = {
                "current_streak": current,
                "longest_streak": longest,
                "active_days": len(active),
                "total_days": span,
            }

        if "firsts" in selected:
            busiest = max(per_day.items(), key=lambda item: item[1]) if per_day else None
            hourly = result.get("hourly") or _fold_hourly(rows)
            result["firsts"] = {
                "first_active_day": active[0] if active else None,
                "last_active_day": active[-1] if active else None,
                "busiest_day": busiest[0] if busiest else None,
                "busiest_day_tokens": busiest[1] if busiest else 0,
                "peak_hour": hourly.get("peak_hour"),
            }

    if "projects" in selected:
        if store is None:
            result["projects"] = {
                "projects": [],
                "unattributed": _empty_bucket(),
                "attributed_project_count": 0,
                "names_included": bool(include_project_names),
                "unavailable_reason": "persistent usage database is disabled",
            }
        else:
            result["projects"] = _fold_projects(
                store, since=since, until=until, include_names=include_project_names
            )

    return result
