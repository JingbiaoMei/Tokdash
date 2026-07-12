"""Phase 0a write-protection: loopback + Host/Origin + per-session token gate on mutations."""
import asyncio

import pytest

pytest.importorskip("fastapi")

import tokdash.api as api


class _Req:
    """Minimal stand-in for a Starlette Request (headers only, lowercased keys)."""

    def __init__(self, headers):
        self.headers = {k.lower(): v for k, v in headers.items()}


@pytest.fixture(autouse=True)
def _loopback_app():
    api.app.state.bind = "127.0.0.1"
    api.app.state.port = 55423
    yield
    # Reset so a later test that relies on the fail-closed unset-bind path can't be
    # polluted by leftover module-global app.state.
    api.app.state.bind = None
    api.app.state.port = None


def _hdrs(**kw):
    h = {"host": "127.0.0.1:55423"}
    h.update({k.replace("_", "-"): v for k, v in kw.items()})
    return h


def _deny(method, headers, **kw):
    return api.mutation_denied_reason(method, _Req(headers).headers, **kw)


# --- safe methods are never gated ------------------------------------------------

def test_get_is_never_gated():
    assert _deny("GET", _hdrs()) is None


# --- token requirement -----------------------------------------------------------

def test_put_allowed_with_valid_token():
    assert _deny("PUT", _hdrs(**{"x-tokdash-token": api._CSRF_TOKEN})) is None


def test_put_denied_without_token():
    assert _deny("PUT", _hdrs()) is not None


def test_put_denied_with_wrong_token():
    assert _deny("PUT", _hdrs(**{"x-tokdash-token": "nope"})) is not None


# --- Host / Origin -------------------------------------------------------------

def test_put_denied_foreign_host():
    assert _deny("PUT", {"host": "evil.example.com", "x-tokdash-token": api._CSRF_TOKEN}) is not None


def test_put_denied_cross_origin():
    h = _hdrs(**{"x-tokdash-token": api._CSRF_TOKEN, "origin": "http://evil.example.com"})
    assert _deny("PUT", h) is not None


def test_put_allowed_same_origin():
    h = _hdrs(**{"x-tokdash-token": api._CSRF_TOKEN, "origin": "http://127.0.0.1:55423"})
    assert _deny("PUT", h) is None


def test_put_denied_cross_referer_when_no_origin():
    h = _hdrs(**{"x-tokdash-token": api._CSRF_TOKEN, "referer": "http://evil.example.com/x"})
    assert _deny("PUT", h) is not None


# --- bind / port ---------------------------------------------------------------

def test_put_denied_non_loopback_bind():
    assert _deny("PUT", _hdrs(**{"x-tokdash-token": api._CSRF_TOKEN}), bind="0.0.0.0") is not None


def test_custom_port_allowlist():
    tok = api._CSRF_TOKEN
    assert _deny("PUT", {"host": "127.0.0.1:8080", "x-tokdash-token": tok}, port=8080) is None
    # the default-port Host must be rejected when serving on a custom port
    assert _deny("PUT", {"host": "127.0.0.1:55423", "x-tokdash-token": tok}, port=8080) is not None


def test_ipv6_loopback_host():
    assert _deny("PUT", {"host": "[::1]:55423", "x-tokdash-token": api._CSRF_TOKEN}) is None


# --- _is_loopback classification -------------------------------------------------

def test_is_loopback_accepts_real_loopback():
    for addr in ("127.0.0.1", "127.5.6.7", "::1", "[::1]", "localhost", "LOCALHOST"):
        assert api._is_loopback(addr), addr


def test_is_loopback_rejects_spoofed_and_non_loopback():
    # A prefix check like startswith("127.") accepted these; an IP parse must not.
    for addr in ("127.0.0.1.evil.com", "127notanip", "0.0.0.0", "::", "10.0.0.1", ""):
        assert not api._is_loopback(addr), addr


# --- /api/csrf-token -----------------------------------------------------------

def test_csrf_token_endpoint_same_origin():
    body = asyncio.run(api.get_csrf_token(_Req(_hdrs())))
    assert body["token"] == api._CSRF_TOKEN


def test_csrf_token_endpoint_foreign_host_403():
    with pytest.raises(api.HTTPException) as exc:
        asyncio.run(api.get_csrf_token(_Req({"host": "evil.example.com"})))
    assert exc.value.status_code == 403


def test_csrf_token_endpoint_non_loopback_bind_403():
    api.app.state.bind = "0.0.0.0"
    with pytest.raises(api.HTTPException) as exc:
        asyncio.run(api.get_csrf_token(_Req(_hdrs())))
    assert exc.value.status_code == 403


def test_csrf_token_endpoint_cross_origin_403():
    # A page on another localhost port must not be able to read the token.
    with pytest.raises(api.HTTPException) as exc:
        asyncio.run(api.get_csrf_token(_Req(_hdrs(**{"origin": "http://localhost:3000"}))))
    assert exc.value.status_code == 403


# --- F1: implicit-port (80/443) origins must NOT pass at a real port -------------

def test_put_denied_implicit_port_origin():
    # Browsers omit :80/:443 from Origin, so a page at http://localhost (:80) sends
    # Origin: http://localhost. With the server on 55423 this must be rejected.
    for origin in ("http://localhost", "https://localhost", "http://127.0.0.1"):
        h = _hdrs(**{"x-tokdash-token": api._CSRF_TOKEN, "origin": origin})
        assert _deny("PUT", h) is not None, origin


def test_implicit_port_allowed_only_when_serving_on_80():
    # Legit case: the server is actually bound to :80, so the bare-host Host/Origin
    # are the correct same-origin values.
    tok = api._CSRF_TOKEN
    h = {"host": "localhost", "x-tokdash-token": tok, "origin": "http://localhost"}
    assert _deny("PUT", h, port=80) is None


def test_origin_scheme_enforced_at_implicit_port():
    # Cross-scheme must be rejected even at an implicit port: an https://localhost (:443)
    # page must NOT pass the gate of an http server on :80 (and vice versa).
    tok = api._CSRF_TOKEN
    assert _deny("PUT", {"host": "localhost", "x-tokdash-token": tok, "origin": "https://localhost"}, port=80) is not None
    assert _deny("PUT", {"host": "localhost", "x-tokdash-token": tok, "origin": "http://localhost"}, port=80) is None


# --- F3: the write guard is actually wired into the ASGI stack -------------------


def _asgi_status(method: str, path: str, headers: dict) -> int:
    """Drive one request through the real app (middleware + routing) via raw ASGI.

    Deliberately avoids TestClient/ASGITransport — this repo documents a sync-handler
    deadlock there (test_api_smoke.py). A denied mutation 403s inside the middleware
    before the sync handler runs, and GET /health is async, so a raw ASGI call is safe.
    """
    sent = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 55423),
        "scheme": "http",
    }

    async def receive():
        return {"type": "http.request", "body": b'{"data":{"models":{}}}', "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(api.app(scope, receive, send))
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def test_write_guard_registered_and_blocks_through_asgi(monkeypatch):
    monkeypatch.setenv("TOKDASH_WARM_ON_START", "0")
    # bind/port already set by the autouse fixture.
    assert _asgi_status("GET", "/health", {"host": "127.0.0.1:55423"}) == 200
    assert _asgi_status("GET", "/tokdash/health", {"host": "127.0.0.1:55423"}) == 200
    # Foreign Host -> middleware 403, short-circuiting before the sync handler.
    denied = _asgi_status("PUT", "/api/pricing-db", {"host": "evil.example.com", "content-type": "application/json"})
    assert denied == 403


def test_update_check_post_endpoints_are_write_gated(monkeypatch):
    # POST /api/update-check/consent writes config.json, so it must stay gated end-to-end,
    # not just the PUT. /api/update-check itself is now GET (see
    # test_update_check_get_is_not_write_gated below) since it is read-only.
    monkeypatch.setenv("TOKDASH_WARM_ON_START", "0")
    monkeypatch.delenv("TOKDASH_UPDATE_CHECK", raising=False)
    from tokdash.onboard import updatecheck

    assert updatecheck.is_enabled() is False
    assert _asgi_status("POST", "/api/update-check/consent", {"host": "evil.example.com", "content-type": "application/json"}) == 403
    # the denied consent must NOT have persisted (the gate stopped it before the handler)
    assert updatecheck.is_enabled() is False


def test_quota_post_endpoints_are_write_gated(monkeypatch):
    monkeypatch.setenv("TOKDASH_WARM_ON_START", "0")
    assert _asgi_status("POST", "/api/quota/consent", {"host": "evil.example.com", "content-type": "application/json"}) == 403


def test_quota_refresh_get_is_not_write_gated(monkeypatch):
    # /api/quota/refresh is GET, not a mutating method (see _MUTATING_METHODS), because it
    # only polls providers' read-only usage endpoints -- no quota is consumed. It must NOT be
    # blocked by the loopback write-guard, even from a non-loopback Host, so it keeps working
    # over Tailscale/WSL/any forward.
    #
    # Verified with pure, I/O-free checks -- deliberately NOT by driving the handler through
    # the raw-ASGI helper. refresh_quota is a SYNC (`def`) handler, which FastAPI dispatches
    # on a threadpool (anyio.to_thread.run_sync); doing that under a bare asyncio.run(app())
    # deadlocks in some environments. (The update-check GET test may use the ASGI helper only
    # because its handler is `async def`, run directly on the loop -- no threadpool.)
    from fastapi import HTTPException

    # 1) The route is registered as GET, not a mutating method.
    route = next(r for r in api.app.routes if getattr(r, "path", "") == "/api/quota/refresh")
    assert "GET" in route.methods and "POST" not in route.methods
    # 2) The write-guard admits a GET regardless of Host/Origin (pure function, no I/O).
    assert api.mutation_denied_reason("GET", {"host": "evil.example.com"}) is None
    # 3) The handler short-circuits to 409 under the kill switch, before any network/session
    #    I/O -- called directly (no ASGI, no threadpool) so it can never hang.
    monkeypatch.setenv("TOKDASH_QUOTA_POLL", "0")
    with pytest.raises(HTTPException) as exc:
        api.refresh_quota()
    assert exc.value.status_code == 409


def test_update_check_get_is_not_write_gated(monkeypatch):
    # /api/update-check is GET, not a mutating method (see _MUTATING_METHODS), because it only
    # does a read-only PyPI check plus an in-memory cache -- no disk write. It must NOT be
    # blocked by the loopback write-guard, even from a non-loopback Host, so the "update
    # available" badge keeps working over Tailscale/WSL/any forward.
    #
    # Primary, I/O-free check (cannot hang): the guard admits a GET regardless of Host/Origin.
    assert api.mutation_denied_reason("GET", {"host": "evil.example.com"}) is None
    # End-to-end check through the real middleware+routing. Update checks are disabled by
    # default (isolated data dir) so the handler short-circuits to enabled:False; stubbing
    # updatecheck.check keeps the test hermetic (no real PyPI network call) even if that
    # short-circuit ever regressed -- this test exercises the guard, not the PyPI-check logic.
    from tokdash.onboard import updatecheck

    monkeypatch.setenv("TOKDASH_WARM_ON_START", "0")
    monkeypatch.delenv("TOKDASH_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(
        updatecheck,
        "check",
        lambda *a, **k: {"current": "x", "latest": None, "update_available": False, "error": None, "cached": False},
    )
    status = _asgi_status("GET", "/api/update-check", {"host": "evil.example.com"})
    assert status == 200  # reached the handler; the write-guard did not 403 it


def test_authorized_consent_is_admitted_through_asgi(monkeypatch):
    monkeypatch.setenv("TOKDASH_WARM_ON_START", "0")
    monkeypatch.delenv("TOKDASH_UPDATE_CHECK", raising=False)
    from tokdash.onboard import updatecheck

    status = _asgi_status(
        "POST", "/api/update-check/consent",
        {"host": "127.0.0.1:55423", "origin": "http://127.0.0.1:55423",
         "content-type": "application/json", "x-tokdash-token": api._CSRF_TOKEN},
    )
    assert status == 200 and updatecheck.is_enabled() is True


def test_non_ascii_token_denied_not_500():
    # secrets.compare_digest raises on non-ASCII str; the gate must return a denial, not crash.
    reason = _deny("PUT", _hdrs(**{"x-tokdash-token": "café"}))
    assert reason is not None and "token" in reason.lower()


def test_malformed_referer_denied_not_500():
    # A malformed Referer makes urlsplit raise ValueError ("Invalid IPv6 URL"); the gate must
    # fail closed (return a denial reason), never let it bubble out as an HTTP 500.
    # (Regression: secgate-01.)
    assert api._origin_value("http://[") == ""  # unparseable -> empty, never raises
    reason = _deny("PUT", _hdrs(**{"x-tokdash-token": api._CSRF_TOKEN, "referer": "http://["}))
    assert reason is not None and "referer" in reason.lower()


def test_malformed_referer_csrf_token_endpoint_403_not_500():
    # Same crash surface on the same-origin-gated csrf-token route: must 403, not 500.
    with pytest.raises(api.HTTPException) as exc:
        asyncio.run(api.get_csrf_token(_Req(_hdrs(**{"referer": "http://["}))))
    assert exc.value.status_code == 403


def test_malformed_referer_returns_403_through_asgi(monkeypatch):
    # End-to-end through the real middleware: a malformed Referer 403s instead of 500ing.
    monkeypatch.setenv("TOKDASH_WARM_ON_START", "0")
    status = _asgi_status(
        "PUT", "/api/pricing-db",
        {"host": "127.0.0.1:55423", "referer": "http://[",
         "content-type": "application/json", "x-tokdash-token": api._CSRF_TOKEN},
    )
    assert status == 403
