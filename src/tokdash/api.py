from __future__ import annotations

import json
import os
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


app = FastAPI(title="Tokdash")
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
CACHE_TTL = int(os.environ.get("TOKDASH_CACHE_TTL", "120"))  # seconds


def get_cached_or_fetch(key: str, fetch_fn) -> Any:
    now = datetime.now().timestamp()
    if key in _cache:
        cached_time, cached_data = _cache[key]
        if now - cached_time < CACHE_TTL:
            return cached_data
    data = fetch_fn()
    _cache[key] = (now, data)
    return data


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

    _cache.clear()
    reload_pricing_db()
    return {"path": str(PRICING_DB_PATH), "data": data, "text": formatted}


@app.get("/api/usage")
def get_usage(period: str = "today", date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict[str, Any]:
    _validate_date_params(date_from, date_to)
    try:
        cache_key = f"usage_{period}_{date_from}_{date_to}"
        return get_cached_or_fetch(cache_key, lambda: compute_usage_with_comparison(period, date_from, date_to))
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
        return get_cached_or_fetch(f"openclaw_{period}", fetch)
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

        return get_cached_or_fetch(f"tools_{period}", fetch)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/codex/sessions")
def get_codex_sessions(period: str = "today") -> Dict[str, Any]:
    try:
        return get_cached_or_fetch(f"codex_sessions_{period}", lambda: get_codex_sessions_data(period))
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
def get_sessions(tool: str, period: str = "today", date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict[str, Any]:
    _validate_date_params(date_from, date_to)
    try:
        cache_key = f"sessions_{tool.strip().lower()}_{period}_{date_from}_{date_to}"
        return get_cached_or_fetch(cache_key, lambda: get_sessions_data(tool, period, date_from, date_to))
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


@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    html_path = STATIC_DIR / "index.html"
    if not html_path.exists():
        return HTMLResponse(content="<h1>Dashboard not found</h1><p>Please create static/index.html</p>", status_code=404)
    return FileResponse(html_path, headers=NO_CACHE_HEADERS)


@app.get("/manifest.webmanifest")
def serve_manifest():
    path = STATIC_DIR / "manifest.webmanifest"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Manifest not found")
    return FileResponse(path, media_type="application/manifest+json", headers=NO_CACHE_HEADERS)


@app.get("/sw.js")
def serve_service_worker():
    path = STATIC_DIR / "sw.js"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Service worker not found")
    content = path.read_text(encoding="utf-8").replace(SW_CACHE_NAME_PLACEHOLDER, get_static_cache_name())
    return Response(content=content, media_type="application/javascript", headers=NO_CACHE_HEADERS)


@app.get("/api/stats")
def get_stats(year: Optional[int] = None) -> Dict[str, Any]:
    try:
        return get_cached_or_fetch(f"stats_{year}", lambda: compute_stats(year))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
def health_check():
    return {"status": "ok"}
