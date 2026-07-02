#!/usr/bin/env python3
"""One-shot, read-only probe of the quota provider endpoints — fixture prep only.

Run this MANUALLY when you want to refresh the frozen quota fixtures. It:

  * reads the SAME local CLI credentials the runtime uses (``sources/quota/codex.py``
    and ``sources/quota/antigravity.py``) — it never refreshes or writes any token;
  * performs read-only calls: Codex ``wham/usage`` + ``wham/rate-limit-reset-credits``,
    and Antigravity ``loadCodeAssist`` -> ``fetchAvailableModels``;
  * deeply scrubs ALL token material and every account identifier / email before
    anything touches disk;
  * writes the scrubbed payloads to ``tests/fixtures/quota/*.json`` so the parser tests
    can be frozen against real response shapes.

It is intentionally NOT wired into the app, the poller, or CI — nothing runs it for you.

Usage (from the repo root):

    python scripts/probe_quota_endpoints.py                 # both providers
    python scripts/probe_quota_endpoints.py --only codex    # just Codex
    python scripts/probe_quota_endpoints.py --out /tmp/q    # custom output dir
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

# Allow running from a plain checkout (editable installs already resolve `tokdash`).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_DEFAULT_OUT = _REPO_ROOT / "tests" / "fixtures" / "quota"

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_JWT_RE = re.compile(r"^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}$")
_TOKENISH_RE = re.compile(r"^(ya29\.|sk-|Bearer\s|eyJ|gho_|ghp_|AIza)")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_ACCOUNT_RE = re.compile(r"^(acct[_-]|user[_-]|usr[_-]|org[-_]|proj[-_]|projects/|users/)")
# Opaque server-side resource ids (e.g. "RateLimitResetCredit_<32 hex>") are unique
# per account grant — treat any long contiguous hex run as identifying material.
_OPAQUE_HEX_RE = re.compile(r"[0-9a-fA-F]{24,}")

_REDACT_KEY_SUBSTRINGS = (
    "token",
    "secret",
    "password",
    "credential",
    "authorization",
    "bearer",
    "cookie",
)
_REDACT_KEYS_EXACT = {
    "email",
    "account",
    "account_id",
    "accountid",
    "chatgpt_account_id",
    "project_id",
    "projectid",
    # Antigravity loadCodeAssist: an auto-generated GCP project id tied to the account.
    "cloudaicompanionproject",
    "sub",
    "user_id",
    "userid",
    "apikey",
    "api_key",
    "organization_id",
    "org_id",
    "client_id",
    "id_token",
}

_REDACTED = "<redacted>"


def _error_detail(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    return type(exc).__name__


def _redact_key(key: str) -> bool:
    low = key.lower()
    if low in _REDACT_KEYS_EXACT:
        return True
    return any(sub in low for sub in _REDACT_KEY_SUBSTRINGS)


def _redact_value(value: str) -> bool:
    return bool(
        _EMAIL_RE.search(value)
        or _JWT_RE.match(value)
        or _TOKENISH_RE.match(value)
        or _UUID_RE.match(value)
        or _ACCOUNT_RE.match(value)
        or _OPAQUE_HEX_RE.search(value)
    )


def scrub(obj: Any) -> Any:
    """Recursively strip token material, account ids, and emails from a payload."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            out[key] = _REDACTED if _redact_key(str(key)) else scrub(value)
        return out
    if isinstance(obj, list):
        return [scrub(item) for item in obj]
    if isinstance(obj, str):
        return _REDACTED if _redact_value(obj) else obj
    return obj


def _write(out_dir: Path, name: str, payload: Any) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    path.write_text(json.dumps(scrub(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"  wrote {path} ({path.stat().st_size} bytes, scrubbed)")


def probe_codex(out_dir: Path, timeout: float) -> None:
    import urllib.request

    from tokdash.sources.quota import codex

    print("Codex:")
    token, account, _claims = codex._read_auth()
    if not token:
        print("  no usable Codex token (auth.json missing/expired) — skipping.")
        return
    for name, url in (
        ("codex_usage.json", codex.CODEX_USAGE_URL),
        ("codex_reset_credits.json", codex.CODEX_RESET_CREDITS_URL),
    ):
        try:
            payload = codex._get_json(url, token, account, urllib.request.urlopen, timeout)
        except Exception as exc:  # noqa: BLE001 - probe never raises past the console
            print(f"  {name}: request failed ({_error_detail(exc)})")
            continue
        _write(out_dir, name, payload)


def probe_antigravity(out_dir: Path, timeout: float) -> None:
    import urllib.request

    from tokdash.sources.quota import antigravity

    print("Antigravity:")
    token, _meta = antigravity._read_token()
    if not token:
        print("  no usable Antigravity token (oauth-token missing/expired) — skipping.")
        return
    try:
        assist = antigravity._post_json(
            f"{antigravity.BASE_URL}/v1internal:loadCodeAssist", token, {}, urllib.request.urlopen, timeout
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  loadCodeAssist: request failed ({_error_detail(exc)})")
        return
    _write(out_dir, "antigravity_loadcodeassist.json", assist)
    project_id = assist.get("cloudaicompanionProject") or assist.get("projectId") or assist.get("project_id")
    try:
        models = antigravity._post_json(
            f"{antigravity.BASE_URL}/v1internal:fetchAvailableModels",
            token,
            {"project": project_id} if project_id else {},
            urllib.request.urlopen,
            timeout,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  fetchAvailableModels: request failed ({_error_detail(exc)})")
        return
    _write(out_dir, "antigravity_models.json", models)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(_DEFAULT_OUT), help="output directory for scrubbed fixtures")
    parser.add_argument("--only", choices=["codex", "antigravity"], help="probe just one provider")
    parser.add_argument("--timeout", type=float, default=15.0, help="per-request timeout in seconds")
    args = parser.parse_args(argv)

    out_dir = Path(args.out).expanduser()
    print(f"Writing scrubbed quota fixtures to {out_dir}")
    if args.only in (None, "codex"):
        probe_codex(out_dir, args.timeout)
    if args.only in (None, "antigravity"):
        probe_antigravity(out_dir, args.timeout)
    print("Done. Review the scrubbed JSON before committing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
