"""Read-only HTTP delegation from a fresh TUI/report process to a live ``tokdash serve``.

Why this exists: ``api._cache`` is a process-local ``OrderedDict``, so every fresh
``tokdash tui`` / ``tokdash report`` process cold-computes what an already-warm
service computed minutes ago — and its keys match the server's warmer keys exactly,
so the answers are the same answers. When a same-version Tokdash answers on the
port the install manifest records, ask it; when anything says otherwise (no
manifest, no service, foreign app on the port, version skew, connection died
mid-request) fail closed and let ``tui/data.py`` fall back to the in-process
cache/compute path with those same keys.

Hard boundaries (each has a test in test_tui_remote.py):

- Pure stdlib. NO textual/rich import here — the same headless law as ``data.py``
  (that file imports this one), and the report path must stay importable on a
  stripped install.
- Read-only: this module only ever GETs. No POST, no writes, ever.
- Tests never open a real socket: they monkeypatch :func:`_get_opener`. Tests
  never probe localhost either: the suite's conftest sets TOKDASH_TUI_NO_REMOTE=1
  and :func:`probe_service` returns ``None`` before any socket code runs.
- Version skew is strict equality against ``__version__`` (mirrors the /health
  fingerprint gate in ``onboard.detect.probe_port``: identity comes from the
  distinctive service field, never a generic ``{"status": "ok"}``). A dev build
  beside an installed service of another version falls back to in-process —
  documented, deliberate.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from .. import __version__
from ..api import CacheBackpressureError
from ..onboard.engine import _probe_host_for_bind
from ..onboard.manifest import read_manifest
from ..onboard.plan import DEFAULT_PORT

# Set (non-empty) and this module answers "no service" before touching a socket.
# The kill switch for `TOKDASH_TUI_NO_REMOTE=1 tokdash tui` behaving exactly like
# the round-1, no-HTTP build.
KILL_SWITCH_ENV = "TOKDASH_TUI_NO_REMOTE"


class ServiceGone(RuntimeError):
    """The service vanished (or was never reachable).

    data.py latches in-process for the rest of the run on this; ``r`` (refresh)
    re-probes.
    """


@dataclass(frozen=True)
class RemoteService:
    """A live Tokdash we may delegate to."""

    base: str  # e.g. "http://127.0.0.1:55423" — always derived, never hard-coded
    version: str


# ProxyHandler({}) disables proxies for this opener: a corporate HTTP_PROXY must
# never intercept (or leak, or 407) a loopback delegation.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get_opener() -> urllib.request.OpenerDirector:
    """The module opener — the seam tests monkeypatch so no test opens a socket."""
    return _OPENER


def probe_service(*, timeout: float = 0.5) -> Optional[RemoteService]:
    """Is a matching Tokdash live on the install's port? Fail-closed on everything.

    Host and port come from the install manifest (``onboard.manifest.read_manifest``,
    the single guarded reader) with the same bind→host mapping the doctor probe uses
    (``onboard.engine._probe_host_for_bind``), falling back to ``DEFAULT_PORT`` from
    ``onboard.plan`` — the number is never hard-coded here. Any exception, a foreign
    ``service`` field, or a version that isn't byte-equal to ours answers ``None``:
    a wrong host is a fallback, never a wrong answer.
    """
    if os.environ.get(KILL_SWITCH_ENV):
        return None
    try:
        man = read_manifest() or {}
        port = int(man.get("port") or DEFAULT_PORT)
        host = _probe_host_for_bind(str(man.get("bind") or ""))
        with _get_opener().open(f"http://{host}:{port}/health", timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if not isinstance(body, dict) or body.get("service") != "tokdash":
            return None
        version = body.get("version")
        if version != __version__:
            return None  # strict equality; dev builds fall back in-process
        return RemoteService(base=f"http://{host}:{port}", version=str(version))
    except Exception:
        return None


def _http_error_excerpt(exc: urllib.error.HTTPError, limit: int = 180) -> str:
    try:
        return exc.read().decode("utf-8", "replace")[:limit]
    except Exception:
        return ""


def remote_get(
    svc: RemoteService,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    timeout: float = 120.0,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """GET ``path`` on ``svc`` and split payload from the injected cache metadata.

    ``None`` params are omitted (absent params answer with the route's own
    defaults, exactly like a browser that sends nothing); bools go lowercase
    because FastAPI parses ``true``/``false``, not ``True``/``False``.

    Returns ``(payload, metadata)`` where metadata is the route-injected
    ``"response_cache"`` block (``_cached_route`` / ``_response_cache_metadata``:
    ``status`` / ``served_from_cache`` / ``age_seconds``) popped OUT of the
    payload, or ``None`` on routes that don't inject it (e.g. /api/stats — data.py
    synthesizes "hit" for those).

    Error mapping: 503 → :class:`tokdash.api.CacheBackpressureError` (the same
    class the in-process path raises, so ``_with_backpressure_retry`` covers both
    rides); 404 → :class:`ServiceGone` (a route this build doesn't have is not
    this build's server); any other status → ``RuntimeError`` carrying the code
    and a body excerpt; transport-class failures (URLError, socket timeout,
    truncated body, unparseable JSON) → :class:`ServiceGone`.
    """
    query: Dict[str, str] = {}
    for key, value in (params or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            query[key] = "true" if value else "false"
        else:
            query[key] = str(value)
    url = svc.base + path
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    try:
        with _get_opener().open(url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # subclass of URLError — must come first
        if exc.code == 503:
            raise CacheBackpressureError(f"{path} → HTTP 503: {_http_error_excerpt(exc)}") from exc
        if exc.code == 404:
            raise ServiceGone(f"{path} → HTTP 404 at {svc.base}") from exc
        raise RuntimeError(
            f"tokdash service returned HTTP {exc.code} for {path}: {_http_error_excerpt(exc)}"
        ) from exc
    except (
        urllib.error.URLError,
        socket.timeout,
        http.client.IncompleteRead,
        OSError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        raise ServiceGone(f"{path} → service unreachable or answer incomplete: {exc!r}") from exc
    if not isinstance(payload, dict):
        raise ServiceGone(f"{path} → non-object JSON payload from {svc.base}")
    meta = payload.pop("response_cache", None)
    return payload, meta
