from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).parent / "static"

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

SW_CACHE_NAME_PLACEHOLDER = "__TOKDASH_CACHE_NAME__"


def build_static_cache_name() -> str:
    digest = hashlib.sha256()
    for path in sorted(STATIC_DIR.rglob("*")):
        if not path.is_file():
            continue
        digest.update(path.relative_to(STATIC_DIR).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return f"tokdash-{digest.hexdigest()[:12]}"


STATIC_CACHE_NAME = build_static_cache_name()


class NoCacheStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.update(NO_CACHE_HEADERS)
        return response
