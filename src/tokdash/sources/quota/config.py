from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ...onboard import paths

QUOTA_KEYS = ("codex_api", "claude_api", "antigravity_api")

# Poll-interval choices offered in the UI / setup wizard and the effective default
# (Rev 3: 30 min balances snapshot freshness against provider-call volume).
POLL_INTERVAL_CHOICES = (15, 30, 60, 120)
DEFAULT_POLL_INTERVAL_MINUTES = 30
DEFAULT_POLL_INTERVAL_SECONDS = DEFAULT_POLL_INTERVAL_MINUTES * 60
POLL_INTERVAL_FLOOR_SECONDS = 300


def config_path() -> Path:
    return paths.config_path()


def _read_config() -> dict[str, Any]:
    p = config_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_config(data: dict[str, Any]) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(p)


def read_quota_config() -> dict[str, bool]:
    cfg = _read_config()
    quota = cfg.get("quota") if isinstance(cfg.get("quota"), dict) else {}
    return {key: bool(quota.get(key)) for key in QUOTA_KEYS}


def set_quota_consent(updates: dict[str, Any]) -> dict[str, bool]:
    # Merge into the existing quota block instead of rebuilding it from the consent keys
    # alone — otherwise the sibling keys ``enabled`` (master switch) and
    # ``poll_interval_minutes`` would be dropped, silently re-enabling tracking and
    # resetting the interval whenever consent changes.
    cfg = _read_config()
    quota = dict(cfg.get("quota")) if isinstance(cfg.get("quota"), dict) else {}
    for key in QUOTA_KEYS:
        # Apply the update if present, otherwise normalize the existing value — either way
        # all three consent keys stay materialized, while sibling keys (enabled,
        # poll_interval_minutes) are left untouched.
        quota[key] = bool(updates[key]) if key in updates else bool(quota.get(key))
    cfg["quota"] = quota
    _write_config(cfg)
    return {key: bool(quota.get(key)) for key in QUOTA_KEYS}


def quota_poll_killed() -> bool:
    return os.environ.get("TOKDASH_QUOTA_POLL", "").strip().lower() in {"0", "false", "no", "off"}


def quota_config_enabled() -> bool:
    """``config.json`` ``quota.enabled`` master switch (default ``True``).

    Independent of the ``TOKDASH_QUOTA_POLL`` kill switch — this is only the persisted
    user preference. Use :func:`quota_tracking_enabled` for the effective state.
    """
    cfg = _read_config()
    quota = cfg.get("quota") if isinstance(cfg.get("quota"), dict) else {}
    value = quota.get("enabled")
    return True if value is None else bool(value)


def quota_tracking_enabled() -> bool:
    """Master switch: is any quota work (session scan, network, DB writes) allowed?

    False when the ``TOKDASH_QUOTA_POLL=0`` kill switch is set OR when the persisted
    ``quota.enabled`` preference is off. The kill switch always wins.
    """
    if quota_poll_killed():
        return False
    return quota_config_enabled()


def set_quota_enabled(enabled: bool) -> bool:
    cfg = _read_config()
    quota = dict(cfg.get("quota")) if isinstance(cfg.get("quota"), dict) else {}
    quota["enabled"] = bool(enabled)
    cfg["quota"] = quota
    _write_config(cfg)
    return bool(enabled)


def read_poll_interval_minutes() -> int | None:
    """Persisted ``quota.poll_interval_minutes`` (one of :data:`POLL_INTERVAL_CHOICES`) or ``None``."""
    cfg = _read_config()
    quota = cfg.get("quota") if isinstance(cfg.get("quota"), dict) else {}
    try:
        value = int(quota.get("poll_interval_minutes"))
    except (TypeError, ValueError):
        return None
    return value if value in POLL_INTERVAL_CHOICES else None


def set_poll_interval_minutes(minutes: int) -> int:
    value = int(minutes)
    if value not in POLL_INTERVAL_CHOICES:
        raise ValueError(f"poll_interval_minutes must be one of {POLL_INTERVAL_CHOICES}")
    cfg = _read_config()
    quota = dict(cfg.get("quota")) if isinstance(cfg.get("quota"), dict) else {}
    quota["poll_interval_minutes"] = value
    cfg["quota"] = quota
    _write_config(cfg)
    return value


def _env_poll_interval_seconds() -> int | None:
    raw = os.environ.get("TOKDASH_QUOTA_POLL_INTERVAL", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    if value <= 0:
        return None
    return max(POLL_INTERVAL_FLOOR_SECONDS, value)


def effective_poll_interval() -> tuple[int, str]:
    """Return ``(seconds, source)`` where source is ``env`` | ``config`` | ``default``.

    Precedence: ``TOKDASH_QUOTA_POLL_INTERVAL`` (seconds, floor 300) > config
    ``quota.poll_interval_minutes`` > default 1800 s.
    """
    env_seconds = _env_poll_interval_seconds()
    if env_seconds is not None:
        return env_seconds, "env"
    minutes = read_poll_interval_minutes()
    if minutes is not None:
        return minutes * 60, "config"
    return DEFAULT_POLL_INTERVAL_SECONDS, "default"





def network_enabled(key: str) -> bool:
    if not quota_tracking_enabled():
        return False
    return bool(read_quota_config().get(key))


def enabled_network_sources() -> list[str]:
    if not quota_tracking_enabled():
        return []
    consent = read_quota_config()
    return [key for key in QUOTA_KEYS if consent.get(key)]
