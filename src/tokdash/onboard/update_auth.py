"""Authorization for dashboard self-update — localhost plus one explicit Tailscale origin.

Two authorization planes, never merged (feasibility §5):

- **Localhost** keeps today's write gate untouched (loopback bind + loopback Host/Origin
  + per-process token). Nothing here widens it, and token issuance stays loopback-only.
- **Remote update** requires ALL of: an explicitly configured *exact* HTTPS dashboard
  origin (``TOKDASH_UPDATE_ORIGIN`` or ``update_origin`` in config.json — no
  ``*.ts.net`` wildcards, the ``/tokdash`` serve path is not part of the origin), an
  enrolled operator session, and a per-session CSRF token on writes. Enrollment itself
  is gated by a short-lived single-use pairing code that only a local terminal can
  mint (:meth:`tokdash.cli` ``update-enroll``), plus rate limiting.

State lives in ``<data_dir>/update_access.json`` so the session survives the planned
service restart (the cookie is host-only, Secure, HttpOnly; only hashes of the session
token and the pairing code are ever written). No credential ever ships in JS or lives
in localStorage: the browser holds nothing but the cookie and, transiently, the CSRF
token from the enrollment response.

The pairing code is never written to the file, returned by any read path, or accepted
from a URL query — only the body of the enroll request.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import string
import time
from typing import Any, Dict, List, Optional

from . import paths
from ..filelock import process_lock

SCHEMA = 1
PAIR_TTL_SECONDS = 5 * 60
SESSION_TTL_SECONDS = 30 * 24 * 3600
# Enrollment brute-force bound: attempts inside the window block further redemption.
ATTEMPT_WINDOW_SECONDS = 15 * 60
MAX_ATTEMPTS = 12
SESSION_COOKIE = "tokdash_update_session"
CSRF_HEADER = "X-Tokdash-Csrf"

# Unambiguous alphabet (no I/O/0/1) for the read-the-code-off-a-terminal flow.
_CODE_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "IO01")


def state_path():
    return paths.data_dir() / "update_access.json"


def _now() -> float:
    return time.time()


def _load() -> Dict[str, Any]:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"schema": SCHEMA, "pair": None, "sessions": [], "attempts": []}


def _store(data: Dict[str, Any]) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _matches(a: str, b: str) -> bool:
    try:
        return secrets.compare_digest(a, b)
    except TypeError:
        return False


# --- configured remote origin -----------------------------------------------------


def _normalize_origin(raw: str) -> Optional[str]:
    """Exact ``https://host[:port]`` or None. No path, no wildcard, no credentials."""
    from urllib.parse import urlsplit

    raw = (raw or "").strip().rstrip("/")
    if not raw:
        return None
    if raw.startswith("*."):
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        return None
    host = parts.hostname.lower()
    netloc = f"{host}:{parts.port}" if parts.port else host
    return f"https://{netloc}"


def configured_origin() -> Optional[str]:
    """The exact HTTPS origin allowed for remote update traffic, or None (off)."""
    raw = os.environ.get("TOKDASH_UPDATE_ORIGIN", "").strip()
    if not raw:
        try:
            cfg = json.loads(paths.config_path().read_text(encoding="utf-8"))
            raw = str(cfg.get("update_origin") or "") if isinstance(cfg, dict) else ""
        except Exception:
            raw = ""
    return _normalize_origin(raw)


def origin_matches(origin_config: Optional[str], *, host_header: str, origin_header: Optional[str] = None) -> bool:
    """Whether request Host (and browser Origin, when present) equal the configured origin.

    A forwarded loopback address, an arbitrary tailnet hostname, or a *subdomain-prefix*
    match grants nothing: both values must equal the configured exact origin.
    """
    if not origin_config:
        return False
    host = (host_header or "").strip().lower()
    if not host:
        return False
    want = origin_config.split("://", 1)[1]
    if host != want:
        return False
    if origin_header:
        normalized = _normalize_origin(origin_header)
        if normalized != origin_config:
            return False
    return True


# --- pairing codes -----------------------------------------------------------------


def create_pairing_code() -> str:
    """Mint a short-lived single-use enrollment code (local terminal only)."""
    code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    with process_lock(state_path().with_suffix(".lock")):
        data = _load()
        data["pair"] = {
            "code_hash": _hash(_canon(code)),
            "expires_at": _now() + PAIR_TTL_SECONDS,
        }
        _store(data)
    return code


def _canon(code: str) -> str:
    return "".join(ch for ch in (code or "").upper() if ch not in " \t-").strip()


def _prune_attempts(data: Dict[str, Any]) -> List[float]:
    cutoff = _now() - ATTEMPT_WINDOW_SECONDS
    data["attempts"] = [t for t in data.get("attempts", []) if isinstance(t, (int, float)) and t >= cutoff]
    return data["attempts"]


def redeem_pairing_code(code: str) -> bool:
    """One-time redemption: correct, unexpired, under the rate limit → True once."""
    with process_lock(state_path().with_suffix(".lock")):
        data = _load()
        attempts = _prune_attempts(data)
        if len(attempts) >= MAX_ATTEMPTS:
            return False
        pair = data.get("pair")
        data["pair"] = None  # consumed whether or not the guess was right
        attempts.append(_now())
        ok = bool(
            isinstance(pair, dict)
            and pair.get("expires_at", 0) > _now()
            and _matches(_hash(_canon(code)), str(pair.get("code_hash") or ""))
        )
        _store(data)
        return ok


# --- operator sessions ---------------------------------------------------------------


def create_session() -> Optional[Dict[str, str]]:
    """Establish an update-only operator session. Returns ``(token, csrf)`` or None if
    the remote origin is not configured — enrollment without a configured origin has no
    purpose (localhost uses the existing write token)."""
    if not configured_origin():
        return None
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    now = _now()
    with process_lock(state_path().with_suffix(".lock")):
        data = _load()
        sessions = [
            s for s in data.get("sessions", [])
            if isinstance(s, dict) and s.get("expires_at", 0) > now
        ]
        # Bound the store; sessions are rare, so keeping the newest is plenty.
        sessions = sessions[-20:]
        sessions.append({"token_hash": _hash(token), "csrf": csrf, "expires_at": now + SESSION_TTL_SECONDS})
        data["sessions"] = sessions
        _store(data)
    return {"token": token, "csrf": csrf, "expires_at": now + SESSION_TTL_SECONDS}


def validate_session(token: Optional[str], *, csrf: Optional[str] = None) -> bool:
    """Session validity; when ``csrf`` is given, also require it to match (writes)."""
    if not token:
        return False
    now = _now()
    with process_lock(state_path().with_suffix(".lock")):
        data = _load()
        sessions = [s for s in data.get("sessions", []) if isinstance(s, dict) and s.get("expires_at", 0) > now]
        changed = len(sessions) != len(data.get("sessions", []))
        hit = any(_matches(_hash(token), str(s.get("token_hash") or "")) for s in sessions)
        if hit and csrf is not None:
            hit = any(
                _matches(_hash(token), str(s.get("token_hash") or ""))
                and _matches(csrf, str(s.get("csrf") or ""))
                for s in sessions
            )
        if changed:
            data["sessions"] = sessions
            _store(data)
        return hit


def revoke_all_sessions() -> int:
    with process_lock(state_path().with_suffix(".lock")):
        data = _load()
        count = len(data.get("sessions", []))
        data["sessions"] = []
        _store(data)
        return count


def enrollment_locked() -> bool:
    with process_lock(state_path().with_suffix(".lock")):
        data = _load()
        return len(_prune_attempts(data)) >= MAX_ATTEMPTS
