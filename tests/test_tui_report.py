"""``tokdash report``: one-shot render, --json contract, parser guards, non-mutation.

Fixtures are canned deepcopies built from the ACTUAL response shapes (verified
against compute.py compute_usage/compute_usage_with_comparison, insights.py
_fold_* helpers, sessions.py get_active_time_data) — the report renderer is a
pure view over those dicts, so the payload IS the fixture.

The null-vs-zero law is asserted where the layout exposes it: a missing
active-time join and an unpriced (cost 0.0) row render EM_DASH in a row context,
while a measured $0.00 headline stays "$0.00".
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import sys
import types
from types import SimpleNamespace

import pytest

import tokdash.tui.report as report
from tokdash.cli import cli
from tokdash.tui.data import FetchOutcome
from tokdash.usage_store import UsageDatabaseSchemaTooNewError

# ---------------------------------------------------------------------------
# Canned payloads — field names verified against the compute/insights/sessions
# layer (by_tool{tokens,cost,...}; comparison{*_pct}; hourly buckets[{hour,
# tokens,...}]; weekday buckets[{weekday,name,...}]; tools/models ranked;
# active by_tool{tool_label,session_count,active_ms,active_ms_sum}).
# ---------------------------------------------------------------------------

USAGE_PAYLOAD = {
    "period": "today",
    "range": {"from": "2026-09-14", "to": "2026-09-20", "days": 7, "recognized": True},
    "total_tokens": 12_400_000,
    "total_cost": 142.31,
    "total_messages": 4_820,
    "cache_hit_rate": 0.71,
    "timestamp": "2026-09-20T12:00:00",
    "by_tool": {
        "codex": {"tokens": 8_000_000, "cost": 90.0, "tokens_in": 6_000_000,
                  "tokens_cache": 4_000_000, "cache_hit_rate": 0.66},
        "claude": {"tokens": 4_400_000, "cost": 52.31, "tokens_in": 3_000_000,
                   "tokens_cache": 2_000_000, "cache_hit_rate": 0.78},
    },
    "apps": {},
    "coding_apps": {},
    "coding_models": [],
    "top_models": [],
    "top_models_by_cost": [],
    "openclaw_models": [],
    "combined_models": [
        {"name": "gpt-5-codex", "tokens": 8_000_000, "tokens_in": 6_000_000,
         "tokens_out": 2_000_000, "tokens_cache": 4_000_000, "cost": 90.0,
         "messages": 3_000, "cache_hit_rate": 0.66, "source": "codex"},
    ],
    "comparison": {
        "tokens_prev": 11_460_000,
        "cost_prev": 146.86,
        "messages_prev": 4_820,
        "tokens_pct": 8.2,
        "cost_pct": -3.1,
        "messages_pct": 0.0,
    },
    "source_errors": [],
}

INSIGHTS_PAYLOAD = {
    "schema_version": 1,
    "range": {"from": "2026-09-14", "to": "2026-09-20", "days": 7, "recognized": True},
    "facets": ["daily", "streaks", "firsts", "hourly", "weekday", "tools", "models", "projects"],
    "timezone": "UTC",
    "coverage": {"stored_sources": ["codex", "claude"], "live_sources": [], "group_count": 12},
    "totals": {"tokens": 12_000_000, "cost": 142.31, "messages": 4_800, "entries": 9_600},
    "timestamp": "2026-09-20T12:00:01",
    "daily": [
        {"date": f"2026-09-{day:02d}", "tokens": 1_500_000 * i, "cost": 12.0 * i,
         "messages": 600 * i, "entries": 1_200 * i, "intensity": (i % 4) + 1}
        for i, day in enumerate(range(14, 21), start=1)
    ],
    "streaks": {"current_streak": 6, "longest_streak": 12, "active_days": 6, "total_days": 7},
    "firsts": {
        "first_active_day": "2026-09-14",
        "last_active_day": "2026-09-20",
        "busiest_day": "2026-09-18",
        "busiest_day_tokens": 7_500_000,
        "peak_hour": 14,
    },
    "hourly": {
        "buckets": [
            {"hour": hour, "tokens": 900_000 - hour * 10_000, "cost": 9.0,
             "messages": 400, "entries": 800}
            for hour in range(8, 20)
        ],
        "peak_hour": 14,
        "night_share": 0.12,
        "night_hours": [0, 1, 2, 3, 4, 5],
    },
    "weekday": {
        "buckets": [
            {"weekday": i, "name": name, "tokens": 1_800_000 * (i + 1), "cost": 20.0,
             "messages": 700, "entries": 1_400}
            for i, name in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
        ],
        "peak_weekday": 2,
    },
    "tools": {
        "ranked": [
            {"tool": "codex", "tokens": 8_000_000, "cost": 90.0, "messages": 3_000,
             "entries": 6_000},
            # cost 0.0: unpriced row — renders EM_DASH in row context, not $0.00.
            {"tool": "claude", "tokens": 4_000_000, "cost": 0.0, "messages": 1_800,
             "entries": 3_600},
            # tokens>0 but NO active_time row: the web rule (index.html:14683)
            # filters it out of the table while active time answered.
            {"tool": "kimi", "tokens": 100_000, "cost": 1.5, "messages": 40,
             "entries": 80},
        ]
    },
    "models": {
        "ranked": [
            {"model": "gpt-5-codex", "tokens": 8_000_000, "cost": 90.0, "messages": 3_000,
             "entries": 6_000},
        ],
        "most_used": "gpt-5-codex",
        "highest_cost": "gpt-5-codex",
    },
    "projects": {
        "projects": [
            {"project": "tokdash", "tokens": 9_000_000, "cost": 100.5, "messages": 3_400,
             "entries": 6_800}
        ],
        "unattributed": {"tokens": 3_000_000, "cost": 10.0, "messages": 900, "entries": 1_800},
        "attributed_project_count": 1,
        "names_included": True,
    },
}

ACTIVE_TIME_PAYLOAD = {
    "period": "today",
    "range": {"from": "2026-09-14", "to": "2026-09-20", "days": 7, "recognized": True},
    "active_ms": 3_600_000,
    "active_ms_sum": 5_400_000,
    "comparison": {
        "active_ms_prev": 3_400_000,
        "active_ms_sum_prev": 5_000_000,
        "active_ms_pct": 5.9,
        "active_ms_sum_pct": 8.0,
    },
    # Real payloads key by_tool by the tool id (sessions.py:6544); "kimi" is
    # deliberately absent so the join-miss rule is exercised on the podium.
    "by_tool": {
        "codex": {"tool_label": "Codex", "session_count": 42, "active_ms": 2_400_000,
                  "active_ms_sum": 3_000_000},
        "claude": {"tool_label": "Claude", "session_count": 17, "active_ms": 1_200_000,
                   "active_ms_sum": 2_400_000},
    },
    "unavailable_tools": [],
    "active_gap_cap_ms": 300_000,
    "active_time_estimated": True,
    "active_time_method": "capped-inter-event-gap",
    "include_review_sessions": True,
    "timestamp": "2026-09-20T12:00:02",
}


def _outcome(payload):
    return FetchOutcome(value=payload, status="hit", age_seconds=0.0)


def _patch_fetchers(monkeypatch, usage=USAGE_PAYLOAD, insights=INSIGHTS_PAYLOAD,
                    active=ACTIVE_TIME_PAYLOAD):
    """Install deepcopy-returning fetchers; an Exception instance as the payload
    means the source raises (insights/active then go through the soft-fail
    path; usage through the hard-fail one)."""

    def _make(payload):
        if isinstance(payload, Exception):
            def _raise(*a, **k):
                raise payload
            return _raise
        return lambda *a, **k: _outcome(copy.deepcopy(payload))

    monkeypatch.setattr(report, "fetch_usage", _make(usage))
    monkeypatch.setattr(report, "fetch_insights", _make(insights))
    monkeypatch.setattr(report, "fetch_active_time", _make(active))


def _args(**overrides):
    base = {"period": "week", "json": False, "pretty": False, "output": None}
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def no_db(monkeypatch):
    """TOKDASH_USAGE_DB=0: ensure_usage_db_compatible no-ops and db_summary
    prints the disabled line without touching the filesystem."""
    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")


@pytest.fixture
def piped(monkeypatch):
    """Force the non-tty branch (deterministic 80-col plain bytes)."""
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)


# ---------------------------------------------------------------------------
# Human path
# ---------------------------------------------------------------------------

def test_human_path_full_render(monkeypatch, no_db, piped, capsys):
    _patch_fetchers(monkeypatch)
    assert report.run_report(_args()) == 0
    out = capsys.readouterr().out

    assert out.startswith("Tokdash report · week · 2026-09-14 → 2026-09-20 · 7 days · UTC")
    # hero: values + comparison deltas in PERCENTAGE POINTS, never re-scaled.
    assert "Tokens 12.4M  ↑ 8.2%" in out
    assert "Cost   $142.31  ↓ 3.1%" in out
    assert "Msgs   4,820  ↑ 0.0%" in out
    # clock union vs summed agent time: both figures, both labels.
    assert "Agent  1h 00m clock · 1h 30m sum  ↑ 8.0%" in out
    assert "Streak 6 days · longest 12 · active 6 of 7 · busiest 2026-09-18 (7.5M)" in out
    assert "Daily  " in out and "2026-09-14 → 2026-09-20" in out
    assert "When   peak hour 14:00 · night share 12.0%" in out
    assert "busiest weekday Wed (" in out
    # podium
    assert "Top agent    Codex · 8.0M · $90.00 · 42 sessions · 50m" in out
    assert "Top model    gpt-5-codex · 8.0M · $90.00" in out
    assert "Top project  tokdash · 9.0M · $100.50" in out
    # agents table: the re-goldened header (22-char name column, numeric
    # columns right-aligned to fixed widths)
    assert "       Agent                   Sessions   Tokens      Cost Active" in out
    assert "Sessions" in out and "42" in out
    # Daily section: bold header, faint rule, one row per calendar day
    lines = out.splitlines()
    idx = lines.index("Daily")  # the hero spark line is "Daily  …", never equal
    assert lines[idx + 1] == "─" * 80
    assert lines[idx + 2].startswith("09-14  █")
    assert lines[idx + 2].rstrip().endswith("1.5M $12.00")
    assert lines.count("─" * 80) == 3  # Daily + agents-table + after-Note rules
    assert "─" * 80 + "\n       Agent" in out  # rule sits before the table header
    # null-vs-zero law in row context
    assert "—" in out
    # note + db footer
    assert "Note   Headline totals are the usage scan; facet/agent figures are the analytics" in out
    assert "usage db disabled" in out
    assert "\x1b" not in out  # piped bytes are plain


def test_agents_table_join_and_filter_rules(monkeypatch, no_db, piped, capsys):
    _patch_fetchers(monkeypatch)
    report.run_report(_args())
    out = capsys.readouterr().out
    agents = out.split("Cost Active")[1]  # header tail; rows follow
    # codex and claude joined onto active time stay; claude's unpriced cost is
    # an em dash, not $0.00 (fmt_cost_row; pricing.py:320 returns 0.0 unknown).
    assert "Codex" in agents and "42" in agents and "$90.00" in agents and "50m" in agents
    assert "Claude Code" in agents and "17" in agents and "40m" in agents
    assert "$0.00" not in agents
    # kimi has tokens but no session row: filtered while active time answered.
    assert "Kimi" not in agents  # normalized away with the rest


def test_missing_active_join_podium_em_dash_and_table_keeps_rows(monkeypatch, no_db, piped, capsys):
    """active_time soft-fails → podium join miss renders —, and the agents table
    stands on its token columns (web rule: no session filter when missing)."""
    _patch_fetchers(monkeypatch, active=RuntimeError("scanner down"))
    assert report.run_report(_args()) == 0
    out = capsys.readouterr().out
    assert "Top agent    Codex · 8.0M · $90.00 · — sessions · —" in out
    assert "[warn] active time scan failed: scanner down" in out
    agents = out.split("Cost Active")[1]  # header tail; rows follow
    # With active time absent, even the session-less tool keeps its row.
    assert "Kimi" in agents and "—" in agents


def test_top10_truncation(monkeypatch, no_db, piped, capsys):
    many = copy.deepcopy(INSIGHTS_PAYLOAD)
    many["tools"]["ranked"] = [
        {"tool": f"tool{i:02d}", "tokens": 1_000_000 * (20 - i), "cost": 5.0 * (20 - i),
         "messages": 10, "entries": 20}
        for i in range(15)
    ]
    many_at = copy.deepcopy(ACTIVE_TIME_PAYLOAD)
    many_at["by_tool"] = {
        f"tool{i:02d}": {"tool_label": f"T{i}", "session_count": i + 1,
                         "active_ms": 1000, "active_ms_sum": 2000}
        for i in range(15)
    }
    _patch_fetchers(monkeypatch, insights=many, active=many_at)
    report.run_report(_args())
    out = capsys.readouterr().out
    agents = out.split("Cost Active")[1]  # header tail; rows follow
    assert "Tool09" in agents   # 10th row is in… (normalized)
    assert "Tool10" not in agents  # …the 11th is not


def test_projects_unavailable_reason_warn(monkeypatch, no_db, piped, capsys):
    no_projects = copy.deepcopy(INSIGHTS_PAYLOAD)
    no_projects["projects"] = {
        "projects": [], "unattributed": {"tokens": 0, "cost": 0.0, "messages": 0, "entries": 0},
        "attributed_project_count": 0, "names_included": True,
        "unavailable_reason": "persistent usage database is disabled",
    }
    _patch_fetchers(monkeypatch, insights=no_projects)
    assert report.run_report(_args()) == 0
    out = capsys.readouterr().out
    assert "projects: persistent usage database is disabled" in out
    assert "Top project" not in out


def test_source_errors_footer_strings_and_dicts(monkeypatch, no_db, piped, capsys):
    noisy = copy.deepcopy(USAGE_PAYLOAD)
    noisy["source_errors"] = ["gemini-cli", {"source": "grok"}, {"other": "no source"}]
    _patch_fetchers(monkeypatch, usage=noisy)
    report.run_report(_args())
    out = capsys.readouterr().out
    assert "[warn] 2 source(s) failed this window: gemini-cli, grok" in out


def test_db_footer_both_modes(monkeypatch, piped, capsys):
    _patch_fetchers(monkeypatch)
    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")
    report.run_report(_args())
    assert "usage db disabled (TOKDASH_USAGE_DB=0) — live parsing" in capsys.readouterr().out

    monkeypatch.delenv("TOKDASH_USAGE_DB")
    monkeypatch.setattr(report, "db_summary", lambda: "/home/me/.tokdash/usage.sqlite3 · 9 rows")
    report.run_report(_args())
    assert "db     /home/me/.tokdash/usage.sqlite3 · 9 rows" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Soft/hard failure policy
# ---------------------------------------------------------------------------

def test_insights_soft_fail_dashes_warn_exit_zero(monkeypatch, no_db, piped, capsys):
    _patch_fetchers(monkeypatch, insights=RuntimeError("disk on fire"))
    assert report.run_report(_args()) == 0  # dashes + warnings, not a dead report
    out = capsys.readouterr().out
    assert "[warn] analytics scan failed: disk on fire" in out
    assert "Streak — days · longest — · active — of — · busiest — (—)" in out
    assert "Daily  —  — → —" in out
    assert "peak hour — · night share n/a" in out
    assert "Top agent    —" in out
    assert "insights" not in out  # the payload simply is not there


def test_usage_hard_fails(monkeypatch, no_db):
    _patch_fetchers(monkeypatch, usage=RuntimeError("boom"))
    with pytest.raises(SystemExit) as exc:
        report.run_report(_args())
    assert exc.value.code == "boom"

    from tokdash.api import CacheBackpressureError

    _patch_fetchers(monkeypatch, usage=CacheBackpressureError("busy"))
    with pytest.raises(SystemExit) as exc:
        report.run_report(_args())
    assert exc.value.code == "Tokdash is busy computing — try again shortly"


def test_schema_too_new_is_terminal_everywhere(monkeypatch, no_db):
    err = UsageDatabaseSchemaTooNewError(path="/tmp/usage.sqlite3", found=99, supported=9)
    _patch_fetchers(monkeypatch, insights=err)
    with pytest.raises(SystemExit) as exc:
        report.run_report(_args())
    assert "run 'tokdash update'" in str(exc.value.code)

    monkeypatch.setattr(report, "ensure_usage_db_compatible", lambda: (_ for _ in ()).throw(err))
    with pytest.raises(SystemExit) as exc:
        report.run_report(_args())
    assert "run 'tokdash update'" in str(exc.value.code)


def test_unknown_period_exits_one_from_run_report(monkeypatch, no_db):
    # run_report's own ValueError mapping (the parser guard normally fires first).
    with pytest.raises(SystemExit) as exc:
        report.run_report(_args(period="banana"))
    assert "Unknown period" in str(exc.value.code)


# ---------------------------------------------------------------------------
# --json / --output / pipe contract
# ---------------------------------------------------------------------------

def test_json_contract_via_cli(monkeypatch, no_db, piped, capsys):
    _patch_fetchers(monkeypatch)
    assert cli(["report", "--period", "week", "--json", "--pretty"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"generated_at", "window", "usage", "insights", "active_time"}
    assert set(payload["window"]) == {
        "period", "mode", "from", "to", "days", "recognized", "timezone"
    }
    assert payload["window"]["mode"] == "calendar"  # week → _report_windows
    assert payload["window"]["period"] == "week"
    assert payload["window"]["timezone"] == "UTC"
    assert payload["usage"]["total_tokens"] == 12_400_000
    assert payload["insights"]["timezone"] == "UTC"
    assert payload["active_time"]["active_ms_sum"] == 5_400_000


def test_json_rolling_mode(monkeypatch, no_db, piped, capsys):
    _patch_fetchers(monkeypatch)
    cli(["report", "--period", "7d", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["window"]["mode"] == "rolling"
    assert payload["generated_at"]  # iso8601 local, timespec seconds


def test_output_file_utf8_trailing_newline_no_ansi(monkeypatch, no_db, tmp_path):
    # Even with the color gate patched open, --output bytes are plain: run_report
    # picks emit_plain for files, and the file is utf-8 with a trailing newline.
    monkeypatch.setattr(report, "_color_enabled", lambda: True)
    _patch_fetchers(monkeypatch)
    out_file = tmp_path / "report.txt"
    assert report.run_report(_args(output=str(out_file))) == 0
    raw = out_file.read_bytes()
    assert b"\x1b" not in raw
    text = raw.decode("utf-8")
    assert text.endswith("\n")
    assert "Tokdash report" in text and "█" in text  # files always get blocks


def test_piped_stdout_plain_even_with_color_patched(monkeypatch, no_db, piped, capsys):
    # Double gate: run_report's own check is patched open, the engine's real
    # _color_enabled (isatty False under capture) still suppresses ANSI.
    monkeypatch.setattr(report, "_color_enabled", lambda: True)
    _patch_fetchers(monkeypatch)
    report.run_report(_args())
    assert "\x1b" not in capsys.readouterr().out


def test_width_is_80_when_piped_regardless_of_terminal_size(monkeypatch, no_db, piped):
    seen = {}
    real_build = report.build_report_segments

    def spy(usage, insights, active_time, **kwargs):
        seen.update(kwargs)
        return real_build(usage, insights, active_time, **kwargs)

    monkeypatch.setattr(report, "build_report_segments", spy)
    # report.py only reads .columns, so a stub terminal size is honest here.
    monkeypatch.setattr(shutil, "get_terminal_size",
                        lambda fallback=(80, 24): SimpleNamespace(columns=200, lines=24))
    _patch_fetchers(monkeypatch)
    report.run_report(_args())
    assert seen["width"] == 80
    assert seen["glyphs"] == "blocks"


def test_width_from_terminal_when_tty(monkeypatch, no_db):
    seen = {}
    real_build = report.build_report_segments

    def spy(usage, insights, active_time, **kwargs):
        seen.update(kwargs)
        return real_build(usage, insights, active_time, **kwargs)

    monkeypatch.setattr(report, "build_report_segments", spy)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(report, "glyph_style", lambda: "blocks")
    _patch_fetchers(monkeypatch)
    monkeypatch.setattr(shutil, "get_terminal_size",
                        lambda fallback=(80, 24): SimpleNamespace(columns=300, lines=24))
    report.run_report(_args())
    assert seen["width"] == 200  # capped
    monkeypatch.setattr(shutil, "get_terminal_size",
                        lambda fallback=(80, 24): SimpleNamespace(columns=40, lines=24))
    report.run_report(_args())
    assert seen["width"] == 60  # floored


# ---------------------------------------------------------------------------
# Parser guards (exit 2 — flat parser, period is flag-only)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["tui", "--json"],
    ["report", "--period", "banana"],
    ["tui", "--period", "banana"],
    ["report", "week"],                       # period is flag-only: db_action choices
    ["report", "--dev-fixture", "dense"],     # existing serve-only guard
])
def test_parser_guards_exit_two(monkeypatch, no_db, argv):
    with pytest.raises(SystemExit) as exc:
        cli(argv)
    assert exc.value.code == 2


def test_tui_status_parses_and_is_ignored(monkeypatch, no_db):
    """`tokdash tui status`: "status" is a legal db_action choice and must be
    silently ignored — stub the app module so this test does not need the TUI."""
    captured = {}
    stub = types.ModuleType("tokdash.tui.app")
    stub.run_tui = lambda args: captured.update(vars(args)) or 0
    monkeypatch.setitem(sys.modules, "tokdash.tui.app", stub)
    assert cli(["tui", "status"]) == 0
    assert captured["command"] == "tui"
    assert captured["db_action"] == "status"
    assert captured["period"] == "today"


# ---------------------------------------------------------------------------
# Non-mutation (compute payloads share nested rows — compute.py:989)
# ---------------------------------------------------------------------------

def test_build_and_run_never_mutate_the_payloads(monkeypatch, no_db, piped, capsys):
    usage = copy.deepcopy(USAGE_PAYLOAD)
    insights = copy.deepcopy(INSIGHTS_PAYLOAD)
    active = copy.deepcopy(ACTIVE_TIME_PAYLOAD)
    pristine = (copy.deepcopy(usage), copy.deepcopy(insights), copy.deepcopy(active))

    monkeypatch.setattr(report, "fetch_usage", lambda *a, **k: _outcome(usage))
    monkeypatch.setattr(report, "fetch_insights", lambda *a, **k: _outcome(insights))
    monkeypatch.setattr(report, "fetch_active_time", lambda *a, **k: _outcome(active))
    assert report.run_report(_args()) == 0

    window = {"period": "week", "from": "x", "to": "y", "days": 1, "timezone": None}
    report.build_report_segments(usage, insights, active, window=window)

    assert (usage, insights, active) == pristine


# ---------------------------------------------------------------------------
# Emitters on report segments
# ---------------------------------------------------------------------------

def _segments(monkeypatch):
    _patch_fetchers(monkeypatch)
    return report.build_report_segments(
        copy.deepcopy(USAGE_PAYLOAD),
        copy.deepcopy(INSIGHTS_PAYLOAD),
        copy.deepcopy(ACTIVE_TIME_PAYLOAD),
        window={"period": "week", "from": "2026-09-14", "to": "2026-09-20", "days": 7,
                "timezone": "UTC", "recognized": True},
    )


def test_emitters(monkeypatch, no_db):
    # no_db: the builder's db footer would otherwise open the REAL
    # ~/.tokdash store (and on a fresh machine create it).
    seg = _segments(monkeypatch)
    plain = report.emit_plain(seg)
    assert "\x1b" not in plain
    assert "↑ 8.2%" in plain

    markup = report.emit_markup(seg)
    assert "\x1b" not in markup
    assert "[green]↑ 8.2%[/]" in markup       # report surface: up = green
    assert "[red]↓ 3.1%[/]" in markup         # down = red

    # Hand-rolled escape: "[warn]" is data, never a markup tag.
    warn_seg = report.build_report_segments(
        copy.deepcopy(USAGE_PAYLOAD), None, None,
        window={"period": "w", "from": "x", "to": "y", "days": 1, "timezone": None,
                "warnings": ["analytics scan failed: boom"]},
    )
    warn_markup = report.emit_markup(warn_seg)
    assert "\\[warn] analytics scan failed: boom" in warn_markup

    ansi = report.emit_ansi(seg)  # non-tty under capture → engine no-ops
    assert "\x1b" not in ansi


def test_piped_bytes_are_deterministic(monkeypatch, no_db, piped, capsys):
    _patch_fetchers(monkeypatch)
    report.run_report(_args())
    first = capsys.readouterr().out
    report.run_report(_args())
    second = capsys.readouterr().out
    assert first == second
    # Width-80 canned layout: lock the line count so any accidental extra/
    # dropped line in the shared seam is caught for both surfaces.
    # 1 header + 4 hero + streak + daily + peak + 3 hour bars + weekday
    # + Daily section (header + rule + 7 rows) + 3 podium + rule + table
    # header + 2 agent rows (kimi filtered) + 2 note + rule + db.
    assert len(first.rstrip("\n").split("\n")) == 32


# ---------------------------------------------------------------------------
# Daily section (round 2): week/month only, calendar-enumerated, capped
# ---------------------------------------------------------------------------

def _week_window(**over):
    base = {"period": "week", "from": "2026-09-14", "to": "2026-09-20", "days": 7,
            "timezone": "UTC", "recognized": True, "db_line": "db off"}
    base.update(over)
    return base


def test_daily_section_gaps_render_em_dash_rows():
    """The daily facet carries ACTIVE days only; the client enumerates the
    window's calendar, so a facet gap renders an em-dash row — never a
    silently shorter section (null-vs-zero law)."""
    gapped = copy.deepcopy(INSIGHTS_PAYLOAD)
    gapped["daily"] = [d for d in gapped["daily"] if d["date"] != "2026-09-16"]
    seg = report.build_report_segments(
        copy.deepcopy(USAGE_PAYLOAD), gapped, copy.deepcopy(ACTIVE_TIME_PAYLOAD),
        window=_week_window())
    lines = report.emit_plain(seg).splitlines()
    assert lines.count("─" * 80) == 3  # Daily, agents-table, after-Note rules
    idx = lines.index("Daily")
    body = lines[idx + 2 : idx + 9]
    assert len(body) == 7  # every calendar day of the window, gap included
    assert body[0].startswith("09-14  █")
    gap = body[2]
    assert gap.startswith("09-16") and "█" not in gap and "—" in gap
    assert gap.rstrip().endswith("— —")  # tokens AND cost dashed
    assert body[6].startswith("09-20")


def test_daily_section_year_window_omitted():
    """year (and any >31-day) window: no Daily section — the year's calendar
    lives on the Overview tab, not in a 365-row dump."""
    out = report.emit_plain(report.build_report_segments(
        copy.deepcopy(USAGE_PAYLOAD), copy.deepcopy(INSIGHTS_PAYLOAD),
        copy.deepcopy(ACTIVE_TIME_PAYLOAD),
        window=_week_window(period="year", days=365, **{"from": "2026-01-01",
                                                        "to": "2026-12-31"})))
    assert "Daily\n" not in out  # section header; hero spark line is "Daily  …"
    assert out.count("─" * 80) == 2  # only the unconditional table/Note rules


def test_daily_section_no_insights_no_crash_and_no_section():
    # insights=None (soft-failed fetch): the section is simply absent and the
    # report still builds — dashes and warn lines, never an exception.
    out = report.emit_plain(report.build_report_segments(
        copy.deepcopy(USAGE_PAYLOAD), None, copy.deepcopy(ACTIVE_TIME_PAYLOAD),
        window=_week_window()))
    assert "Daily\n" not in out
    assert "─" * 80 in out  # the unconditional table/Note rules still render


def test_daily_section_empty_facet_omits_section():
    empty = copy.deepcopy(INSIGHTS_PAYLOAD)
    empty["daily"] = []
    out = report.emit_plain(report.build_report_segments(
        copy.deepcopy(USAGE_PAYLOAD), empty, copy.deepcopy(ACTIVE_TIME_PAYLOAD),
        window=_week_window()))
    assert "Daily\n" not in out  # facet entirely empty → no section at all


def test_daily_section_ascii_rules():
    out = report.emit_plain(report.build_report_segments(
        copy.deepcopy(USAGE_PAYLOAD), copy.deepcopy(INSIGHTS_PAYLOAD),
        copy.deepcopy(ACTIVE_TIME_PAYLOAD),
        window=_week_window(), glyphs="ascii"))
    assert "-" * 80 in out and "─" not in out and "█" not in out
    lines = out.splitlines()
    assert lines[lines.index("Daily") + 1] == "-" * 80
    assert any(re.match(r"^\d\d-\d\d  #+ ", l) for l in lines)  # ascii bars


def test_daily_section_rows_capped_at_31():
    """A malformed window whose from/to span more days than "days" claims
    still renders at most 31 rows (the cap, not the claim, is the bound)."""
    seg = report.build_report_segments(
        copy.deepcopy(USAGE_PAYLOAD), copy.deepcopy(INSIGHTS_PAYLOAD),
        copy.deepcopy(ACTIVE_TIME_PAYLOAD),
        window=_week_window(days=31, **{"from": "2026-01-01", "to": "2026-02-14"}))
    rows = [l for l in report.emit_plain(seg).splitlines()
            if re.match(r"^\d\d-\d\d  ", l)]
    assert len(rows) == 31
    assert rows[0].startswith("01-01") and rows[-1].startswith("01-31")


def test_agents_table_numeric_columns_right_aligned():
    # Numbers line up on fixed right edges: Sessions(10)/Tokens(9)/Cost(10),
    # names padded to the 22-char cap so a long label cannot shift them.
    many = copy.deepcopy(INSIGHTS_PAYLOAD)
    many["tools"]["ranked"] = [
        {"tool": "a-very-long-agent-name-over-22", "tokens": 9_000_000,
         "cost": 90.0, "messages": 10, "entries": 20},
        {"tool": "codex", "tokens": 8_000_000, "cost": 90.0, "messages": 10,
         "entries": 20},
    ]
    many_at = copy.deepcopy(ACTIVE_TIME_PAYLOAD)
    many_at["by_tool"]["a-very-long-agent-name-over-22"] = {
        "tool_label": "X", "session_count": 7, "active_ms": 1000, "active_ms_sum": 2000}
    out = report.emit_plain(report.build_report_segments(
        copy.deepcopy(USAGE_PAYLOAD), many, many_at, window=_week_window()))
    rows = [l for l in out.splitlines()
            if re.match(r"^       \S", l) and "█" not in l and "·" not in l
            and "Sessions" not in l and "weekday" not in l
            and not l.startswith("       scan")]
    assert len(rows) == 2
    long_row, short_row = rows
    # Columns: 7-space indent + 22-char name field (3..28), Sessions
    # rjust(10) = cols 29..38, Tokens rjust(9) = 39..47, Cost rjust(10) =
    # 48..57 — exact slices prove every row shares the same right edges.
    assert long_row.startswith("       A Very Long Agent Nam…")  # normalized + 22-char cap
    assert long_row[29:39] == "         7" and short_row[29:39] == "        42"
    assert long_row[39:48] == "     9.0M" and short_row[39:48] == "     8.0M"
    assert long_row[48:58] == short_row[48:58] == "    $90.00"


def test_agents_table_keeps_measured_zero_seconds():
    # The report side of the round-3 split: the null-vs-zero law STAYS here
    # — a measured 0 of agent time renders "0s", never a dash. (The Overview
    # tools table dashes its 0 per the user's explicit ask, locked by
    # tests/test_tui_app.py::test_tools_table_time_zero_and_missing_are_dashes.
    # The two surfaces diverge DELIBERATELY — do not "unify" them.)
    many = copy.deepcopy(INSIGHTS_PAYLOAD)
    many["tools"]["ranked"] = [
        {"tool": "codex", "tokens": 8_000_000, "cost": 90.0, "messages": 10,
         "entries": 20},
    ]
    at = copy.deepcopy(ACTIVE_TIME_PAYLOAD)
    at["by_tool"]["codex"] = {"tool_label": "Codex", "session_count": 1,
                              "active_ms": 0, "active_ms_sum": 0}
    out = report.emit_plain(report.build_report_segments(
        copy.deepcopy(USAGE_PAYLOAD), many, at, window=_week_window()))
    rows = [l for l in out.splitlines() if l.startswith("       Codex")]
    assert len(rows) == 1
    assert rows[0].rstrip().split()[-1] == "0s"
    # and the podium line above the table says the same 0s, not a dash
    assert "sessions · 0s" in out
