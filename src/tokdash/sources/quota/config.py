from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...onboard import paths

QUOTA_KEYS = (
    "codex_api",
    "claude_api",
    "antigravity_api",
    "minimax_api",
    "kimi_api",
    "grok_api",
)
CREDENTIAL_SCAN_KEY = "credential_scan"
CONSENT_KEYS = (CREDENTIAL_SCAN_KEY, *QUOTA_KEYS)

# Providers that predate the credential_scan consent gate. On upgrade, an install
# that already consented to polling one of these implicitly authorized reading its
# credential — polling is strictly broader than reading — so credential access is
# grandfathered for them until the user makes an explicit credential_scan choice.
# New providers and fresh installs have no such prior consent and stay default-off.
_LEGACY_PROVIDER_KEYS = ("codex_api", "claude_api", "antigravity_api")

# Poll-interval choices offered in the UI / setup wizard and the effective default
# (Rev 3: 30 min balances snapshot freshness against provider-call volume).
POLL_INTERVAL_CHOICES = (15, 30, 60, 120)
DEFAULT_POLL_INTERVAL_MINUTES = 30
DEFAULT_POLL_INTERVAL_SECONDS = DEFAULT_POLL_INTERVAL_MINUTES * 60
POLL_INTERVAL_FLOOR_SECONDS = 300

# Boundary polling: sample shortly before and after each fixed-reset window's reset so the
# running-high consumption model (see `usage_store.quota_history`) catches the true
# pre-reset peak and the true post-reset baseline instead of whatever the coarse regular
# interval happens to land on. This changes only WHEN a poll fires, not the consumption
# algorithm or schema.
DEFAULT_BOUNDARY_POLL_ENABLED = True
DEFAULT_BOUNDARY_PRE_RESET_SECONDS = 120
DEFAULT_BOUNDARY_POST_RESET_ENABLED = True
DEFAULT_BOUNDARY_POST_RESET_SECONDS = 120


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


def _raw_quota() -> dict[str, Any]:
    """The stored ``quota`` block verbatim, so callers can tell an absent consent
    key (upgrade) apart from one explicitly set to ``False``."""
    cfg = _read_config()
    quota = cfg.get("quota")
    return quota if isinstance(quota, dict) else {}


def _grandfathered_credential_scan(raw: dict[str, Any]) -> bool:
    return any(bool(raw.get(key)) for key in _LEGACY_PROVIDER_KEYS)


def read_quota_config() -> dict[str, bool]:
    cfg = _read_config()
    quota = cfg.get("quota") if isinstance(cfg.get("quota"), dict) else {}
    return {key: bool(quota.get(key)) for key in CONSENT_KEYS}


def set_quota_consent(updates: dict[str, Any]) -> dict[str, bool]:
    # Merge into the existing quota block instead of rebuilding it from the consent keys
    # alone — otherwise the sibling keys ``enabled`` (master switch) and
    # ``poll_interval_minutes`` would be dropped, silently re-enabling tracking and
    # resetting the interval whenever consent changes.
    cfg = _read_config()
    quota = dict(cfg.get("quota")) if isinstance(cfg.get("quota"), dict) else {}
    # Preserve the upgrade grandfather: if credential_scan was never stored and this
    # call isn't setting it, seed it from the legacy providers before the normalize
    # loop materializes it. Otherwise it would be written as False here and silently
    # revoke credential access the install already had.
    if CREDENTIAL_SCAN_KEY not in quota and CREDENTIAL_SCAN_KEY not in updates:
        quota[CREDENTIAL_SCAN_KEY] = _grandfathered_credential_scan(quota)
    for key in CONSENT_KEYS:
        # Apply the update if present, otherwise normalize the existing value — either way
        # all consent keys stay materialized, while sibling keys (enabled,
        # poll_interval_minutes) are left untouched.
        quota[key] = bool(updates[key]) if key in updates else bool(quota.get(key))
    cfg["quota"] = quota
    _write_config(cfg)
    return {key: bool(quota.get(key)) for key in CONSENT_KEYS}


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


def credential_scan_enabled() -> bool:
    """Whether Tokdash may open allowlisted local credential/config stores.

    An explicit ``quota.credential_scan`` value always wins. When it was never
    stored — an upgrade from a version without this gate — fall back to the legacy
    grandfather (see ``_LEGACY_PROVIDER_KEYS``) so existing polling keeps working.
    """
    if not quota_tracking_enabled():
        return False
    raw = _raw_quota()
    if CREDENTIAL_SCAN_KEY in raw:
        return bool(raw.get(CREDENTIAL_SCAN_KEY))
    return _grandfathered_credential_scan(raw)


def ensure_quota_consent_migrated() -> None:
    """Persist the credential_scan grandfather once, so later reads and consent
    writes see an explicit value instead of re-deriving it. Idempotent; a no-op
    on fresh installs (nothing stored) and once credential_scan is materialized."""
    raw = _raw_quota()
    if not raw or CREDENTIAL_SCAN_KEY in raw:
        return
    set_quota_consent({CREDENTIAL_SCAN_KEY: _grandfathered_credential_scan(raw)})


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


def _env_off_switch(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"0", "false", "no", "off"}


def _env_positive_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


@dataclass(frozen=True)
class BoundaryPollConfig:
    """Resolved boundary-poll knobs for one scheduling decision (see `effective_boundary_config`)."""

    enabled: bool
    pre_seconds: int
    post_reset_enabled: bool
    post_seconds: int


def effective_boundary_config() -> BoundaryPollConfig:
    """Resolve boundary-poll settings: env override, else the module default.

    Unlike ``poll_interval_minutes`` there is no persisted ``config.json`` key for these
    yet (no setup-wizard UI exposes them), so precedence per knob is simply env-or-default.
    ``TOKDASH_QUOTA_BOUNDARY_POLL`` gates the whole feature; the narrower
    ``TOKDASH_QUOTA_BOUNDARY_POST`` only turns off the post-reset half while leaving the
    pre-reset sample enabled.
    """
    enabled = DEFAULT_BOUNDARY_POLL_ENABLED and not _env_off_switch("TOKDASH_QUOTA_BOUNDARY_POLL")
    pre_seconds = _env_positive_int("TOKDASH_QUOTA_BOUNDARY_PRE_SECONDS") or DEFAULT_BOUNDARY_PRE_RESET_SECONDS
    post_reset_enabled = DEFAULT_BOUNDARY_POST_RESET_ENABLED and not _env_off_switch("TOKDASH_QUOTA_BOUNDARY_POST")
    post_seconds = _env_positive_int("TOKDASH_QUOTA_BOUNDARY_POST_SECONDS") or DEFAULT_BOUNDARY_POST_RESET_SECONDS
    return BoundaryPollConfig(
        enabled=enabled,
        pre_seconds=pre_seconds,
        post_reset_enabled=post_reset_enabled,
        post_seconds=post_seconds,
    )


def network_enabled(key: str) -> bool:
    if not credential_scan_enabled():
        return False
    return bool(read_quota_config().get(key))


def enabled_network_sources() -> list[str]:
    if not credential_scan_enabled():
        return []
    consent = read_quota_config()
    return [key for key in QUOTA_KEYS if consent.get(key)]
