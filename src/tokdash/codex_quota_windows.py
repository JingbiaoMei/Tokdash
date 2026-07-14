from __future__ import annotations

from typing import Any


FIVE_HOUR_SECONDS = 5 * 60 * 60
SEVEN_DAY_SECONDS = 7 * 24 * 60 * 60


def _fixed_window_seconds(window: dict[str, Any]) -> int | None:
    try:
        seconds = window.get("limit_window_seconds")
        if seconds is None:
            seconds = window.get("window_seconds")
        if seconds is not None:
            return int(float(seconds))
        minutes = window.get("window_minutes")
        return int(float(minutes) * 60) if minutes is not None else None
    except (TypeError, ValueError):
        return None


def _duration_bucket(window: dict[str, Any]) -> str | None:
    seconds = _fixed_window_seconds(window)
    if seconds == FIVE_HOUR_SECONDS:
        return "5h"
    if seconds == SEVEN_DAY_SECONDS:
        return "7d"
    return None


def classify_codex_api_windows(
    primary: dict[str, Any] | None,
    secondary: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Classify a pair-shaped live Codex API limit container.

    Fixed duration metadata is authoritative. Without recognized durations, a complete
    pair retains primary=5h/secondary=7d, while a single returned window is weekly
    because Codex temporarily omits its disabled 5-hour window.
    """
    present = [("primary", primary), ("secondary", secondary)]
    present = [(position, window) for position, window in present if isinstance(window, dict)]
    if not present:
        return {}

    if len(present) == 1:
        _position, window = present[0]
        return {_duration_bucket(window) or "7d": window}

    assert primary is not None and secondary is not None
    primary_bucket = _duration_bucket(primary)
    secondary_bucket = _duration_bucket(secondary)

    if primary_bucket and secondary_bucket and primary_bucket != secondary_bucket:
        return {primary_bucket: primary, secondary_bucket: secondary}
    if primary_bucket and not secondary_bucket:
        other = "7d" if primary_bucket == "5h" else "5h"
        return {primary_bucket: primary, other: secondary}
    if secondary_bucket and not primary_bucket:
        other = "7d" if secondary_bucket == "5h" else "5h"
        return {other: primary, secondary_bucket: secondary}

    # With no recognized duration, or contradictory duplicate durations, preserve both
    # windows using the established positional interpretation instead of dropping one.
    return {"5h": primary, "7d": secondary}
