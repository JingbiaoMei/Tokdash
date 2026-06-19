from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from .assets import (
    NO_CACHE_HEADERS,
    STATIC_DIR,
    SW_CACHE_NAME_PLACEHOLDER,
    get_static_cache_name,
)
from .compute import compute_stats, compute_usage_with_comparison, get_openclaw_data, get_tools_data
from .dateutil import parse_date_range
from .sessions import (
    get_codex_session_detail,
    get_codex_sessions_data,
    get_session_detail,
    get_sessions_data,
    reload_pricing_db,
)


PRICING_DB_PATH = Path(__file__).parent / "pricing_db.json"
logger = logging.getLogger(__name__)


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


def _warm_caches() -> None:
    """Best-effort background warm so the first user request hits hot caches.

    Populates the parser caches (coding_tools._entry_cache, openclaw._ENTRY_CACHE)
    and the API response cache for the dashboard's initial loads — Overview (today)
    and Stats. Without this, the first cold request pays the full multi-second parse.
    Disable with TOKDASH_WARM_ON_START=0.
    Failures are swallowed; warming must never crash `serve`.
    """
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    for key, fetch in (
        ("usage_today_None_None", lambda: compute_usage_with_comparison("today", None, None)),
        (
            f"usage_today_{today}_{today}",
            lambda: compute_usage_with_comparison("today", today, today),
        ),
        ("stats_None", lambda: compute_stats(None)),
    ):
        try:
            get_cached_or_fetch(key, fetch)
        except Exception:
            pass


@asynccontextmanager
async def _lifespan(_app: "FastAPI"):
    if os.environ.get("TOKDASH_WARM_ON_START", "1") != "0":
        threading.Thread(target=_warm_caches, name="tokdash-warm", daemon=True).start()
    yield


app = FastAPI(title="Tokdash", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.add_middleware(NoCacheMiddleware)


cors_allow_origins = [o.strip() for o in os.environ.get("TOKDASH_ALLOW_ORIGINS", "").split(",") if o.strip()]
cors_allow_origin_regex = os.environ.get("TOKDASH_ALLOW_ORIGIN_REGEX", "").strip() or None
if not cors_allow_origins and cors_allow_origin_regex is None:
    cors_allow_origin_regex = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allow_origins,
    allow_origin_regex=cors_allow_origin_regex,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


_cache: Dict[str, tuple[float, Any]] = {}
_cache_guard = threading.Lock()  # protects _cache, _key_locks, and _cache_epoch
_key_locks: Dict[str, threading.Lock] = {}
_cache_epoch = 0


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


class CacheBackpressureError(RuntimeError):
    """Raised when a cold cache fill would block request workers under load."""


# Bound the number of *heavy* computes (full-history reparses) running at once.
# Without this, a burst of requests for distinct cache keys each grabs an AnyIO
# worker token and runs a multi-second parse; the pool saturates (so even cache
# hits and /health can't get a worker) and RSS balloons. Capping heavy work well
# below the worker pool keeps headroom for cheap requests.
# This is the app-side companion to the uvicorn backpressure knobs in cli.py.
_COMPUTE_CONCURRENCY = _positive_int_env("TOKDASH_COMPUTE_CONCURRENCY", 2)
_compute_semaphore = threading.BoundedSemaphore(_COMPUTE_CONCURRENCY)


def _raise_backpressure(message: str, *, key: str, reason: str, had_stale: bool) -> None:
    logger.warning(
        "tokdash cache backpressure key=%s reason=%s had_stale=%s compute_concurrency=%s",
        key,
        reason,
        had_stale,
        _COMPUTE_CONCURRENCY,
    )
    raise CacheBackpressureError(message)


def _cached_route(route_name: str, cache_key: str, fetch_fn) -> Any:
    started = time.monotonic()
    try:
        return get_cached_or_fetch(cache_key, fetch_fn)
    finally:
        logger.debug(
            "tokdash route cache fetch route=%s key=%s duration_ms=%.1f",
            route_name,
            cache_key,
            (time.monotonic() - started) * 1000,
        )


def _key_lock(key: str) -> threading.Lock:
    with _cache_guard:
        lock = _key_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _key_locks[key] = lock
        return lock


def _cache_get(key: str) -> Optional[tuple[float, Any]]:
    with _cache_guard:
        return _cache.get(key)


def _cache_epoch_value() -> int:
    with _cache_guard:
        return _cache_epoch


def _cache_set_if_epoch(key: str, value: Any, epoch: int) -> bool:
    with _cache_guard:
        if epoch != _cache_epoch:
            return False
        _cache[key] = (datetime.now().timestamp(), value)
        return True


def _clear_cache() -> None:
    """Drop all cached responses (e.g. after the pricing DB is edited).

    Only cached values are cleared; per-key locks are left intact. The generation
    counter prevents an in-flight compute that started before this clear from
    repopulating stale values after it finishes.
    """
    global _cache_epoch
    with _cache_guard:
        _cache_epoch += 1
        _cache.clear()


def get_cached_or_fetch(key: str, fetch_fn) -> Any:
    """Cache with single-flight, stale-while-revalidate, and a heavy-compute cap.

    - Fresh hit (age < TTL): returned immediately with no locking or worker contention.
    - Stale hit: at most one request refreshes the key; concurrent callers keep
      getting the stale value instead of stampeding the parser.
    - Cold miss: if this key or the global heavy-compute pool is already busy, fail
      fast with ``CacheBackpressureError`` so request workers do not pile up while
      blocked. A later request can retry once the in-flight fill finishes.
    - A global semaphore bounds how many heavy computes run at once across all keys.
    """
    now = datetime.now().timestamp()
    hit = _cache_get(key)
    if hit is not None and now - hit[0] < CACHE_TTL:
        return hit[1]

    lock = _key_lock(key)
    if not lock.acquire(blocking=False):
        # Another thread is already computing this key.
        if hit is not None:
            return hit[1]  # serve stale rather than stampede the parser
        _raise_backpressure(
            f"Cache fill already in progress for {key}",
            key=key,
            reason="same_key_inflight",
            had_stale=False,
        )
    try:
        # Re-check under the lock: a prior holder may have just stored a fresh value.
        latest = _cache_get(key)
        if latest is not None and datetime.now().timestamp() - latest[0] < CACHE_TTL:
            return latest[1]
        epoch = _cache_epoch_value()
        if not _compute_semaphore.acquire(blocking=False):
            if latest is not None:
                return latest[1]
            _raise_backpressure(
                "Too many cold requests; retry shortly",
                key=key,
                reason="compute_cap",
                had_stale=False,
            )
        try:
            fresh = fetch_fn()
        finally:
            _compute_semaphore.release()
        _cache_set_if_epoch(key, fresh, epoch)
        return fresh
    finally:
        lock.release()


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


@app.get("/api/pricing-db")
def get_pricing_db() -> Dict[str, Any]:
    try:
        data = _validate_pricing_db(json.loads(PRICING_DB_PATH.read_text(encoding="utf-8")))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="pricing_db.json not found")
    except JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"pricing_db.json is invalid JSON: {e.msg}")
    return {"path": str(PRICING_DB_PATH), "data": data, "text": _format_pricing_db(data)}


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
    tmp_path = PRICING_DB_PATH.with_suffix(PRICING_DB_PATH.suffix + ".tmp")
    try:
        tmp_path.write_text(formatted, encoding="utf-8")
        tmp_path.replace(PRICING_DB_PATH)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to write pricing_db.json: {e}")

    reload_pricing_db()
    _clear_cache()
    return {"path": str(PRICING_DB_PATH), "data": data, "text": formatted}


@app.get("/api/usage")
def get_usage(period: str = "today", date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict[str, Any]:
    _validate_date_params(date_from, date_to)
    try:
        cache_key = f"usage_{period}_{date_from}_{date_to}"
        return _cached_route(
            "/api/usage",
            cache_key,
            lambda: compute_usage_with_comparison(period, date_from, date_to),
        )
    except CacheBackpressureError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/openclaw")
def get_openclaw(period: str = "today") -> Dict[str, Any]:
    def fetch():
        data = get_openclaw_data(period)
        data["period"] = period
        data["timestamp"] = datetime.now().isoformat()
        return data

    try:
        return _cached_route("/api/openclaw", f"openclaw_{period}", fetch)
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
            data["timestamp"] = datetime.now().isoformat()
            return data

        return _cached_route("/api/tools", f"tools_{period}", fetch)
    except CacheBackpressureError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/codex/sessions")
def get_codex_sessions(period: str = "today", include_review_sessions: Optional[bool] = None) -> Dict[str, Any]:
    try:
        cache_key = f"codex_sessions_{period}_{include_review_sessions}"
        return _cached_route(
            "/api/codex/sessions",
            cache_key,
            lambda: get_codex_sessions_data(period, include_review_sessions=include_review_sessions),
        )
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
        cache_key = f"sessions_{tool.strip().lower()}_{period}_{date_from}_{date_to}_{include_review_sessions}"
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
@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    html_path = STATIC_DIR / "index.html"
    if not html_path.exists():
        return HTMLResponse(content="<h1>Dashboard not found</h1><p>Please create static/index.html</p>", status_code=404)
    return FileResponse(html_path, headers=NO_CACHE_HEADERS)


@app.get("/manifest.webmanifest")
async def serve_manifest():
    path = STATIC_DIR / "manifest.webmanifest"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Manifest not found")
    return FileResponse(path, media_type="application/manifest+json", headers=NO_CACHE_HEADERS)


@app.get("/sw.js")
async def serve_service_worker():
    path = STATIC_DIR / "sw.js"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Service worker not found")
    content = path.read_text(encoding="utf-8").replace(SW_CACHE_NAME_PLACEHOLDER, get_static_cache_name())
    return Response(content=content, media_type="application/javascript", headers=NO_CACHE_HEADERS)


@app.get("/api/stats")
def get_stats(year: Optional[int] = None) -> Dict[str, Any]:
    try:
        return _cached_route("/api/stats", f"stats_{year}", lambda: compute_stats(year))
    except CacheBackpressureError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health_check():
    # async so the liveness probe answers even when every worker thread is busy in a
    # heavy compute — this is what makes an external /health watchdog reliable (P4).
    return {"status": "ok"}
