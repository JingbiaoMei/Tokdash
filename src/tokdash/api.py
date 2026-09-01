from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import math
import os
import secrets
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .assets import (
    NO_CACHE_HEADERS,
    STATIC_DIR,
    SW_CACHE_NAME_PLACEHOLDER,
    get_static_cache_name,
)
from .compute import (
    compute_stats,
    compute_usage_with_comparison,
    get_openclaw_data,
    get_tools_data,
    resolve_period,
)
from .dateutil import parse_date_range
from .insights import UnknownFacetError, compute_insights
from .usage_store import SCHEMA_VERSION as USAGE_DB_SCHEMA_VERSION
from .usage_store import UsageDatabaseSchemaTooNewError
from .sessions import (
    SESSION_TOOLS,
    get_active_time_data,
    get_codex_activity_insights,
    get_codex_session_detail,
    get_codex_sessions_data,
    get_session_detail,
    get_sessions_data,
    reload_pricing_db,
)


PRICING_DB_PATH = Path(__file__).parent / "pricing_db.json"
logger = logging.getLogger(__name__)
BASE_PATH_PLACEHOLDER = "__TOKDASH_BASE_PATH__"
SUPPORTED_BASE_PATHS = ("/tokdash",)
ACTIVITY_INSIGHTS_CACHE_KEY = "activity_insights_v1"


def _normalize_public_base_path(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw or raw == "/":
        return ""
    if not raw.startswith("/"):
        raw = "/" + raw
    return raw.rstrip("/")


def _request_base_path(request: Optional[Request]) -> str:
    """Resolve the public URL prefix used for generated browser assets.

    Tailscale Serve's `--set-path=/tokdash` strips the prefix before proxying to
    Tokdash, so the HTML shell usually cannot infer it from the backend request path.
    The dashboard therefore also detects `/tokdash` in `window.location`. Manifest and
    service-worker requests pass `?base=/tokdash` so those generated assets can use the
    same public prefix without requiring service-level environment configuration.
    """
    if request is not None:
        query_base = _normalize_public_base_path(request.query_params.get("base"))
        if query_base:
            return query_base
        header_base = _normalize_public_base_path(
            request.headers.get("x-forwarded-prefix") or request.headers.get("x-script-name")
        )
        if header_base:
            return header_base
    return _normalize_public_base_path(os.environ.get("TOKDASH_PUBLIC_BASE_PATH"))


def _with_base_path(base_path: str, path: str) -> str:
    return f"{base_path}{path}" if base_path else path


def _validate_date_params(date_from: Optional[str], date_to: Optional[str]) -> None:
    """Raise HTTPException(400) if date params are malformed or incomplete."""
    if bool(date_from) != bool(date_to):
        raise HTTPException(status_code=400, detail="Both date_from and date_to are required")
    if date_from and date_to:
        try:
            parse_date_range(date_from, date_to)
        except ValueError as exc:
            detail = str(exc) or "Invalid date format, expected YYYY-MM-DD"
            if "does not match format" in detail:
                detail = "Invalid date format, expected YYYY-MM-DD"
            raise HTTPException(status_code=400, detail=detail)


class NoCacheMiddleware:
    """ASGI middleware that adds no-cache headers to /static/ responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/static/"):
            await self.app(scope, receive, send)
            return

        async def send_with_no_cache(message):
            if message["type"] == "http.response.start":
                headers = dict(message.get("headers", []))
                for k, v in NO_CACHE_HEADERS.items():
                    headers[k.lower().encode()] = v.encode()
                message["headers"] = list(headers.items())
            await send(message)

        await self.app(scope, receive, send_with_no_cache)


class BasePathMiddleware:
    """Let the local app answer under known public prefixes such as /tokdash."""

    def __init__(self, app: ASGIApp, base_paths: tuple[str, ...]) -> None:
        self.app = app
        self.base_paths = tuple(p for p in base_paths if p)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        for base_path in self.base_paths:
            if path == base_path or path.startswith(base_path + "/"):
                new_scope = dict(scope)
                stripped = path[len(base_path):] or "/"
                new_scope["path"] = stripped
                new_scope["root_path"] = (scope.get("root_path") or "") + base_path
                await self.app(new_scope, receive, send)
                return
        await self.app(scope, receive, send)


def _tailnet_dns_suffix(hostname: str | None) -> str | None:
    """Return ``<tailnet>.ts.net`` for a Tailscale MagicDNS hostname."""
    labels = (hostname or "").strip().lower().rstrip(".").split(".")
    if len(labels) < 4 or labels[-2:] != ["ts", "net"] or not all(labels):
        return None
    return ".".join(labels[-3:])


def _same_tailnet_https_origin(origin: str, request_host: str) -> bool:
    """Whether an HTTPS browser origin and request Host belong to one tailnet.

    Tailscale Serve preserves the public MagicDNS name in ``Host``. Matching the
    complete tailnet suffix permits cross-machine dashboard reads without opening
    CORS to another ``*.ts.net`` tailnet.
    """
    try:
        origin_url = urlsplit(origin)
        request_url = urlsplit(f"//{request_host}")
        if (
            origin_url.scheme.lower() != "https"
            or not origin_url.hostname
            or origin_url.username is not None
            or origin_url.password is not None
            or request_url.username is not None
            or request_url.password is not None
            or origin_url.path not in ("", "/")
            or origin_url.query
            or origin_url.fragment
        ):
            return False
        # Accessing .port validates malformed and out-of-range port strings.
        origin_url.port
        request_url.port
        origin_suffix = _tailnet_dns_suffix(origin_url.hostname)
        request_suffix = _tailnet_dns_suffix(request_url.hostname)
    except ValueError:
        return False
    return origin_suffix is not None and origin_suffix == request_suffix


class TailnetCORSMiddleware(CORSMiddleware):
    """Add same-tailnet HTTPS origins to the default CORS policy.

    ``CORSMiddleware.is_allowed_origin`` does not receive the request scope. A
    context variable carries the current request Host safely across concurrent
    async requests so the decision can compare both MagicDNS suffixes.
    """

    def __init__(self, app: ASGIApp, *, allow_same_tailnet: bool = False, **kwargs) -> None:
        super().__init__(app, **kwargs)
        self.allow_same_tailnet = allow_same_tailnet
        self._request_host: ContextVar[str] = ContextVar("tokdash_cors_request_host", default="")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.allow_same_tailnet:
            await super().__call__(scope, receive, send)
            return
        token = self._request_host.set(Headers(scope=scope).get("host", ""))
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._request_host.reset(token)

    def is_allowed_origin(self, origin: str) -> bool:
        return super().is_allowed_origin(origin) or (
            self.allow_same_tailnet
            and _same_tailnet_https_origin(origin, self._request_host.get())
        )


def _local_today() -> date:
    """Today in the machine's local timezone, matching the dashboard's date picker.

    The single clock for both halves of the window rule, so the stamp a key carries and
    the open/closed test that decides whether to stamp it can never disagree.
    """
    return datetime.now().astimezone().date()


def _usage_window_is_open(date_from: Optional[str], date_to: Optional[str]) -> bool:
    """True while the requested window can still gain usage.

    A period-only query ("today", "7", ...) is resolved against the clock on every
    compute, so it is always open. An explicit range stays open until its last day has
    passed. An unparseable date counts as open, so a bad value can only cost a
    recompute and never serves incomplete data.

    The comparison is on parsed dates, not on the strings: ``strptime`` accepts an
    unpadded ``2026-9-1``, which sorts after ``2026-10-01`` lexically and would keep a
    long-closed window recomputing every day.
    """
    if not date_to:
        return True
    try:
        end = datetime.strptime(date_to, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return True
    return end >= _local_today()


def _day_scoped_key(base: str) -> str:
    """Pin a key to the local day it was computed on."""
    return f"{base}_asof_{_local_today().isoformat()}"


def _window_cache_key(base: str, date_from: Optional[str], date_to: Optional[str]) -> str:
    """Pricing-aware cache key that also pins an OPEN window to its capture day.

    A day's usage is only complete once the day is over, and the response cache serves a
    stale entry with no upper bound on its age. The dashboard requests every quick range
    as an explicit date_from/date_to pair, so viewing "Today" on day D and clicking
    "Yesterday" on day D+1 build the same key: without this scope the second request is
    answered from the partial mid-day snapshot the first one left behind, and only the
    Refresh button (force_refresh) recomputes it.

    Stamping the capture day into open-window keys means a key WITHOUT the stamp can only
    have been filled after its window closed. Past ranges are therefore always complete,
    and stay cacheable indefinitely because their inputs no longer change.
    """
    if _usage_window_is_open(date_from, date_to):
        base = _day_scoped_key(base)
    return _pricing_cache_key(base)


def _session_response_cache_key(
    tool: str,
    period: str,
    date_from: Optional[str],
    date_to: Optional[str],
    include_review_sessions: Optional[bool],
) -> str:
    return _window_cache_key(
        f"sessions_{tool.strip().lower()}_{period}_{date_from}_{date_to}_{include_review_sessions}",
        date_from,
        date_to,
    )


def _active_time_cache_key(
    period: str,
    date_from: Optional[str],
    date_to: Optional[str],
    include_review_sessions: Optional[bool],
) -> str:
    return _window_cache_key(
        f"active_time_{period}_{date_from}_{date_to}_{include_review_sessions}",
        date_from,
        date_to,
    )


def _warm_caches() -> None:
    """Best-effort background warm so the first user request hits hot caches.

    Populates the parser caches (coding_tools._entry_cache, openclaw._ENTRY_CACHE)
    and the API response cache for the dashboard's initial loads — the exact Overview
    date range, Stats, and each Sessions tool panel. The period-only Today usage key
    is intentionally not warmed: the dashboard never requests it, and computing both
    forms used to duplicate the largest startup aggregation.

    A foreground request for the key currently being warmed may join that one fill;
    see ``get_cached_or_fetch``. This keeps the browser's first load from racing the
    warmer into a transient 503 without relaxing the normal backpressure policy.
    Disable with TOKDASH_WARM_ON_START=0.
    Failures are swallowed; warming must never crash `serve`.
    """
    today = _local_today().isoformat()
    # Composed by name, not by slicing _day_warm_targets(): the warm order is the order
    # the dashboard needs these in, and stats belongs second, immediately behind the
    # Overview usage key. Reordering the per-day targets must not silently demote it.
    warmers = [
        _usage_warm_target(today),
        (_window_cache_key("stats_None", None, None), lambda: compute_stats(None)),
        *_session_warm_targets(today),
        (_day_scoped_key(ACTIVITY_INSIGHTS_CACHE_KEY), get_codex_activity_insights),
    ]
    _run_warmers(warmers)


def _day_warm_targets(day: str) -> list:
    """Every (key, fetch) pair the dashboard asks for with ``day`` as the whole range."""
    return [_usage_warm_target(day), *_session_warm_targets(day)]


def _usage_warm_target(day: str):
    """The Overview usage pair for ``day``.

    Mirror the dashboard exactly: its date picker sends an explicit date_from/date_to
    pair, not period=today. Building these through the same key helpers the routes use
    is what stops the warmer and the browser from drifting apart.
    """
    return (
        _window_cache_key(f"usage_today_{day}_{day}", day, day),
        lambda: compute_usage_with_comparison("today", day, day),
    )


def _session_warm_targets(day: str) -> list:
    """The per-tool Sessions pairs for ``day``, plus the cross-tool active time."""
    targets: list = []
    for tool in SESSION_TOOLS:
        targets.append(
            (
                _session_response_cache_key(tool, "today", day, day, None),
                lambda tool=tool: get_sessions_data(
                    tool, "today", day, day, include_review_sessions=None
                ),
            )
        )
    targets.append(
        (
            _active_time_cache_key("today", day, day, None),
            lambda: get_active_time_data("today", day, day, include_review_sessions=None),
        )
    )
    return targets


def _run_warmers(warmers, *, join_seconds: Optional[float] = None) -> None:
    for key, fetch in warmers:
        warm_event = _begin_startup_warm(key, join_seconds)
        try:
            get_cached_or_fetch(key, fetch, _startup_warm=True)
        except Exception:
            pass
        finally:
            _finish_startup_warm(key, warm_event)


def _warm_previous_day() -> None:
    """Warm the day that just ended, shortly after the local date rolls over.

    Every open-window key goes cold at midnight, and the day that just closed is the
    one the Yesterday button asks for all day. Its numbers are final, so this is
    computed once and then served from cache for the rest of the day. Today is
    deliberately NOT warmed here: at 00:05 it holds almost nothing, and warming it
    would put a near-empty snapshot in front of the morning's first request.
    """
    yesterday = (_local_today() - timedelta(days=1)).isoformat()
    # A shorter join than the startup warm: this one runs while the server is live, so a
    # racing request pays the join before it learns anything. The two waits rarely stack
    # — a joiner that times out re-enters with _startup_waited=True and, while the warm
    # still holds the key lock, is refused immediately as same_key_inflight without ever
    # reaching _acquire_compute_slot; they compound only if the warm released that lock
    # inside the window having stored nothing, i.e. it raised. The join alone is reason
    # enough to shorten it: 10s-to-503 beats 30s-to-503 on a live server.
    _run_warmers(_day_warm_targets(yesterday), join_seconds=DAILY_WARM_JOIN_SECONDS)


def _daily_warm_minute() -> int:
    """Minutes past local midnight for the daily warm, default 5.

    ``0`` is a legitimate setting (warm exactly at midnight), so this cannot use
    ``_positive_int_env``, which would silently turn it back into the default. A value
    outside a day falls back to the default rather than wrapping into an unrelated time.
    """
    minute = _non_negative_int_env("TOKDASH_DAILY_WARM_MINUTE", _DEFAULT_DAILY_WARM_MINUTE)
    return minute if minute < 24 * 60 else _DEFAULT_DAILY_WARM_MINUTE


def _seconds_until_daily_warm(now: Optional[datetime] = None) -> float:
    """Seconds until the next local warm time (default 00:05)."""
    current = now or datetime.now().astimezone()
    minute = _daily_warm_minute()
    target = current.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=minute)
    if target <= current:
        target += timedelta(days=1)
    return max(1.0, (target - current).total_seconds())


def _daily_warm_loop() -> None:
    while True:
        time.sleep(_seconds_until_daily_warm())
        try:
            _warm_previous_day()
        except Exception:  # pragma: no cover - a warm failure must never kill the loop
            logger.debug("tokdash daily warm failed", exc_info=True)


@asynccontextmanager
async def _lifespan(_app: "FastAPI"):
    if os.environ.get("TOKDASH_WARM_ON_START", "1") != "0":
        threading.Thread(target=_warm_caches, name="tokdash-warm", daemon=True).start()
    if os.environ.get("TOKDASH_DAILY_WARM", "1") != "0":
        threading.Thread(target=_daily_warm_loop, name="tokdash-daily-warm", daemon=True).start()
    yield


app = FastAPI(title="Tokdash", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.add_middleware(NoCacheMiddleware)


cors_allow_origins = [o.strip() for o in os.environ.get("TOKDASH_ALLOW_ORIGINS", "").split(",") if o.strip()]
cors_allow_origin_regex = os.environ.get("TOKDASH_ALLOW_ORIGIN_REGEX", "").strip() or None
cors_allow_same_tailnet = not cors_allow_origins and cors_allow_origin_regex is None
if cors_allow_same_tailnet:
    cors_allow_origin_regex = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"

app.add_middleware(
    TailnetCORSMiddleware,
    allow_origins=cors_allow_origins,
    allow_origin_regex=cors_allow_origin_regex,
    allow_same_tailnet=cors_allow_same_tailnet,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(BasePathMiddleware, base_paths=SUPPORTED_BASE_PATHS)


# --- Local write protection (Phase 0a) -------------------------------------------
# The local API is unauthenticated, so every state-changing request must clear a gate
# before it reaches a handler: the server must be bound to loopback, the Host (and any
# Origin/Referer) must be a loopback address, and a per-process token must match. This
# blocks CSRF from a page the user visits AND writes arriving through Tailscale Serve
# (which forwards from 127.0.0.1 but carries the tailnet hostname as Host and an https
# Origin — both rejected). An `ssh -L` forward to localhost is deliberately different: it
# preserves a loopback Host, so the SSH-authenticated user keeps write access by design
# (SSH itself is the auth layer). It fails closed: an unknown bind is treated as non-loopback.
#
# The token is intentionally per-process. With uvicorn --workers, each worker has its own
# token; the dashboard fetches /api/csrf-token immediately before a write and browsers usually
# reuse the same HTTP connection for the following PUT/POST. A client that gets a 403 after a
# worker switch should fetch a new token and retry.
_CSRF_TOKEN = secrets.token_urlsafe(32)
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _is_loopback(addr: str) -> bool:
    addr = (addr or "").strip().lower()
    if addr == "localhost":
        return True
    # Strip brackets from IPv6 literals like "[::1]" before parsing.
    candidate = addr[1:-1] if addr.startswith("[") and addr.endswith("]") else addr
    try:
        # Parse as an IP so only the real 127.0.0.0/8 and ::1 loopback ranges match. A prefix
        # check like addr.startswith("127.") would wrongly accept "127.0.0.1.evil.com".
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def _effective_bind() -> str:
    # serve() sets app.state before uvicorn.run; fall back to env, else "" (fail closed).
    return getattr(app.state, "bind", None) or os.environ.get("TOKDASH_HOST", "")


def _effective_port() -> int:
    port = getattr(app.state, "port", None)
    if port:
        return int(port)
    try:
        return int(os.environ.get("TOKDASH_PORT", "55423"))
    except ValueError:
        return 55423


def _host_allowlist(port: int) -> "set[str]":
    # The Host header carries no scheme, so this is netloc-only. The dashboard's own
    # requests always carry the explicit port (127.0.0.1:55423); a bare port-less Host is
    # legitimate ONLY when the server runs on :80 (Tokdash serves plain HTTP on loopback,
    # so :80 is the only implicit-port case). Adding bare forms unconditionally would let a
    # page served at http://localhost (:80) clear the gate (a real CSRF hole).
    allow: "set[str]" = set()
    for host in ("127.0.0.1", "localhost", "[::1]"):
        allow.add(f"{host}:{port}")
        if port == 80:
            allow.add(host)
    return allow


def _origin_allowlist(port: int) -> "set[str]":
    # Origin/Referer carry a scheme, so these are full origins and HTTP-only — comparing
    # netloc alone would accept https://localhost for an HTTP server on :80 (and vice
    # versa). Tokdash never serves TLS, so only http:// origins are same-origin.
    allow: "set[str]" = set()
    for host in ("127.0.0.1", "localhost", "[::1]"):
        allow.add(f"http://{host}:{port}")
        if port == 80:
            allow.add(f"http://{host}")
    return allow


def _origin_value(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        # urlsplit raises ValueError on malformed input (e.g. "http://[" — "Invalid IPv6
        # URL"). A bad Referer must fail CLOSED: return "" so it can't match the allowlist
        # and the gate yields 403, never a 500 bubbling out of the write guard (the gate's
        # "never 500 / fail-closed" invariant). Attacker-reachable on unauthenticated routes.
        return ""
    return f"{parts.scheme}://{parts.netloc}".strip().lower()


def _origin_denied(headers, origin_allow: "set[str]") -> Optional[str]:
    """Reject a cross-origin Origin (or, absent Origin, a cross-origin Referer).

    Scheme-aware: the value must match a full ``scheme://host[:port]`` in the allowlist.
    """
    origin = headers.get("origin")
    if origin and origin.strip().lower() not in origin_allow:
        return "Cross-origin request rejected."
    referer = headers.get("referer")
    if not origin and referer and _origin_value(referer) not in origin_allow:
        return "Cross-origin referer rejected."
    return None


def mutation_denied_reason(
    method: str, headers, *, bind: Optional[str] = None, port: Optional[int] = None
) -> Optional[str]:
    """Return why a state-changing request is denied, or None if allowed.

    Pure and dependency-free (takes a header mapping, not a Request) so it is
    unit-testable without an ASGI client.
    """
    if method.upper() not in _MUTATING_METHODS:
        return None
    bind = bind if bind is not None else _effective_bind()
    if not _is_loopback(bind):
        return "Tokdash is not bound to loopback; write endpoints are disabled. Bind 127.0.0.1 to make changes."
    port = port if port is not None else _effective_port()
    allow = _host_allowlist(port)
    host = (headers.get("host") or "").strip().lower()
    if host not in allow:
        return "Host header is not a recognized loopback address."
    cross = _origin_denied(headers, _origin_allowlist(port))
    if cross:
        return cross
    token = headers.get("x-tokdash-token", "")
    try:
        # compare_digest raises TypeError on non-ASCII str operands; a header decoded as
        # latin-1 can carry such bytes. Treat that as a normal mismatch (403), never a 500.
        ok = bool(token) and secrets.compare_digest(token, _CSRF_TOKEN)
    except TypeError:
        ok = False
    if not ok:
        return "Missing or invalid Tokdash write token."
    return None


@app.middleware("http")
async def _write_guard(request: Request, call_next):
    reason = mutation_denied_reason(request.method, request.headers)
    if reason is not None:
        return JSONResponse({"detail": reason}, status_code=403)
    return await call_next(request)


_cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()
_cache_guard = threading.Lock()  # protects _cache, _key_locks, and _cache_epoch
_key_locks: OrderedDict[str, threading.Lock] = OrderedDict()
_cache_epoch = 0
_pricing_sig_guard = threading.Lock()
_pricing_baseline_sig_cache: Optional[tuple[str, tuple[str, int, int]]] = None
_pricing_override_sig_cache: Optional[tuple[str, int, int, str]] = None
_quota_refresh_guard = threading.Lock()
_quota_last_refresh_monotonic = 0.0
_quota_prev_refresh_monotonic = 0.0
_QUOTA_REFRESH_COOLDOWN_SECONDS = 60.0


def _positive_int_env(name: str, default: int) -> int:
    """Read a positive integer env var, falling back on bad or empty values."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


# Keep the default comfortably above the dashboard's 5-minute auto-refresh so a
# scheduled refresh does not always land on an expired key and compete for a cold
# parse slot. Operators can still lower it with TOKDASH_CACHE_TTL.
CACHE_TTL = _positive_int_env("TOKDASH_CACHE_TTL", 600)  # seconds
CACHE_MAX_ENTRIES = _positive_int_env("TOKDASH_CACHE_MAX_ENTRIES", 256)
STARTUP_WARM_JOIN_SECONDS = _positive_int_env("TOKDASH_STARTUP_WARM_JOIN_SECONDS", 30)
# The daily rollover warm runs on a live server, so its join budget is deliberately
# shorter than the startup one; see _warm_previous_day.
DAILY_WARM_JOIN_SECONDS = _positive_int_env("TOKDASH_DAILY_WARM_JOIN_SECONDS", 10)
_DEFAULT_DAILY_WARM_MINUTE = 5  # minutes past local midnight

# A startup warmer is the one safe exception to cold-miss fail-fast: the automatic
# browser load is asking for work that is already running on its behalf. Let at most
# one foreground request per key wait for that result so a tab storm still cannot
# occupy the AnyIO worker pool. All ordinary same-key misses retain the old 503 path.
_startup_warm_guard = threading.Lock()
_startup_warm_events: dict[str, tuple[threading.Event, float]] = {}
_startup_warm_waiters: set[str] = set()


class CacheBackpressureError(RuntimeError):
    """Raised when a cold cache fill would block request workers under load."""


@dataclass(frozen=True)
class CacheFetchResult:
    value: Any
    status: str
    age_seconds: Optional[float]

    @property
    def served_from_cache(self) -> bool:
        return self.status in {"hit", "stale"}


def _non_negative_int_env(name: str, default: int) -> int:
    """Read a non-negative integer env var. Unlike ``_positive_int_env`` an explicit
    ``0`` is honoured, so a knob whose whole point is "none" stays reachable."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _bounded_float_env(name: str, default: float, *, maximum: float) -> float:
    """Read a positive, FINITE float env var, clamped to ``maximum``.

    ``float("inf")`` parses, and an unbounded wait would park a worker thread for the
    life of the process — the exact failure the compute cap exists to prevent.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return min(default, maximum)
    try:
        value = float(raw)
    except ValueError:
        return min(default, maximum)
    if not math.isfinite(value) or value <= 0:
        return min(default, maximum)
    return min(value, maximum)


def _parse_cpu_max(path: Path) -> Optional[int]:
    """Whole CPUs from a cgroup v2 ``cpu.max``, or None when unlimited or unreadable."""
    try:
        fields = path.read_text(encoding="utf-8").split()
    except OSError:
        return None
    if not fields or fields[0] == "max":
        return None
    try:
        quota = int(fields[0])
        period = int(fields[1]) if len(fields) > 1 else 100000
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        return None
    return max(1, quota // period)


def _cgroup_v2_cpu_max_paths(root: Path, proc_self_cgroup: Path) -> list:
    """``cpu.max`` candidates from this process's own cgroup up to the root.

    A container's cgroup is normally namespaced, so the root IS its limit. A systemd
    unit with ``CPUQuota=`` is not: it sits in a sub-cgroup such as
    ``/system.slice/tokdash.service`` whose limit the root's ``cpu.max`` never shows —
    and ``tokdash setup`` installs exactly such a unit. Any ancestor can also cap the
    leaf, so every level between the two is a candidate.
    """
    paths = [root / "cpu.max"]
    try:
        lines = proc_self_cgroup.read_text(encoding="utf-8").splitlines()
    except OSError:
        return paths
    for line in lines:
        parts = line.split(":", 2)
        if len(parts) != 3 or parts[0] != "0":  # only the unified (v2) line
            continue
        relative = parts[2].strip().strip("/")
        if relative:
            current = root
            for segment in relative.split("/"):
                current = current / segment
                paths.append(current / "cpu.max")
        break
    return paths


def _parse_cfs_quota(directory: Path) -> Optional[int]:
    """Whole CPUs from a cgroup v1 cpu controller dir, or None when unlimited."""
    try:
        quota = int((directory / "cpu.cfs_quota_us").read_text(encoding="utf-8").strip())
        period = int((directory / "cpu.cfs_period_us").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if quota <= 0 or period <= 0:  # -1 means unlimited
        return None
    return max(1, quota // period)


def _cgroup_v1_cpu_dirs(base: Path, proc_self_cgroup: Path) -> list:
    """cpu controller dirs from the controller root down to this process's own cgroup.

    v1 has the same sub-cgroup shape as v2 — a systemd unit sits at
    ``/sys/fs/cgroup/cpu/system.slice/<unit>`` — so reading only the root misses
    exactly the ``CPUQuota=`` that the v2 walk was added to catch.
    """
    dirs = [base]
    try:
        lines = proc_self_cgroup.read_text(encoding="utf-8").splitlines()
    except OSError:
        return dirs
    for line in lines:
        parts = line.split(":", 2)
        if len(parts) != 3 or "cpu" not in parts[1].split(","):
            continue
        relative = parts[2].strip().strip("/")
        if relative:
            current = base
            for segment in relative.split("/"):
                current = current / segment
                dirs.append(current)
        break
    return dirs


def _cgroup_cpu_quota(
    root: Path = Path("/sys/fs/cgroup"),
    proc_self_cgroup: Path = Path("/proc/self/cgroup"),
    v1_dir: Optional[Path] = None,
) -> Optional[int]:
    """Whole CPUs a cgroup quota allows, or None when unlimited or unreadable.

    ``docker --cpus=2`` and systemd's ``CPUQuota=`` both set a CFS quota, not an
    affinity mask, so neither ``os.cpu_count()`` nor the affinity mask reflects them.
    The most restrictive level wins. Paths are parameters so this is testable without
    a container.
    """
    quotas = [
        quota
        for quota in (
            _parse_cpu_max(path)
            for path in _cgroup_v2_cpu_max_paths(root, proc_self_cgroup)
        )
        if quota is not None
    ]
    if quotas:
        return min(quotas)
    # cgroup v1 (legacy), which has the same sub-cgroup shape as v2.
    base = (root / "cpu") if v1_dir is None else v1_dir
    v1_quotas = [
        quota
        for quota in (
            _parse_cfs_quota(directory)
            for directory in _cgroup_v1_cpu_dirs(base, proc_self_cgroup)
        )
        if quota is not None
    ]
    return min(v1_quotas) if v1_quotas else None


def _available_cpus() -> int:
    """CPUs this process may actually use.

    ``os.cpu_count()`` reports the whole host, which is wrong in precisely the case
    this scaling protects: a small container on a big machine. Prefer the
    process-aware count (3.13+), then the scheduler affinity mask, and fold in a
    cgroup quota, which neither of those reflects.
    """
    count: Optional[int] = None
    process_cpu_count = getattr(os, "process_cpu_count", None)
    if process_cpu_count is not None:  # Python 3.13+
        count = process_cpu_count()
    if count is None:
        try:
            count = len(os.sched_getaffinity(0))
        except AttributeError:  # not Linux
            count = None
    if count is None:
        count = os.cpu_count()
    quota = _cgroup_cpu_quota()
    if quota is not None:
        count = quota if count is None else min(count, quota)
    return max(1, count or 1)


def _default_compute_concurrency() -> int:
    """Heavy computes allowed at once, scaled to the machine and capped at 8.

    A flat 2 was safe while a cold key almost always had a stale value to serve
    meanwhile. It is not safe against a *cold* fan-out: the Sessions tab issues one
    request per tool, so a range change asks for far more distinct cold keys at once
    than the cap allows. Scaling lets a large host drain that fan-out quickly while a
    small VPS, Pi or CPU-limited container keeps the original ceiling.
    """
    return max(2, min(8, _available_cpus() // 2))


# Bound the number of *heavy* computes (full-history reparses) running at once.
# Without this, a burst of requests for distinct cache keys each grabs an AnyIO
# worker token and runs a multi-second parse; the pool saturates (so even cache
# hits and /health can't get a worker) and RSS balloons. Capping heavy work well
# below the worker pool keeps headroom for cheap requests.
# This is the app-side companion to the uvicorn backpressure knobs in cli.py.
_COMPUTE_CONCURRENCY = _positive_int_env(
    "TOKDASH_COMPUTE_CONCURRENCY", _default_compute_concurrency()
)
_compute_semaphore = threading.BoundedSemaphore(_COMPUTE_CONCURRENCY)

# How long a cold request may WAIT for a compute slot, and how many may wait at once.
# Refusing instantly made sense while a stale value was almost always available to
# serve instead. Once a closed window has to be computed to be correct, a Sessions
# fan-out is N distinct cold keys arriving together, and an instant refusal rejected
# every request past the cap while the slot it needed freed a second later — the
# browser retries only a few times, so panels failed outright. Waiting preserves the
# cap (parser stampede and RSS ceiling are unchanged) and lets the fan-out drain.
# The waiter cap keeps a pathological burst from parking the whole worker pool.
# Computing + parked threads are budgeted TOGETHER against AnyIO's default 40-thread
# pool (nothing in this tree raises that limiter; cli.py bounds connections, not
# threads). Deriving the waiter allowance from the concurrency means raising
# TOKDASH_COMPUTE_CONCURRENCY spends the same budget rather than pushing the total
# past the pool and starving /health and cache hits — the very failure the cap exists
# to prevent. A concurrency at or above the budget leaves no waiters, i.e. the old
# fail-fast behaviour, which is the safe end of the trade.
_COMPUTE_THREAD_BUDGET = _positive_int_env("TOKDASH_COMPUTE_THREAD_BUDGET", 32)


def _default_max_waiters(concurrency: int, budget: Optional[int] = None) -> int:
    """Requests allowed to park while ``concurrency`` are computing.

    The two share one thread budget, so raising the concurrency spends the waiter
    allowance rather than pushing the total past AnyIO's pool. At or above the budget
    this is 0 — the old fail-fast behaviour, which is the safe end of the trade.
    """
    ceiling = _COMPUTE_THREAD_BUDGET if budget is None else budget
    return max(0, ceiling - concurrency)


_COMPUTE_WAIT_SECONDS = _bounded_float_env(
    "TOKDASH_COMPUTE_WAIT_SECONDS", 15.0, maximum=120.0
)
_COMPUTE_MAX_WAITERS = _non_negative_int_env(
    "TOKDASH_COMPUTE_MAX_WAITERS", _default_max_waiters(_COMPUTE_CONCURRENCY)
)
_compute_waiters = 0
_compute_waiters_guard = threading.Lock()


def _acquire_compute_slot(*, wait: bool = True) -> bool:
    """Take a heavy-compute slot, optionally waiting briefly for one to free up.

    ``wait=False`` keeps the old instant refusal for callers that have something else
    to serve (a stale value, or an opportunistic background refresh): those must never
    occupy a worker thread waiting when they can answer immediately.
    """
    global _compute_waiters
    if _compute_semaphore.acquire(blocking=False):
        return True
    if not wait:
        return False
    with _compute_waiters_guard:
        if _compute_waiters >= _COMPUTE_MAX_WAITERS:
            return False
        _compute_waiters += 1
    try:
        return _compute_semaphore.acquire(timeout=_COMPUTE_WAIT_SECONDS)
    finally:
        with _compute_waiters_guard:
            _compute_waiters -= 1


def _raise_backpressure(
    message: str,
    *,
    key: str,
    reason: str,
    had_stale: bool,
    warn: bool = True,
) -> None:
    log = logger.warning if warn else logger.debug
    log(
        "tokdash cache backpressure key=%s reason=%s had_stale=%s compute_concurrency=%s",
        key,
        reason,
        had_stale,
        _COMPUTE_CONCURRENCY,
    )
    raise CacheBackpressureError(message)


def _begin_startup_warm(
    key: str, join_seconds: Optional[float] = None
) -> threading.Event:
    """Register a warm fill one foreground request may join.

    The join budget is stored per entry: a warm that runs on a live server (the daily
    rollover) must not make a racing request wait the startup allowance on top of its
    own slot wait.
    """
    event = threading.Event()
    budget = STARTUP_WARM_JOIN_SECONDS if join_seconds is None else join_seconds
    with _startup_warm_guard:
        _startup_warm_events[key] = (event, budget)
        _startup_warm_waiters.discard(key)
    return event


def _finish_startup_warm(key: str, event: threading.Event) -> None:
    with _startup_warm_guard:
        entry = _startup_warm_events.get(key)
        if entry is not None and entry[0] is event:
            # Publish completion before removing the registry entry. Otherwise a
            # request arriving in the tiny pop-before-set window would see neither
            # a cached join target nor an unlocked cache key and emit a stray 503.
            event.set()
            _startup_warm_events.pop(key, None)
            _startup_warm_waiters.discard(key)
            return
    event.set()


def _claim_startup_warm_wait(key: str) -> tuple[threading.Event, float] | None:
    """Claim the right to join an in-flight warm, with that warm's join budget."""
    with _startup_warm_guard:
        entry = _startup_warm_events.get(key)
        if entry is None or key in _startup_warm_waiters:
            return None
        _startup_warm_waiters.add(key)
        return entry


def _release_startup_warm_wait(key: str, event: threading.Event) -> None:
    with _startup_warm_guard:
        entry = _startup_warm_events.get(key)
        if entry is not None and entry[0] is event:
            _startup_warm_waiters.discard(key)


def _response_cache_metadata(result: CacheFetchResult) -> Dict[str, Any]:
    return {
        "status": result.status,
        "served_from_cache": result.served_from_cache,
        "age_seconds": result.age_seconds,
    }


def _cached_route(
    route_name: str,
    cache_key: str,
    fetch_fn,
    *,
    force_refresh: bool = False,
    include_cache_metadata: bool = False,
) -> Any:
    started = time.monotonic()
    try:
        result = get_cached_or_fetch(
            cache_key,
            fetch_fn,
            force_refresh=force_refresh,
            return_metadata=include_cache_metadata,
        )
        if not include_cache_metadata:
            return result
        assert isinstance(result, CacheFetchResult)
        if not isinstance(result.value, dict):
            return result.value
        payload = dict(result.value)
        payload["response_cache"] = _response_cache_metadata(result)
        return payload
    finally:
        logger.debug(
            "tokdash route cache fetch route=%s key=%s duration_ms=%.1f",
            route_name,
            cache_key,
            (time.monotonic() - started) * 1000,
        )


def _try_key_lock(key: str) -> tuple[threading.Lock, bool]:
    """Return the key lock and acquire it atomically with registry lookup."""
    with _cache_guard:
        lock = _key_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _key_locks[key] = lock
        _key_locks.move_to_end(key)
        acquired = lock.acquire(blocking=False)
        _prune_key_locks_locked(exclude=key)
        return lock, acquired


def _prune_key_locks_locked(*, exclude: Optional[str] = None) -> None:
    """Bound idle per-key locks while preserving every in-flight single flight."""
    if len(_key_locks) <= CACHE_MAX_ENTRIES:
        return
    for candidate, candidate_lock in list(_key_locks.items()):
        if len(_key_locks) <= CACHE_MAX_ENTRIES:
            break
        if candidate == exclude or candidate_lock.locked():
            continue
        _key_locks.pop(candidate, None)


def _release_key_lock(key: str, lock: threading.Lock) -> None:
    with _cache_guard:
        # Keep unlock and registry cleanup atomic with _try_key_lock(). If the
        # mutex were released first, another caller could acquire this lock
        # before we remove it, letting a later caller create a second lock for
        # the same key and run a duplicate fill.
        lock.release()
        # Failed/invalidated fills have no cached value and should leave no lock behind.
        if _key_locks.get(key) is lock and key not in _cache:
            _key_locks.pop(key, None)
        _prune_key_locks_locked()


def _cache_get(key: str) -> Optional[tuple[float, Any]]:
    with _cache_guard:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
        return hit


def _cache_epoch_value() -> int:
    with _cache_guard:
        return _cache_epoch


def _cache_set_if_epoch(key: str, value: Any, epoch: int) -> bool:
    with _cache_guard:
        if epoch != _cache_epoch:
            return False
        _cache[key] = (datetime.now().timestamp(), value)
        _cache.move_to_end(key)
        while len(_cache) > CACHE_MAX_ENTRIES:
            _cache.popitem(last=False)
        _prune_key_locks_locked()
        return True


def _clear_cache() -> None:
    """Drop all cached responses (e.g. after the pricing DB is edited).

    Idle locks are cleared with their values. In-flight locks remain until their
    holder exits, and the generation counter prevents those older computes from
    repopulating stale values after this clear.
    """
    global _cache_epoch
    with _cache_guard:
        _cache_epoch += 1
        _cache.clear()
        for key, lock in list(_key_locks.items()):
            if not lock.locked():
                _key_locks.pop(key, None)


def _refresh_stale_in_background(
    key: str,
    fetch_fn,
    lock: threading.Lock,
    epoch: int,
) -> None:
    acquired_compute = False
    try:
        acquired_compute = _acquire_compute_slot(wait=False)
        if not acquired_compute:
            logger.debug("tokdash stale refresh deferred key=%s reason=compute_cap", key)
            return
        try:
            fresh = fetch_fn()
        except Exception:
            logger.warning("tokdash stale refresh failed key=%s", key, exc_info=True)
            return
        _cache_set_if_epoch(key, fresh, epoch)
    finally:
        if acquired_compute:
            _compute_semaphore.release()
        _release_key_lock(key, lock)


def get_cached_or_fetch(
    key: str,
    fetch_fn,
    *,
    force_refresh: bool = False,
    return_metadata: bool = False,
    _startup_warm: bool = False,
    _startup_waited: bool = False,
) -> Any:
    """Cache with single-flight, stale-while-revalidate, and a heavy-compute cap.

    - Fresh hit (age < TTL): returned immediately with no locking or worker contention.
      ``force_refresh=True`` skips this fast path so manual refreshes recompute.
    - Stale hit: returned immediately. At most one daemon refreshes the key in the
      background, so no caller pays the recompute latency and parsers do not stampede.
    - Cold miss: one foreground request may join an active startup warm for this key.
      Otherwise, if this key or the global heavy-compute pool is already busy, fail
      fast with ``CacheBackpressureError`` so request workers do not pile up while
      blocked. A later request can retry once the in-flight fill finishes.
    - A global semaphore bounds how many heavy computes run at once across all keys.
    """
    def result(value: Any, status: str, age_seconds: Optional[float]) -> Any:
        cache_result = CacheFetchResult(value=value, status=status, age_seconds=age_seconds)
        return cache_result if return_metadata else value

    now = datetime.now().timestamp()
    hit = _cache_get(key)
    if hit is not None and now - hit[0] < CACHE_TTL and not force_refresh:
        return result(hit[1], "hit", now - hit[0])

    if hit is not None and not force_refresh:
        lock, acquired = _try_key_lock(key)
        if acquired:
            # A prior background holder may have refreshed between our first read and
            # acquiring the single-flight lock.
            latest = _cache_get(key)
            locked_now = datetime.now().timestamp()
            if latest is not None and locked_now - latest[0] < CACHE_TTL:
                _release_key_lock(key, lock)
                return result(latest[1], "hit", locked_now - latest[0])
            epoch = _cache_epoch_value()
            try:
                threading.Thread(
                    target=_refresh_stale_in_background,
                    args=(key, fetch_fn, lock, epoch),
                    name="tokdash-cache-refresh",
                    daemon=True,
                ).start()
            except Exception:
                _release_key_lock(key, lock)
                logger.warning("tokdash failed to start stale refresh key=%s", key, exc_info=True)
        return result(hit[1], "stale", now - hit[0])

    lock, acquired = _try_key_lock(key)
    if not acquired:
        # Another thread is already computing this key.
        if hit is not None:
            return result(hit[1], "stale", now - hit[0])  # serve cached rather than stampede the parser
        if not force_refresh and not _startup_warm and not _startup_waited:
            claimed = _claim_startup_warm_wait(key)
            if claimed is not None:
                warm_event, join_seconds = claimed
                try:
                    warm_event.wait(timeout=join_seconds)
                finally:
                    _release_startup_warm_wait(key, warm_event)
                # The warmer may have completed successfully, failed, or timed out.
                # Re-enter once: a success is now a hit, a failure can be computed by
                # this request, and a timeout retains the ordinary fail-fast behavior.
                return get_cached_or_fetch(
                    key,
                    fetch_fn,
                    force_refresh=force_refresh,
                    return_metadata=return_metadata,
                    _startup_waited=True,
                )
        _raise_backpressure(
            f"Cache fill already in progress for {key}",
            key=key,
            reason="same_key_inflight",
            had_stale=False,
            warn=not _startup_warm,
        )
    try:
        # Re-check under the lock: a prior holder may have just stored a fresh value.
        latest = _cache_get(key)
        locked_now = datetime.now().timestamp()
        if latest is not None and locked_now - latest[0] < CACHE_TTL and not force_refresh:
            return result(latest[1], "hit", locked_now - latest[0])
        epoch = _cache_epoch_value()
        # A stale value beats waiting, so only a cold key with nothing to show parks
        # for a slot. This is what keeps a Sessions fan-out from failing wholesale.
        #
        # NOTE: this waits while still holding the per-key lock, so the window in which
        # a second request for the SAME key is refused as same_key_inflight grows from
        # "the compute" to "the compute plus up to _COMPUTE_WAIT_SECONDS". That is
        # deliberate. A fan-out is distinct keys, so it never collides here; the cost
        # falls on a repeat of one key, where the caller has nothing to be given anyway
        # and a 503 is the honest answer. Releasing the lock to wait would instead let
        # several threads compute the same cold key, which is what single-flight exists
        # to prevent.
        if not _acquire_compute_slot(wait=latest is None):
            if latest is not None:
                return result(latest[1], "stale", locked_now - latest[0])
            _raise_backpressure(
                "Too many cold requests; retry shortly",
                key=key,
                reason="compute_cap",
                had_stale=False,
                warn=not _startup_warm,
            )
        try:
            fresh = fetch_fn()
        finally:
            _compute_semaphore.release()
        _cache_set_if_epoch(key, fresh, epoch)
        return result(fresh, "recomputed", 0.0)
    finally:
        _release_key_lock(key, lock)


def _format_pricing_db(data: Dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _validate_pricing_db(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="pricing_db.json must be a JSON object")
    if not isinstance(data.get("models"), dict):
        raise HTTPException(status_code=400, detail="pricing_db.json must include a models object")
    aliases = data.get("aliases")
    if aliases is not None and not isinstance(aliases, dict):
        raise HTTPException(status_code=400, detail="pricing_db.json aliases must be an object")
    return data


def _pricing_override_path() -> Path:
    # User edits persist under the data dir (TOKDASH_DATA_DIR), NOT in the packaged file, so
    # they survive `tokdash update` (pip/pipx reinstall) and don't 500 on a read-only install.
    from .onboard import paths

    return paths.pricing_db_override_path()


def _read_pricing_override() -> Optional[Dict[str, Any]]:
    """The user override dict if present AND a valid pricing object, else None.

    None (not {}) means "no usable override" so callers fall back to the baseline rather
    than treating a missing/corrupt override as an empty pricing DB.
    """
    try:
        data = json.loads(_pricing_override_path().read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict) and isinstance(data.get("models"), dict):
        return data
    return None


def _baseline_version() -> Optional[str]:
    """The packaged baseline's ``version`` string (best-effort, never raises).

    A saved override FULLY REPLACES the baseline, which means it also freezes future bundled
    pricing updates until the user deletes it. Surfacing the baseline version alongside the
    override lets the editor make that trade-off explicit (e.g. "your override was forked from
    baseline vX; the shipped baseline is now vY — delete the override to pick up updates").
    """
    try:
        base = json.loads(PRICING_DB_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    version = base.get("version") if isinstance(base, dict) else None
    return version if isinstance(version, str) else None


def _effective_pricing_db() -> tuple[Dict[str, Any], str]:
    """The effective pricing DB and its source: the override (authoritative full replacement)
    when present/valid, else the packaged baseline. Raises 404/500 only on a broken baseline."""
    override = _read_pricing_override()
    if override is not None:
        return override, "override"
    try:
        base = _validate_pricing_db(json.loads(PRICING_DB_PATH.read_text(encoding="utf-8")))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="pricing_db.json not found")
    except JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"pricing_db.json is invalid JSON: {e.msg}")
    return base, "baseline"


def _clear_pricing_signature_cache() -> None:
    """Forget pricing-file signatures after an in-process pricing write."""
    global _pricing_baseline_sig_cache, _pricing_override_sig_cache
    with _pricing_sig_guard:
        _pricing_baseline_sig_cache = None
        _pricing_override_sig_cache = None


def _baseline_pricing_signature() -> tuple[str, int, int]:
    """Stable packaged pricing signature for response-cache keys.

    The packaged baseline is not expected to mutate while a process is running. Cache its
    stat result so hot cached API routes do not hit the filesystem on every request. Tests
    that monkeypatch ``PRICING_DB_PATH`` still force a recompute because the path changes.
    """
    global _pricing_baseline_sig_cache
    path = str(PRICING_DB_PATH)
    with _pricing_sig_guard:
        cached = _pricing_baseline_sig_cache
        if cached is not None and cached[0] == path:
            return cached[1]

    try:
        st = PRICING_DB_PATH.stat()
        sig = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        sig = (path, 0, 0)

    with _pricing_sig_guard:
        _pricing_baseline_sig_cache = (path, sig)
    return sig


def _override_pricing_signature(override: Path) -> tuple[str, int, int, str]:
    """User override signature for response-cache keys.

    We stat on every pricing-aware route so manual edits or sibling worker writes are noticed,
    but read/hash the override only when its ``(path, mtime_ns, size)`` changes. This keeps the
    hot path cheap while still busting stale cost responses after out-of-band edits.
    """
    global _pricing_override_sig_cache
    path = str(override)
    try:
        st = override.stat()
    except OSError:
        sig = (path, 0, 0, "")
        with _pricing_sig_guard:
            _pricing_override_sig_cache = sig
        return sig

    with _pricing_sig_guard:
        cached = _pricing_override_sig_cache
        if cached is not None and cached[:3] == (path, st.st_mtime_ns, st.st_size):
            return cached

    try:
        raw = override.read_bytes()
        digest = hashlib.blake2b(raw, digest_size=16).hexdigest()
    except OSError:
        sig = (path, 0, 0, "")
    else:
        sig = (path, st.st_mtime_ns, st.st_size, digest)

    with _pricing_sig_guard:
        _pricing_override_sig_cache = sig
    return sig


def _pricing_cache_key(base: str) -> str:
    """Cache key suffix for routes whose response includes pricing-derived costs.

    ``PUT /api/pricing-db`` clears this process's response cache, but a pricing override can
    also change outside that handler: manual edit while serving, or another uvicorn worker
    handling the write. Include the effective pricing files in the key so those routes miss
    stale API responses without relying on cross-process cache invalidation.
    """
    override = _pricing_override_path()
    sig = [_baseline_pricing_signature(), _override_pricing_signature(override)]
    encoded = json.dumps(sig, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.blake2b(encoded.encode("utf-8"), digest_size=12).hexdigest()
    return f"{base}_pricing_{digest}"


@app.get("/api/pricing-db")
def get_pricing_db() -> Dict[str, Any]:
    data, source = _effective_pricing_db()
    # `path` is where edits PERSIST (the override under the data dir); baseline is read-only.
    # `baseline_version` is the shipped baseline's version even when an override is in effect,
    # so the editor can warn when an override has drifted behind newer bundled pricing.
    return {
        "path": str(_pricing_override_path()),
        "baseline_path": str(PRICING_DB_PATH),
        "baseline_version": _baseline_version(),
        "source": source,
        "data": data,
        "text": _format_pricing_db(data),
    }


@app.put("/api/pricing-db")
def update_pricing_db(payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        if "text" in payload:
            data = json.loads(str(payload["text"]))
        else:
            data = payload.get("data")
    except JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e.msg}")

    data = _validate_pricing_db(data)
    formatted = _format_pricing_db(data)
    # Write to the data-dir override (user-writable, survives `tokdash update`), NOT the
    # packaged file. The override fully replaces the baseline (WYSIWYG editor semantics).
    override = _pricing_override_path()
    tmp_path = override.with_suffix(override.suffix + ".tmp")
    try:
        override.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(formatted, encoding="utf-8")
        tmp_path.replace(override)
    except OSError as e:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise HTTPException(status_code=500, detail=f"Failed to write {override}: {e}")

    reload_pricing_db()
    _clear_pricing_signature_cache()
    _clear_cache()
    return {"path": str(override), "baseline_path": str(PRICING_DB_PATH),
            "baseline_version": _baseline_version(), "source": "override", "data": data, "text": formatted}


@app.get("/api/usage")
def get_usage(
    period: str = "today",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    refresh: bool = False,
) -> Dict[str, Any]:
    _validate_date_params(date_from, date_to)
    try:
        cache_key = _window_cache_key(f"usage_{period}_{date_from}_{date_to}", date_from, date_to)
        return _cached_route(
            "/api/usage",
            cache_key,
            lambda: compute_usage_with_comparison(period, date_from, date_to),
            force_refresh=refresh,
            include_cache_metadata=True,
        )
    except UsageDatabaseSchemaTooNewError as e:
        # 500, not 503: 503 is the dashboard retry signal, and a database
        # written by a newer build never becomes readable on retry. Fail fast
        # and carry the remediation in the detail.
        raise HTTPException(status_code=500, detail=str(e))
    except CacheBackpressureError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/openclaw")
def get_openclaw(period: str = "today") -> Dict[str, Any]:
    def fetch():
        data = get_openclaw_data(period)
        data["period"] = period
        data["range"] = resolve_period(period)
        data["timestamp"] = datetime.now().isoformat()
        return data

    try:
        return _cached_route("/api/openclaw", _window_cache_key(f"openclaw_{period}", None, None), fetch)
    except UsageDatabaseSchemaTooNewError as e:
        # 500, not 503: 503 is the dashboard retry signal, and a database
        # written by a newer build never becomes readable on retry. Fail fast
        # and carry the remediation in the detail.
        raise HTTPException(status_code=500, detail=str(e))
    except CacheBackpressureError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/tools")
def get_tools(period: str = "today") -> Dict[str, Any]:
    """Coding tools usage (local parsers)."""

    try:
        def fetch():
            data = get_tools_data(period)
            data["period"] = period
            data["range"] = resolve_period(period)
            data["timestamp"] = datetime.now().isoformat()
            return data

        return _cached_route("/api/tools", _window_cache_key(f"tools_{period}", None, None), fetch)
    except UsageDatabaseSchemaTooNewError as e:
        # 500, not 503: 503 is the dashboard retry signal, and a database
        # written by a newer build never becomes readable on retry. Fail fast
        # and carry the remediation in the detail.
        raise HTTPException(status_code=500, detail=str(e))
    except CacheBackpressureError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/quota")
def get_quota() -> Dict[str, Any]:
    """Subscription quota state from local files and stored snapshots.

    M1 is intentionally local-only: this route never performs provider network I/O.
    """

    try:
        from .sources.quota import quota_state

        return _cached_route("/api/quota", "quota_state", quota_state)
    except UsageDatabaseSchemaTooNewError as e:
        # 500, not 503: 503 is the dashboard retry signal, and a database
        # written by a newer build never becomes readable on retry. Fail fast
        # and carry the remediation in the detail.
        raise HTTPException(status_code=500, detail=str(e))
    except CacheBackpressureError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/quota/history")
def get_quota_history(
    providers: Optional[str] = None,
    granularity: str = "hour",
    start: Optional[int] = None,
    end: Optional[int] = None,
    max_points: Optional[int] = 300,
) -> Dict[str, Any]:
    try:
        from .sources.quota.config import network_enabled
        from .usage_store import UsageEntryStore

        provider_list = [p.strip() for p in (providers or "").split(",") if p.strip()]
        # When Codex API polling is enabled, the API is the sole oracle for Codex consumption:
        # exclude codex_session rows (stale cached snapshots) so they can't contaminate the
        # chart. See `quota_history`'s `network_only_providers` param.
        network_only_providers = {"codex"} if network_enabled("codex_api") else set()
        return UsageEntryStore().quota_history(
            providers=provider_list or None,
            granularity=granularity,
            start=start,
            end=end,
            max_points=max_points,
            network_only_providers=network_only_providers,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _try_begin_quota_refresh() -> float:
    """Atomically check the refresh cooldown and, if clear, reserve the slot.

    Returns the remaining cooldown seconds: ``> 0`` means blocked (caller should 429);
    ``0.0`` means the slot was reserved under the lock and the caller may proceed. Doing
    the check and the record in one critical section closes the check-then-act race where
    two concurrent refreshes could both pass a separate read-only check before either
    recorded, doubling the provider calls.
    """
    global _quota_last_refresh_monotonic, _quota_prev_refresh_monotonic
    with _quota_refresh_guard:
        now = time.monotonic()
        remaining = _QUOTA_REFRESH_COOLDOWN_SECONDS - (now - _quota_last_refresh_monotonic)
        if remaining > 0:
            return remaining
        _quota_prev_refresh_monotonic = _quota_last_refresh_monotonic
        _quota_last_refresh_monotonic = now
        return 0.0


def _abort_quota_refresh() -> None:
    """Roll back a reservation made by :func:`_try_begin_quota_refresh`.

    Called when the refresh fails after reserving the slot, so an error response does not
    burn the user's cooldown window. Safe because only one caller can hold the reservation
    per window (concurrent attempts 429 until it is released or expires), so restoring the
    previous mark exactly restores the pre-reservation state.
    """
    global _quota_last_refresh_monotonic
    with _quota_refresh_guard:
        _quota_last_refresh_monotonic = _quota_prev_refresh_monotonic


@app.post("/api/quota/consent")
def set_quota_consent(payload: Dict[str, Any]) -> Dict[str, Any]:
    from .sources.quota.config import set_quota_consent as _set_quota_consent

    consent = _set_quota_consent(payload or {})
    _clear_cache()
    return {"consent": consent}


@app.post("/api/quota/settings")
def set_quota_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Persist the quota master switch and poll interval (write-gated).

    Body: ``{"enabled": bool, "poll_interval_minutes": 15|30|60|120}`` (either optional).
    """
    from .sources.quota import config as quota_config

    payload = payload or {}
    if "enabled" in payload:
        quota_config.set_quota_enabled(bool(payload["enabled"]))
    if "poll_interval_minutes" in payload:
        try:
            quota_config.set_poll_interval_minutes(int(payload["poll_interval_minutes"]))
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail=f"poll_interval_minutes must be one of {list(quota_config.POLL_INTERVAL_CHOICES)}",
            )
    _clear_cache()
    interval_seconds, interval_source = quota_config.effective_poll_interval()
    return {
        "enabled": quota_config.quota_tracking_enabled(),
        "config_enabled": quota_config.quota_config_enabled(),
        "poll_interval_minutes": quota_config.read_poll_interval_minutes()
        or quota_config.DEFAULT_POLL_INTERVAL_MINUTES,
        "interval": interval_seconds,
        "interval_source": interval_source,
    }


# Read-only poll (no quota consumed): providers' usage endpoints are read-only, so this is
# intentionally GET, not POST, so it works over Tailscale/WSL/any forward while genuine
# config-write endpoints stay loopback-guarded.
@app.get("/api/quota/refresh")
def refresh_quota() -> Dict[str, Any]:
    from .sources.quota import config as quota_config

    if not quota_config.quota_tracking_enabled():
        raise HTTPException(status_code=409, detail="Quota tracking is disabled; enable it to refresh.")
    # Atomically reserves the slot if the cooldown is clear (single critical section), so
    # two concurrent refreshes can't both pass and double the provider calls.
    remaining = _try_begin_quota_refresh()
    if remaining > 0:
        raise HTTPException(status_code=429, detail=f"Quota refresh cooldown active for {int(remaining)}s")
    from .sources.quota import collect_enabled_snapshots, remember_current_snapshots
    from .usage_store import UsageEntryStore, persistent_usage_db_enabled

    try:
        store = UsageEntryStore() if persistent_usage_db_enabled() else None
        snapshots = collect_enabled_snapshots(include_network=True, store=store)
        remember_current_snapshots(snapshots)
        inserted = store.insert_quota_snapshots(snapshots) if store is not None else 0
    except UsageDatabaseSchemaTooNewError as e:
        # Release the reservation first (same reason as below), then convert. A bare
        # `raise` reaches FastAPI as a generic 500 whose body is "Internal Server
        # Error", so the dashboard renders "HTTP 500" and the remediation this error
        # exists to carry is lost on exactly the route a user hits when retrying.
        _abort_quota_refresh()
        raise HTTPException(status_code=500, detail=str(e))
    except Exception:
        # A failed refresh must not burn the cooldown slot: release the reservation so
        # the user can retry immediately instead of being locked out for 60 s by a 500.
        _abort_quota_refresh()
        raise
    _clear_cache()
    return {"snapshots": len(snapshots), "inserted": inserted}


@app.get("/api/codex/sessions")
def get_codex_sessions(period: str = "today", include_review_sessions: Optional[bool] = None) -> Dict[str, Any]:
    try:
        cache_key = _window_cache_key(
            f"codex_sessions_{period}_{include_review_sessions}", None, None
        )
        return _cached_route(
            "/api/codex/sessions",
            cache_key,
            lambda: get_codex_sessions_data(period, include_review_sessions=include_review_sessions),
        )
    except UsageDatabaseSchemaTooNewError as e:
        # 500, not 503: 503 is the dashboard retry signal, and a database
        # written by a newer build never becomes readable on retry. Fail fast
        # and carry the remediation in the detail.
        raise HTTPException(status_code=500, detail=str(e))
    except CacheBackpressureError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/codex/session")
def get_codex_session(session_id: str) -> Dict[str, Any]:
    try:
        return get_codex_session_detail(session_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/sessions")
def get_sessions(
    tool: str,
    period: str = "today",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    include_review_sessions: Optional[bool] = None,
) -> Dict[str, Any]:
    _validate_date_params(date_from, date_to)
    try:
        cache_key = _session_response_cache_key(
            tool,
            period,
            date_from,
            date_to,
            include_review_sessions,
        )
        return _cached_route(
            "/api/sessions",
            cache_key,
            lambda: get_sessions_data(
                tool,
                period,
                date_from,
                date_to,
                include_review_sessions=include_review_sessions,
            ),
        )
    except UsageDatabaseSchemaTooNewError as e:
        # 500, not 503: 503 is the dashboard retry signal, and a database
        # written by a newer build never becomes readable on retry. Fail fast
        # and carry the remediation in the detail.
        raise HTTPException(status_code=500, detail=str(e))
    except CacheBackpressureError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/active-time")
def get_active_time(
    period: str = "today",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    include_review_sessions: Optional[bool] = None,
    refresh: bool = False,
) -> Dict[str, Any]:
    """Cross-tool active time for the Overview KPI.

    Kept off /api/usage so the Overview's first paint still costs one request:
    this one reads every session tool, so the card fills in when it lands.
    ``refresh`` bypasses the response cache, as /api/usage does, so the dashboard's
    Refresh button can clear a stale figure or one missing a tool that failed.
    """
    _validate_date_params(date_from, date_to)
    def fetch():
        data = get_active_time_data(
            period,
            date_from,
            date_to,
            include_review_sessions=include_review_sessions,
        )
        if isinstance(data, dict):
            data["range"] = resolve_period(period, date_from, date_to)
        return data

    try:
        cache_key = _active_time_cache_key(period, date_from, date_to, include_review_sessions)
        return _cached_route(
            "/api/active-time",
            cache_key,
            fetch,
            force_refresh=refresh,
        )
    except UsageDatabaseSchemaTooNewError as e:
        # 500, not 503: 503 is the dashboard retry signal, and a database
        # written by a newer build never becomes readable on retry. Fail fast
        # and carry the remediation in the detail.
        raise HTTPException(status_code=500, detail=str(e))
    except CacheBackpressureError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/session")
def get_session(tool: str, session_id: str) -> Dict[str, Any]:
    try:
        return get_session_detail(tool, session_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# NOTE: the handlers below are intentionally ``async def`` so they run on the event
# loop and never need an AnyIO worker token. Under heavy load every worker may be
# busy in a multi-second compute; keeping these (and /health) async means the
# dashboard shell, manifest, service worker, and the liveness probe stay responsive
# regardless. They do only trivial, near-instant file I/O.
def _render_dashboard_html(base_path: str) -> str:
    html_path = STATIC_DIR / "index.html"
    if not html_path.exists():
        return "<h1>Dashboard not found</h1><p>Please create static/index.html</p>"
    return html_path.read_text(encoding="utf-8").replace(BASE_PATH_PLACEHOLDER, base_path)


def _render_manifest(base_path: str) -> str:
    path = STATIC_DIR / "manifest.webmanifest"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Manifest not found")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="Manifest is invalid JSON") from exc
    start = _with_base_path(base_path, "/")
    data["start_url"] = start
    data["scope"] = start
    for icon in data.get("icons", []):
        src = icon.get("src")
        if isinstance(src, str) and src.startswith("/"):
            icon["src"] = _with_base_path(base_path, src)
    return json.dumps(data, separators=(",", ":"))


def _render_service_worker(base_path: str) -> str:
    path = STATIC_DIR / "sw.js"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Service worker not found")
    return (
        path.read_text(encoding="utf-8")
        .replace(SW_CACHE_NAME_PLACEHOLDER, get_static_cache_name())
        .replace(BASE_PATH_PLACEHOLDER, base_path)
    )


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard(request: Request):
    html_path = STATIC_DIR / "index.html"
    if not html_path.exists():
        return HTMLResponse(content=_render_dashboard_html(""), status_code=404)
    return HTMLResponse(content=_render_dashboard_html(_request_base_path(request)), headers=NO_CACHE_HEADERS)


@app.get("/manifest.webmanifest")
async def serve_manifest(request: Request):
    return Response(
        content=_render_manifest(_request_base_path(request)),
        media_type="application/manifest+json",
        headers=NO_CACHE_HEADERS,
    )


@app.get("/sw.js")
async def serve_service_worker(request: Request):
    return Response(
        content=_render_service_worker(_request_base_path(request)),
        media_type="application/javascript",
        headers=NO_CACHE_HEADERS,
    )


@app.get("/api/stats")
def get_stats(year: Optional[int] = None) -> Dict[str, Any]:
    try:
        cache_key = _window_cache_key(
            f"stats_{year}", None, f"{year}-12-31" if year else None
        )
        return _cached_route("/api/stats", cache_key, lambda: compute_stats(year))
    except UsageDatabaseSchemaTooNewError as e:
        # 500, not 503: 503 is the dashboard retry signal, and a database
        # written by a newer build never becomes readable on retry. Fail fast
        # and carry the remediation in the detail.
        raise HTTPException(status_code=500, detail=str(e))
    except CacheBackpressureError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/insights")
def get_insights(
    period: str = "year",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    facets: Optional[str] = None,
    include_project_names: bool = True,
    refresh: bool = False,
) -> Dict[str, Any]:
    """Facet-selected usage analytics for report-style consumers.

    One request covers every facet a yearly report needs. The alternative -- an
    endpoint per metric -- is the /api/sessions fan-out shape, which serialises
    against the compute semaphore and sheds load at 503; a report would open
    with eight cold round trips instead of one.

    A closed window is cached indefinitely by ``_window_cache_key``, so a past
    year is computed once and every later view is a cache hit.
    """
    _validate_date_params(date_from, date_to)
    try:
        cache_key = _window_cache_key(
            f"insights_{period}_{date_from}_{date_to}_{facets}_{include_project_names}",
            date_from,
            date_to,
        )
        return _cached_route(
            "/api/insights",
            cache_key,
            lambda: compute_insights(
                period,
                date_from,
                date_to,
                facets=facets,
                include_project_names=include_project_names,
            ),
            force_refresh=refresh,
        )
    except UnknownFacetError as e:
        # Refused rather than ignored: a dropped facet renders as a blank
        # section labelled as data, which is the D1 failure mode in a new place.
        raise HTTPException(status_code=400, detail=str(e))
    except UsageDatabaseSchemaTooNewError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except CacheBackpressureError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/activity-insights")
def get_activity_insights(refresh: bool = False) -> dict[str, Any]:
    try:
        return _cached_route(
            "/api/activity-insights",
            _day_scoped_key(ACTIVITY_INSIGHTS_CACHE_KEY),
            get_codex_activity_insights,
            force_refresh=refresh,
        )
    except UsageDatabaseSchemaTooNewError as e:
        # 500, not 503: 503 is the dashboard retry signal, and a database
        # written by a newer build never becomes readable on retry. Fail fast
        # and carry the remediation in the detail.
        raise HTTPException(status_code=500, detail=str(e))
    except CacheBackpressureError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:  # noqa: BLE001 - isolate parser/index failures from other routes
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health_check():
    # async so the liveness probe answers even when every worker thread is busy in a
    # heavy compute — this is what makes an external /health watchdog reliable (P4).
    # The service/version fields are a distinctive fingerprint so a port probe can tell
    # "this is Tokdash" instead of trusting a generic {"status":"ok"} any app could return.
    return {"status": "ok", "service": "tokdash", "version": __version__}


def _read_install_manifest() -> Dict[str, Any]:
    """Best-effort read of the setup manifest; always returns a dict, never raises.

    Delegates to ``onboard.manifest.read_manifest`` (the single guarded reader) so that a
    present-but-non-dict ``install.json`` — valid JSON like ``[1,2,3]`` or ``"x"`` — yields
    ``{}`` instead of crashing version reporting with an AttributeError (HTTP 500).
    """
    try:
        from .onboard import manifest as _manifest

        return _manifest.read_manifest() or {}
    except Exception:
        return {}


@app.get("/api/version")
async def get_version() -> Dict[str, Any]:
    # Local-only version info; async to stay responsive like /health. Provenance
    # fields come from the setup manifest when present (Phase 1+), else None.
    manifest = _read_install_manifest()
    return {
        "service": "tokdash",
        "runtime_version": __version__,
        "install_method": manifest.get("install_method"),
        "update_check_enabled": _update_check_enabled(),
        # The usage-DB schema this build can read. A constant, not a DB read, so
        # this route stays as cheap as /health — comparing it against another
        # process's value is how a version skew is spotted without opening the
        # database. `tokdash doctor` reports what the file on disk actually holds.
        "usage_db_schema_supported": USAGE_DB_SCHEMA_VERSION,
    }


def _update_check_enabled() -> bool:
    try:
        from .onboard import updatecheck

        return updatecheck.is_enabled()
    except Exception:
        return False


@app.post("/api/update-check/consent")
async def update_check_consent() -> Dict[str, Any]:
    # Write-gated by _write_guard (loopback + Host/Origin + token). One-time opt-in that
    # persists consent to config.json so the dashboard can offer update checks (§14).
    from .onboard import updatecheck

    updatecheck.enable()
    return {"enabled": True}


# Read-only poll (PyPI read + in-memory cache only, no disk write): intentionally GET, not
# POST, so it works over Tailscale/WSL/any forward while the CONSENT endpoint above (which
# writes config.json) stays loopback-guarded. Opt-in still applies: it only ever *reports*
# availability when the user has enabled update checks — never an automatic/background call
# (§14) — and it never runs an upgrade (no web-triggered shell, §15).
@app.get("/api/update-check")
async def run_update_check() -> Dict[str, Any]:
    from .onboard import updatecheck

    if not updatecheck.is_enabled():
        return {"enabled": False, "update_available": False}
    return {"enabled": True, **updatecheck.check(__version__)}


@app.get("/api/csrf-token")
async def get_csrf_token(request: Request) -> Dict[str, str]:
    # The dashboard fetches this right before a write and echoes it back as
    # X-Tokdash-Token. The default CORS regex permits any localhost *port*, so we cannot
    # rely on same-origin policy alone to keep the token secret — we apply the same
    # Host + Origin allowlist as the write gate, plus require a loopback bind. A page on
    # another localhost port (or a non-loopback exposure) therefore cannot read it.
    port = _effective_port()
    host = (request.headers.get("host") or "").strip().lower()
    if (
        not _is_loopback(_effective_bind())
        or host not in _host_allowlist(port)
        or _origin_denied(request.headers, _origin_allowlist(port))
    ):
        raise HTTPException(status_code=403, detail="unavailable")
    return {"token": _CSRF_TOKEN}
