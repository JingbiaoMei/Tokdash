"""The one data-access module for ``tokdash tui`` and ``tokdash report``.

Both surfaces reproduce the exact route-level call shapes: same functions, same
cache keys, same fetch closures as ``tokdash.api``. That is the whole point of
this module — identical keys give the TUI the routes' within-process semantics
(single-flight, TTL, stale-while-revalidate, pricing-identity folding, and the
day-pin on open windows) instead of a parallel cache that drifts from the one
the warmers fill. The shared SQLite usage store stays the cross-process cache.

Hard boundary: no textual and no rich import here (``tokdash report`` must stay
headless-safe; only ``tui/app.py`` may pull Textual).

Delegation (round 2): ``api._cache`` is process-local, so a fresh TUI/report
process beside a warm ``tokdash serve`` used to cold-compute answers the service
already had. When a same-version Tokdash answers on the install manifest's port,
the fetch helpers below ask it over read-only HTTP (``tui/remote.py``) and fall
back to the in-process path on ANY doubt — including the whole-suite kill switch
``TOKDASH_TUI_NO_REMOTE=1``, which makes every fetch answer exactly as it did in
round 1. Quota is never delegated; it is always in-process.
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from ..api import (
    REPORT_FACETS,
    CacheBackpressureError,
    _active_time_cache_key,
    _active_time_payload,
    _insights_cache_key,
    _local_today,
    _report_windows,
    _usage_cache_key,
    _window_cache_key,
    get_cached_or_fetch,
)
from ..compute import (
    compute_stats,
    compute_usage_with_comparison,
    period_is_recognized,
    resolve_period,
)
from ..dateutil import parse_date_range
from ..insights import compute_insights
from ..usage_store import (
    UsageDatabaseSchemaTooNewError,
    UsageEntryStore,
    persistent_usage_db_enabled,
    raise_if_usage_db_incompatible,
)
from . import remote as _remote

OVERVIEW_PERIODS: tuple[str, ...] = ("today", "week", "month", "year", "all")  # TUI cycle order
REPORT_PERIODS: tuple[str, ...] = ("week", "month", "year")  # index-aligned with _report_windows()
# Overview period keys (round 2): the calendar tokens replaced the rolling "7d"
# so Overview, Report, the web, and the server's warmed keys all mean the same
# week/month/year. resolve_overview_period does the translation.
PERIOD_KEYS: dict[str, str] = {"t": "today", "w": "week", "m": "month", "y": "year", "a": "all"}


@dataclass(frozen=True)
class FetchOutcome:
    """A cache answer with its freshness attached, so a UI can show "stale"
    instead of silently painting an old snapshot (web parity)."""

    value: dict[str, Any]
    status: str  # "hit" | "stale" | "recomputed" (CacheFetchResult.status)
    age_seconds: float | None


def _outcome(result: Any) -> FetchOutcome:
    """Adapt a CacheFetchResult (return_metadata=True) to a FetchOutcome."""
    return FetchOutcome(value=result.value, status=result.status, age_seconds=result.age_seconds)


def _with_backpressure_retry(fn: Callable[[], FetchOutcome]) -> FetchOutcome:
    """Run ``fn``, absorbing CacheBackpressureError the way the web does.

    Mirrors ``fetchJsonWithRetry`` in index.html: 3 total attempts, delay =
    450 ms * (attempt + 1) + up to 250 ms of jitter. Only the transient 503
    class is retried — UsageDatabaseSchemaTooNewError is terminal by design and
    a ValueError is a caller bug; sleeping on either would just hide it.
    """
    attempts = 3
    for attempt in range(attempts):
        try:
            return fn()
        except CacheBackpressureError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.45 * (attempt + 1) + random.uniform(0.0, 0.25))
    raise AssertionError("unreachable: the loop always returns or raises")


# ---------------------------------------------------------------------------
# Remote delegation state (module-global; one probe per run, not per fetch)
# ---------------------------------------------------------------------------

_REMOTE_LOCK = threading.Lock()
_REMOTE: Any = "unchecked"  # "unchecked" | RemoteService | "gone"


def _remote_service(force_reprobe: bool = False) -> Any:
    """The cached service handle, or None for "delegate nothing".

    The probe verdict latches: a RemoteService (or "gone") is probed once.
    "gone" re-probes only on a forced path (``r`` — refresh re-probes the way a
    dead service deserves a second look per run of the Refresh button, not per
    cache key); reset_remote() clears it fully.
    """
    global _REMOTE
    with _REMOTE_LOCK:
        if _REMOTE == "unchecked" or (force_reprobe and _REMOTE == "gone"):
            _REMOTE = _remote.probe_service() or "gone"
        return _REMOTE if isinstance(_REMOTE, _remote.RemoteService) else None


def _latch_remote_gone() -> None:
    global _REMOTE
    with _REMOTE_LOCK:
        _REMOTE = "gone"


def reset_remote() -> None:
    """Forget the probe verdict → back to "unchecked" (tests, and day one-shot)."""
    global _REMOTE
    with _REMOTE_LOCK:
        _REMOTE = "unchecked"


def _fetch_remote_or_local(
    route_path: str,
    params: dict[str, Any],
    key: str,
    local_closure: Callable[[], Any],
    *,
    refresh: bool,
    remote: bool,
) -> FetchOutcome:
    """Remote-first fetch with an in-process fallback that is byte-identical to round 1.

    ``key``/``local_closure`` are exactly what the FastAPI route hands
    ``get_cached_or_fetch`` — the fallback keeps the round-1 keys and closures
    bit-for-bit when the service is absent (kill switch, down, version skew).
    A ServiceGone answer latches "gone" for the run and falls through to local
    in the SAME attempt (the local closure runs at most once per call).
    The server's 503 raises the same ``CacheBackpressureError`` the in-process
    fill raises, so the web-parity retry below covers both rides verbatim.
    """

    def _once() -> FetchOutcome:
        if remote:
            svc = _remote_service(force_reprobe=refresh)
            if svc is not None:
                try:
                    payload, meta = _remote.remote_get(svc, route_path, params)
                except _remote.ServiceGone:
                    _latch_remote_gone()  # fall through to in-process below
                else:
                    # Route-injected metadata (``_response_cache_metadata``) or
                    # None on non-injecting routes (/api/stats) → synthesize
                    # "hit" with no age.
                    status = str(meta.get("status") or "hit") if meta else "hit"
                    age = meta.get("age_seconds") if meta else None
                    return FetchOutcome(value=payload, status=status, age_seconds=age)
        return _outcome(
            get_cached_or_fetch(
                key,
                local_closure,
                force_refresh=refresh,
                return_metadata=True,
            )
        )

    return _with_backpressure_retry(_once)


def ensure_usage_db_compatible() -> None:
    """Call once before UI/compute starts. Raises UsageDatabaseSchemaTooNewError.

    No-op unless the persistent DB is enabled: under TOKDASH_USAGE_DB=0 the
    store is never consulted, and constructing UsageEntryStore would mkdir the
    DB parent — a filesystem mutation forbidden in read-only mode (same rule
    ``sources/quota`` follows).
    """
    if persistent_usage_db_enabled():
        raise_if_usage_db_incompatible()


def resolve_report_period(
    period: str, *, today: date | None = None
) -> tuple[str, str | None, str | None]:
    """Map a user period token to the (period, date_from, date_to) triple the
    report / TUI-Report surfaces fetch with — the exact shape
    ``_report_warm_targets`` builds, so keys collide with the server's warmed
    report key families.

    week/month/year mean the CALENDAR report windows (Monday week / month-1st /
    jan-1st, all ending today), NOT compute's rolling month/year tokens; the
    route-period becomes "today" for those, matching the warmer. Everything else
    that compute recognizes passes through untouched. The cli.py parse-time
    guard normally fires before the ValueError can.
    """
    token = str(period or "").strip().lower()
    if token in REPORT_PERIODS:
        date_from, date_to = _report_windows(today or _local_today())[REPORT_PERIODS.index(token)]
        return ("today", date_from, date_to)
    if period_is_recognized(token):
        return (token, None, None)
    raise ValueError(
        f"Unknown period {period!r}; use today, week, month, year, all, "
        f"N days as an integer, or Nd/Nw/Nm/Ny shorthand"
    )


def resolve_overview_period(
    token: str, *, today: date | None = None
) -> tuple[str, str | None, str | None]:
    """Map an Overview period token to the (period, date_from, date_to) triple it
    fetches with — the pairs are EQUAL to the warmer's inputs, on purpose.

    "today" becomes the explicit (D, D) pair ``_usage_warm_target(D)`` warms;
    week/month/year become the CALENDAR windows (Monday week / month-1st /
    jan-1st, ending today) straight out of ``_report_windows`` — the same pairs
    ``_report_warm_targets`` builds usage keys from. So an Overview period
    change lands on a key the warm server has already filled (remote) or the
    same day-pinned key round 1 used (in-process), never a key nobody warmed.
    Everything else ("all", "Nd", "7d", ints) passes through: (token, None, None),
    computed as the rolling period it names.
    """
    token = str(token or "").strip().lower()
    day = today or _local_today()
    if token == "today":
        return ("today", day.isoformat(), day.isoformat())
    if token in REPORT_PERIODS:
        date_from, date_to = _report_windows(day)[REPORT_PERIODS.index(token)]
        return ("today", date_from, date_to)
    return (token, None, None)


def fetch_usage(
    period: str,
    date_from: str | None = None,
    date_to: str | None = None,
    *,
    refresh: bool = False,
    remote: bool = True,
) -> FetchOutcome:
    params: dict[str, Any] = {"period": period, "date_from": date_from, "date_to": date_to}
    if refresh:
        params["refresh"] = True  # /api/usage accepts it
    return _fetch_remote_or_local(
        "/api/usage",
        params,
        _usage_cache_key(period, date_from, date_to),
        lambda: compute_usage_with_comparison(period, date_from, date_to),
        refresh=refresh,
        remote=remote,
    )


def fetch_active_time(
    period: str = "today",
    date_from: str | None = None,
    date_to: str | None = None,
    include_review_sessions: bool | None = None,
    *,
    refresh: bool = False,
    remote: bool = True,
) -> FetchOutcome:
    # Must go through _active_time_payload, not get_active_time_data directly:
    # the payload carries the route-only "range" augmentation (the warmer
    # divergence the codebase already hit once — api.py:325 docstring).
    params: dict[str, Any] = {"period": period, "date_from": date_from, "date_to": date_to}
    if include_review_sessions is not None:
        params["include_review_sessions"] = include_review_sessions  # None → omit (route default)
    if refresh:
        params["refresh"] = True
    return _fetch_remote_or_local(
        "/api/active-time",
        params,
        _active_time_cache_key(period, date_from, date_to, include_review_sessions),
        lambda: _active_time_payload(period, date_from, date_to, include_review_sessions),
        refresh=refresh,
        remote=remote,
    )


def fetch_insights(
    period: str = "year",
    date_from: str | None = None,
    date_to: str | None = None,
    facets: str = REPORT_FACETS,
    include_project_names: bool = True,
    *,
    refresh: bool = False,
    remote: bool = True,
) -> FetchOutcome:
    # Callers pass REPORT_FACETS verbatim: facet-string ORDER is part of the
    # key (api.py:445-449), so a reordered list is a key nobody warmed. Report
    # windows pass period="year" (the route default the warmer primes), never
    # the user's period token — and include_project_names=True, exactly the
    # warmed report triple.
    params: dict[str, Any] = {
        "period": period,
        "date_from": date_from,
        "date_to": date_to,
        "facets": facets,
        "include_project_names": include_project_names,
    }
    if refresh:
        params["refresh"] = True
    return _fetch_remote_or_local(
        "/api/insights",
        params,
        _insights_cache_key(period, date_from, date_to, facets, include_project_names),
        lambda: compute_insights(
            period,
            date_from,
            date_to,
            facets=facets,
            include_project_names=include_project_names,
        ),
        refresh=refresh,
        remote=remote,
    )


def fetch_stats(year: int | None = None, *, refresh: bool = False, remote: bool = True) -> FetchOutcome:
    # The key is byte-for-byte the /api/stats route expression (api.py:2330),
    # ``if year`` — not ``is not None`` — so year=0 behaves as None there too.
    # None means TRAILING 365 days; label it that way, never "this year".
    # The route has NO refresh param, so the remote call never sends one (a
    # stale answer here is the route's own contract, not a bug); local keeps
    # force_refresh=refresh for the in-process Refresh button.
    params: dict[str, Any] = {"year": year}
    return _fetch_remote_or_local(
        "/api/stats",
        params,
        _window_cache_key(f"stats_{year}", None, f"{year}-12-31" if year else None),
        lambda: compute_stats(year),
        refresh=refresh,
        remote=remote,
    )


def fetch_quota_state(*, refresh: bool = False) -> FetchOutcome:
    """Subscription quota state — ALWAYS in-process, never delegated (locked).

    Key literal "quota_state", byte-for-byte the /api/quota route's
    ``_cached_route("/api/quota", "quota_state", quota_state)``. The lazy
    import keeps the collector stack off the Overview/startup path.
    """

    def _local_fetch() -> FetchOutcome:
        from ..sources.quota import quota_state  # lazy: only the Quota tab pays it

        return _outcome(
            get_cached_or_fetch(
                "quota_state",
                quota_state,
                force_refresh=refresh,
                return_metadata=True,
            )
        )

    return _with_backpressure_retry(_local_fetch)


def fetch_quota_history(*, hours: int = 24, now_ts: float | None = None) -> FetchOutcome:
    """24 h (by default) of quota snapshots — DB-only, ALWAYS in-process.

    Mirrors /api/quota/history's call shape (route defaults: providers=None,
    granularity="hour", max_points=300, and the codex network-only gate via
    ``network_enabled("codex_api")``). Not cached — like the route. With the
    persistent DB disabled there is nowhere to read snapshots from, and
    constructing UsageEntryStore would mkdir the DB parent, the mutation
    round-1's db_summary already refuses: answer an empty series, build nothing.
    """
    end = int(now_ts if now_ts is not None else time.time())
    start = end - int(hours) * 3600
    if not persistent_usage_db_enabled():
        return FetchOutcome(value={"series": []}, status="hit", age_seconds=None)
    from ..sources.quota.config import network_enabled  # lazy, mirrors the route

    network_only_providers = {"codex"} if network_enabled("codex_api") else set()
    series = UsageEntryStore().quota_history(
        providers=None,
        granularity="hour",
        start=start,
        end=end,
        max_points=300,
        network_only_providers=network_only_providers,
    )
    return FetchOutcome(value=series, status="hit", age_seconds=None)


def report_windows(today: date | None = None) -> list[tuple[str, str]]:
    """The Report tab's three calendar-aligned windows, ending on ``today``."""
    return _report_windows(today or _local_today())


def db_summary() -> str:
    """One-line db status for the TUI status bar / report footer.

    Constructing the store here is acceptable: it only runs when the persistent
    DB is enabled, and mkdir of the parent is then normal runtime behavior.
    """
    if not persistent_usage_db_enabled():
        return "usage db disabled (TOKDASH_USAGE_DB=0) — live parsing"
    store = UsageEntryStore()
    st = store.status()
    return f"{st['path']} · {st['usage_entries']} usage rows"
