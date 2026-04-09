from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

STATIC_DIR = Path(__file__).parent / "static"

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

SW_CACHE_NAME_PLACEHOLDER = "__TOKDASH_CACHE_NAME__"

_static_cache_name: Optional[str] = None


def get_static_cache_name() -> str:
    """Lazily compute and cache a content-based hash of all static assets."""
    global _static_cache_name
    if _static_cache_name is not None:
        return _static_cache_name
    digest = hashlib.sha256()
    for path in sorted(STATIC_DIR.rglob("*")):
        if not path.is_file():
            continue
        digest.update(path.relative_to(STATIC_DIR).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    _static_cache_name = f"tokdash-{digest.hexdigest()[:12]}"
    return _static_cache_name
