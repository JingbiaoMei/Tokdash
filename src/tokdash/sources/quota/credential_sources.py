"""Allowlisted, read-only discovery of third-party quota credentials.

This module is called only after ``quota.credential_scan`` consent. It never
opens logs, refreshes credentials, writes provider files, or persists secrets.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from ... import clientpaths


@dataclass(frozen=True)
class CredentialCandidate:
    provider: str
    token: str
    base_url: str
    source: str
    source_ref: str
    region: str | None = None


_TOKEN_FIELDS = {
    "apikey",
    "api_key",
    "key",
    "token",
    "anthropic_auth_token",
    "anthropic_api_key",
    "openai_api_key",
}
_URL_FIELDS = {
    "baseurl",
    "base_url",
    "endpoint",
    "api_url",
    "url",
    "anthropic_base_url",
    "openai_base_url",
}


def _jsonc_without_comments(text: str) -> str:
    """Remove JSONC comments without altering comment-like text inside strings."""
    out: list[str] = []
    in_string = False
    escaped = False
    i = 0
    while i < len(text):
        char = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            i += 1
            continue
        if char == "/" and nxt == "/":
            i += 2
            while i < len(text) and text[i] not in "\r\n":
                i += 1
            continue
        if char == "/" and nxt == "*":
            i += 2
            while i + 1 < len(text) and text[i : i + 2] != "*/":
                i += 1
            i += 2
            continue
        out.append(char)
        i += 1
    uncommented = "".join(out)
    cleaned: list[str] = []
    in_string = False
    escaped = False
    i = 0
    while i < len(uncommented):
        char = uncommented[i]
        if in_string:
            cleaned.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            cleaned.append(char)
            i += 1
            continue
        if char == ",":
            lookahead = i + 1
            while lookahead < len(uncommented) and uncommented[lookahead].isspace():
                lookahead += 1
            if lookahead < len(uncommented) and uncommented[lookahead] in "}]":
                i += 1
                continue
        cleaned.append(char)
        i += 1
    return "".join(cleaned)


def _read_json(path: Path, *, jsonc: bool = False) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
        return json.loads(_jsonc_without_comments(text) if jsonc else text)
    except Exception:
        return None


def _canonical_provider_url(raw: str) -> tuple[str, str, str | None] | None:
    try:
        parsed = urlsplit(raw.strip())
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        return None
    if host in {"api.minimax.io", "www.minimax.io"}:
        return "minimax", f"{parsed.scheme}://api.minimax.io", "global"
    if host in {"api.minimaxi.com", "www.minimaxi.com"}:
        return "minimax", f"{parsed.scheme}://api.minimaxi.com", "cn"
    if host == "api.kimi.com" and parsed.path.rstrip("/").startswith("/coding"):
        return "kimi", f"{parsed.scheme}://api.kimi.com/coding/v1", None
    return None


def endpoint_host_allowed(url: str, allowed_hosts: frozenset[str], *, path_prefix: str | None = None) -> bool:
    """True iff ``url`` is HTTPS, its host is EXACTLY one of ``allowed_hosts``, and (when
    given) its path starts with ``path_prefix``.

    A bearer token is only attached after this passes, so a crafted/misconfigured base URL
    (e.g. ``https://evil.example/api.kimi.com/coding`` — host ``evil.example``) cannot
    exfiltrate the token. Host match is exact, not substring/suffix, so
    ``api.kimi.com.evil.com`` is rejected too.
    """
    try:
        parsed = urlsplit(url)
    except Exception:
        return False
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in allowed_hosts:
        return False
    return path_prefix is None or parsed.path.startswith(path_prefix)


def _resolve_config_token(value: Any) -> str:
    token = str(value or "").strip()
    if token.startswith("{env:") and token.endswith("}"):
        return os.environ.get(token[5:-1].strip(), "").strip()
    # Do not follow arbitrary {file:...} references. They are outside this
    # reader's disclosed allowlist and need their own explicit consent.
    if token.startswith("{") and token.endswith("}"):
        return ""
    if token.startswith("<") or token.lower() in {"optional", "your-api-key", "sk-xxx"}:
        return ""
    return token


def _auth_token(entry: Any) -> str:
    if isinstance(entry, str):
        return entry.strip()
    if not isinstance(entry, dict):
        return ""
    for key in ("key", "apiKey", "api_key", "token", "access_token"):
        token = _resolve_config_token(entry.get(key))
        if token:
            return token
    return ""


# Fallback hosts for entries that carry no baseURL (e.g. an auth.json-only API
# key). Region guesses from the provider id are intentionally permissive: a wrong
# guess produces a candidate the poll then rejects, not silent bad data.
_PROVIDER_ID_HOSTS = {
    "minimax": {"global": "https://api.minimax.io", "cn": "https://api.minimaxi.com"},
}


def _classify_provider_id(provider_id: str) -> tuple[str, str, str | None] | None:
    pid = provider_id.lower()
    if "kimi" in pid:
        return "kimi", "https://api.kimi.com/coding/v1", None
    if "minimax" in pid:
        region = "cn" if ("cn" in pid or "minimaxi" in pid) else "global"
        return "minimax", _PROVIDER_ID_HOSTS["minimax"][region], region
    return None


def _candidate_from(provider_id: str, raw_url: str, token: str, source: str, source_ref: str) -> CredentialCandidate | None:
    """Build a candidate, preferring a classifiable baseURL (host is authoritative)
    and falling back to the provider id when no usable URL is present."""
    if not token:
        return None
    classified = _canonical_provider_url(raw_url) if raw_url else None
    if classified is None:
        classified = _classify_provider_id(provider_id)
    if classified is None:
        return None
    provider, base_url, region = classified
    return CredentialCandidate(provider, token, base_url, source, source_ref, region)


def _candidate(raw_url: str, token: str, source: str, source_ref: str) -> CredentialCandidate | None:
    """URL-only classification, for sources with no provider-id hint (Claude
    settings env, CC-Switch rows)."""
    return _candidate_from("", raw_url, token, source, source_ref)


def _opencode_candidates() -> list[CredentialCandidate]:
    auth_path = clientpaths.opencode_auth_path()
    auth = _read_json(auth_path)
    auth = auth if isinstance(auth, dict) else {}
    out: list[CredentialCandidate] = []
    covered: set[str] = set()
    config_urls: dict[str, str] = {}
    for path in clientpaths.opencode_config_paths():
        cfg = _read_json(path, jsonc=path.suffix.lower() == ".jsonc")
        providers = cfg.get("provider") if isinstance(cfg, dict) and isinstance(cfg.get("provider"), dict) else {}
        for provider_id, spec in providers.items():
            if not isinstance(spec, dict):
                continue
            options = spec.get("options") if isinstance(spec.get("options"), dict) else {}
            raw_url = str(options.get("baseURL") or options.get("base_url") or "")
            if raw_url:
                config_urls.setdefault(str(provider_id), raw_url)
            token = _resolve_config_token(options.get("apiKey") or options.get("api_key"))
            if not token:
                token = _auth_token(auth.get(str(provider_id)))
            item = _candidate_from(str(provider_id), raw_url, token, "opencode", f"{path}:provider.{provider_id}")
            if item is not None:
                out.append(item)
                covered.add(str(provider_id))
    # auth.json can hold keys OpenCode uses with no matching provider block (e.g.
    # `kimi-for-coding`) or a block that declared no baseURL. Classify those by id,
    # enriched with any baseURL the config did declare for the same id.
    for provider_id, entry in auth.items():
        if str(provider_id) in covered:
            continue
        token = _auth_token(entry)
        if not token:
            continue
        raw_url = config_urls.get(str(provider_id), "")
        item = _candidate_from(str(provider_id), raw_url, token, "opencode_auth", f"{auth_path}:{provider_id}")
        if item is not None:
            out.append(item)
    return out


def _claude_settings_candidates() -> list[CredentialCandidate]:
    path = clientpaths.claude_config_dir() / "settings.json"
    settings = _read_json(path)
    env = settings.get("env") if isinstance(settings, dict) and isinstance(settings.get("env"), dict) else {}
    raw_url = str(env.get("ANTHROPIC_BASE_URL") or "")
    token = _resolve_config_token(env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY"))
    item = _candidate(raw_url, token, "claude_settings", f"{path}:env")
    return [item] if item is not None else []


def _walk_strings(value: Any, prefix: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield from _walk_strings(child, path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_strings(child, f"{prefix}[{index}]")
    elif isinstance(value, str):
        yield prefix, value


def _embedded_config_values(text: str) -> list[tuple[str, str]]:
    """Extract allowlisted key/value lines from CC-Switch's embedded TOML."""
    out: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().rsplit(".", 1)[-1].lower()
        value = value.split("#", 1)[0].strip().strip('"\'')
        if key in _TOKEN_FIELDS or key in _URL_FIELDS:
            out.append((key, value))
    return out


def _cc_switch_candidates() -> list[CredentialCandidate]:
    path = clientpaths.cc_switch_db_path()
    if not path.is_file():
        return []
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
        conn.row_factory = sqlite3.Row
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(providers)")}
        required = {"id", "name", "settings_config"}
        if not required.issubset(columns):
            return []
        app_expr = "app_type" if "app_type" in columns else "'' AS app_type"
        rows = conn.execute(f"SELECT id, name, {app_expr}, settings_config FROM providers").fetchall()
    except sqlite3.Error:
        return []
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass

    out: list[CredentialCandidate] = []
    for row in rows:
        try:
            settings = json.loads(str(row["settings_config"] or "{}"))
        except Exception:
            continue
        values = list(_walk_strings(settings))
        for field, value in list(values):
            if field.rsplit(".", 1)[-1].lower() in {"config", "auth"}:
                values.extend((f"{field}.{key}", child) for key, child in _embedded_config_values(value))
        urls = [
            value for field, value in values
            if field.rsplit(".", 1)[-1].lower() in _URL_FIELDS and _canonical_provider_url(value)
        ]
        tokens = [
            _resolve_config_token(value) for field, value in values
            if field.rsplit(".", 1)[-1].lower() in _TOKEN_FIELDS
        ]
        token = next((value for value in tokens if value), "")
        for raw_url in urls:
            item = _candidate(
                raw_url,
                token,
                "cc_switch",
                f"{path}:providers/{row['app_type']}/{row['id']}",
            )
            if item is not None:
                out.append(item)
    return out


def discover_external_credentials(provider: str | None = None) -> list[CredentialCandidate]:
    """Return deduplicated OpenCode, Claude-settings, and CC-Switch candidates."""
    out = [*_opencode_candidates(), *_cc_switch_candidates(), *_claude_settings_candidates()]
    deduped: list[CredentialCandidate] = []
    seen: set[tuple[str, str, str]] = set()
    for item in out:
        if provider is not None and item.provider != provider:
            continue
        key = (item.provider, item.token, item.base_url)
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


def discover_provider_sources() -> dict[str, list[str]]:
    """Return non-secret provider/source labels for consented onboarding UI."""
    found: dict[str, set[str]] = {}

    def add(provider: str, source: str) -> None:
        found.setdefault(provider, set()).add(source)

    native_checks = {
        "codex": [clientpaths.codex_home() / "auth.json"],
        "claude": [clientpaths.claude_config_dir() / ".credentials.json"],
        "antigravity": [clientpaths.antigravity_cli_dir() / "antigravity-oauth-token"],
        "minimax": [clientpaths.minimax_cli_root() / "config.json"],
        "kimi": [root / "config.toml" for root in clientpaths.kimi_roots()]
        + [root / "credentials" / "kimi-code.json" for root in clientpaths.kimi_roots()],
        "grok": [clientpaths.grok_home() / "auth.json"],
    }
    for provider, paths in native_checks.items():
        if any(path.is_file() for path in paths):
            add(provider, "native CLI")

    env_checks = {
        "codex": ("CODEX_API_KEY",),
        "claude": ("CLAUDE_CODE_OAUTH_TOKEN",),
        "minimax": ("MINIMAX_API_KEY", "MINIMAX_TOKEN_PLAN_GLOBAL_KEY", "MINIMAX_TOKEN_PLAN_CN_KEY"),
        "kimi": ("KIMI_API_KEY",),
    }
    for provider, names in env_checks.items():
        if any(os.environ.get(name, "").strip() for name in names):
            add(provider, "environment")

    for candidate in discover_external_credentials():
        add(candidate.provider, candidate.source.replace("_", " "))
    return {provider: sorted(sources) for provider, sources in sorted(found.items())}
