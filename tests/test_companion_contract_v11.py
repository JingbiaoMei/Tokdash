"""The v1.1+ enhancement rules, executed against the shared fixtures.

`COMPANION_API.md` now documents a second layer of behavior beyond decoding: the
delta-row/active-time/top-rank/credits strings, the calendar-week window, the
glance windows, and the update badge. Both native apps implement each rule in
their own language; the expected/*.json files pin the observable strings. This
file transcribes the documented pseudocode and runs it against the fixtures,
asserting it reproduces exactly what the expected files pin - so a fixture edit
that silently changes a display, or a doc edit that changes a rule, fails here
before it diverges in two app codebases.

The server side of these shapes is already pinned by tests/test_insights_api.py,
test_overview_active_time.py, test_api_quota.py and test_version_surfaces.py;
this file pins the contract's own layer, the way test_companion_contract_accounts.py
pins the account-row rule.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "companion" / "contract"
FIX = ROOT / "fixtures"
EXP = ROOT / "expected"


def fx(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def ex(name: str) -> dict:
    return json.loads((EXP / name).read_text(encoding="utf-8"))


# --- the documented display rules, transcribed ----------------------------------


def compact(v: int) -> str:
    """`COMPANION_API.md` "Token compact notation"."""
    if v >= 1_000_000:
        s = f"{v / 1_000_000:.1f}"
        return (s[:-2] if s.endswith(".0") else s) + "M"
    if v >= 1_000:
        return f"{round(v / 1_000)}k"
    return str(v)


def delta_row(comparison: dict | None, sentence: str) -> str | None:
    """`COMPANION_API.md` "Full delta row". Absent when every pct is null."""
    if not comparison:
        return None
    parts = []
    for key, label in (("cost_pct", "cost"), ("tokens_pct", "tokens"), ("messages_pct", "msgs")):
        pct = comparison.get(key)
        if pct is None:
            continue
        glyph = "▲" if pct > 0 else "▼" if pct < 0 else "±"
        parts.append(f"{glyph} {abs(round(pct))}% {label}")
    return " · ".join(parts) + " " + sentence if parts else None


def active_segment(active_ms: int | None) -> str | None:
    """`COMPANION_API.md` "Active time". Zero, None and 404 all mean absent."""
    if not active_ms:
        return None
    m = active_ms // 60_000
    if active_ms < 60_000:
        return "active <1 m"
    if m < 60:
        return f"active {m} m"
    if m < 1_440:
        return f"active {m // 60} h {m % 60} m"
    return f"active {m // 1440} d {(m % 1440) // 60} h"


def hero_active_multi(servers: list[dict | None]) -> str | None:
    """Sum across reachable servers; drop the segment when any server lacks data."""
    if any(a is None for a in servers):
        return None
    return active_segment(sum(a["active_ms"] for a in servers))


TOOL_LABELS = {"codex": "Codex", "claude": "Claude", "kimi": "Kimi",
               "opencode": "OpenCode", "openclaw": "OpenClaw"}


def top_ranks(usage: dict) -> dict:
    """`COMPANION_API.md` "Top ranks": tools by tokens top-3, models = combined[:3]."""
    tools = sorted(usage.get("by_tool", {}).items(), key=lambda kv: kv[1]["tokens"], reverse=True)
    return {
        "tools": [f"{TOOL_LABELS.get(tid, tid)} {compact(item['tokens'])}" for tid, item in tools[:3]],
        "models": [
            f"{m['name'].split('/')[-1]} {compact(m['tokens'])}"
            for m in usage.get("combined_models", [])[:3]
        ],
    }


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def credits_row(codex_provider: dict, now: datetime) -> str | None:
    """`COMPANION_API.md` "Reset credits"."""
    rc = codex_provider.get("reset_credits")
    count = (rc or {}).get("available_count", 0)
    future = sorted(
        (_parse_iso(c["expires_at"]) for c in (rc or {}).get("credits", [])),
    )
    future = [d for d in future if d > now]
    if count < 1:
        return None
    row = f"⚡ Codex · {count} reset credits"
    if future:
        days = math.floor((future[0] - now).total_seconds() / 86_400)
        clause = "today" if days < 1 else "tomorrow" if days < 2 else f"in {days} d"
        row += f" · expire {clause}"
    return row


def credits_alert(codex_provider: dict, now: datetime) -> bool:
    """Notification: a future credit inside its last 48 hours."""
    rc = codex_provider.get("reset_credits") or {}
    return any(0 < (c - now).total_seconds() <= 172_800
               for c in map(_parse_iso, (x["expires_at"] for x in rc.get("credits", []))))


def week_window(today: date) -> tuple[date, date]:
    """`COMPANION_API.md` "Period windows": calendar Mon..today, never period=week."""
    monday = today - timedelta(days=today.weekday())
    return monday, today


def grid_window(contributions: list[dict], end: date, days: int) -> list[dict]:
    """`COMPANION_API.md` "Activity glance": trailing N calendar days, client-side."""
    start = end - timedelta(days=days - 1)
    return [c for c in contributions
            if start <= date.fromisoformat(c["date"]) <= end]


def update_badge(version: dict, update_check: dict | None) -> str | None:
    """`COMPANION_API.md` "Server update badge"."""
    if not version.get("update_check_enabled") or not update_check:
        return None
    if not update_check.get("enabled"):
        return None  # no server-side consent: render nothing
    if update_check.get("update_available") and update_check.get("latest"):
        return f"Server update available: v{update_check['latest']}"
    return None


def glance_visible(source: dict, kind: str) -> bool:
    """All-zero sources hide the strip; a zero peak_hour is the today signal."""
    if kind == "hourly":
        return bool(source["hourly"]["peak_hour"] is not None)
    if kind == "daily":
        return any(d["tokens"] for d in source["daily"])
    return any(c["totals"]["tokens"] for c in source["contributions"])


# --- the expected strings, regenerated from the fixtures -------------------------


def test_healthy_expected_strings_are_reproducible():
    e = ex("healthy.json")
    usage = fx("usage-today.json")
    assert compact(usage["total_tokens"]) == e["expected"]["today"]["tokens_compact"] == "18.7M"
    assert delta_row(usage["comparison"], "vs yesterday") == e["expected"]["delta_row"]
    assert active_segment(fx("active-time-today.json")["active_ms"]) == "active 3 h 12 m"
    ranks = top_ranks(usage)
    assert ranks["tools"] == e["expected"]["top_ranks"]["tools"] == ["Codex 13M", "Claude 4.7M", "Kimi 779k"]
    assert ranks["models"] == e["expected"]["top_ranks"]["models"]


def test_period_delta_sentences_and_rounding():
    assert delta_row(fx("usage-week.json")["comparison"], "vs last week") == \
        "▼ 10% cost · ▼ 8% tokens · ▼ 5% msgs vs last week"
    assert delta_row(fx("usage-month.json")["comparison"], "vs last month") == \
        "▼ 10% cost · ▼ 10% tokens · ▼ 12% msgs vs last month"
    # -11.7 shows as 12, -10.1 as 10: abs(round(pct)), not truncation.
    assert delta_row({"cost_pct": -11.7, "tokens_pct": 0.0, "messages_pct": 3.2}, "vs yesterday") == \
        "▼ 12% cost · ± 0% tokens · ▲ 3% msgs vs yesterday"


def test_year_delta_row_is_absent_not_empty():
    # usage-year's previous window is all zeros -> every pct null -> the whole row hides.
    assert delta_row(fx("usage-year.json")["comparison"], "vs last year") is None


def test_period_hero_strings_across_all_four_windows():
    for case, usage_file, ms_file, active in [
        ("healthy", "usage-today.json", "active-time-today.json", "active 3 h 12 m"),
        ("healthy-week", "usage-week.json", "active-time-week.json", "active 2 d 4 h"),
        ("healthy-month", "usage-month.json", "active-time-month.json", "active 9 d 20 h"),
        ("healthy-year", "usage-year.json", "active-time-year.json", "active 74 d 5 h"),
    ]:
        e = ex(f"{case}.json")["expected"]
        assert active_segment(fx(ms_file)["active_ms"]) == active == e["today"]["active"], case
        assert compact(fx(usage_file)["total_tokens"]) == e["today"]["tokens_compact"], case
        ranks = top_ranks(fx(usage_file))
        assert ranks["tools"] == e["top_ranks"]["tools"], case
        assert ranks["models"] == e["top_ranks"]["models"], case


def test_zero_active_absent_and_multi_server_sum_and_drop():
    assert active_segment(fx("active-time-zero.json")["active_ms"]) is None
    two = [fx("active-time-today.json")] * 2
    assert hero_active_multi(two) == "active 6 h 24 m"  # == expected/multi-server.json
    assert hero_active_multi([fx("active-time-today.json"), None]) is None  # == per-server.json


def test_week_uses_the_calendar_window():
    monday, today = week_window(date(2026, 7, 26))  # that day was a Sunday
    assert (monday, today) == (date(2026, 7, 20), date(2026, 7, 26))
    rng = fx("usage-week.json")["range"]
    assert (rng["from"], rng["to"]) == ("2026-07-20", "2026-07-26")
    # the echo is the raw query param, NOT the window: fixtures pin the trap.
    assert fx("usage-week.json")["period"] == "today"
    assert rng["period_resolved"] == "custom"
    # A Monday request is a one-day window; weekday() must be what moves it.
    assert week_window(date(2026, 7, 27)) == (date(2026, 7, 27), date(2026, 7, 27))


def test_credits_row_and_notification_match_the_pinned_case():
    quota = fx("quota-reset-credits.json")
    now = datetime.fromtimestamp(quota["timestamp"], timezone.utc)
    codex = quota["providers"]["codex"]
    assert credits_row(codex, now) == ex("credits.json")["expected"]["credits_row"] \
        == "⚡ Codex · 2 reset credits · expire in 2 d"
    assert credits_alert(codex, now) is True
    # Three days out: the soonest is gone; 38 d remains, and nothing notifies.
    later = now + timedelta(days=3)
    assert credits_row(codex, later) == "⚡ Codex · 2 reset credits · expire in 38 d"
    assert credits_alert(codex, later) is False
    # Past both: the count still renders, the clause is dropped, no alert.
    long_after = now + timedelta(days=60)
    assert credits_row(codex, long_after) == "⚡ Codex · 2 reset credits"
    assert credits_alert(codex, long_after) is False


def test_quota_without_credits_renders_no_row():
    assert credits_row(fx("quota.json")["providers"]["codex"], datetime.now(timezone.utc)) is None


def test_glance_windows_slice_the_sparse_grid():
    stats = fx("stats-contributions.json")
    end = date(2026, 7, 26)
    assert len(grid_window(stats["contributions"], end, 90)) == \
        ex("healthy-month.json")["expected"]["glance"]["filled_cells"] == 68
    assert len(grid_window(stats["contributions"], end, 180)) == \
        ex("healthy-year.json")["expected"]["glance"]["filled_cells"] == 143


def test_glance_hides_on_zero_sources():
    assert glance_visible(fx("insights-today.json"), "hourly") is True
    assert glance_visible(fx("insights-empty.json"), "hourly") is False  # peak_hour null
    assert glance_visible(fx("insights-week.json"), "daily") is True
    assert glance_visible({"daily": []}, "daily") is False
    assert glance_visible({"contributions": []}, "grid") is False


def test_update_badge_rules():
    version = fx("version.json")
    assert update_badge(version, fx("update-check-available.json")) == "Server update available: v2.6.0"
    assert update_badge({**version, "update_check_enabled": False},
                        fx("update-check-available.json")) is None
    assert update_badge(version, fx("update-check-off.json")) is None
    assert update_badge(version, None) is None


def test_fixtures_carry_every_field_the_contract_documents_as_used():
    usage = fx("usage-today.json")
    for key in ("total_cost", "total_tokens", "total_messages", "by_tool",
                "combined_models", "timestamp"):
        assert key in usage
    for key in ("cost_pct", "tokens_pct", "messages_pct", "cost_prev",
                "tokens_prev", "messages_prev"):
        assert key in usage["comparison"]

    at = fx("active-time-today.json")
    assert isinstance(at["active_ms"], int)  # milliseconds
    ins_t = fx("insights-today.json")
    assert len(ins_t["hourly"]["buckets"]) == 24
    assert set(ins_t["hourly"]["buckets"][0]) >= {"hour", "tokens"}
    assert ins_t["hourly"]["peak_hour"] == 14
    ins_w = fx("insights-week.json")
    assert {d["date"] for d in ins_w["daily"]} == {  # sparse: no Tuesday
        "2026-07-20", "2026-07-22", "2026-07-23", "2026-07-24", "2026-07-25", "2026-07-26"}
    assert all(set(d) >= {"date", "tokens", "intensity"} for d in ins_w["daily"])
    assert all(set(c) >= {"date", "totals", "intensity"} and "tokens" in c["totals"]
               for c in fx("stats-contributions.json")["contributions"])

    rc = fx("quota-reset-credits.json")["providers"]["codex"]["reset_credits"]
    assert set(rc) == {"available_count", "credits"}
    assert all(set(c) >= {"id", "expires_at"} for c in rc["credits"])


def test_insights_totals_agree_with_usage_across_endpoints():
    # The same window must not report different totals through two endpoints;
    # if this breaks, one of the two fixtures was edited without the other.
    day = fx("insights-today.json")
    usage = fx("usage-today.json")
    assert sum(b["tokens"] for b in day["hourly"]["buckets"]) == usage["total_tokens"]
    assert sum(b["messages"] for b in day["hourly"]["buckets"]) == usage["total_messages"]
    week = fx("insights-week.json")
    usage_w = fx("usage-week.json")
    assert sum(d["tokens"] for d in week["daily"]) == usage_w["total_tokens"]
    assert sum(d["messages"] for d in week["daily"]) == usage_w["total_messages"]


def test_case_files_follow_the_v11_case_schema():
    for path in sorted(EXP.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        assert "case" in case and "expected" in case, path.name
        if "fixtures" in case:
            assert "usage" not in case["fixtures"] or "usage_today" not in case["fixtures"], \
                f"{path.name}: mixed old and new usage slot names"
