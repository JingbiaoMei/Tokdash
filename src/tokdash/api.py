from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .compute import compute_stats, compute_usage, get_openclaw_data, get_tools_data

app = FastAPI(title="Tokdash")
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


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


@app.get("/api/usage")
def get_usage(period: str = "today") -> Dict[str, Any]:
    try:
        return get_cached_or_fetch(f"usage_{period}", lambda: compute_usage(period))
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


@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    html_path = STATIC_DIR / "index.html"
    if not html_path.exists():
        return HTMLResponse(content="<h1>Dashboard not found</h1><p>Please create static/index.html</p>", status_code=404)
    return FileResponse(html_path)

@app.get("/manifest.webmanifest")
def serve_manifest():
    path = STATIC_DIR / "manifest.webmanifest"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Manifest not found")
    return FileResponse(path, media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
def serve_service_worker():
    path = STATIC_DIR / "sw.js"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Service worker not found")
    return FileResponse(path, media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/api/stats")
def get_stats(year: Optional[int] = None) -> Dict[str, Any]:
    try:
        return get_cached_or_fetch(f"stats_{year}", lambda: compute_stats(year))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
def health_check():
    return {"status": "ok"}
