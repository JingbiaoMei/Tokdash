from __future__ import annotations

from datetime import datetime

import tokdash.compute as compute


def test_get_session_data_month_vs_numeric_days(monkeypatch):
    calls: list[tuple] = []

    def fake_month():
        calls.append(("month",))
        return {"range": "month"}

    def fake_days(days: int):
        calls.append(("days", days))
        return {"range": f"{days}d"}

    monkeypatch.setattr(compute, "get_session_usage_month", fake_month)
    monkeypatch.setattr(compute, "get_session_usage_days", fake_days)

    assert compute.get_session_data("month") == {"range": "month"}
    assert compute.get_session_data("30") == {"range": "30d"}
    assert compute.get_session_data("week") == {"range": "7d"}

    assert calls == [("month",), ("days", 30), ("days", 7)]


def test_period_to_range_args_month_is_calendar_month():
    args = compute.period_to_range_args("month")
    assert args[:1] == ["--since"]
    assert args[2:3] == ["--until"]

    since = datetime.strptime(args[1], "%Y-%m-%d").date()
    until = datetime.strptime(args[3], "%Y-%m-%d").date()

    now_local = datetime.now().astimezone()

    assert since == now_local.replace(day=1).date()
    assert until == now_local.date()
