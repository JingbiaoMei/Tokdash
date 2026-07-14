from __future__ import annotations

import json

import pytest

from tokdash.codex_quota_windows import classify_codex_api_windows
from tokdash.usage_store import _codex_window_used_percent_from_raw


@pytest.mark.parametrize(
    ("primary", "secondary", "expected"),
    [
        ({"limit_window_seconds": 18000}, None, {"5h"}),
        ({"limit_window_seconds": 604800}, None, {"7d"}),
        ({"used_percent": 1}, None, {"7d"}),
        ({"used_percent": 1}, {"used_percent": 2}, {"5h", "7d"}),
        ({"limit_window_seconds": 604800}, {"limit_window_seconds": 18000}, {"5h", "7d"}),
        ({"limit_window_seconds": 604800}, {"used_percent": 2}, {"5h", "7d"}),
        ({"used_percent": 1}, {"window_minutes": 300}, {"5h", "7d"}),
    ],
)
def test_classify_codex_api_windows(primary, secondary, expected):
    assert set(classify_codex_api_windows(primary, secondary)) == expected


def test_classify_codex_api_windows_assigns_complementary_unknown_window():
    primary = {"limit_window_seconds": 604800, "used_percent": 61}
    secondary = {"used_percent": 4}

    windows = classify_codex_api_windows(primary, secondary)

    assert windows["7d"] is primary
    assert windows["5h"] is secondary


@pytest.mark.parametrize("with_duration", [False, True])
def test_raw_plural_weekly_only_uses_same_classifier(with_duration):
    weekly = {"used_percent": 61}
    if with_duration:
        weekly["limit_window_seconds"] = 604800
    raw = json.dumps({"usage": {"rate_limits": {"primary": weekly}}})

    assert _codex_window_used_percent_from_raw("7d", raw) == 61.0
    assert _codex_window_used_percent_from_raw("5h", raw) is None
