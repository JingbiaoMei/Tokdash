from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Optional

STATIC_DIR = Path(__file__).parent / "static"

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

# Vendored libraries carry their version in the filename, so a URL under
# /static/vendor/ never changes content and the browser can keep it, as it did
# when the same files came from the CDNs.
VENDOR_CACHE_HEADERS = {
    "Cache-Control": "public, max-age=31536000, immutable",
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


# scripts/vendor_cjk_fonts.sh can vendor a Simplified-Chinese sans face for a
# machine with no CJK font of its own and no path to a font CDN. The directory
# carries the upstream version in its name because /static/vendor/ is cached for
# a year, and a re-download must not be answerable under a stale cached name.
CJK_FONT_CSS_PLACEHOLDER = "__TOKDASH_CJK_FONTS__"
CJK_FONT_CSS_GLOB = "vendor/fonts/noto-sans-sc-*/noto-sans-sc.css"


def _cjk_font_css_rank(path: Path) -> tuple[int, str]:
    """Order vendored font directories by the upstream version in the name."""
    digits = "".join(char for char in path.parent.name if char.isdigit())
    return (int(digits) if digits else 0, path.parent.name)


@lru_cache(maxsize=1)
def local_cjk_font_css() -> str:
    """Return the URL of a vendored CJK stylesheet, or '' when none is installed.

    Cached for the process lifetime, so installing a face takes a restart of the
    dashboard rather than a reload.
    """
    candidates = sorted(STATIC_DIR.glob(CJK_FONT_CSS_GLOB), key=_cjk_font_css_rank)
    if not candidates:
        return ""
    return "/static/" + candidates[-1].relative_to(STATIC_DIR).as_posix()
