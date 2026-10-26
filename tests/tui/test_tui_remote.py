"""tokdash.tui.remote: fail-closed probe + metadata-stripping GET, WITHOUT sockets.

Every opener here is a fake object: not one test in this file (or, enforced by
the session fixture in conftest, anywhere in the suite) opens a socket or probes
localhost — the live dev service on the manifest port is nobody to poke.
"""
from __future__ import annotations

import io
import json
import socket
import sys
import urllib.error

import pytest

from tokdash import __version__
from tokdash.api import CacheBackpressureError
from tokdash.onboard.plan import DEFAULT_PORT
from tokdash.tui import remote

SVC = remote.RemoteService(base="http://127.0.0.1:55999", version=__version__)


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeOpener:
    """Stands in for urllib's OpenerDirector. Raises Exception results verbatim."""

    def __init__(self, result):
        self.result = result
        self.requests: list[tuple[str, float | None]] = []

    def open(self, url, timeout=None):
        self.requests.append((url, timeout))
        if isinstance(self.result, Exception):
            raise self.result
        body = self.result if isinstance(self.result, bytes) else json.dumps(self.result).encode()
        return _FakeResponse(body)


def _install_opener(monkeypatch, result) -> _FakeOpener:
    opener = _FakeOpener(result)
    monkeypatch.setattr(remote, "_get_opener", lambda: opener)
    return opener


def _http_error(code: int, body: bytes = b"server said no") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://x/y", code, "err", {}, io.BytesIO(body)  # fp: HTTPError.read() reads it
    )


@pytest.fixture()
def no_kill_switch(monkeypatch):
    """The suite-wide TOKDASH_TUI_NO_REMOTE=1 is lifted for probe tests only."""
    monkeypatch.delenv(remote.KILL_SWITCH_ENV, raising=False)
    # Never let a probe test read the developer's install manifest.
    monkeypatch.setattr(remote, "read_manifest", lambda: None)


# ---------------------------------------------------------------------------
# probe_service: fail-closed matrix
# ---------------------------------------------------------------------------

def test_probe_returns_none_when_kill_switch_set(monkeypatch):
    opener = _install_opener(
        monkeypatch, {"status": "ok", "service": "tokdash", "version": __version__}
    )
    monkeypatch.setattr(remote, "read_manifest", lambda: {"bind": "127.0.0.1", "port": 55999})
    monkeypatch.setenv(remote.KILL_SWITCH_ENV, "1")
    assert remote.probe_service() is None
    assert opener.requests == []  # None BEFORE any socket code, not after a probe


def test_probe_uses_manifest_host_and_port(no_kill_switch, monkeypatch):
    # bind "0.0.0.0" maps to the probe host exactly like doctor's port probe
    # (engine._probe_host_for_bind), and the port is the recorded one.
    monkeypatch.setattr(remote, "read_manifest", lambda: {"bind": "0.0.0.0", "port": 55999})
    opener = _install_opener(
        monkeypatch, {"status": "ok", "service": "tokdash", "version": __version__}
    )
    svc = remote.probe_service()
    assert svc == remote.RemoteService(base="http://127.0.0.1:55999", version=__version__)
    assert opener.requests[0][0] == "http://127.0.0.1:55999/health"
    assert opener.requests[0][1] == pytest.approx(0.5)


def test_probe_falls_back_to_default_port_without_manifest(no_kill_switch, monkeypatch):
    # DEFAULT_PORT is imported from onboard.plan — the number is asserted through
    # the import, never re-typed here. (The no_kill_switch fixture already
    # patches read_manifest to None: the "manifest missing" world.)
    opener = _install_opener(
        monkeypatch, {"status": "ok", "service": "tokdash", "version": __version__}
    )
    svc = remote.probe_service()
    assert svc is not None and svc.base == f"http://127.0.0.1:{DEFAULT_PORT}"
    assert f"/health" in opener.requests[0][0]
    assert f":{DEFAULT_PORT}/health" in opener.requests[0][0]


@pytest.mark.parametrize(
    "body",
    [
        {"status": "ok", "service": "other-app", "version": __version__},  # foreign app
        {"status": "ok", "service": "tokdash", "version": "99.99.98"},  # version skew
        {"status": "ok", "service": "tokdash"},  # missing version field
        {"status": "ok"},  # missing service field: never trust generic ok
        [1, 2, 3],  # non-dict body
    ],
)
def test_probe_rejects_mismatched_fingerprints(no_kill_switch, monkeypatch, body):
    _install_opener(monkeypatch, body)
    assert remote.probe_service() is None


@pytest.mark.parametrize(
    "exc",
    [
        urllib.error.URLError("no route"),
        socket.timeout("timed out"),
        OSError("sandbox says no"),  # socket creation itself (mirrors detect.probe_port)
        _http_error(500),
        ValueError("not json"),
    ],
)
def test_probe_swallows_every_exception_class(no_kill_switch, monkeypatch, exc):
    _install_opener(monkeypatch, exc)
    assert remote.probe_service() is None


# ---------------------------------------------------------------------------
# remote_get: metadata split + error mapping
# ---------------------------------------------------------------------------

def test_remote_get_strips_and_returns_response_cache(monkeypatch):
    meta = {"status": "stale", "served_from_cache": True, "age_seconds": 7.5}
    _install_opener(monkeypatch, {"tokens": 5, "response_cache": meta})
    payload, got_meta = remote.remote_get(SVC, "/api/usage", {"period": "today"})
    assert payload == {"tokens": 5}  # injected block popped OUT of the payload
    assert got_meta == meta
    assert "response_cache" not in payload


def test_remote_get_without_injection_returns_none_meta(monkeypatch):
    # /api/stats answers via _cached_route WITHOUT include_cache_metadata.
    _install_opener(monkeypatch, {"contributions": []})
    payload, meta = remote.remote_get(SVC, "/api/stats", {"year": 2026})
    assert payload == {"contributions": []} and meta is None


def test_remote_get_503_maps_to_backpressure(monkeypatch):
    _install_opener(monkeypatch, _http_error(503))
    with pytest.raises(CacheBackpressureError):
        remote.remote_get(SVC, "/api/usage", {})


def test_remote_get_404_maps_to_service_gone(monkeypatch):
    _install_opener(monkeypatch, _http_error(404))
    with pytest.raises(remote.ServiceGone):
        remote.remote_get(SVC, "/api/insights", {})


def test_remote_get_urlerror_maps_to_service_gone(monkeypatch):
    _install_opener(monkeypatch, urllib.error.URLError("connection refused"))
    with pytest.raises(remote.ServiceGone):
        remote.remote_get(SVC, "/api/usage", {})


def test_remote_get_other_http_error_carries_status_and_excerpt(monkeypatch):
    _install_opener(monkeypatch, _http_error(500, b"database schema too new"))
    with pytest.raises(RuntimeError, match="500") as exc:
        remote.remote_get(SVC, "/api/usage", {})
    assert "database schema too new" in str(exc.value)


def test_remote_get_query_omits_none_and_lowercases_bools(monkeypatch):
    opener = _install_opener(monkeypatch, {"ok": True})
    remote.remote_get(
        SVC,
        "/api/active-time",
        {
            "period": "today",
            "date_to": None,  # absent params answer with route defaults
            "include_review_sessions": False,
            "refresh": True,
            "year": 2026,
        },
        timeout=3.5,
    )
    url, timeout = opener.requests[0]
    assert "period=today" in url
    assert "date_to" not in url  # None params omitted entirely
    assert "refresh=true" in url and "include_review_sessions=false" in url  # lowercase
    assert "year=2026" in url
    assert timeout == 3.5


# ---------------------------------------------------------------------------
# Import discipline (local proof: remote.py stays pure stdlib)
# ---------------------------------------------------------------------------

def test_remote_imports_no_rich_or_textual(monkeypatch):
    # Re-execute remote.py's own module body with rich poisoned: any
    # rich/textual import anywhere in its (or its fresh deps') body raises.
    import importlib

    monkeypatch.setitem(sys.modules, "rich", None)
    monkeypatch.setitem(sys.modules, "textual", None)
    # delitem: monkeypatch puts the ORIGINAL module object back at teardown, so
    # data._remote and every top-level import stay the same objects after this.
    monkeypatch.delitem(sys.modules, "tokdash.tui.remote", raising=False)
    mod = importlib.import_module("tokdash.tui.remote")  # re-executes the module body
    assert mod.__file__ == remote.__file__
    assert issubclass(mod.ServiceGone, RuntimeError)
    assert mod.DEFAULT_PORT == DEFAULT_PORT  # its stdlib/onboard deps imported fine
