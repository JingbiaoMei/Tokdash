#!/usr/bin/env python3
"""Render the shipped TUI to text and SVG captures, for review against the design.

    TOKDASH_TUI_NO_REMOTE=1 PYTHONPATH=src python3 scripts/capture_tui_renders.py

Every pane is driven by a real Textual pilot over the contract-shaped payloads
the revision-5 suite already pins (tests/test_tui_revision5.py), with the fetch
seams patched -- so a capture is what the widgets actually draw, clipping and
all, not a styled mockup. The fixture data is SYNTHETIC; the prices are made up.

Two files per case in output/tui-rev5-captures/:

* ``.txt``  the terminal's own cells, one line per row (what a user reads, and
            the only honest check that a number is not cut in half);
* ``.svg``  Textual's own screenshot of the same frame, colour included.
"""
from __future__ import annotations

import asyncio
import copy
import os
import sys
from pathlib import Path

os.environ.setdefault("TOKDASH_TUI_NO_REMOTE", "1")
os.environ.setdefault("TOKDASH_DATA_DIR", "/tmp/tui-rev5-capture-data")
# NO_COLOR is not a tokdash setting. Textual reads it when the App is built and
# installs a Monochrome line filter that bakes greyscale into every rendered
# strip, SVG screenshot included. A capture of the palette must be taken without
# it; the monochrome cases put it back on purpose, per app.
os.environ.pop("NO_COLOR", None)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
OUT = ROOT / "output" / "tui-rev5-captures"

import test_tui_revision5 as fx  # noqa: E402  (contract-shaped fixtures)
import tokdash.tui.app as app_mod  # noqa: E402
from tokdash.tui.app import TokdashApp  # noqa: E402


FAKE: fx.FakeApi | None = None  # the current frame's fake, for cases that edit it


def install_fakes() -> fx.FakeApi:
    """Answer every read from the fixtures, exactly as the suite does."""
    global FAKE
    fake = fx.FakeApi()
    FAKE = fake
    seams = {
        "fetch_usage": ("usage", lambda *a, **k: fake.usage),
        "fetch_active_time": ("active", lambda *a, **k: fake.active),
        "fetch_sessions": ("sessions", fake.sessions_for),
        "fetch_session_detail": ("session_detail", lambda *a, **k: fake.session_detail),
        "fetch_session_speed": ("session_speed", lambda *a, **k: fake.session_speed),
        "fetch_stats": ("stats", fake.stats_for),
        "fetch_insights": ("insights", lambda *a, **k: fake.insights),
        "fetch_output_speed": ("speed", fake.speed_for),
        "fetch_output_speed_job_status": ("job_status", lambda *a, **k: fake.job_status),
        "fetch_quota_state": ("quota", lambda *a, **k: fake.quota),
        "fetch_quota_history": ("quota_history", lambda *a, **k: fake.quota_history),
        "fetch_pricing_db": ("pricing", lambda *a, **k: fake.pricing),
    }
    for name, (key, pick) in seams.items():
        setattr(app_mod, name, fake.seam(key, pick))
    app_mod.follow_output_speed_job = fake.follow
    app_mod.db_summary = lambda: "usage db disabled (TOKDASH_USAGE_DB=0) - live parsing"
    app_mod.ensure_usage_db_compatible = lambda: None
    app_mod._local_today = lambda: fx.TODAY
    return fake


def screen_lines(app) -> list[str]:
    return [strip.text for strip in app.screen._compositor.render_strips(app.screen.size)]


def capture(name: str, size: tuple[int, int], body, *, initial: str = "today",
            mono: bool = False) -> None:
    """One pilot run, one frame, two files."""
    if mono:  # a real terminal asks for this with NO_COLOR=1
        os.environ["NO_COLOR"] = "1"
    else:
        os.environ.pop("NO_COLOR", None)
    app = TokdashApp(initial_period=initial)

    async def main():
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.6)
            await fx.settle(pilot, 3)
            if body is not None:
                await body(app, pilot)
                await fx.settle(pilot, 2)
            if mono:
                app.no_color = True  # drives the {nocolor} CSS variable as well
                app._repaint_pane(app._active_pane())
                await fx.settle(pilot, 1)
            rows = screen_lines(app)
            (OUT / f"{name}.txt").write_text(
                "\n".join(row.rstrip() for row in rows) + "\n", encoding="utf-8"
            )
            (OUT / f"{name}.svg").write_text(app.export_screenshot(), encoding="utf-8")
            print(f"{name:38} {size[0]:3}x{size[1]:<3} {len(rows)} rows", flush=True)

    # Not asyncio.run(): its Runner.close() waits for the default thread
    # executor, and the app's off-loop reads (a cold usage scan on a fresh data
    # directory, above all) can still be in flight when the frame is done. The
    # capture owns the two files it just wrote; waiting for a worker it never
    # asked to finish costs minutes per case and changes nothing on screen.
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        for task in asyncio.all_tasks(loop):
            task.cancel()
        loop.close()


# --------------------------------------------------------------- interactions --

async def pick_range(app, pilot, key: str) -> None:
    """Choose a window from the strip's chooser, the way the keyboard does.

    The strip shows five shortcuts; everything further back -- the seven days
    the preview was drawn with among them -- lives behind "p More". A button
    that is not on screen cannot be pressed at all (Textual refuses), so the
    captures go through the chooser rather than through a widget the user
    never sees.
    """
    await pilot.press("p")
    await fx.settle(pilot, 2)
    assert type(app.screen).__name__ == "PresetPicker", type(app.screen).__name__
    app.screen.dismiss(key)  # what picking the row does
    await fx.settle(pilot, 4)


async def week(app, pilot):
    """The preview's seven-day window. Production opens on Today; the range
    strip and every pane re-anchor to whatever is selected."""
    await pick_range(app, pilot, "7d")


async def models(app, pilot):
    await week(app, pilot)
    app.query_one("#ov-tab-models").press()
    await fx.settle(pilot, 2)


async def models_by_cost(app, pilot):
    await models(app, pilot)
    await pilot.press("s")  # tokens -> cost: the SAME table, re-sorted
    await fx.settle(pilot, 2)


async def harness_detail(app, pilot):
    await week(app, pilot)
    app.query_one("#ov-table").focus()
    await pilot.press("enter")
    await fx.settle(pilot, 2)


async def overview_search(app, pilot):
    await week(app, pilot)
    await pilot.press("/")
    for ch in "cod":
        await pilot.press(ch)
    await fx.settle(pilot, 2)


async def overview_scrolled(app, pilot):
    await week(app, pilot)
    table = app.query_one("#ov-table")
    table.focus()
    for _ in range(6):
        await pilot.press("down")
    await fx.settle(pilot, 2)


async def sessions(app, pilot):
    await week(app, pilot)
    app.action_show_pane("sessions")
    await fx.settle(pilot, 5)


async def session_detail(app, pilot):
    await sessions(app, pilot)
    app.query_one("#sn-table").focus()
    await pilot.press("enter")
    await fx.settle(pilot, 5)


async def sessions_search(app, pilot):
    await sessions(app, pilot)
    await pilot.press("/")
    for ch in "tok":
        await pilot.press(ch)
    await fx.settle(pilot, 3)


async def stats(app, pilot):
    app.action_show_pane("stats")
    await fx.settle(pilot, 4)
    await pilot.press("p")  # trailing year -> the named calendar year
    await fx.settle(pilot, 4)


async def stats_narrow(app, pilot):
    app.action_show_pane("stats")
    await fx.settle(pilot, 4)
    await pilot.press("p")
    await fx.settle(pilot, 3)
    await pilot.press("left_square_bracket")  # one month back, at 80 columns
    await fx.settle(pilot, 2)


async def report(app, pilot):
    await week(app, pilot)
    app.action_show_pane("report")
    await fx.settle(pilot, 5)


async def report_when(app, pilot):
    # A NIGHT SHARE WORTH READING. The fixture's 0.0 cannot tell a percentage
    # from a ratio printed with a "%" stuck on it, which is exactly the mistake
    # this frame is here to catch.
    FAKE.insights = {
        **FAKE.insights,
        "hourly": {**FAKE.insights.get("hourly", {}), "night_share": 0.213},
    }
    await report(app, pilot)
    app.query_one("#rp-tab-when").press()
    await fx.settle(pilot, 2)


async def report_scan_failed(app, pilot):
    """A facet read that FAILED is named as failed, not left "still running"."""
    FAKE.insights = RuntimeError("capture: analytics reader unavailable")
    await report(app, pilot)
    app.query_one("#rp-tab-when").press()
    await fx.settle(pilot, 4)


async def report_projects(app, pilot):
    await report(app, pilot)
    app.query_one("#rp-tab-projects").press()
    await fx.settle(pilot, 2)


async def speed(app, pilot):
    await week(app, pilot)
    app.action_show_pane("speed")
    await fx.settle(pilot, 5)


async def speed_combined(app, pilot):
    await speed(app, pilot)
    app.query_one("#sp-lens-combined").press()
    await fx.settle(pilot, 5)


async def speed_trend(app, pilot):
    await speed(app, pilot)
    app.query_one("#sp-mode-trend").press()
    await fx.settle(pilot, 5)


async def speed_hour(app, pilot):
    await speed(app, pilot)
    app.query_one("#sp-mode-hour").press()
    await fx.settle(pilot, 5)


async def speed_hour_chooser(app, pilot):
    """``k`` inside Hour: every comparison key this snapshot measured."""
    await speed_hour(app, pilot)
    await pilot.press("k")
    await fx.settle(pilot, 2)


async def speed_detail(app, pilot):
    await speed(app, pilot)
    app.query_one("#sp-table").focus()
    await pilot.press("enter")
    await fx.settle(pilot, 5)


async def quota(app, pilot):
    app.action_show_pane("quota")
    await fx.settle(pilot, 4)


async def pricing(app, pilot):
    app.action_show_pane("pricing")
    await fx.settle(pilot, 4)
    await pilot.press("/")
    for ch in "gpt":
        await pilot.press(ch)
    await fx.settle(pilot, 2)


async def help_panel(app, pilot):
    await pilot.press("question_mark")
    await fx.settle(pilot, 2)


async def view_picker(app, pilot):
    await pilot.press("v")
    await fx.settle(pilot, 2)


# ---- the workflows the review found broken, now that they work -------------

async def sessions_sources(app, pilot):
    """``h`` opens a chooser over every supported source, marked where it is."""
    await sessions(app, pilot)
    await pilot.press("h")
    await fx.settle(pilot, 2)


async def session_turns(app, pilot):
    """Whole-session detail: every turn, and the arrows reach the last one."""
    detail = copy.deepcopy(fx.SESSION_DETAIL)
    detail["turns"] = [
        dict(detail["turns"][0], turn_index=index, model=f"unique-model-{index}")
        for index in range(1, 21)
    ]
    FAKE.session_detail = detail
    await session_detail(app, pilot)
    for _ in range(19):  # Pilot has no repeat= in this Textual
        await pilot.press("down")
    await fx.settle(pilot, 3)


async def quota_detail(app, pilot):
    """Enter on a meter names the account behind it and what that account says."""
    await quota(app, pilot)
    table = app.query_one("#quota-table")
    table.focus()
    rows = [table.get_row_at(row) for row in range(table.row_count)]
    target = next(
        (i for i, row in enumerate(rows) if any("academic" in str(c) for c in row)), 0
    )
    table.move_cursor(row=target)
    await pilot.press("enter")
    await fx.settle(pilot, 2)
    app.query_one("#quota-detail").scroll_visible()
    await fx.settle(pilot, 2)


async def report_projects_cost(app, pilot):
    """``s`` re-ranks the same list; the cost leader moves to the top."""
    await report_projects(app, pilot)
    await pilot.press("s")
    await fx.settle(pilot, 3)


async def stats_this_year(app, pilot):
    """One ``p`` from the trailing window to the calendar year in progress."""
    app.action_show_pane("stats")
    await fx.settle(pilot, 4)
    await pilot.press("p")
    await fx.settle(pilot, 4)


async def speed_whole_request(app, pilot):
    """``x`` widens the timing set and the note says which set is in force."""
    await speed(app, pilot)
    await pilot.press("x")
    await fx.settle(pilot, 3)


async def speed_combined_hour(app, pilot):
    """Hour of day for a MERGED model: its own 24 hours, not the time buckets."""
    await speed_combined(app, pilot)
    app.query_one("#sp-mode-hour").press()
    await fx.settle(pilot, 4)


async def speed_trend_detail(app, pilot):
    """Enter on a bucket: the bucket's own key, its window, and whose series it is."""
    await speed_trend(app, pilot)
    app.query_one("#sp-table").focus()
    await pilot.press("enter")
    await fx.settle(pilot, 2)


async def speed_trend_second_model(app, pilot):
    """A merged-model pick changes the RATES, not just the name above the table."""
    other = copy.deepcopy(fx.SPEED_TREND["model_series"][0])
    other.update(model="claude-opus-4-7", combined=[233.0, 244.0], calls=[7, 8],
                 parallelism=[2.5, 2.5])
    FAKE.speed_trend["model_series"].append(other)
    await speed_combined(app, pilot)
    app.query_one("#sp-mode-trend").press()
    await fx.settle(pilot, 4)
    await pilot.press("k")
    await fx.settle(pilot, 2)
    await pilot.press("down")
    await pilot.press("enter")
    await fx.settle(pilot, 4)


async def speed_hour_detail(app, pilot):
    """Enter on an hour: that hour's rate, its responses, and the selection."""
    await speed(app, pilot)
    app.query_one("#sp-mode-hour").press()
    await fx.settle(pilot, 4)
    table = app.query_one("#sp-table")
    table.focus()
    table.move_cursor(row=9)
    await pilot.press("enter")
    await fx.settle(pilot, 2)


async def _second_merged_model():
    """A second merged series, so a series PICK is something the reader can do."""
    other = copy.deepcopy(fx.SPEED_TREND["model_series"][0])
    other.update(model="claude-opus-4-7", combined=[233.0, 244.0], calls=[7, 8],
                 parallelism=[2.5, 2.5],
                 hours={"combined": [77.0] * 24, "calls": [7] * 24,
                        "parallelism": [1.1] * 24})
    FAKE.speed_trend["model_series"].append(other)


async def speed_hour_series_pending(app, pilot):
    """A series read that is still out there: the rows keep the OLD name.

    The heading used to follow the pick, so a slow answer put model two's name
    over model one's rates. The wait is now said out loud, above rows that still
    belong to the series they belonged to.
    """
    import threading
    await _second_merged_model()
    hold = threading.Event()
    await speed_combined(app, pilot)
    app.query_one("#sp-mode-hour").press()
    await fx.settle(pilot, 4)
    def hold_series(**kwargs):
        # Returns None so the fixture still answers once the wait expires; the
        # frame is taken while the thread is parked, which is the point.
        if kwargs.get("view") in ("trend", "time-of-day"):
            hold.wait(3)
        return None

    FAKE.hooks["speed"] = hold_series
    await pilot.press("k")
    await fx.settle(pilot, 2)
    await pilot.press("down")
    await pilot.press("enter")
    await fx.settle(pilot, 2)
    # A repaint is what makes the stale state visible, so force one the way a
    # reader does by dragging the terminal: the note must admit the wait.
    await pilot.resize_terminal(100, 31)
    await pilot.resize_terminal(100, 30)
    await fx.settle(pilot, 2)


async def speed_hour_series_failed(app, pilot):
    """A series read that FAILED: the pane says so and offers r, not a new name."""
    await _second_merged_model()
    await speed_combined(app, pilot)
    app.query_one("#sp-mode-hour").press()
    await fx.settle(pilot, 4)
    FAKE.hooks["speed"] = lambda **kwargs: (
        RuntimeError("capture: the series reader did not answer")
        if kwargs.get("view") in ("trend", "time-of-day") else None
    )
    await pilot.press("k")
    await fx.settle(pilot, 2)
    await pilot.press("down")
    await pilot.press("enter")
    await fx.settle(pilot, 3)


async def speed_combined_detail(app, pilot):
    """Enter on a merged row: its rate divides by the UNION of busy time."""
    await speed_combined(app, pilot)
    table = app.query_one("#sp-table")
    table.focus()
    table.move_cursor(row=0)
    await pilot.press("enter")
    await fx.settle(pilot, 2)


# ------------------------------------------------------- populated fixtures --
#
# The pinned fixtures in the suite are small on purpose: three rows and a short
# name are the clearest way to pin a rule. They are the wrong size to judge a
# LAYOUT with -- a pane holding three rows cannot show whether a table crowds its
# caption or whether a name survives its column. These payloads are the same
# contract filled to what a working week returns, including names long enough to
# be cropped so a capture shows the crop. Synthetic data, invented prices.

_RICH_SOURCES = [
    # tool, tokens, cost, cache-hit, messages, models[(name, tokens, cost, msgs)]
    ("codex", 6_100_000, 243.72, 0.912, 791, [
        ("gpt-5.1-codex", 3_100_000, 121.40, 402),
        ("gpt-5.5", 2_200_000, 96.20, 260),
        ("gpt-5.1-codex-mini", 800_000, 26.12, 129)]),
    ("claude", 4_300_000, 201.44, 0.887, 249, [
        ("claude-opus-4.1", 2_600_000, 160.90, 118),
        ("claude-sonnet-4", 1_700_000, 40.54, 131)]),
    ("opencode", 2_050_000, 54.81, 0.795, 964, [
        ("deepseek-v3.2", 1_150_000, 12.40, 502),
        ("kimi-k2.6", 900_000, 42.41, 462)]),
    ("cursor", 1_210_000, 24.55, 0.813, 314, [
        ("gpt-5.5", 700_000, 15.20, 180),
        ("claude-sonnet-4", 510_000, 9.35, 134)]),
    ("kimi", 1_050_000, 20.44, 0.809, 835, [
        ("kimi-k2.6", 1_050_000, 20.44, 835)]),
    ("omp", 910_000, 14.24, 0.838, 421, [
        ("gpt-5.5", 910_000, 14.24, 421)]),
    ("qwen_code", 560_000, 8.46, 0.753, 471, [
        # A name with no room in it, on purpose.
        ("gemini-3-flash-experimental-2507", 560_000, 8.46, 471)]),
]


def rich_usage() -> dict:
    """Seven sources, a dozen models, a week of daily buckets."""
    by_tool, apps, merged = {}, {}, {}
    for tool, tokens, cost, hit, messages, models in _RICH_SOURCES:
        cache = round(tokens * hit)
        by_tool[tool] = {
            "tokens": tokens, "cost": cost,
            "tokens_in": round(tokens * 0.62), "tokens_out": round(tokens * 0.16),
            "tokens_cache": cache, "tokens_reasoning": round(tokens * 0.13),
            "cache_hit_rate": hit,
        }
        apps[tool] = {
            "messages": messages,
            "tokens_in": round(tokens * 0.62), "tokens_out": round(tokens * 0.16),
            "tokens_cache": cache,
            "models": [{"name": name, "tokens": tk, "cost": c, "messages": m}
                       for name, tk, c, m in models],
        }
        for name, tk, c, m in models:
            row = merged.setdefault(name, {"name": name, "tokens": 0, "tokens_in": 0,
                                           "tokens_out": 0, "tokens_cache": 0,
                                           "cost": 0.0, "messages": 0})
            row["tokens"] += tk
            row["tokens_in"] += round(tk * 0.6)
            row["tokens_out"] += round(tk * 0.16)
            row["tokens_cache"] += round(tk * 0.8)
            row["cost"] = round(row["cost"] + c, 2)
            row["messages"] += m
    rows = sorted(merged.values(), key=lambda r: -r["tokens"])
    for row in rows:
        row["cache_hit_rate"] = round(row["tokens_cache"] / max(1, row["tokens_in"] * 1.6), 3)
    tokens = sum(t["tokens"] for t in by_tool.values())
    days = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15",
            "2026-10-16", "2026-10-17", "2026-10-18"]
    shape = [270_000, 349_000, 571_000, 612_000, 0, 403_000, 479_000]
    return {
        "period": "last7days",
        "range": {"from": days[0], "to": days[-1], "days": 7, "recognized": True},
        "total_tokens": tokens,
        "total_cost": round(sum(t["cost"] for t in by_tool.values()), 2),
        "total_messages": sum(a["messages"] for a in apps.values()),
        "cache_hit_rate": round(sum(t["tokens_cache"] for t in by_tool.values()) / tokens, 3),
        "comparison": {"tokens_pct": 12.0, "cost_pct": -4.0, "messages_pct": 6.1,
                       "prior_range": {"from": "2026-10-05", "to": "2026-10-11"}},
        "by_tool": by_tool,
        "apps": apps,
        "openclaw_models": [],
        "combined_models": rows,
        "top_models": [{"name": r["name"], "tokens": r["tokens"], "cost": r["cost"]}
                       for r in rows[:5]],
        "top_models_by_cost": sorted(
            ({"name": r["name"], "tokens": r["tokens"], "cost": r["cost"]} for r in rows),
            key=lambda r: -r["cost"])[:5],
        # A real zero in the middle of the week: measured, not missing.
        "sparkline": {
            "granularity": "day",
            "keys": days,
            "buckets": [{"key": day, "tokens": value,
                         "cost": round(value * 0.00004, 2),
                         "messages": value // 4000,
                         "input": value * 6 // 10, "cache": value * 8 // 10}
                        for day, value in zip(days, shape)],
        },
        "source_errors": [],
    }


def rich_active() -> dict:
    """Agent time for the sources that can report it; one that cannot."""
    by_tool = {}
    for tool, tokens, _cost, _hit, _msgs, _models in _RICH_SOURCES:
        if tool == "kimi":
            continue  # unreadable here, and said so below rather than drawn as 0
        ms = int(tokens * 9)
        by_tool[tool] = {"tool_label": tool, "session_count": max(2, tokens // 400_000),
                         "active_ms": ms, "active_ms_sum": int(ms * 1.35)}
    return {
        "period": "last7days",
        "active_ms": sum(t["active_ms"] for t in by_tool.values()),
        "active_ms_sum": sum(t["active_ms_sum"] for t in by_tool.values()),
        "comparison": {"active_ms_sum_pct": 8.0},
        "by_tool": by_tool,
        "unavailable_tools": ["kimi"],
        "active_gap_cap_ms": 300_000,
        "active_time_estimated": True,
        "active_time_method": "capped-inter-event-gap",
        "include_review_sessions": False,
    }


def _speed_row(source, model, kind, basis, rate, combined, calls, eligible, par=1.4):
    return {"source": source, "model": model, "measurement_kind": kind,
            "token_basis": basis, "output_tok_per_s": rate,
            "combined_tok_per_s": combined, "parallelism": par,
            "peak_in_flight": max(2, int(par * 2)), "speed_calls": calls,
            "eligible_calls": eligible,
            "coverage": {"ratio": round(calls / eligible, 3)},
            "last_ms": 1_760_000_000_000}


_RICH_SPEED_ROWS = [
    _speed_row("qwen_code", "gemini-3-flash-experimental-2507", "post_first_token",
               "output_including_reasoning", 213.8, 268.4, 1_128, 1_412, 1.2),
    _speed_row("omp", "gpt-5.5", "post_first_token",
               "output_including_reasoning", 164.6, 220.1, 973, 1_073, 1.6),
    _speed_row("codex", "gpt-5.5", "response_window",
               "output_including_reasoning", 160.8, 214.0, 2_471, 2_631, 1.9),
    _speed_row("codex", "gpt-5.1-codex", "response_window",
               "output_including_reasoning", 138.2, 191.7, 3_902, 4_154, 1.4),
    _speed_row("codex", "gpt-5.1-codex", "request_window",
               "output_only", 41.0, 55.2, 12, 1_000, 1.1),
    _speed_row("kimi", "kimi-k2.6", "server_decode",
               "output_including_reasoning", 124.5, 168.9, 1_157, 1_195, 1.05),
    _speed_row("claude", "claude-sonnet-4", "post_first_token",
               "output_including_reasoning", 68.1, 92.4, 1_198, 1_365, 1.3),
    _speed_row("claude", "claude-opus-4.1", "post_first_token",
               "output_including_reasoning", 59.7, 80.2, 1_968, 2_240, 1.15),
    _speed_row("opencode", "deepseek-v3.2", "request_window",
               "output_only", 96.3, 130.7, 640, 702, 1.0),
    _speed_row("cursor", "gpt-5.5", "response_window",
               "output_including_reasoning", 152.4, 199.6, 388, 420, 1.25),
]


def rich_speed_across() -> dict:
    """Ten comparison keys: same model under two timings, same name in two sources."""
    merged: dict[str, dict] = {}
    for row in _RICH_SPEED_ROWS:
        entry = merged.setdefault(row["model"], {
            "model": row["model"], "sources": [], "measurement_kinds": [],
            "token_bases": [], "speed_tokens": 0, "speed_ms": 0.0, "speed_calls": 0,
            "combined_tok_per_s": row["combined_tok_per_s"], "busy_ms": 0.0,
            "parallelism": row["parallelism"], "peak_in_flight": row["peak_in_flight"],
            "eligible_calls": 0, "coverage": {"ratio": 0.82}})
        entry["sources"].append(row["source"])
        entry["measurement_kinds"].append(row["measurement_kind"])
        entry["token_bases"].append(row["token_basis"])
        entry["speed_calls"] += row["speed_calls"]
        entry["eligible_calls"] += row["eligible_calls"]
    return {
        "schema_version": 7, "view": "across-models", "metric": "output_tok_per_s",
        "aggregation": "sum_tokens_over_sum_duration",
        "range": {"from": "2026-10-12", "to": "2026-10-18"},
        "cache": {"state": "ready", "job_id": None, "updated_at": "2026-10-18T10:00:00"},
        "rows": copy.deepcopy(_RICH_SPEED_ROWS),
        "models": [dict(entry, sources=sorted(set(entry["sources"])),
                        measurement_kinds=sorted(set(entry["measurement_kinds"])),
                        token_bases=sorted(set(entry["token_bases"])))
                   for entry in merged.values()],
        "hidden_request_windows": [{"source": "opencode", "measurement_kind": "request_window"}],
        "source_status": [{"source": "mimo", "status": "timing_unavailable", "usage_rows": 14}],
        "available_measurement_range": {"from": "2026-09-02", "to": "2026-10-18"},
    }


_RICH_TREND_DAYS = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15",
                    "2026-10-16", "2026-10-17", "2026-10-18"]
# None is a bucket the server measured NOTHING in; 0.0 is a bucket that measured
# a zero. A layout that draws them alike is lying about one of them.
_RICH_TREND_STREAM = [188.4, 204.8, 213.0, 221.2, None, 245.8, 229.4]
_RICH_TREND_COMBINED = [230.1, 260.4, 268.0, 281.6, 0.0, 301.2, 288.9]
_RICH_TREND_SECOND = [121.0, 130.5, None, 144.2, 150.0, 151.8, 149.3]


def rich_speed_trend() -> dict:
    """Two weeks of DAILY buckets, a gap, a measured zero, four comparison keys."""
    def arrays(base):
        return {"per_stream": list(_RICH_TREND_STREAM), "combined": list(base),
                "calls": [226, 114, 148, 182, 0, 216, 138],
                "parallelism": [1.2, 1.4, 1.3, 1.5, None, 1.6, 1.35]}
    return {
        "granularity": "day", "requested_bucket": "auto", "auto_bucket": "day",
        "offered_buckets": [{"bucket": "day", "count": 7}, {"bucket": "hour", "count": 168}],
        "buckets": [{"key": day, "from": day, "to": day} for day in _RICH_TREND_DAYS],
        "spread": {"quantiles": [10, 50, 90], "min_responses": 30, "weight": "duration",
                   "p10": [140.0, 150.0, 160.0, 170.0, None, 190.0, 180.0],
                   "p50": [188.4, 204.8, 213.0, 221.2, None, 245.8, 229.4],
                   "p90": [230.0, 250.0, 260.0, 270.0, None, 300.0, 280.0]},
        "series": [
            dict({"source": "codex", "model": "gpt-5.1-codex",
                  "measurement_kind": "response_window",
                  "token_basis": "output_including_reasoning"}, **arrays(_RICH_TREND_COMBINED)),
            dict({"source": "claude", "model": "claude-opus-4.1",
                  "measurement_kind": "post_first_token",
                  "token_basis": "output_including_reasoning"}, **arrays(_RICH_TREND_SECOND)),
        ],
        "model_series": [
            dict({"model": "gpt-5.1-codex", "sources": ["claude", "codex"],
                  "measurement_kinds": ["post_first_token", "response_window"]},
                 **arrays(_RICH_TREND_COMBINED)),
            dict({"model": "claude-opus-4.1", "sources": ["claude"],
                  "measurement_kinds": ["post_first_token"]},
                 **arrays(_RICH_TREND_SECOND)),
        ],
    }


# Hour-of-day, one value per hour the server measured. None is an hour with no
# measurement at all (a gap); 0.0 at 13:00 is an hour that measured a zero. The
# overnight hours are the gap, so a layout that fills them has invented them.
_RICH_HOURS = [
    (None, 0), (None, 0), (None, 0), (None, 0), (None, 0), (None, 0),
    (None, 0), (None, 0), (96.0, 41), (148.2, 88), (161.0, 121), (172.4, 143),
    (155.1, 130), (0.0, 7), (140.8, 118), (168.0, 151), (175.6, 168), (181.2, 175),
    (176.0, 170), (164.3, 152), (150.9, 128), (133.4, 96), (118.7, 71), (92.1, 44),
]
# The merged model's own hours: the combined lens answers for a whole model over
# the UNION of its busy time, so these are higher than the per-stream figures.
_RICH_HOURS_COMBINED = [
    (rate * 1.22 if rate is not None else None) for rate, _calls in _RICH_HOURS
]


def rich_speed_hour() -> dict:
    """A day's worth of hours, in both lens shapes the view reads."""
    calls = [c for _rate, c in _RICH_HOURS]
    return {
        "schema_version": 7, "view": "time-of-day", "metric": "output_tok_per_s",
        "bucket": "local_hour_of_day", "timestamp_basis": "usage_timestamp",
        "granularity": "hour",
        "selected_key": {"source": "codex", "model": "gpt-5.1-codex",
                         "measurement_kind": "response_window",
                         "token_basis": "output_including_reasoning"},
        "hourly": [
            {"hour": hour, "label": "%02d:00-%02d:00" % (hour, (hour + 1) % 24),
             "output_tok_per_s": rate,
             "speed_tokens": int((rate or 0) * 1400), "speed_ms": (rate or 0) * 1400 / 150,
             "speed_calls": c, "eligible_calls": 210,
             "coverage": {"ratio": round(c / 210, 3)},
             "days_with_measurements": (0 if not c else 6),
             "excluded_calls_by_reason": {}, "missing_timing_calls": (210 - c)}
            for hour, (rate, c) in enumerate(_RICH_HOURS)
        ],
        "days_with_measurements": 6,
        "eligible_calls": sum(210 for _ in _RICH_HOURS),
        "coverage": {"ratio": 0.52},
        "first_measured_at": 1_760_000_000_000,
        # Combined-lens arrays, read when the lens asks for the merged model.
        "combined": list(_RICH_HOURS_COMBINED), "calls": calls,
        "parallelism": [None] * 8 + [1.6, 1.9, 2.1, 2.3, 2.2, 1.0, 2.0, 2.4, 2.5,
                                     2.6, 2.5, 2.3, 2.1, 1.9, 1.7, 1.5, 1.3, 1.1],
        "sources": ["claude", "codex"], "kinds": ["post_first_token", "response_window"],
    }


def use_rich_payloads() -> None:
    """Point the fake at the populated week. Must run before the pane loads."""
    assert FAKE is not None, "install_fakes() first"
    FAKE.usage = rich_usage()
    FAKE.active = rich_active()
    FAKE.speed_across = rich_speed_across()
    FAKE.speed_trend = rich_speed_trend()
    FAKE.speed_hour = rich_speed_hour()
    FAKE.sessions = rich_sessions()


# Six of the twenty supported tools carry sessions this week, and one carries a
# lot: the chip row is a filter over what the window HOLDS, so a capture where
# every tool looks alike cannot show whether it orders itself usefully, thins
# itself at eighty columns, or keeps the tool that is in force on screen.
_RICH_PROJECTS = [
    ("tokdash", "TUI parity"),
    ("tokdash", "pricing normalize aliases"),
    ("companion", "status refresh"),
    ("campus_runs", "r56B budget sweep"),
    ("ad-prof", "stage_w train"),
    ("docs", "release notes"),
]
_RICH_MODELS = ["gpt-5.1-codex", "gpt-5.5", "claude-opus-4-1", "kimi-k2.6",
                "deepseek-v3.2", "gemini-3-flash"]
_RICH_TOOL_LOAD = {"codex": 6, "claude": 5, "opencode": 3, "kimi": 2,
                   "omp": 2, "qwen_code": 1}


def rich_sessions() -> dict:
    """One /api/sessions body per supported tool, in the shape the route sends."""
    out: dict[str, dict] = {}
    load = _RICH_TOOL_LOAD
    for tool in fx.SESSION_TOOLS:
        rows = []
        for n in range(load.get(tool, 0)):
            project, name = _RICH_PROJECTS[n % len(_RICH_PROJECTS)]
            day = 18 - (n % 6)
            rows.append({
                "tool": tool,
                "session_id": f"{tool}-202610{day:02d}-{n + 1}",
                "project": f"{project} / {name}",
                "model": _RICH_MODELS[n % len(_RICH_MODELS)],
                "token_events": 14 + n * 9,
                "tokens": 929_000 + n * 1_900_000,
                "cost": 1.31 + n * 2.4,
                "started_at": f"2026-10-{day:02d}T09:{n:02d}:00+00:00",
                "last_seen_at": f"2026-10-{day:02d}T{11 + n}:{40 + n}:00+00:00",
            })
        out[tool] = {"tool": tool, "tool_label": tool, "sessions": rows}
    return out


async def rich_sessions_pane(app, pilot):
    """Sessions at a working load: six tools, nineteen rows."""
    use_rich_payloads()
    app.action_show_pane("sessions")
    await fx.settle(pilot, 6)


async def rich_sessions_filtered(app, pilot):
    """One tool chosen from the chip row: the row must still name it and still
    offer the way back."""
    from textual.widgets import Button
    await rich_sessions_pane(app, pilot)
    app.query_one("#sn-tool-claude", Button).press()
    await fx.settle(pilot, 6)


async def rich_sessions_detail(app, pilot):
    from textual.widgets import DataTable
    await rich_sessions_pane(app, pilot)
    table = app.query_one("#sn-table", DataTable)
    table.action_cursor_down()
    app.action_open_row()
    await fx.settle(pilot, 6)


async def rich_overview(app, pilot):
    """A working week on Overview: seven sources, a dozen merged models."""
    use_rich_payloads()
    await week(app, pilot)


async def rich_models(app, pilot):
    await rich_overview(app, pilot)
    app.query_one("#ov-tab-models").press()
    await fx.settle(pilot, 3)


async def rich_activity(app, pilot):
    """Scrolled to the activity surface, so the chart is seen whole."""
    await rich_overview(app, pilot)
    app.query_one("#ov-activity").scroll_visible()
    await fx.settle(pilot, 2)


async def rich_speed(app, pilot):
    use_rich_payloads()
    await week(app, pilot)
    app.action_show_pane("speed")
    await fx.settle(pilot, 6)


async def rich_speed_combined(app, pilot):
    await rich_speed(app, pilot)
    app.query_one("#sp-lens-combined").press()
    await fx.settle(pilot, 6)


async def rich_view_trend(app, pilot):
    await rich_speed(app, pilot)
    app.query_one("#sp-mode-trend").press()
    await fx.settle(pilot, 6)


async def rich_view_hour(app, pilot):
    await rich_speed(app, pilot)
    app.query_one("#sp-mode-hour").press()
    await fx.settle(pilot, 6)


async def rich_speed_hour_combined(app, pilot):
    await rich_speed_combined(app, pilot)
    app.query_one("#sp-mode-hour").press()
    await fx.settle(pilot, 6)


async def rich_speed_detail(app, pilot):
    """Enter on a row: the exact figures a clipped cell could not carry."""
    await rich_speed(app, pilot)
    app.query_one("#sp-table").move_cursor(row=1)
    await pilot.press("enter")
    await fx.settle(pilot, 4)


async def rich_speed_trend_detail(app, pilot):
    await rich_view_trend(app, pilot)
    app.query_one("#sp-table").move_cursor(row=2)
    await pilot.press("enter")
    await fx.settle(pilot, 4)


CASES = [
    # name, size, body, initial period, monochrome
    ("01-overview-today-120x36", (120, 36), None, "today", False),
    ("02-overview-7d-120x36", (120, 36), week, "today", False),
    ("03-overview-models-120x36", (120, 36), models, "today", False),
    ("04-overview-models-by-cost-120x36", (120, 36), models_by_cost, "today", False),
    ("05-overview-harness-detail-120x36", (120, 36), harness_detail, "today", False),
    ("06-overview-search-120x36", (120, 36), overview_search, "today", False),
    ("07-overview-scrolled-120x36", (120, 36), overview_scrolled, "today", False),
    ("08-overview-80x24", (80, 24), week, "today", False),
    ("09-overview-100x30", (100, 30), week, "today", False),
    ("10-sessions-100x30", (100, 30), sessions, "today", False),
    ("11-session-detail-100x30", (100, 30), session_detail, "today", False),
    ("12-sessions-search-100x30", (100, 30), sessions_search, "today", False),
    ("13-sessions-80x24", (80, 24), sessions, "today", False),
    ("14-stats-year-100x30", (100, 30), stats, "today", False),
    ("15-stats-80x24", (80, 24), stats_narrow, "today", False),
    ("16-report-summary-100x30", (100, 30), report, "today", False),
    ("17-report-when-100x30", (100, 30), report_when, "today", False),
    ("18-report-projects-100x30", (100, 30), report_projects, "today", False),
    ("19-report-80x24", (80, 24), report, "today", False),
    ("20-speed-across-100x30", (100, 30), speed, "today", False),
    ("21-speed-combined-100x30", (100, 30), speed_combined, "today", False),
    ("22-speed-trend-100x30", (100, 30), speed_trend, "today", False),
    ("23-speed-hour-100x30", (100, 30), speed_hour, "today", False),
    ("24-speed-detail-100x30", (100, 30), speed_detail, "today", False),
    ("25-speed-80x24", (80, 24), speed, "today", False),
    ("26-quota-100x30", (100, 30), quota, "today", False),
    ("27-quota-80x24", (80, 24), quota, "today", False),
    ("28-pricing-100x30", (100, 30), pricing, "today", False),
    ("29-overview-monochrome-120x36", (120, 36), week, "today", True),
    ("30-sessions-monochrome-80x24", (80, 24), sessions, "today", True),
    ("31-overview-year-120x36", (120, 36), None, "year", False),
    ("32-help-panel-100x30", (100, 30), help_panel, "today", False),
    ("33-view-picker-80x24", (80, 24), view_picker, "today", False),
    ("34-sessions-sources-100x30", (100, 30), sessions_sources, "today", False),
    ("35-session-turns-100x30", (100, 30), session_turns, "today", False),
    ("36-session-turns-80x24", (80, 24), session_turns, "today", False),
    ("37-quota-account-detail-100x30", (100, 30), quota_detail, "today", False),
    ("38-report-projects-by-cost-100x30", (100, 30), report_projects_cost, "today", False),
    ("39-report-when-80x24", (80, 24), report_when, "today", False),
    ("40-stats-this-year-100x30", (100, 30), stats_this_year, "today", False),
    ("41-speed-whole-request-100x30", (100, 30), speed_whole_request, "today", False),
    ("42-speed-combined-hour-100x30", (100, 30), speed_combined_hour, "today", False),
    ("43-overview-month-120x36", (120, 36), None, "month", False),
    ("44-speed-hour-chooser-100x30", (100, 30), speed_hour_chooser, "today", False),
    ("45-report-scan-failed-100x30", (100, 30), report_scan_failed, "today", False),
    ("46-speed-hour-80x24", (80, 24), speed_hour, "today", False),
    ("47-speed-trend-detail-100x30", (100, 30), speed_trend_detail, "today", False),
    ("48-speed-trend-second-model-100x30", (100, 30), speed_trend_second_model, "today", False),
    ("49-speed-hour-detail-80x24", (80, 24), speed_hour_detail, "today", False),
    ("50-speed-hour-series-pending-100x30", (100, 30), speed_hour_series_pending, "today", False),
    ("51-speed-hour-series-failed-100x30", (100, 30), speed_hour_series_failed, "today", False),
    ("52-speed-combined-detail-100x30", (100, 30), speed_combined_detail, "today", False),
    # -- the populated week: enough rows, names and buckets to judge a LAYOUT --
    # The pinned fixtures above are three rows and a short name on purpose; they
    # pin rules. They cannot show whether a table crowds its caption, whether a
    # long model name survives its column, or whether a chart has anything to
    # draw, so these cases repeat the main views at a normal working load.
    ("60-overview-populated-120x36", (120, 36), rich_overview, "today", False),
    ("61-overview-populated-models-120x36", (120, 36), rich_models, "today", False),
    ("62-overview-populated-100x30", (100, 30), rich_overview, "today", False),
    ("63-overview-populated-80x24", (80, 24), rich_overview, "today", False),
    ("64-overview-activity-120x36", (120, 36), rich_activity, "today", False),
    ("65-overview-populated-80x24-scrolled", (80, 24), rich_activity, "today", False),
    ("70-speed-across-populated-120x36", (120, 36), rich_speed, "today", False),
    ("71-speed-across-populated-100x30", (100, 30), rich_speed, "today", False),
    ("72-speed-across-populated-80x24", (80, 24), rich_speed, "today", False),
    ("73-speed-combined-populated-100x30", (100, 30), rich_speed_combined, "today", False),
    ("74-speed-trend-populated-120x36", (120, 36), rich_view_trend, "today", False),
    ("75-speed-trend-populated-100x30", (100, 30), rich_view_trend, "today", False),
    ("76-speed-trend-populated-80x24", (80, 24), rich_view_trend, "today", False),
    ("77-speed-hour-populated-120x36", (120, 36), rich_view_hour, "today", False),
    ("78-speed-hour-populated-100x30", (100, 30), rich_view_hour, "today", False),
    ("79-speed-hour-populated-80x24", (80, 24), rich_view_hour, "today", False),
    ("80-speed-hour-combined-populated-100x30", (100, 30), rich_speed_hour_combined, "today", False),
    ("81-speed-detail-populated-100x30", (100, 30), rich_speed_detail, "today", False),
    ("82-speed-trend-detail-populated-100x30", (100, 30), rich_speed_trend_detail, "today", False),
    ("90-sessions-populated-120x36", (120, 36), rich_sessions_pane, "7d", False),
    ("91-sessions-populated-100x30", (100, 30), rich_sessions_pane, "7d", False),
    ("92-sessions-populated-80x24", (80, 24), rich_sessions_pane, "7d", False),
    ("93-sessions-filtered-100x30", (100, 30), rich_sessions_filtered, "7d", False),
    ("94-sessions-detail-populated-100x30", (100, 30), rich_sessions_detail, "7d", False),
]


def main() -> int:
    wanted = set(sys.argv[1:])
    OUT.mkdir(parents=True, exist_ok=True)
    for name, size, body, initial, mono in CASES:
        if wanted and name not in wanted:
            continue
        install_fakes()  # a fresh fake per frame: no state bleeds between captures
        capture(name, size, body, initial=initial, mono=mono)
    print(f"\ncaptures in {OUT}", flush=True)
    # Every file is closed and flushed by now. The exit is explicit because the
    # app's worker threads are not: a plain return would sit in
    # threading._shutdown until an in-flight read finished on its own.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
