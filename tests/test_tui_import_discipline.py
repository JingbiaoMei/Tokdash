"""Import discipline for the TUI feature (spec §10): `tokdash report` never pulls in Textual.

Headless safety is the contract — ``data.py``, ``formatting.py``, ``report.py`` and
the round-2 ``charts.py``/``remote.py`` stay free of textual/rich so the one-shot report works on installs where Textual is
absent (a pipx runtime predating the release, a stripped venv, a broken wheel).
A ``sys.meta_path`` finder models that absence; because ``sys.modules`` caches would
make importing an already-imported module a no-op, the fixture also evicts the
``tokdash.tui`` submodules (and any pre-imported ``textual``) for the duration of
the test, so a green run proves a real import happened under the block.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import sys

import pytest

# Pure modules that must import with textual/rich absent. ``tokdash.tui.charts``
# (round-2 pure chart renderers) and ``tokdash.tui.remote`` (round-2 read-only
# HTTP delegation, pure stdlib) are both part of the set.
PURE_TUI_MODULES = (
    "tokdash.tui.data",
    "tokdash.tui.formatting",
    "tokdash.tui.charts",
    "tokdash.tui.remote",
    "tokdash.tui.report",
)


class _BlockTextualFinder:
    """Meta-path finder that makes ``import textual``/``import rich`` fail.

    Both are blocked: the documented contract is that the report path stays
    free of textual AND rich (spec §8 — rich only ever arrives transitively
    via textual, and data/formatting/report must not import it directly).
    Inserted at the FRONT of ``sys.meta_path`` so it raises before any real
    finder (installed dist, PyPI path) can resolve the module. Raising from
    ``find_spec`` propagates out of the import machinery as-is. (A cached
    sys.modules entry would bypass finders entirely — hence the eviction of
    BOTH packages in _evict_textual_and_tui_modules.)
    """

    def find_spec(self, fullname, path=None, target=None):  # noqa: D102
        if fullname == "textual" or fullname.startswith("textual."):
            raise ImportError(f"textual blocked (import-discipline test): {fullname}")
        if fullname == "rich" or fullname.startswith("rich."):
            raise ImportError(f"rich blocked (import-discipline test): {fullname}")
        return None


def _evict_textual_and_tui_modules() -> dict:
    """Drop cached textual/rich + tokdash.tui modules; return them for restore.

    The rich eviction is what makes the block real rather than decorative:
    pytest itself may have pulled rich into sys.modules, and an in-cache
    module is returned without consulting meta_path. The fixture window only
    contains our own imports, so evicting rich cannot break test machinery
    mid-run; it is restored in ``finally``.
    """
    evicted = {}
    for name in list(sys.modules):
        if (
            name == "textual"
            or name.startswith("textual.")
            or name == "rich"
            or name.startswith("rich.")
            or name == "tokdash.tui"
            or name.startswith("tokdash.tui.")
        ):
            evicted[name] = sys.modules.pop(name)
    return evicted


@pytest.fixture
def block_textual():
    evicted = _evict_textual_and_tui_modules()
    finder = _BlockTextualFinder()
    sys.meta_path.insert(0, finder)
    try:
        yield
    finally:
        sys.meta_path.remove(finder)
        # Remove the modules the test imported under the block, then restore
        # whatever was cached beforehand, so neither state leaks to other tests.
        for name in list(sys.modules):
            if name == "tokdash.tui" or name.startswith("tokdash.tui."):
                del sys.modules[name]
        sys.modules.update(evicted)


# Canned payloads follow the field sources pinned in spec §5 (compute_usage_with_comparison,
# compute_insights with REPORT_FACETS, _active_time_payload). Deep-copied per fetch so a
# renderer mutating nested rows (compute payloads share objects — compute.py:989) could not
# leak across the three fetchers even within this test.
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
    "apps": {
        "codex": {"tokens": 8_000_000, "tokens_in": 6_000_000, "tokens_out": 2_000_000,
                  "tokens_cache": 4_000_000, "cost": 90.0, "messages": 3_000,
                  "cache_hit_rate": 0.66, "models": []},
        "claude": {"tokens": 4_400_000, "tokens_in": 3_000_000, "tokens_out": 1_400_000,
                   "tokens_cache": 2_000_000, "cost": 52.31, "messages": 1_820,
                   "cache_hit_rate": 0.78, "models": []},
    },
    "coding_apps": {},
    "coding_models": [],
    "top_models": [],
    "top_models_by_cost": [],
    "openclaw_models": [],
    "combined_models": [
        {"name": "gpt-5-codex", "tokens": 8_000_000, "tokens_in": 6_000_000,
         "tokens_out": 2_000_000, "tokens_cache": 4_000_000, "cost": 90.0,
         "messages": 3_000, "cache_hit_rate": 0.66, "source": "codex"},
        {"name": "claude-opus-4", "tokens": 4_400_000, "tokens_in": 3_000_000,
         "tokens_out": 1_400_000, "tokens_cache": 2_000_000, "cost": 52.31,
         "messages": 1_820, "cache_hit_rate": 0.78, "source": "claude"},
        # cost 0.0: unpriced row — must render as an em-dash, not "$0.00" (null-vs-zero law).
        {"name": "mystery-model", "tokens": 100, "tokens_in": 50, "tokens_out": 50,
         "tokens_cache": 0, "cost": 0.0, "messages": 5, "cache_hit_rate": None,
         "source": "kimi"},
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
    "facets": "daily,streaks,firsts,hourly,weekday,tools,models,projects",
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
            {"weekday": i, "name": name, "tokens": 1_800_000, "cost": 20.0,
             "messages": 700, "entries": 1_400}
            for i, name in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
        ],
        "peak_weekday": 2,
    },
    "tools": {
        "ranked": [
            {"tool": "codex", "tokens": 8_000_000, "cost": 90.0, "messages": 3_000,
             "entries": 6_000},
            {"tool": "claude", "tokens": 4_000_000, "cost": 52.31, "messages": 1_800,
             "entries": 3_600},
        ]
    },
    "models": {
        "ranked": [
            {"model": "gpt-5-codex", "tokens": 8_000_000, "cost": 90.0, "messages": 3_000},
            {"model": "claude-opus-4", "tokens": 4_000_000, "cost": 52.31, "messages": 1_800},
        ],
        "most_used": {"model": "gpt-5-codex", "messages": 3_000},
        "highest_cost": {"model": "gpt-5-codex", "cost": 90.0},
    },
    "projects": {
        "projects": [
            {"project": "tokdash", "tokens": 9_000_000, "cost": 100.5, "messages": 3_400}
        ],
        "unattributed": 3_000_000,
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
    "by_tool": {
        "Codex": {"tool_label": "Codex", "session_count": 42, "active_ms": 2_400_000,
                  "active_ms_sum": 3_000_000},
        "Claude": {"tool_label": "Claude", "session_count": 17, "active_ms": 1_200_000,
                   "active_ms_sum": 2_400_000},
    },
    "unavailable_tools": [],
    "active_gap_cap_ms": 300_000,
    "active_time_estimated": False,
    "active_time_method": "session-windows",
    "include_review_sessions": True,
    "timestamp": "2026-09-20T12:00:02",
}


def test_report_path_works_with_textual_absent(block_textual, monkeypatch, capsys):
    """The whole report path — imports included — must survive without textual."""
    # TOKDASH_USAGE_DB=0 keeps the run off the filesystem: ensure_usage_db_compatible
    # no-ops and db_summary prints the disabled line without constructing the store.
    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")

    import tokdash.tui.data as tui_data
    import tokdash.tui.formatting as tui_formatting  # noqa: F401 — the import IS the assertion
    # charts.py is round-2's pure renderer lane: segments out, no widgets in;
    # remote.py is round-2's stdlib-only HTTP delegation: GETs, no widgets,
    # and the report path may import it (data.py does, at module level).
    import tokdash.tui.charts as tui_charts  # noqa: F401 — the import IS the assertion
    import tokdash.tui.remote as tui_remote  # noqa: F401 — the import IS the assertion
    import tokdash.tui.report as tui_report

    assert "tokdash.tui.charts" in sys.modules
    assert "tokdash.tui.remote" in sys.modules

    def _outcome(payload):
        return tui_data.FetchOutcome(value=payload, status="hit", age_seconds=0.0)

    # The three fetch names are the module-level import contract of report.py
    # (mirrors test_tui_report.py); monkeypatching them keeps this test about
    # imports and plumbing, not compute.
    monkeypatch.setattr(
        tui_report, "fetch_usage", lambda *a, **k: _outcome(copy.deepcopy(USAGE_PAYLOAD))
    )
    monkeypatch.setattr(
        tui_report, "fetch_insights", lambda *a, **k: _outcome(copy.deepcopy(INSIGHTS_PAYLOAD))
    )
    monkeypatch.setattr(
        tui_report, "fetch_active_time",
        lambda *a, **k: _outcome(copy.deepcopy(ACTIVE_TIME_PAYLOAD)),
    )
    # report.py may import _local_today for the calendar-window default; optional.
    monkeypatch.setattr(
        tui_report, "_local_today", lambda: dt.date(2026, 9, 20), raising=False
    )

    args = argparse.Namespace(period="week", json=False, pretty=False, output=None)
    assert tui_report.run_report(args) == 0

    out = capsys.readouterr().out
    assert "Tokdash report" in out          # the header line rendered
    assert "usage db disabled" in out       # db footer took the disabled branch
    assert "textual" not in sys.modules
    assert "rich" not in sys.modules        # §8: rich is textual-transitive only


def test_app_module_imports_after_the_block(monkeypatch):
    """With no finder installed, the Textual app module imports normally.

    ``tokdash.tui.app`` is the ONLY tokdash module allowed to import textual; skip
    (not fail) when textual or the tui lane is absent so stripped envs stay green.
    """
    pytest.importorskip("textual", reason="textual not installed in this environment")
    app = pytest.importorskip("tokdash.tui.app", reason="tui lane has not landed yet")
    assert callable(app.run_tui)
