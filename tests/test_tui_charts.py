"""Tests for tokdash/tui/charts.py — the pure period-visual renderers.

Pure-python (no textual, no rich, no DB, no sockets). Calendar-locked cases
are deliberate: 2026-01-01 is a Thursday (so the 2026 heatmap column 0 is
Monday 2025-12-29) and 2026-09-01 is a Tuesday (so the month grid's first
row carries one blank pad cell) — a grid that naively starts the window at
its first day or at Jan 1 fails these, which is the point.
"""

from __future__ import annotations

import copy
import datetime as dt

from tokdash.tui.charts import (
    day_bars,
    heatmap_origin,
    month_grid,
    quota_table,
    tool_model_detail,
    year_heatmap,
)
from tokdash.tui.formatting import EM_DASH, Run, heat_glyph, tool_label


def text(line: list[Run]) -> str:
    return "".join(run.text for run in line)


# ---------------------------------------------------------------------------
# heat_glyph (lives in formatting.py; covered here per the round-2 split —
# test_tui_formatting.py is round-1-frozen and must not change)
# ---------------------------------------------------------------------------

class TestHeatGlyph:
    def test_blocks_ramp(self):
        assert [heat_glyph(level) for level in range(5)] == [" ", "▂", "▃", "▅", "▇"]

    def test_ascii_ramp(self):
        assert [heat_glyph(level, glyphs="ascii") for level in range(5)] == [
            " ", ":", "-", "=", "#",
        ]

    def test_clamp(self):
        assert heat_glyph(-3) == " "
        assert heat_glyph(None) == " "
        assert heat_glyph(9) == "▇"
        assert heat_glyph(9, glyphs="ascii") == "#"


# ---------------------------------------------------------------------------
# tool_label (lives in formatting.py; covered here — test_tui_formatting.py is
# round-1-frozen). The web's formatToolName, ported: map hit else title-case.
# ---------------------------------------------------------------------------

class TestToolLabel:
    def test_map_hit(self):
        assert tool_label("claude", {"claude": "Claude Code"}) == "Claude Code"

    def test_fallback_title_case(self):
        # Map miss → the JS fallback: separators to spaces, word-initial caps.
        assert tool_label("gemini_cli") == "Gemini Cli"
        assert tool_label("antigravity-cli") == "Antigravity Cli"
        assert tool_label("codex") == "Codex"

    def test_fallback_preserves_inner_case(self):
        # The JS capitalizes only each word's FIRST letter — it never forces
        # the rest lower ("myTool" keeps its internal capitals).
        assert tool_label("myTool") == "MyTool"

    def test_empty_is_unknown(self):
        assert tool_label("") == "Unknown"
        assert tool_label(None) == "Unknown"
        assert tool_label("__") == "Unknown"

    def test_map_beats_fallback(self):
        assert tool_label("omp", {"omp": "omp"}) == "omp"  # map can force lowercase


# ---------------------------------------------------------------------------
# (round 3) active_time_bars + the day-view bar section were DELETED: per-tool
# time is now the Overview tools table's Time column, not a top chart.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# day_bars (week view) — the F1 bug class lives here
# ---------------------------------------------------------------------------

def _contrib(date_str, tokens, intensity=None):
    row = {"date": date_str, "totals": {"tokens": tokens, "cost": 1.0}}
    if intensity is not None:
        row["intensity"] = intensity
    return row


class TestDayBars:
    # Mon 14 .. Sun 20 September 2026, with holes on the 16th and 19th.
    CONTRIBS = [
        _contrib("2026-09-14", 12_400),
        _contrib("2026-09-15", 6_200),
        _contrib("2026-09-17", 500),
        _contrib("2026-09-18", 0),      # measured zero, NOT a dash
        _contrib("2026-09-20", 30_000),
    ]

    def test_enumerates_every_calendar_day(self):
        lines = day_bars(self.CONTRIBS, "2026-09-14", "2026-09-20", width=10)
        assert len(lines) == 7  # span, not the 5 active days

    def test_gap_days_render_em_dash(self):
        lines = day_bars(self.CONTRIBS, "2026-09-14", "2026-09-20", width=10)
        gaps = [text(line) for line in lines if EM_DASH in text(line)]
        assert [g for g in gaps if "Wed 16" in g]
        assert [g for g in gaps if "Sat 19" in g]
        assert len(gaps) == 2

    def test_nested_totals_tokens_is_the_value_source(self):
        # A renderer reading row["tokens"] instead of row["totals"]["tokens"]
        # renders a dash here — this is the round-1 F1 bug class.
        lines = day_bars(self.CONTRIBS, "2026-09-14", "2026-09-20", width=10)
        assert "12.4K" in text(lines[0])

    def test_weekday_and_day_label(self):
        lines = day_bars(self.CONTRIBS, "2026-09-14", "2026-09-20", width=10)
        assert text(lines[0]).startswith("Mon 14")
        assert text(lines[1]).startswith("Tue 15")

    def test_measured_zero_prints_zero(self):
        lines = day_bars(self.CONTRIBS, "2026-09-14", "2026-09-20", width=10)
        thu = text(lines[4])
        assert "0" in thu and EM_DASH not in thu

    def test_bar_scales_to_window_max(self):
        lines = day_bars(self.CONTRIBS, "2026-09-14", "2026-09-20", width=10)
        assert "█" * 10 in text(lines[6])                       # 30K is the max
        assert "█" * round(10 * 12_400 / 30_000) in text(lines[0])


# ---------------------------------------------------------------------------
# month_grid (month view)
# ---------------------------------------------------------------------------

class TestMonthGrid:
    CONTRIBS = [
        _contrib("2026-09-01", 10_000, intensity=2),
        _contrib("2026-09-15", 50_000, intensity=4),
        _contrib("2026-09-30", 9_000, intensity=1),
    ]

    def lines(self):
        return month_grid(self.CONTRIBS, "2026-09-01", "2026-09-30")

    def test_header_row_then_one_row_per_calendar_week(self):
        # Header, then Sep 2026 spans Mon Aug 31 .. Sun Oct 4 → 5 week rows.
        lines = self.lines()
        assert len(lines) == 6
        # Weekdays run HORIZONTALLY (user-reported bug: rows used to carry
        # Mon/Tue/… gutter labels — calendar grids put the weekday header on
        # top, one week per line).
        assert text(lines[0]) == "Mon Tue Wed Thu Fri Sat Sun "

    def test_first_row_offset_sep_1_is_tuesday(self):
        cells = text(self.lines()[1])   # week of Aug 31
        assert cells[0:4] == "    "        # Mon Aug 31: outside the window → blank
        assert cells[4:8] == " 1▃ "        # Tue Sep 1, intensity 2 → ramp[2]
        assert cells[8:12] == " 2  "       # in range, inactive → number, blank heat

    def test_last_row_trailing_pad(self):
        # Rows are Aug31, Sep7, Sep14, Sep21, Sep28. The Sep28 row: Mon 28,
        # Tue 29, Wed 30 (intensity 1 → ▂), then the Oct pads render blank.
        cells = text(self.lines()[5])
        assert cells[0:8] == "28  29  "  # 4-char cells: "dd" + heat + space
        assert cells[8:12] == "30▂ "
        assert cells[12:20] == "        "  # Thu Oct 1, Fri Oct 2: outside → blank

    def test_intensity_wins_over_tokens(self):
        # intensity 4 with SMALL tokens must still render the top glyph —
        # the level is read, never recomputed.
        contribs = [
            _contrib("2026-09-02", 100, intensity=4),
            _contrib("2026-09-03", 10_000, intensity=1),
        ]
        lines = month_grid(contribs, "2026-09-01", "2026-09-30")
        cells = text(lines[1])  # week of Aug 31
        assert " 2▇ " in cells
        assert " 3▂ " in cells

    def test_fallback_rank_only_without_intensity(self):
        contribs = [
            _contrib("2026-09-02", 100),        # no intensity → ranked low
            _contrib("2026-09-03", 10_000),     # no intensity → ranked high
            _contrib("2026-09-04", 999_999, intensity=0),  # explicit 0 is kept
        ]
        lines = month_grid(contribs, "2026-09-01", "2026-09-30")
        cells = text(lines[1])
        assert " 2▃ " in cells   # 2 active values: bisect rank 0.5 → level 2
        assert " 3▇ " in cells   # rank 1.0 → level 4
        assert " 4  " in cells   # intensity 0 → blank heat despite huge tokens

    def test_does_not_mutate_payload(self):
        contribs = copy.deepcopy(self.CONTRIBS)
        month_grid(contribs, "2026-09-01", "2026-09-30")
        assert contribs == copy.deepcopy(self.CONTRIBS)


# ---------------------------------------------------------------------------
# year_heatmap (year view)
# ---------------------------------------------------------------------------

class TestYearHeatmap:
    def test_column_zero_is_the_monday_on_or_before_jan1(self):
        # Jan 1 2026 is a Thursday → column 0 starts Mon 2025-12-29.
        assert heatmap_origin(2026) == dt.date(2025, 12, 29)
        # A Monday-start year gets no pad column at all.
        assert heatmap_origin(2024) == dt.date(2024, 1, 1)

    def test_grid_geometry_53_columns(self):
        contribs = [_contrib("2026-01-05", 100, intensity=1)]
        seg = year_heatmap(contribs, 2026, today="2026-09-21")
        assert len(seg) == 8  # header + 7 weekday rows
        assert len(text(seg[1])[4:]) == 53 * 2  # 2 chars per column

    def test_month_header_labels(self):
        seg = year_heatmap([], 2026, today="2026-09-21")
        header = text(seg[0])
        assert header.startswith("    Jan")  # gutter, then col 0
        # Feb 1 2026 is a Sunday — the first column whose FIRST in-year day
        # is February is col 5 (Feb 2..8), label at char 10 of the header.
        assert header[4 + 10:4 + 13] == "Feb"
        assert "Dec" in header

    def test_intensity_read_never_recomputed(self):
        contribs = [_contrib("2026-02-01", 9_999_999, intensity=1)]
        seg = year_heatmap(contribs, 2026, today="2026-12-31")
        glyph = self.cell(seg, dt.date(2026, 2, 1))
        assert glyph == "▂ "  # level 1, NOT the top glyph the tokens would rank

    def test_intensity_clamped(self):
        contribs = [
            _contrib("2026-03-10", 1, intensity=7),   # clamps to 4
            _contrib("2026-03-11", 1, intensity=-2),  # clamps to 0 (blank)
        ]
        seg = year_heatmap(contribs, 2026, today="2026-12-31")
        assert self.cell(seg, dt.date(2026, 3, 10)) == "▇ "
        assert self.cell(seg, dt.date(2026, 3, 11)) == "  "

    def test_future_days_blank(self):
        contribs = [
            _contrib("2026-06-01", 5_000, intensity=3),
            _contrib("2026-03-01", 5_000, intensity=3),
        ]
        seg = year_heatmap(contribs, 2026, today="2026-03-31")
        assert self.cell(seg, dt.date(2026, 3, 1)) == "▅ "
        assert self.cell(seg, dt.date(2026, 6, 1)) == "  "  # future → blank

    def test_off_year_padding_blank(self):
        contribs = [_contrib("2025-12-30", 5_000, intensity=3)]
        seg = year_heatmap(contribs, 2026, today="2026-12-31")
        assert self.cell(seg, dt.date(2025, 12, 30)) == "  "

    def test_empty_year_renders_skeleton_plus_note(self):
        seg = year_heatmap([], 2026, today="2026-09-21")
        assert len(seg) == 9
        assert text(seg[8]) == "no activity 2026"

    @staticmethod
    def cell(seg, day: dt.date) -> str:
        """Heat cell text for a date: 2 chars per column, 7 rows, gutter 4."""
        col = (day - heatmap_origin(day.year)).days // 7
        return text(seg[1 + day.weekday()])[4 + col * 2: 4 + col * 2 + 2]


# ---------------------------------------------------------------------------
# tool_model_detail
# ---------------------------------------------------------------------------

def _usage_payload():
    return {
        "by_tool": {
            "codex": {"tokens": 8_000_000, "cost": 90.0, "cache_hit_rate": 0.66,
                      "tokens_in": 6_000_000, "tokens_cache": 4_000_000},
            "claude": {"tokens": 4_400_000, "cost": 52.31, "cache_hit_rate": 0.78,
                       "tokens_in": 3_000_000, "tokens_cache": 2_000_000},
            "openclaw": {"tokens": 1_000_000, "cost": 3.0, "cache_hit_rate": None,
                         "tokens_in": 900_000, "tokens_cache": 50_000},
        },
        "apps": {
            "codex": {"tokens": 8_000_000,
                      "tokens_in": 6_000_000, "tokens_out": 2_000_000,
                      "tokens_cache": 4_000_000, "messages": 3_000,
                      "models": [
                # payload order is token-ranked; cost order is the reverse —
                # a renderer sorting in place is caught by the deep compare,
                # one sorting wrongly by the row order below.
                {"name": "gpt-5", "tokens": 5_000_000, "cost": 10.0,
                 "tokens_in": 3_000_000, "tokens_out": 2_000_000,
                 "tokens_cache": 1_500_000, "messages": 1_200,
                 "cache_hit_rate": 0.5},
                {"name": "gpt-5-codex", "tokens": 3_000_000, "cost": 80.0,
                 "tokens_in": 3_000_000, "tokens_out": 0,
                 "tokens_cache": 2_500_000, "messages": 1_800,
                 "cache_hit_rate": 0.7},
            ]},
            "claude": {"tokens": 4_400_000,
                       "tokens_in": 3_000_000, "tokens_out": 1_400_000,
                       "tokens_cache": 2_000_000, "messages": 1_820,
                       "models": [
                {"name": f"m{i}", "tokens": i * 1_000, "cost": float(i),
                 "tokens_in": i * 700, "tokens_out": i * 300,
                 "tokens_cache": i * 500, "messages": i * 10,
                 "cache_hit_rate": None}
                for i in range(1, 6)  # five models — full list by default (round 3)
            ]},
        },
        # openclaw is NOT in apps (compute_usage strips it) — these are the
        # top-level rows the openclaw section must synthesize from.
        "openclaw_models": [
            {"name": "ocl-a", "tokens": 400, "cost": 1.0},
            {"name": "ocl-b", "tokens": 600, "cost": 2.0},
        ],
    }


# Active payload by_tool (the Time join input): one exact key, one
# CASE-variant key ("Claude" — the fold is case-only, so it catches a
# capitalized tool id but never bridges the "claude" → "Claude Code" label
# gap; a display-label key would stay a miss and dash, same as the tools
# table join), and NO openclaw row (its Time must dash). 3_000_000 ms = "50m".
def _active_by_tool():
    return {
        "codex": {"active_ms_sum": 3_000_000},
        "Claude": {"active_ms_sum": 2_400_000},  # folded key on purpose
    }


# the map a real caller passes (app uses sessions.TOOL_LABELS); charts must
# not import the backend, so the test hands the map in as data.
_LABELS = {"codex": "Codex", "claude": "Claude Code", "openclaw": "OpenClaw"}


class TestToolModelDetail:
    def test_section_order_tokens_desc_name_asc(self):
        rows = tool_model_detail(_usage_payload(), labels=_LABELS)
        sections = [r for r in rows if r[0] == "tool"]
        assert [r[1] for r in sections] == ["Codex", "Claude Code", "OpenClaw"]
        # The KIND marker, not the indent, is the contract: every indented
        # row is a "model" row.
        assert all(r[0] == "model" for r in rows if r[1].startswith("  "))

    def test_row_shape_kind_prefixed(self):
        rows = tool_model_detail(_usage_payload(), labels=_LABELS)
        # (kind, name, in, out, cache, total, hit, cost, msgs, time)
        assert all(len(r) == 10 for r in rows)
        assert {r[0] for r in rows} == {"tool", "model"}

    def test_section_cells(self):
        # Round 4: the tools table's full field set on section rows plus the
        # active-time join — apps first, by_tool fallback (mutation target:
        # drop any join source and this tuple stops matching).
        rows = tool_model_detail(_usage_payload(), labels=_LABELS,
                                 active=_active_by_tool())
        assert rows[0] == ("tool", "Codex", "6.0M", "2.0M", "4.0M", "8.0M",
                           "66.0%", "$90.00", "3,000", "50m")

    def test_section_time_join_folds_label_keys(self):
        # Fixture-style payloads key active time by a CASED variant ("Claude")
        # while the section key is "claude" — the exact-then-folded join
        # finds it. openclaw has no active row at all → dash, never a 0.
        rows = tool_model_detail(_usage_payload(), labels=_LABELS,
                                 active=_active_by_tool())
        claude = next(r for r in rows if r[1] == "Claude Code")
        assert claude[9] == "40m"
        ocl = next(r for r in rows if r[1] == "OpenClaw")
        assert ocl[9] == EM_DASH

    def test_section_cells_without_active(self):
        # Progressive paint before the active payload lands: every Time
        # dashes (the app repaints the table once the join can succeed).
        rows = tool_model_detail(_usage_payload(), labels=_LABELS)
        assert [r[9] for r in rows if r[0] == "tool"] == [EM_DASH] * 3

    def test_apps_miss_falls_back_to_by_tool(self):
        # openclaw is absent from apps: Input/Cache come from the by_tool
        # fallback, Output/Msgs have no fallback and dash.
        rows = tool_model_detail(_usage_payload(), labels=_LABELS)
        ocl = next(r for r in rows if r[1] == "OpenClaw")
        assert ocl[2] == "900.0K" and ocl[4] == "50.0K"
        assert ocl[3] == EM_DASH and ocl[8] == EM_DASH

    def test_labels_fallback_when_map_misses(self):
        # No map at all (labels=None): the web's title-case fallback, so an
        # unmapped id is never shown raw. "codex" → "Codex".
        rows = tool_model_detail(_usage_payload())
        assert rows[0][1] == "Codex"

    def test_model_rows_carry_full_fields_and_dashed_time(self):
        # Round 4: model rows read their OWN fields (compute.py ships
        # in/out/cache/msgs per model) — and Time is ALWAYS EM_DASH: no
        # per-model active time exists anywhere in the payloads (per-tool
        # only). The active payload being present must not leak a time onto
        # a model row.
        rows = tool_model_detail(_usage_payload(), labels=_LABELS,
                                 active=_active_by_tool())
        gpt = next(r for r in rows if r[1] == "  gpt-5")
        assert gpt == ("model", "  gpt-5", "3.0M", "2.0M", "1.5M", "5.0M",
                       "50.0%", "$10.00", "1,200", EM_DASH)

    def test_models_cost_desc_on_a_copy(self):
        rows = tool_model_detail(_usage_payload(), labels=_LABELS)
        codex_models = [r[1] for r in rows[1:3]]
        assert codex_models == ["  gpt-5-codex", "  gpt-5"]  # 80.0 before 10.0

    def test_default_lists_every_model(self):
        # Round 3: no collapse. Claude's five models ALL appear; no "+k more".
        rows = tool_model_detail(_usage_payload(), labels=_LABELS)
        claude_rows = [r[1] for r in rows
                       if r[0] == "model" and r[1].startswith("  m")]
        assert claude_rows == ["  m5", "  m4", "  m3", "  m2", "  m1"]  # cost desc
        assert not [r for r in rows if "...+" in r[1]]

    def test_explicit_top_n_still_caps_with_overflow(self):
        rows = tool_model_detail(_usage_payload(), top_n=3)
        claude_rows = [r[1] for r in rows
                       if r[1].startswith("  m") or "...+" in r[1]]
        assert claude_rows == ["  m5", "  m4", "  m3", "  ...+2 more"]
        # The overflow row is KIND-tagged so the caller never styles it as
        # data, and its numeric cells are empty — not zeros, not dashes.
        assert [r[0] for r in rows if "...+" in r[1]] == ["more"]
        more = next(r for r in rows if "...+" in r[1])
        assert more[2:] == ("",) * 8

    def test_top_n_override(self):
        rows = tool_model_detail(_usage_payload(), top_n=1)
        assert [r[1] for r in rows if r[1].startswith("  m")] == ["  m5"]

    def test_openclaw_synthesized_section(self):
        rows = tool_model_detail(_usage_payload(), labels=_LABELS)
        ocl = next(r for r in rows if r[0] == "tool" and r[1] == "OpenClaw")
        assert ocl[5] == "1.0M"
        assert ocl[6] == "n/a"  # hit None → n/a
        assert ocl[7] == "$3.00"
        ocl_models = [r for r in rows if r[1] in ("  ocl-a", "  ocl-b")]
        assert [r[1] for r in ocl_models] == ["  ocl-b", "  ocl-a"]  # cost desc
        assert ocl_models[0][6] == "n/a"  # openclaw model rows carry no hit
        assert ocl_models[0][2] == EM_DASH  # ...and no token-breakdown fields

    def test_unpriced_model_cost_is_em_dash(self):
        payload = _usage_payload()
        payload["apps"]["codex"]["models"].append(
            {"name": "freebie", "tokens": 10, "cost": 0.0, "cache_hit_rate": None}
        )
        rows = tool_model_detail(payload, top_n=3)
        row = next(r for r in rows if r[1] == "  freebie")
        assert row[7] == EM_DASH  # unpriced, not free

    def test_never_mutates_payload(self):
        payload = _usage_payload()
        before = copy.deepcopy(payload)
        tool_model_detail(payload, labels=_LABELS)
        tool_model_detail(payload, top_n=1)
        tool_model_detail(payload, labels=_LABELS, active=_active_by_tool())
        assert payload == before


# ---------------------------------------------------------------------------
# quota_table
# ---------------------------------------------------------------------------

NOW = 1_760_000_000  # fixed epoch for the 24h trend window
TS_RESET = NOW + 7_200


def _quota_state(**over):
    # Real-world-ish shapes: normalized-key buckets (codex "5h", claude
    # "session"/"weekly_all"), api sources, codex reset credits.
    state = {
        "enabled": True,
        "consent": {"codex_api": True},
        "poll": {"interval_minutes": 30, "last_run": None},
        "providers": {
            "codex": {"status": "ok", "detected": True, "buckets": [
                {"account": "default", "bucket": "5h", "bucket_label": "5-hour window",
                 "used_percent": 42.0, "resets_at": TS_RESET, "source": "codex_api"},
            ], "reset_credits": {"available_count": 2, "credits": [
                    {"title": "Full reset",
                     "expires_at": "2026-10-22T20:48:12.396262Z"},
                    {"title": "Partial reset", "expires_at": NOW + 86_400},
                ]}},
            "claude": {"status": "ok", "detected": True, "buckets": [
                {"account": "work", "bucket": "weekly_all", "bucket_label": "Weekly All",
                 "used_percent": 30.0, "resets_at": TS_RESET, "source": "claude_api"},
                {"account": "home", "bucket": "session", "bucket_label": "Session",
                 "used_percent": 7.5, "resets_at": TS_RESET, "source": "session"},
            ]},
            "kimi": {"status": "unavailable", "detected": True, "buckets": []},
            "grok": {"status": "unavailable", "detected": False, "buckets": []},
        },
    }
    state.update(over)
    return state


def _quota_history():
    return {"series": [
        {"provider": "codex", "account": "default", "bucket": "5h",
         "points": [
             {"captured_at": NOW - 7_200, "used_percent": 10.0},
             {"captured_at": NOW - 3_600, "used_percent": 20.0},
             {"captured_at": NOW - 60, "used_percent": 42.0},
         ]},
        {"provider": "claude", "account": "home", "bucket": "session",
         "points": [
             {"captured_at": NOW - 90_000, "used_percent": 1.0},  # outside 24h
             {"captured_at": NOW - 10, "used_percent": 7.5},
         ]},
    ]}


class TestQuotaTable:
    def render(self, state=None, history=None, labels=None):
        banner, rows, note, credits = quota_table(
            state or _quota_state(),
            history if history is not None else _quota_history(),
            now_ts=NOW,
            labels=labels,
        )
        return banner, rows, note, credits

    def provider_groups(self, rows):
        """Round 5 group walk — the ONLY correct way to read the grouped
        table back. An all-empty row is a SPACER (ends the current group);
        a Provider cell that is non-empty and does not start with "@" OPENS
        a group; "" / "@acct" cells CONTINUE it (a "" continuation is not a
        spacer: its other cells are filled)."""
        groups: dict[str, list] = {}
        current: str | None = None
        for r in rows:
            if not any(r):  # spacer between provider groups
                current = None
                continue
            head = str(r[0])
            if head and not head.startswith("@"):
                current = head.split("@")[0]
                groups.setdefault(current, [])
            assert current is not None, f"continuation before any group head: {r}"
            groups[current].append(r)
        return groups

    def rows_by_provider(self, rows, name="claude"):
        return self.provider_groups(rows).get(name, [])

    def test_row_set_and_undetected_skipped(self):
        _, rows, _, _ = self.render()
        # provider order sorted; codex credits are a SECTION now, not a row.
        assert list(self.provider_groups(rows)) == ["claude", "codex", "kimi"]
        assert len(rows) == 6  # 2 claude + 1 codex + 1 kimi + 2 spacers
        assert "grok" not in self.provider_groups(rows)  # undetected + no buckets → absent

    def test_groups_open_with_provider_and_spacer(self):
        # Round 5 (user: "same provider only show one provider, and give
        # some margin between providers") — exact Provider-column layout:
        # opener names the group, "@acct" continues it, an ALL-EMPTY row
        # is the gap, and no gap exists before the first or after the last.
        _, rows, _, _ = self.render()
        assert [r[0] for r in rows] == [
            "claude@work", "@home", "", "codex", "", "kimi",
        ]
        assert rows[2] == [""] * 6 and rows[4] == [""] * 6
        assert rows[0][0] and rows[-1][0]  # never spacer-bounded

    def test_same_account_continuation_blanks_provider(self):
        # Non-folded provider with TWO windows on ONE account: only the
        # first row names it. The blank Provider cell here is a
        # CONTINUATION (other cells filled) — the walk must not mistake it
        # for a spacer, and must not re-label the provider.
        state = _quota_state()
        state["providers"]["codex"]["buckets"].append(
            {"account": "default", "bucket": "weekly", "bucket_label": "Weekly",
             "used_percent": 12.0, "resets_at": TS_RESET, "source": "codex_api"})
        _, rows, _, _ = self.render(state)
        assert [r[0] for r in self.rows_by_provider(rows, "codex")] == ["codex", ""]

    def test_interleaved_accounts_relabel_on_every_change(self):
        # The group's OPENER is not the reference — the PRECEDING row is:
        # returning to an earlier account RE-ASSERTS it ("@work"), or those
        # rows would silently read as continuations of the group above.
        state = _quota_state()
        state["providers"]["claude"]["buckets"] = [
            {"account": "work", "bucket": "weekly_all", "bucket_label": "Weekly",
             "used_percent": 30.0, "resets_at": TS_RESET, "source": "claude_api"},
            {"account": "home", "bucket": "session", "bucket_label": "Session",
             "used_percent": 7.5, "resets_at": TS_RESET, "source": "claude_api"},
            {"account": "work", "bucket": "session", "bucket_label": "Session",
             "used_percent": 9.0, "resets_at": TS_RESET, "source": "claude_api"},
        ]
        _, rows, _, _ = self.render(state)
        assert [r[0] for r in self.rows_by_provider(rows)] == [
            "claude@work", "@home", "@work",
        ]

    def test_account_folding(self):
        _, rows, _, _ = self.render()
        claude = self.rows_by_provider(rows)
        assert [r[0] for r in claude] == ["claude@work", "@home"]
        codex = self.rows_by_provider(rows, "codex")
        assert [r[0] for r in codex] == ["codex"]  # 1 distinct account: NO fold

    def test_companion_bar_shows_remaining(self):
        # Companion semantics: bar + percent show what is LEFT, not raw used.
        _, rows, _, _ = self.render()
        codex = self.rows_by_provider(rows, "codex")[0]
        assert codex[2] == "█" * 6 + " 58.0% left"  # 100-42=58 → round(10*0.58)=6

    def test_window_labels_normalized_not_raw(self):
        _, rows, _, _ = self.render()
        cells = {(r[0], r[1]) for r in rows}
        assert ("codex", "5-hour") in cells          # not raw "5-hour window"
        assert ("claude@work", "Weekly") in cells    # not raw "Weekly All"
        # home's source is not an *_api one → web's "local logs" tag; the
        # group walk labels it by the bare account (round 5 grouping):
        assert ("@home", "5-hour · local") in cells

    def test_reset_countdown_plus_absolute(self):
        _, rows, _, _ = self.render()
        codex = self.rows_by_provider(rows, "codex")[0]
        hm = dt.datetime.fromtimestamp(TS_RESET).strftime("%H:%M")
        assert codex[3] == f"in 2h 0m · {hm}"        # TS_RESET = NOW + 7200

    def test_reset_credits_section_not_a_row(self):
        # User rule: credits are a BOTTOM SECTION with expiry dates, never a
        # row of the usage table. Real expiry shapes: ISO-8601 Z (codex) +
        # epoch int. Round 4: every row names its PROVIDER, and the rows
        # sort by expiry ascending across the epoch/ISO duality — epoch
        # NOW+86_400 (2025-10-10) lands BEFORE the ISO 2026-10-22 grant.
        _, rows, _, credits = self.render()
        assert not [r for r in rows if "credits" in r[1]]
        lines = [text(line) for line in credits]
        assert lines[0] == "reset credits 2 available"
        iso = dt.datetime.fromisoformat("2026-10-22T20:48:12.396262+00:00").astimezone()
        epoch = dt.datetime.fromtimestamp(NOW + 86_400)
        assert lines[1] == "Codex · Partial reset · expires " + epoch.strftime("%Y-%m-%d %H:%M")
        assert lines[2] == "Codex · Full reset · expires " + iso.strftime("%Y-%m-%d %H:%M")

    def test_credits_section_absent_without_credits(self):
        state = _quota_state()
        del state["providers"]["codex"]["reset_credits"]
        _, _, _, credits = self.render(state)
        assert credits == []

    def test_credits_section_survives_codex_windowless(self):
        # Credits belong to the CARD, not to a rendered window row.
        state = _quota_state()
        state["providers"]["codex"]["buckets"] = []
        _, rows, _, credits = self.render(state)
        assert not self.rows_by_provider(rows, "codex")
        assert credits and text(credits[0]) == "reset credits 2 available"

    # -- round 4: Claude Code limit resets join the section (PR #109 ships
    # the SAME reset_credits key; the user asked "which is which, fix that") --

    def test_claude_credits_provider_level_when_single_install(self):
        # No accounts[] (single install — the backend only attaches one with
        # more than one account): the provider-level block, plain label.
        state = _quota_state()
        state["providers"]["claude"]["reset_credits"] = {
            "available_count": 1, "credits": [
                {"id": "r1", "expires_at": NOW + 3_600},  # no title → id
            ],
        }
        _, _, _, credits = self.render(state, labels=_LABELS)
        lines = [text(line) for line in credits]
        assert lines[0] == "reset credits 3 available"  # codex 2 + claude 1
        assert lines[1].startswith("Claude Code · r1 · expires ")

    def test_claude_credits_per_install_never_doubles_the_primary(self):
        # Multi-install card: grants ride on each accounts[] entry
        # (absent-not-null) AND the provider-level block repeats the PRIMARY
        # install's same block — taking both would print the default
        # install's grants twice. The accounts[] pass covers every install
        # INCLUDING the primary, so the provider-level block is skipped
        # whole, and sibling installs are labeled by account.
        primary_block = {"available_count": 1, "credits": [
            {"title": "Default grant", "expires_at": NOW + 7_200},
        ]}
        state = _quota_state()
        state["providers"]["claude"]["reset_credits"] = primary_block
        state["providers"]["claude"]["accounts"] = [
            {"account": "default", "reset_credits": primary_block},
            {"account": "academic", "reset_credits": {
                "available_count": 1, "credits": [
                    {"title": "Sibling grant", "expires_at": NOW + 600},
                ]}},
            {"account": "work"},  # no key at all — absent-not-null
        ]
        _, _, _, credits = self.render(state, labels=_LABELS)
        lines = [text(line) for line in credits]
        assert lines[0] == "reset credits 4 available"  # codex 2 + default 1 + academic 1
        assert lines[1].startswith("Claude Code@academic · Sibling grant · ")
        assert lines[2].startswith("Claude Code@default · Default grant · ")
        assert lines[3].startswith("Codex · Partial reset · ")
        assert lines[4].startswith("Codex · Full reset · ")
        assert len(lines) == 5  # 5-cap TOTAL across ALL sources (header + 4 rows)

    def test_credits_cap_five_total_sorted_across_providers(self):
        # Sort is by expiry ASCENDING GLOBALLY (nearest first) — not by
        # provider then list order — and the cap is 5 across every source.
        state = _quota_state()
        state["providers"]["claude"]["reset_credits"] = {
            "available_count": 5, "credits": [
                {"title": f"c{i}", "expires_at": NOW + i * 60} for i in range(5)
            ],
        }
        _, _, _, credits = self.render(state)
        assert text(credits[0]) == "reset credits 7 available"
        names = [text(line).split(" · ")[1] for line in credits[1:]]
        assert names == ["c0", "c1", "c2", "c3", "c4"]  # all sooner than codex's
        assert len(credits) == 6  # header + 5, both codex rows evicted by cap

    def test_null_expiry_sorts_last_and_dashes(self):
        state = _quota_state()
        state["providers"]["claude"]["reset_credits"] = {
            "available_count": 1, "credits": [
                {"title": "No expiry", "expires_at": None},  # claude rows: nullable
            ],
        }
        _, _, _, credits = self.render(state, labels=_LABELS)
        assert text(credits[-1]) == f"Claude Code · No expiry · expires {EM_DASH}"

    def test_credit_labels_arrive_as_data(self):
        # labels=None keeps the title-case fallback — proving charts needs
        # no backend import to name a provider (import-discipline law).
        _, _, _, credits = self.render(labels=None)
        assert text(credits[1]).startswith("Codex · ")

    def test_credits_cap_five_and_expiry_dash(self):
        state = _quota_state()
        state["providers"]["codex"]["reset_credits"] = {
            "available_count": 9,
            "credits": [{"title": f"c{i}", "expires_at": None} for i in range(8)],
        }
        _, _, _, credits = self.render(state)
        assert len(credits) == 6  # header + 5 rows max
        assert text(credits[1]).endswith(f"· expires {EM_DASH}")

    def test_claude_fable_window_named_fable(self):
        # The reported bug: substring matching turned the scoped
        # weekly_scoped_fable window into a second plain "Weekly" row; the
        # Fable weekly is its own meter. Prefix-stripped kinds match EXACTLY
        # (web's claudeBucketKind), so "weekly_all" stays "Weekly".
        state = _quota_state()
        state["providers"]["claude"] = {
            "status": "ok", "detected": True,
            "buckets": [
                {"account": "default", "bucket": "session",
                 "bucket_label": "Session", "used_percent": 5.0,
                 "resets_at": TS_RESET, "source": "claude_api"},
                {"account": "default", "bucket": "weekly_all",
                 "bucket_label": "Weekly All", "used_percent": 30.0,
                 "resets_at": TS_RESET, "source": "claude_api"},
                {"account": "default", "bucket": "weekly_scoped_fable",
                 "bucket_label": "Fable", "used_percent": 12.0,
                 "resets_at": TS_RESET, "source": "claude_api"},
                {"account": "academic", "bucket": "academic_weekly_scoped_fable",
                 "bucket_label": "Fable", "used_percent": 40.0,
                 "resets_at": TS_RESET, "source": "claude_api"},
            ],
        }
        _, rows, _, _ = self.render(state)
        assert sorted(r[1] for r in self.rows_by_provider(rows)) == [
            "5-hour", "Fable", "Fable", "Weekly",
        ]

    def test_junk_none_percent_bucket_dropped(self):
        # The antigravity-junk filter: a bucket that measured NOTHING
        # (used_percent None, not unlimited) is noise → no row at all.
        state = _quota_state()
        state["providers"]["claude"]["buckets"].append(
            {"account": "work", "bucket": "ghost", "bucket_label": "Ghost",
             "used_percent": None, "resets_at": None, "source": "claude_api"}
        )
        _, rows, _, _ = self.render(state)
        assert [r for r in self.rows_by_provider(rows) if r[1] == "Ghost"] == []

    def test_unlimited_bucket(self):
        state = _quota_state()
        state["providers"]["claude"]["buckets"].append(
            {"account": "home", "bucket": "unlimited_thing",
             "bucket_label": "Unlimited", "used_percent": None,
             "resets_at": None, "source": "claude_api", "unlimited": True}
        )
        _, rows, _, _ = self.render(state)
        row = next(r for r in self.rows_by_provider(rows)
                   if r[1].startswith("Unlimited"))
        assert row[2].endswith(" unlimited") and "█" * 10 in row[2]

    def test_api_and_reset_credits_bucket_keys_excluded(self):
        # raw snapshot rows may still carry the bookkeeping buckets —
        # never renderable windows (web's isQuotaUsageBucket).
        state = _quota_state()
        state["providers"]["codex"]["buckets"] += [
            {"account": "default", "bucket": "api", "bucket_label": "API",
             "used_percent": 5.0, "resets_at": TS_RESET, "source": "codex_api"},
            {"account": "default", "bucket": "reset_credits",
             "bucket_label": "Credits", "used_percent": 1.0,
             "resets_at": TS_RESET, "source": "codex_api"},
        ]
        _, rows, _, _ = self.render(state)
        windows = [r[1] for r in self.rows_by_provider(rows, "codex")]
        assert windows == ["5-hour"]  # "credits" is the bottom section now

    def test_antigravity_pool_collapses_to_binding_window(self):
        # Antigravity reports one quotaInfo PER MODEL against shared pools —
        # the per-model junk must collapse to one row per window, the one
        # that BINDS (max used). Window inferred from the reset horizon
        # (>8h out → Weekly), never the model-name bucket_label.
        state = _quota_state()
        state["providers"]["antigravity"] = {
            "status": "ok", "detected": True,
            "buckets": [
                {"account": "me@x", "bucket": "gemini-pro", "bucket_label": "Gemini Pro",
                 "used_percent": 10.0, "resets_at": NOW + 2 * 3600,
                 "source": "antigravity_api", "captured_at": NOW},
                {"account": "me@x", "bucket": "gemini-flash", "bucket_label": "Gemini Flash",
                 "used_percent": 55.0, "resets_at": NOW + 4 * 86_400,
                 "source": "antigravity_api", "captured_at": NOW},
                {"account": "me@x", "bucket": "claude-sonnet", "bucket_label": "Claude Sonnet",
                 "used_percent": 20.0, "resets_at": NOW + 3600,
                 "source": "antigravity_api", "captured_at": NOW},
                {"account": "me@x", "bucket": "guru-model", "bucket_label": "Guru",
                 "used_percent": None, "resets_at": None,
                 "source": "antigravity_api", "captured_at": NOW},
                {"account": "me@x", "bucket": "no-signal", "bucket_label": "No Signal",
                 "used_percent": None, "resets_at": None,
                 "source": "antigravity_api", "captured_at": NOW},
            ],
        }
        _, rows, _, _ = self.render(state)
        ag = self.rows_by_provider(rows, "antigravity")
        assert [r[1] for r in ag] == ["5-hour", "Weekly"]
        assert "80.0% left" in ag[0][2]   # binding 5-hour = most-used (20 used)
        assert "45.0% left" in ag[1][2]   # weekly: 100 - 55
        # raw model names must never appear
        assert not [r for r in ag if "Gemini" in "".join(r) or "Guru" in "".join(r)]

    def test_trend_glyph_length_matches_points(self):
        _, rows, _, _ = self.render()
        codex = self.rows_by_provider(rows, "codex")[0]
        assert len(codex[4]) == 3  # 3 points in the window → 3 glyphs
        home = next(r for r in self.rows_by_provider(rows) if r[0] == "@home")
        assert len(home[4]) == 1   # the NOW-90000 point is outside the 24h window

    def test_unavailable_provider_status_row(self):
        _, rows, _, _ = self.render()
        # the status-only card IS its group's opener (provider written);
        # round 5 grouping put it at index 5, so read it by group — never
        # by hardcoded index.
        assert self.rows_by_provider(rows, "kimi") == [
            ["kimi", EM_DASH, EM_DASH, EM_DASH, EM_DASH, "unavailable"]
        ]

    def test_empty_history_trends_dash_and_note(self):
        _, rows, note, _ = self.render(history={})
        # spacer rows are all-EMPTY, not dashed — only DATA rows must dash
        assert all(r[4] == EM_DASH for r in rows if any(r))
        assert "no 24h quota history" in note

    def test_garbage_epochs_render_dash_never_crash(self):
        # Untrusted absolute times ride in from provider payloads:
        # fromtimestamp raises OverflowError on absurd magnitudes on EVERY
        # platform and OSError on epoch 0 under Windows — the pane must
        # dash, not crash mid-paint (same guard _expiry_text already had).
        state = _quota_state()
        state["providers"]["codex"]["buckets"][0]["resets_at"] = 10**18  # absurd
        state["providers"]["claude"]["buckets"][0]["resets_at"] = 0      # epoch 0
        state["poll"]["last_run"] = 0                                    # epoch 0
        banner, rows, _, _ = self.render(state)  # must not raise
        codex = self.rows_by_provider(rows, "codex")[0]
        assert codex[3].endswith(" · " + EM_DASH)  # countdown kept, garbage absolute dashed
        claude = self.rows_by_provider(rows, "claude")[0]
        assert claude[3] == EM_DASH  # epoch 0 reads as "never resets", not 1970
        assert text(banner[0]).endswith(EM_DASH)  # last_run 0 → dash

    def test_banner_enabled_and_last_run_dash(self):
        banner, _, note, _ = self.render()
        assert text(banner[0]).startswith("quota enabled")
        assert text(banner[0]).endswith(EM_DASH)  # last_run None → dash
        assert note == ""

    def test_banner_disabled_guidance(self):
        banner, _, note, _ = self.render(_quota_state(enabled=False))
        assert "disabled" in text(banner[0])
        assert "tokdash quota consent --enabled on" in note

    def test_consent_guidance_when_no_network_consent(self):
        _, _, note, _ = self.render(_quota_state(consent={"codex_api": False}))
        assert "not consented" in note

    def test_never_mutates_payloads(self):
        state, history = _quota_state(), _quota_history()
        s_before, h_before = copy.deepcopy(state), copy.deepcopy(history)
        quota_table(state, history, now_ts=NOW)
        assert state == s_before and history == h_before
