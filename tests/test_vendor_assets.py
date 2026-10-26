"""Dashboard libraries must be local, packaged, and cached for offline use.

The service worker that caches them must also leave every other origin alone:
the multi-server client fetches added Tokdash servers directly from the page.
"""
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

import tokdash
import tokdash.api as api

from support.frontend import needs_node, run_node

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


# The worker's own globals, stubbed just far enough to dispatch a fetch event and
# see whether the worker answered it (respondWith) or let the browser handle it.
SERVICE_WORKER_HARNESS = """
const listeners = {};
const self = {
  location: { origin: 'http://127.0.0.1:55423' },
  addEventListener: (type, fn) => { listeners[type] = fn; },
  skipWaiting() {},
  clients: { claim() {} },
};
const caches = {
  open: async () => ({ put: async () => {}, addAll: async () => {} }),
  match: async () => undefined,
  keys: async () => [],
  delete: async () => true,
};
const fetch = async () => ({ clone() { return this; } });

__SERVICE_WORKER__

const answered = ([url, mode]) => {
  let handled = false;
  listeners.fetch({
    request: { method: 'GET', url, mode },
    respondWith() { handled = true; },
    waitUntil() {},
  });
  return handled;
};
process.stdout.write(JSON.stringify(JSON.parse(process.argv[2]).map(answered)));
"""


@needs_node
def test_service_worker_only_answers_its_own_origin(tmp_path):
    """Added servers are fetched straight from the page, so the worker must never
    intercept, cache or rewrite a request to another origin -- not even a path
    that looks like one of its own static assets."""
    script = tmp_path / "sw.js"
    script.write_text(
        SERVICE_WORKER_HARNESS.replace("__SERVICE_WORKER__", api._render_service_worker("")),
        encoding="utf-8",
    )
    requests = [
        ["http://127.0.0.1:55423/static/vendor/chart-4.4.0.umd.min.js", "no-cors"],
        ["http://127.0.0.1:55423/", "navigate"],
        ["http://127.0.0.1:55423/api/usage?period=today", "cors"],
        ["https://box.tail76535.ts.net/static/vendor/chart-4.4.0.umd.min.js", "cors"],
        ["http://127.0.0.1:55424/static/themes.css", "cors"],
    ]
    answered = json.loads(run_node(script, json.dumps(requests)))
    # Same origin: static assets and the app shell go through the worker, the API never does.
    assert answered[:3] == [True, True, False]
    # Another host, or the same host on another port, is another daemon's business.
    assert answered[3:] == [False, False]


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
