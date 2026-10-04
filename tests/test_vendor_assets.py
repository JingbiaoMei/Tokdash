"""Dashboard libraries must be local, packaged, and cached for offline use."""
import re
from pathlib import Path

from fastapi.testclient import TestClient

import tokdash
import tokdash.api as api

STATIC = Path(tokdash.__file__).parent / "static"
VENDOR_PATHS = [
    "/static/vendor/tailwindcss-3.4.17.js",
    "/static/vendor/chart-4.4.0.umd.min.js",
    "/static/vendor/three-0.160.0.min.js",
    "/static/vendor/flatpickr-4.6.13.min.css",
    "/static/vendor/flatpickr-4.6.13.min.js",
]


def test_dashboard_loads_packaged_vendor_assets_in_order():
    source = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "cdn.jsdelivr.net" not in source
    assert "cdn.tailwindcss.com" not in source
    paths = re.findall(r"window\.tokdashPath\('(/static/vendor/[^']+)'\)", source)
    assert paths == VENDOR_PATHS
    assert re.findall(r"/static/vendor/[^\"\'\s)]+", source) == paths
    for path in paths:
        assert (STATIC / path.removeprefix("/static/")).is_file()
    tailwind = source.index(VENDOR_PATHS[0])
    config = source.index("if (window.tailwind) tailwind.config")
    assert tailwind < config < source.index(VENDOR_PATHS[1])
    assert 'onload="window.__tokdashFlatpickrCssLoaded = true"' in source
    assert "https://fonts.googleapis.com" in source


def test_service_worker_precaches_every_vendor_asset():
    source = (STATIC / "sw.js").read_text(encoding="utf-8")
    core = source.split("const CORE_ASSETS = [", 1)[1].split("];", 1)[0]
    for path in VENDOR_PATHS:
        assert f'appPath("{path}")' in core


def test_library_failure_notices_do_not_blame_a_cdn():
    source = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "from its CDN" not in source
    assert (STATIC / "vendor/NOTICE.txt").is_file()


def test_vendor_assets_are_cached_and_other_static_files_are_not():
    """Versioned vendor files may stay in the browser cache, as they did from the
    CDNs; the rest of /static/ is still revalidated so an upgrade shows up, and a
    missing vendor file is not remembered as missing."""
    client = TestClient(api.app)
    vendor = client.get(VENDOR_PATHS[2])
    assert vendor.status_code == 200
    assert vendor.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "pragma" not in vendor.headers

    for path in ("/static/themes.css", "/static/vendor/no-such-library.js"):
        response = client.get(path)
        assert "no-store" in response.headers["cache-control"], path
