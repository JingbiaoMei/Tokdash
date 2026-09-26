"""Spec §10 tests for tokdash/tui/formatting.py.

Pure string rules, the null-vs-zero law, the percentage-points lock, the
two-surface delta-color split, the glyph probe, and (once report.py lands) the
three emitters. No textual, no rich — same import discipline as the module.
"""

from __future__ import annotations

import contextlib
import sys
import types

import pytest

from tokdash.tui.formatting import (
    EM_DASH,
    NA,
    Run,
    day_spark,
    fmt_cost_headline,
    fmt_cost_row,
    fmt_delta,
    fmt_duration,
    fmt_hit,
    fmt_int,
    fmt_tokens,
    glyph_style,
    text_bar,
    window_line,
)


@contextlib.contextmanager
def fake_stdout_encoding(encoding):
    """Swap sys.stdout.encoding for the duration of a `with` block.

    Deliberately NOT a monkeypatch fixture: with pytest's default fd capture,
    the capture manager re-asserts its own sys.stdout between fixture setup
    and the test call phase, silently reverting any fixture-level patch (the
    body then reads the capture object's utf-8 encoding — verified on
    pytest 9.1). Assigning inside the test body survives until the next phase
    boundary, which is exactly the window these probes need.
    """
    saved = sys.stdout
    sys.stdout = types.SimpleNamespace(encoding=encoding)
    try:
        yield
    finally:
        sys.stdout = saved


class TestNumberFormatting:
    def test_fmt_int(self):
        assert fmt_int(None) == EM_DASH
        assert fmt_int(0) == "0"
        assert fmt_int(999) == "999"
        assert fmt_int(1_000) == "1,000"
        assert fmt_int(12_400) == "12,400"
        assert fmt_int(12_400_000) == "12,400,000"

    def test_fmt_tokens(self):
        assert fmt_tokens(None) == EM_DASH
        assert fmt_tokens(999) == "999"
        assert fmt_tokens(1_000) == "1.0K"
        assert fmt_tokens(12_400) == "12.4K"
        assert fmt_tokens(1_500_000) == "1.5M"
        assert fmt_tokens(2_500_000_000) == "2.5B"
        assert fmt_tokens(1e12) == "1.0T"

    def test_fmt_tokens_rolls_up_at_1000(self):
        # Web formatCompactTokenCount (index.html:7999-8004): rounding that
        # would display "1000.0K" steps up a unit instead.
        assert fmt_tokens(999_950) == "1.0M"
        assert fmt_tokens(999_999) == "1.0M"
        assert fmt_tokens(999_949) == "999.9K"

    def test_fmt_cost(self):
        assert fmt_cost_headline(None) == EM_DASH
        assert fmt_cost_headline(142.31) == "$142.31"
        assert fmt_cost_headline(1234.5) == "$1,234.50"
        assert fmt_cost_row(None) == EM_DASH
        assert fmt_cost_row(5.0) == "$5.00"

    def test_fmt_hit(self):
        assert fmt_hit(None) == NA
        assert fmt_hit(0.71) == "71.0%"


class TestDurationTable:
    """The locked §4 table, ported from the web formatDuration/Units
    (index.html:10295-10340)."""

    @pytest.mark.parametrize(
        ("ms", "expected"),
        [
            (None, EM_DASH),
            (0, "0s"),                 # measured zero — never a dash
            (42_000, "42s"),
            (59_499, "59s"),
            (59_500, "1m"),            # seconds round HALF UP (web Math.round)
            (60_000, "1m"),
            (420_000, "7m"),
            (3_599_000, "59m"),
            (3_600_000, "1h 00m"),
            (18_180_000, "5h 03m"),    # zero-padded minutes
            (86_399_000, "23h 59m"),
            (90_000_000, "1 day 1 hour"),    # singular both sides
            (187_200_000, "2 days 4 hours"),
            (777_600_000, "1 week 2 days"),
            (2_160_000_000, "3 weeks 4 days"),
            (8_812_800_000, "3 months 12 days"),  # months = days // 30
        ],
    )
    def test_duration_table(self, ms, expected):
        assert fmt_duration(ms) == expected

    def test_zero_tails_drop(self):
        # Web `pair` drops a zero tail: "2 days", "1 week", "1 month".
        assert fmt_duration(172_800_000) == "2 days"        # exactly 2 days
        assert fmt_duration(604_800_000) == "1 week"        # exactly 7 days
        assert fmt_duration(2_592_000_000) == "1 month"     # exactly 30 days

    def test_hour_rollover_guard(self):
        # 1 day 23h59m59s rounds its hour remainder to 24 and rolls up
        # instead of printing "1 day 24 hours" (web index.html:10337-10338).
        assert fmt_duration(172_799_000) == "2 days"

    def test_hour_rollover_guard_scoped_to_day_tier(self):
        # The web scopes the rollover guard to the day/hour tier ONLY
        # (index.html:10335-10338): the week/month tiers never see it, and the
        # tier itself is chosen from the PRE-guard day count. Applying the
        # guard before tier selection used to shift these by a full tier
        # versus the web dashboard ("1 week", "1 week 2 days", "1 month").
        assert fmt_duration(603_300_000) == "7 days"          # 6d 23:35
        assert fmt_duration(776_100_000) == "1 week 1 day"    # 8d 23:35
        assert fmt_duration(2_590_500_000) == "4 weeks 1 day"  # 29d 23:35


class TestNullVsZeroLaw:
    """One test class, single source of truth (spec §4): None = unknown →
    EM_DASH / "n/a"; a MEASURED 0 renders as a figure; an unpriced row cost of
    0.0 is NOT a measured zero."""

    def test_none_is_em_dash_or_na(self):
        assert fmt_int(None) == EM_DASH
        assert fmt_tokens(None) == EM_DASH
        assert fmt_cost_headline(None) == EM_DASH
        assert fmt_cost_row(None) == EM_DASH
        assert fmt_hit(None) == NA
        assert fmt_duration(None) == EM_DASH
        assert fmt_delta(None) == Run(EM_DASH, None)

    def test_measured_zero_renders(self):
        assert fmt_int(0) == "0"
        assert fmt_tokens(0) == "0"
        assert fmt_cost_headline(0.0) == "$0.00"  # measured zero, not missing
        assert fmt_hit(0.0) == "0.0%"             # zero prompt input measures 0.0%
        assert fmt_duration(0) == "0s"            # not the web's dash shortcut

    def test_unpriced_row_is_not_free(self):
        # PricingDatabase.get_cost returns 0.0 for unknown models
        # (pricing.py:320) — row context dashes it, headline prints $0.00.
        assert fmt_cost_row(0.0) == EM_DASH
        assert fmt_cost_row(-2.5) == EM_DASH
        assert fmt_cost_headline(0.0) == "$0.00"

    def test_all_zero_day_spark_is_flat_floor_not_dash(self):
        assert day_spark([0.0, 0.0, 0.0]) == "▁▁▁"


class TestPercentPointsLock:
    def test_pct_change_cross_check(self):
        # compute.pct_change already multiplies by 100 (compute.py:1075-1079);
        # fmt_delta must never scale again or 110-vs-100 would read "1000.0%".
        from tokdash.compute import pct_change

        assert pct_change(110, 100) == 10.0
        with fake_stdout_encoding("utf-8"):
            assert fmt_delta(pct_change(110, 100)).text == "↑ 10.0%"


class TestDeltaColorSplit:
    """The web has TWO OPPOSITE delta-color conventions and each surface
    follows its own: Report tab up=GREEN (.usage-report-delta-up,
    index.html:14908 + 1752-1755), Overview KPI chips up=RED (renderDelta,
    index.html:8323). Do not "unify" them."""

    def test_report_surface_up_is_ok(self):
        with fake_stdout_encoding("utf-8"):
            assert fmt_delta(5.0, surface="report").style == "ok"
            assert fmt_delta(-5.0, surface="report").style == "bad"

    def test_overview_surface_up_is_bad(self):
        with fake_stdout_encoding("utf-8"):
            assert fmt_delta(5.0, surface="overview").style == "bad"
            assert fmt_delta(-5.0, surface="overview").style == "ok"

    def test_texts_and_arrows(self):
        with fake_stdout_encoding("utf-8"):
            assert fmt_delta(8.2).text == "↑ 8.2%"
            assert fmt_delta(-3.1).text == "↓ 3.1%"

    def test_zero_on_overview_is_muted_arrow(self):
        with fake_stdout_encoding("utf-8"):
            run = fmt_delta(0.0, surface="overview")
        assert run.text == "→ 0.0%"
        assert run.style == "muted"

    def test_zero_on_report_is_ok(self):
        # The Report tab colors 0 "up" (>= 0), it has no muted branch.
        with fake_stdout_encoding("utf-8"):
            run = fmt_delta(0.0, surface="report")
        assert run.text == "↑ 0.0%"
        assert run.style == "ok"


class TestGlyphProbe:
    def test_utf8_is_blocks(self):
        with fake_stdout_encoding("utf-8"):
            assert glyph_style() == "blocks"

    def test_cp1252_is_ascii(self):
        # monkeypatched sys.stdout.encoding (cp1252 → ascii ramp), spec §10.
        with fake_stdout_encoding("cp1252"):
            assert glyph_style() == "ascii"
            assert fmt_delta(8.2).text == "+8.2%"
            assert fmt_delta(-3.1).text == "-3.1%"
            assert fmt_delta(0.0, surface="overview").text == "=0.0%"  # "→" is cp1252-hostile too

    @pytest.mark.parametrize(
        ("encoding", "expected"),
        [
            ("utf-8", "blocks"),
            ("UTF-8", "blocks"),
            ("utf8", "blocks"),
            ("cp1252", "ascii"),
            ("cp932", "ascii"),
            ("ascii", "ascii"),
            (None, "ascii"),  # no encoding information → assume the worst console
        ],
    )
    def test_encoding_table(self, encoding, expected):
        with fake_stdout_encoding(encoding):
            assert glyph_style() == expected


class TestSparkAndBars:
    def test_day_spark_empty_is_em_dash(self):
        assert day_spark([]) == EM_DASH

    def test_day_spark_five_levels(self):
        # nonzero [1,2,3,4] → nearest-rank percentiles 20/40/60/80 are 1/2/3/4.
        assert day_spark([0, 1, 2, 3, 4]) == "▁▂▃▅▇"
        assert day_spark([0, 1, 2, 3, 4], glyphs="ascii") == ".:-=#"

    def test_day_spark_thresholds(self):
        # nonzero 1..10 → thresholds 2/4/6/8 → level per value.
        assert day_spark(list(range(1, 11))) == "▁▂▂▃▃▅▅▇▇▇"
        # a lone value sits at every percentile → tallest level.
        assert day_spark([5]) == "▇"
        # a value below the 20th percentile of the nonzero values is level 0.
        assert day_spark([1, 5, 5, 5, 5, 5]) == "▁▇▇▇▇▇"

    def test_text_bar(self):
        assert text_bar(1, 1) == "█" * 20
        assert text_bar(0.5, 1) == "█" * 10
        assert text_bar(3, 4, width=8) == "█" * 6
        assert text_bar(0, 5) == ""

    def test_text_bar_maxv_not_positive(self):
        assert text_bar(5, 0) == ""
        assert text_bar(5, -2) == ""

    def test_text_bar_ascii(self):
        assert text_bar(0.5, 1, glyphs="ascii") == "#" * 10


class TestWindowLine:
    def test_recognized(self):
        rng = {"from": "2026-09-14", "to": "2026-09-20", "days": 7, "recognized": True}
        assert window_line(rng) == "2026-09-14 → 2026-09-20 · 7 days"

    def test_unrecognized_warning_branch(self):
        rng = {"from": "1970-01-01", "to": "2026-09-20", "days": 20_000, "recognized": False}
        assert window_line(rng).endswith(" (unrecognized period — showing all time)")

    def test_missing_recognized_key_warns(self):
        # Defense-in-depth: absent/False is treated as "unrecognized".
        rng = {"from": "x", "to": "y", "days": 1}
        assert "unrecognized period" in window_line(rng)


class TestRun:
    def test_frozen_and_equality(self):
        assert Run("x", "ok") == Run("x", "ok")
        assert Run("x").style is None
        with pytest.raises(Exception):
            Run("x").text = "y"


class TestEmitters:
    """Spec §5's emitters live in report.py (it owns build_report_segments);
    their §10 behaviors are locked here too. Skips until the report lane
    lands — test_tui_report.py covers them in depth once it exists."""

    @pytest.fixture
    def report(self):
        return pytest.importorskip("tokdash.tui.report", reason="report lane has not landed yet")

    def test_emit_plain_no_ansi_no_brackets(self, report):
        segs = [
            [Run("Tokens 12.4K", None), Run("↑ 8.2%", "ok")],
            [Run("Cost $142.31", "bold")],
        ]
        out = report.emit_plain(segs)
        assert "\x1b" not in out
        assert "[" not in out and "]" not in out
        assert "12.4K" in out and "8.2" in out and "$142.31" in out

    def test_emit_ansi_gated_by_no_color(self, report, monkeypatch):
        segs = [[Run("up", "ok"), Run(" down", "bad")]]
        monkeypatch.setenv("NO_COLOR", "1")
        out = report.emit_ansi(segs)
        assert "\x1b" not in out
        assert out == report.emit_plain(segs)  # engine helpers no-op with color off

    def test_emit_markup_maps_styles_and_escapes_brackets(self, report):
        segs = [
            [
                Run("g", "ok"), Run("r", "bad"), Run("y", "warn"),
                Run("b", "bold"), Run("c", "accent"), Run("d", "muted"),
            ],
            [Run("[literal]", None)],
        ]
        out = report.emit_markup(segs)
        assert "\x1b" not in out
        for style in ("green", "red", "yellow", "bold", "cyan", "dim"):
            assert style in out  # ok/bad/warn/bold/accent/muted style map, §5
        assert "\\[" in out  # literal '[' hand-escaped, never re-parsed as markup
