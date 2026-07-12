"""Opt-in, default-off update check (plan §14).

No automatic background checks. A check happens only when explicitly requested AND enabled
via ``TOKDASH_UPDATE_CHECK=1`` or one-time consent in ``<data_dir>/config.json``;
``TOKDASH_UPDATE_CHECK=0`` is a hard kill switch that overrides consent. Results are cached
for hours so repeated checks don't hammer PyPI. Used by both the loopback API endpoint and
``tokdash doctor``.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Any, Dict

from . import paths

PYPI_URL = "https://pypi.org/pypi/tokdash/json"
_TTL_SECONDS = 6 * 3600
_cache: Dict[str, Any] = {"ts": 0.0, "data": None}


def kill_switched() -> bool:
    """True when ``TOKDASH_UPDATE_CHECK`` is set to a disabling value.

    A hard override that wins over any persisted consent. Recognizes
    ``0``/``false``/``no``/``off`` (case-insensitive). Shared with :func:`is_enabled` and
    the setup consent step so the two never diverge on which forms count as "off".
    """
    return os.environ.get("TOKDASH_UPDATE_CHECK", "").strip().lower() in {"0", "false", "no", "off"}


def is_enabled() -> bool:
    if kill_switched():
        return False  # hard kill switch wins over any consent
    env = os.environ.get("TOKDASH_UPDATE_CHECK", "").strip().lower()
    if env in {"1", "true", "yes", "on"}:
        return True
    try:
        cfg = json.loads(paths.config_path().read_text(encoding="utf-8"))
        return isinstance(cfg, dict) and bool(cfg.get("update_check"))
    except Exception:
        return False


def enable() -> None:
    """Persist one-time consent to ``config.json`` (used by the dashboard/CLI consent step)."""
    p = paths.config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        cfg = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
        if not isinstance(cfg, dict):
            cfg = {}
    except Exception:
        cfg = {}
    cfg["update_check"] = True
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


def _version_key(value: str) -> tuple:
    parts = []
    for token in str(value).split("."):
        digits = ""
        for ch in token:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    # Drop trailing zero components so 'X.Y.Z' == 'X.Y.Z.0' == 'X.Y' (when equal),
    # otherwise tuple comparison treats the longer tuple as greater (phantom update).
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _is_newer(latest: str, current: str) -> bool:
    # Prefer PEP 440-correct ordering (handles pre/post/dev/release-candidate, and
    # '1.0.0' == '1.0'); fall back to the digit-tuple heuristic if packaging is absent.
    try:
        from packaging.version import Version

        return Version(str(latest)) > Version(str(current))
    except Exception:
        pass
    try:
        return _version_key(latest) > _version_key(current)
    except Exception:
        return False


def check(current_version: str, *, timeout: float = 3.0, use_cache: bool = True) -> Dict[str, Any]:
    """Query PyPI for the latest version. Never raises; reports errors in the result."""
    now = time.monotonic()
    if use_cache and _cache["data"] is not None and now - _cache["ts"] < _TTL_SECONDS:
        # Recompute the verdict against the CALLER's current_version — the cached
        # update_available was computed against whatever current was live when it was stored.
        latest = _cache["data"]["latest"]
        return {
            "current": current_version,
            "latest": latest,
            "update_available": bool(latest) and _is_newer(latest, current_version),
            "error": _cache["data"]["error"],
            "cached": True,
        }

    result: Dict[str, Any] = {"current": current_version, "latest": None, "update_available": False, "error": None, "cached": False}
    try:
        with urllib.request.urlopen(PYPI_URL, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        latest = (data.get("info") or {}).get("version")
        result["latest"] = latest
        result["update_available"] = bool(latest) and _is_newer(latest, current_version)
    except Exception as exc:
        result["error"] = str(exc)
        return result  # don't cache failures

    # Only persist the cache on a real (use_cache) call so use_cache=False is fully
    # side-effect-free (no cross-call/cross-test leakage). Store just latest/error; the
    # verdict is always recomputed per caller above.
    if use_cache:
        _cache["ts"] = now
        _cache["data"] = {"latest": result["latest"], "error": result["error"]}
    return result
