"""Revision-5 TUI: contract-shaped fixtures driven by real Textual pilots.

Every payload below is shaped like the endpoint's real response -- field names,
nesting, and the parts that are deliberately ABSENT. The assertions are about
MEANING: which field a card reads, what a missing figure prints as, which
population a share divides, and whether a late reply can be painted under a new
label. Shared synthetic rows that flatter every endpoint at once would prove
nothing here, so each source gets its own shape.

The fetch seams are the ``fetch_*`` names inside ``tokdash.tui.app`` -- the very
names the workers call -- so the workers, the generation guard, the progressive
paints and the failure paths stay under test while the network and the disk do
not. The suite's conftest keeps TOKDASH_TUI_NO_REMOTE on, so no pilot can reach
a live service or the real usage database.
"""
from __future__ import annotations

import asyncio
import copy
import datetime
import time

import pytest

textual = pytest.importorskip("textual", reason="the TUI tests need textual")

from textual.widgets import Button, DataTable, Input, Static  # noqa: E402
from textual.worker import WorkerState  # noqa: E402

import tokdash.tui.app as app_mod  # noqa: E402
from tokdash.sessions import SESSION_TOOLS  # noqa: E402
from tokdash.tui.app import TokdashApp, fit_columns  # noqa: E402
from tokdash.tui.data import FetchOutcome  # noqa: E402

TODAY = datetime.date(2026, 10, 18)

# --------------------------------------------------------------------------- #
# contract-shaped payloads
# --------------------------------------------------------------------------- #

SHARED_MODEL = "gpt-5.1-codex"

USAGE = {
    "period": "today",
    "range": {"from": "2026-10-18", "to": "2026-10-18", "days": 1, "recognized": True},
    "total_tokens": 1_000_000,
    "total_cost": 12.5,
    "total_messages": 40,
    "cache_hit_rate": 0.75,
    "comparison": {
        "tokens_pct": 12.0,
        "cost_pct": -4.0,
        "messages_pct": None,
        "prior_range": {"from": "2026-10-17", "to": "2026-10-17"},
    },
    "by_tool": {
        "codex": {"tokens": 600_000, "cost": 3.0, "tokens_in": 100_000,
                  "tokens_out": 50_000, "tokens_cache": 450_000,
                  "tokens_reasoning": 0, "cache_hit_rate": 0.818},
        "claude": {"tokens": 300_000, "cost": 2.0, "tokens_in": 20_000,
                   "tokens_out": 10_000, "tokens_cache": 270_000,
                   "tokens_reasoning": 0, "cache_hit_rate": 0.931},
        # OpenClaw carries output and reasoning in by_tool but has no apps entry,
        # so its message count has to come from its model list.
        "openclaw": {"tokens": 100_000, "cost": 7.5, "tokens_in": 5_000,
                     "tokens_out": 4_000, "tokens_cache": 91_000,
                     "tokens_reasoning": 1_000, "cache_hit_rate": 0.948},
    },
    "apps": {
        "codex": {"messages": 20, "tokens_in": 100_000, "tokens_out": 50_000,
                  "tokens_cache": 450_000,
                  "models": [{"name": SHARED_MODEL, "tokens": 400_000, "cost": 2.0,
                              "messages": 12},
                             {"name": "gpt-5-mini", "tokens": 200_000, "cost": 1.0,
                              "messages": 8}]},
        "claude": {"messages": 20, "tokens_in": 20_000, "tokens_out": 10_000,
                   "tokens_cache": 270_000,
                   "models": [{"name": SHARED_MODEL, "tokens": 300_000, "cost": 2.0,
                               "messages": 20}]},
    },
    "openclaw_models": [{"name": SHARED_MODEL, "tokens": 100_000, "cost": 7.5,
                         "messages": 6}],
    # The server's own merged table: ONE row for the shared model, with the sums
    # of all three harnesses. It carries no reasoning field, and no consumer may
    # invent one.
    "combined_models": [
        {"name": SHARED_MODEL, "tokens": 800_000, "tokens_in": 120_000,
         "tokens_out": 60_000, "tokens_cache": 811_000, "cost": 11.5, "messages": 38,
         "cache_hit_rate": 0.871},
        {"name": "claude-opus-4-7", "tokens": 150_000, "tokens_in": 30_000,
         "tokens_out": 5_000, "tokens_cache": 115_000, "cost": 99.0, "messages": 2,
         "cache_hit_rate": None},
        {"name": "gpt-5-mini", "tokens": 50_000, "tokens_in": 10_000,
         "tokens_out": 2_000, "tokens_cache": 38_000, "cost": 0.5, "messages": 4,
         "cache_hit_rate": 0.79},
    ],
    "top_models": [
        {"name": SHARED_MODEL, "tokens": 800_000, "cost": 11.5},
        {"name": "claude-opus-4-7", "tokens": 150_000, "cost": 99.0},
    ],
    "top_models_by_cost": [{"name": "claude-opus-4-7", "tokens": 150_000, "cost": 99.0}],
    "sparkline": {
        "granularity": "hour",
        "keys": ["00", "09", "10"],
        "buckets": [
            {"key": "2026-10-18T09", "tokens": 400_000, "cost": 5.0, "messages": 12,
             "input": 100_000, "cache": 300_000},
            # A real zero: an hour inside the window that recorded nothing.
            {"key": "2026-10-18T10", "tokens": 0, "cost": 0.0, "messages": 0,
             "input": 0, "cache": 0},
        ],
    },
    "source_errors": [],
}

ACTIVE = {
    "period": "today",
    "active_ms": 3_600_000,
    "active_ms_sum": 5_400_000,
    "comparison": {"active_ms_sum_pct": 8.0},
    "by_tool": {
        # A MEASURED ZERO must print 0s, not a dash; the dash is reserved for a
        # source that was not read at all.
        "codex": {"tool_label": "Codex", "session_count": 3, "active_ms": 0,
                  "active_ms_sum": 0},
        "claude": {"tool_label": "Claude Code", "session_count": 2,
                   "active_ms": 3_600_000, "active_ms_sum": 5_400_000},
    },
    # sessions.py returns ``sorted(unavailable)``, so the wire shape is a
    # SORTED ARRAY of source ids; the reason stays in the server log.
    "unavailable_tools": ["kimi"],
    "active_gap_cap_ms": 300_000,
    "active_time_estimated": True,
    "active_time_method": "capped-inter-event-gap",
    "include_review_sessions": False,
}

SESSIONS_BY_TOOL = {
    "codex": {
        "tool": "codex", "tool_label": "Codex", "period": "today",
        "sessions": [
            {"tool": "codex", "session_id": "cx-1", "project": "tokdash",
             "model": SHARED_MODEL, "token_events": 14, "tokens": 500_000,
             "cost": 3.0, "started_at": "2026-10-18T09:00:00+00:00",
             "last_seen_at": "2026-10-18T10:00:00+00:00"},
            # Same session_id as Claude's row below: identity is the PAIR.
            {"tool": "codex", "session_id": "shared-id", "project": "other",
             "model": "gpt-5-mini", "token_events": 2, "tokens": 1_000,
             "cost": 0.01, "last_seen_at": "2026-10-18T08:00:00+00:00"},
        ],
    },
    "claude": {
        "tool": "claude", "tool_label": "Claude Code", "period": "today",
        "sessions": [
            {"tool": "claude", "session_id": "shared-id", "project": "reports",
             "model": "claude-opus-4-7", "token_events": 3, "tokens": 90_000,
             "cost": 1.5, "last_seen_at": "2026-10-18T11:30:00+00:00"},
        ],
    },
}

SESSION_DETAIL = {
    "session": {"tool": "claude", "session_id": "shared-id",
                "project": "reports", "model": "claude-opus-4-7",
                "token_events": 3, "tokens": 90_000, "cost": 1.5,
                "last_seen_at": "2026-10-18T11:30:00+00:00"},
    "turns": [
        {"turn_index": 1, "model": "claude-opus-4-7", "tokens": 30_000,
         "tokens_out": 100, "cost": 0.5, "timestamp": "2026-10-18T11:00:00+00:00",
         "output_speed": None},
        # A turn with no timestamp keeps its order; no time may be invented.
        {"turn_index": 2, "model": "claude-opus-4-7", "tokens": 60_000,
         "tokens_out": 200, "cost": 1.0, "output_speed": None},
    ],
    "speed_measurement": {"measured": 0, "total": 2},
}

SESSION_SPEED = {
    "scope": {"kind": "entire_session"},
    "cache": {"state": "ready", "job_id": None},
    "summary": {"speed_calls": 12, "eligible_calls": 20, "output_tok_per_s": 96.4},
    "groups": [{"model": "claude-opus-4-7", "measurement_kind": "post_first_token",
                "token_basis": "output_only", "output_tok_per_s": 96.4}],
    "series": [],
    "total_responses": 20, "returned_points": 20, "bucketed": False,
    "truncated": False,
}

STATS_TRAILING = {
    "stats": {"total_tokens": 9_000_000, "messages": 5_000, "sessions": 777,
              "current_streak": 4, "longest_streak": 30, "active_days": 120,
              "total_days": 300, "most_used_model": SHARED_MODEL,
              "highest_cost_model": "claude-opus-4-7",
              "favorite_model": SHARED_MODEL},
    "contributions": [
        {"date": "2026-10-18", "totals": {"tokens": 1_000_000, "cost": 12.5,
                                          "messages": 40}, "intensity": 3,
         "sources": [{"modelId": SHARED_MODEL}]},
        {"date": "2026-10-17", "totals": {"tokens": 800_000, "cost": 9.0,
                                          "messages": 31}, "intensity": 2,
         "sources": [{"modelId": "gpt-5-mini"}]},
    ],
}

INSIGHTS = {
    "schema_version": 1,
    "range": {"days": 1, "recognized": True},
    "timezone": "BST",
    "totals": {"tokens": 900_000, "cost": 11.0, "messages": 36, "entries": 500},
    # Shapes copied from tokdash/insights.py: hourly buckets carry "hour",
    # weekday buckets carry "weekday" + "name", and the agent table joins the
    # "tools" facet onto active time.
    "hourly": {
        "buckets": [{"hour": h, "tokens": (400_000 if h == 9 else 0), "cost": 5.0,
                     "messages": (12 if h == 9 else 0), "entries": 1}
                    for h in range(24)],
        "peak_hour": 9, "night_share": 0.0, "night_hours": [0, 1, 22, 23],
    },
    "weekday": {
        "buckets": [{"weekday": d, "name": n, "tokens": (600_000 if d == 0 else 0),
                     "cost": 1.0, "messages": 4, "entries": 1}
                    for d, n in enumerate(["Monday", "Tuesday", "Wednesday", "Thursday",
                                           "Friday", "Saturday", "Sunday"])],
        "peak_weekday": 0,
    },
    "tools": {"ranked": [
        {"tool": "codex", "tokens": 600_000, "cost": 3.0, "messages": 20, "entries": 300},
        {"tool": "claude", "tokens": 300_000, "cost": 2.0, "messages": 16, "entries": 200}]},
    "models": {"ranked": [
        {"model": SHARED_MODEL, "tokens": 800_000, "cost": 11.5, "messages": 38,
         "entries": 480},
        {"model": "gpt-5-mini", "tokens": 50_000, "cost": 0.5, "messages": 4,
         "entries": 20}],
        "most_used": SHARED_MODEL, "highest_cost": SHARED_MODEL},
    "projects": {
        "projects": [
            {"project": "tokdash", "tokens": 500_000, "cost": 7.0, "messages": 20,
             "entries": 300},
            {"project": "reports", "tokens": 100_000, "cost": 1.0, "messages": 4,
             "entries": 60}],
        "unattributed": {"tokens": 200_000, "cost": 2.0, "messages": 8, "entries": 140},
        "names_included": True,
    },
    "daily": [{"date": "2026-10-17", "tokens": 300_000, "cost": 4.0, "messages": 14,
               "entries": 200, "intensity": 2},
              {"date": "2026-10-18", "tokens": 900_000, "cost": 11.0, "messages": 36,
               "entries": 500, "intensity": 4}],
    "streaks": {"current_streak": 4, "longest_streak": 30, "active_days": 120,
                "total_days": 300},
    "firsts": {"first_active_day": "2025-01-04", "last_active_day": "2026-10-18",
               "busiest_day": "2026-09-30", "busiest_day_tokens": 4_200_000,
               "peak_hour": 9},
}


SPEED_ACROSS = {
    "schema_version": 7,
    "view": "across-models",
    "metric": "output_tok_per_s",
    "aggregation": "sum_tokens_over_sum_duration",
    "range": {"from": "2026-10-18", "to": "2026-10-18"},
    "cache": {"state": "ready", "job_id": None, "updated_at": "2026-10-18T10:00:00"},
    "rows": [
        {"source": "codex", "model": SHARED_MODEL, "measurement_kind": "response_window",
         "token_basis": "output_including_reasoning", "output_tok_per_s": 88.4,
         "combined_tok_per_s": 120.1, "parallelism": 1.36, "peak_in_flight": 3,
         "speed_calls": 900, "eligible_calls": 1000, "coverage": {"ratio": 0.9},
         "last_ms": 1_760_000_000_000},
        # Same model, different timing kind: a SEPARATE comparison key, so it
        # stays a separate row and is never averaged with the row above.
        {"source": "codex", "model": SHARED_MODEL, "measurement_kind": "request_window",
         "token_basis": "output_including_reasoning", "output_tok_per_s": 41.0,
         "combined_tok_per_s": 55.0, "parallelism": 1.1, "peak_in_flight": 2,
         "speed_calls": 12, "eligible_calls": 1000, "coverage": {"ratio": 0.012},
         "last_ms": 1_759_900_000_000},
    ],
    "models": [
        {"model": SHARED_MODEL, "sources": ["claude", "codex"],
         "measurement_kinds": ["post_first_token", "response_window"],
         "token_bases": ["output_including_reasoning", "output_only"],
         "speed_tokens": 5_000_000, "speed_ms": 50_000.0, "speed_calls": 1_500,
         "combined_tok_per_s": 101.2, "busy_ms": 49_400.0, "parallelism": 1.2,
         "peak_in_flight": 4, "eligible_calls": 2_000, "coverage": {"ratio": 0.75}},
    ],
    "hidden_request_windows": [],
    "source_status": [{"source": "kimi", "status": "timing_unavailable", "usage_rows": 12}],
    "available_measurement_range": {"from": "2026-09-02", "to": "2026-10-18"},
}

SPEED_TREND = {
    "granularity": "hour",
    "requested_bucket": "auto",
    "auto_bucket": "hour",
    "offered_buckets": [{"bucket": "hour", "count": 12}],
    "buckets": [
        {"key": "2026-10-18T09", "from": "2026-10-18", "to": "2026-10-18", "hour": 9},
        {"key": "2026-10-18T10", "from": "2026-10-18", "to": "2026-10-18", "hour": 10},
    ],
    "spread": {"quantiles": [10, 50, 90], "min_responses": 30, "weight": "duration"},
    "series": [
        {"source": "codex", "model": SHARED_MODEL, "measurement_kind": "response_window",
         "token_basis": "output_including_reasoning",
         "per_stream": [88.0, None], "combined": [95.0, None], "calls": [40, 0],
         "parallelism": [1.2, None],
         "spread": {"p10": [70.0, None], "p50": [88.0, None], "p90": [110.0, None]},
         "hours": {"combined": [95.0] * 24, "calls": [4] * 24, "parallelism": [1.0] * 24}},
    ],
    "model_series": [
        {"model": SHARED_MODEL, "sources": ["claude", "codex"],
         "measurement_kinds": ["post_first_token", "response_window"],
         "combined": [95.0, None], "calls": [40, 0], "parallelism": [1.2, None],
         "hours": {"combined": [90.0] * 24, "calls": [5] * 24, "parallelism": [1.1] * 24}},
    ],
}

PRICING_DB = {
    "path": "/tmp/override/pricing_db.json",
    "baseline_path": "/site-packages/tokdash/pricing_db.json",
    "baseline_version": "2026-10-11",
    "source": "override",
    "data": {
        "version": "2026-09-01",
        "lastUpdated": "2026-09-01T00:00:00Z",
        "aliases": {"gpt-5-codex": SHARED_MODEL},
        "models": {
            SHARED_MODEL: {"input": 1.25, "output": 10.0, "cache_read": 0.125,
                           "cache_write": 1.5},
            # A missing rate stays missing; it is not a free model.
            "gpt-5-mini": {"input": 0.25, "output": 2.0},
        },
    },
    "text": "{}\n",
}

# The quota tab counts against the REAL clock (its "now" is time.time()), so the
# fixture is written relative to it: windows that reset later, points from the
# last hour, and one point that has already aged out of the 24h trend.
QUOTA_NOW = __import__("time").time()
QUOTA_STATE = {
    "enabled": True,
    # Shape of GET /api/quota: providers -> buckets (real usage windows), with
    # the account id on EVERY row, plus the accounts list and the poll block.
    "providers": {
        "claude": {
            "status": "ok",
            "network_enabled": True,
            "plan": "Max 5x",
            "buckets": [
                {"account": "default", "bucket": "session",
                 "bucket_label": "Session", "used_percent": 37.7,
                 "resets_at": QUOTA_NOW + 5 * 3600, "source": "claude_api"},
                # A MEASURED ZERO remaining still prints 0.0% left, never a dash.
                {"account": "default", "bucket": "weekly",
                 "bucket_label": "7-day window", "used_percent": 100.0,
                 "resets_at": QUOTA_NOW + 2 * 86_400, "source": "claude_api"},
                # A local-log window may lag its API twin: the row says so.
                {"account": "academic", "bucket": "academic_session",
                 "bucket_label": "Session", "used_percent": 12.0,
                 "resets_at": QUOTA_NOW + 3600, "source": "local_logs"},
            ],
            "accounts": [
                {"account": "default", "plan": "Max 5x", "status": "ok",
                 "status_detail": None, "updated_at": QUOTA_NOW},
                {"account": "academic", "plan": "Pro", "status": "warning",
                 "status_detail": "stale_token", "status_at": QUOTA_NOW,
                 "updated_at": QUOTA_NOW},
            ],
            "status_account": "academic",
        },
        "codex": {
            "status": "ok",
            "network_enabled": False,
            "plan": "pro",
            "buckets": [
                {"bucket": "5h", "bucket_label": "5-hour window",
                 "unlimited": True, "resets_at": None},
            ],
        },
        "kimi": {"status": "unavailable", "detected": True, "buckets": []},
    },
    "consent": {"credential_scan": False, "claude_api": True, "codex_api": False},
    "poll": {"enabled": True, "interval_minutes": 30, "last_run": QUOTA_NOW,
             "kill_switch": False},
}

QUOTA_HISTORY = {
    "series": [
        {"provider": "claude", "bucket": "session", "account": "default",
         "points": [{"captured_at": QUOTA_NOW - 3_600, "used_percent": 20.0},
                    {"captured_at": QUOTA_NOW - 1_800, "used_percent": 37.7}]},
    ],
}

# Hour-of-day for ONE exact comparison key. An unmeasured hour is null, never
# zero; hour 10 carries a MEASURED zero (three calls, nothing worth timing).
SPEED_HOUR = {
    "schema_version": 7,
    "view": "time-of-day",
    "metric": "output_tok_per_s",
    "bucket": "local_hour_of_day",
    "timestamp_basis": "usage_timestamp",
    "selected_key": {"source": "codex", "model": SHARED_MODEL,
                     "measurement_kind": "response_window",
                     "token_basis": "output_including_reasoning"},
    "hourly": [
        {
            "hour": h,
            "label": "%02d:00-%02d:00" % (h, (h + 1) % 24),
            "output_tok_per_s": (88.4 if h == 9 else (0.0 if h == 10 else None)),
            "speed_tokens": (90_000 if h == 9 else 0),
            "speed_ms": (1_018.0 if h == 9 else 0.0),
            "speed_calls": (900 if h == 9 else (3 if h == 10 else 0)),
            "eligible_calls": (1000 if h in (9, 10) else 4),
            "coverage": {"ratio": (0.9 if h == 9 else 0.75)},
            "days_with_measurements": (1 if h in (9, 10) else 0),
            "excluded_calls_by_reason": {},
            "missing_timing_calls": (0 if h in (9, 10) else 4),
        }
        for h in range(24)
    ],
    "six_hour": [],
    "eligible_calls": 2008,
    "coverage": {"ratio": 0.45},
    "excluded_calls_by_reason": {"no_stream_events": 40},
    "missing_timing_calls": 12,
    "first_measured_at": 1_760_000_000_000,
    "last_measured_at": 1_760_800_000_000,
    "days_with_measurements": 6,
    "selected_days": 7,
    "observed_range_status": "partial",
}

# --------------------------------------------------------------------------- #
# the fake service
# --------------------------------------------------------------------------- #


class FakeApi:
    """Every read the TUI can make, answered from a fixture.

    Set an attribute to change an answer, put an exception in it to fail that
    read, or install ``hooks["usage"] = fn`` to inspect or delay the call itself.
    ``calls`` records every invocation with its arguments, which is how the
    tests check the REQUEST (dates, cache_only, exact speed keys) rather than
    only the picture.
    """

    def __init__(self) -> None:
        self.usage = copy.deepcopy(USAGE)
        self.active = copy.deepcopy(ACTIVE)
        self.sessions = copy.deepcopy(SESSIONS_BY_TOOL)
        self.failing_sources: tuple[str, ...] = ()
        self.session_detail = copy.deepcopy(SESSION_DETAIL)
        self.session_speed = copy.deepcopy(SESSION_SPEED)
        self.stats = copy.deepcopy(STATS_TRAILING)
        self.insights = copy.deepcopy(INSIGHTS)
        self.speed_across = copy.deepcopy(SPEED_ACROSS)
        self.speed_trend = copy.deepcopy(SPEED_TREND)
        self.speed_hour = copy.deepcopy(SPEED_HOUR)
        self.job_status = {"state": "building", "completed_inputs": 3, "total_inputs": 9}
        self.pricing = copy.deepcopy(PRICING_DB)
        self.quota = copy.deepcopy(QUOTA_STATE)
        self.quota_history = copy.deepcopy(QUOTA_HISTORY)
        self.poll_result: object = {"ok": True}
        self.follow_events: list[dict] = []
        self.status: dict[str, str] = {}
        self.hooks: dict[str, object] = {}
        self.calls: list[tuple] = []
        # The quota poll is the TUI's one network mutation path: count it.
        self.calls_poll: list[dict] = []

    # -- plumbing ----------------------------------------------------------
    def seam(self, name: str, pick):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            hook = self.hooks.get(name)
            if hook is not None:
                produced = hook(*args, **kwargs)
                if isinstance(produced, BaseException):
                    raise produced
                if produced is not None:
                    return produced
            payload = pick(*args, **kwargs)
            if isinstance(payload, BaseException):
                raise payload
            return FetchOutcome(
                value=copy.deepcopy(payload),
                status=self.status.get(name, "hit"),
                age_seconds=1.0,
            )

        return call

    def called(self, name: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == name]

    def last(self, name: str):
        for entry in reversed(self.calls):
            if entry[0] == name:
                return entry
        return None

    def follow(self, job_id, on_event, **kwargs) -> int:
        """Stand in for the SSE reader: 0 events means "there is no service"."""
        for event in self.follow_events:
            on_event(event)
        return len(self.follow_events)

    def sessions_for(self, tool, *args, **kwargs):
        if tool in self.failing_sources:
            raise RuntimeError(f"{tool} history is unreadable")
        if tool in self.sessions:
            return self.sessions[tool]
        return {"tool": tool, "tool_label": str(tool), "period": "today", "sessions": []}

    def stats_for(self, *args, **kwargs):
        """Answer /api/stats for the window that was actually asked for.

        One payload for every year would let the pane paint 2026 days under a
        2025 header and look fine. A named year gets that year's dates.
        """
        year = args[0] if args else kwargs.get("year")
        payload = copy.deepcopy(self.stats)
        if year is not None:
            for entry in payload.get("contributions") or []:
                date = str(entry.get("date") or "")
                if len(date) == 10 and date[4] == "-":
                    entry["date"] = f"{int(year)}{date[4:]}"
        return payload

    def speed_for(self, *args, **kwargs):
        view = kwargs.get("view")
        if view == "trend":
            return self.speed_trend
        if view == "time-of-day":
            return self.speed_hour
        return self.speed_across


@pytest.fixture
def api(monkeypatch):
    """Patch every fetch seam in tokdash.tui.app with a FakeApi answer."""
    fake = FakeApi()
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
        monkeypatch.setattr(app_mod, name, fake.seam(key, pick), raising=True)
    monkeypatch.setattr(app_mod, "follow_output_speed_job", fake.follow, raising=True)
    monkeypatch.setattr(app_mod, "db_summary", lambda: "test db · 0 rows", raising=True)
    monkeypatch.setattr(app_mod, "ensure_usage_db_compatible", lambda: None, raising=True)
    monkeypatch.setattr(app_mod, "_local_today", lambda: TODAY, raising=True)
    import tokdash.cli as cli_mod

    def poll(**kwargs):
        fake.calls_poll.append(dict(kwargs))
        if isinstance(fake.poll_result, BaseException):
            raise fake.poll_result
        return copy.deepcopy(fake.poll_result)

    monkeypatch.setattr(cli_mod, "_quota_poll_once", poll, raising=True)
    return fake


# --------------------------------------------------------------------------- #
# pilot helpers
# --------------------------------------------------------------------------- #


def run(api, body, size=(120, 36)):
    """One pilot run. House style: plain asyncio.run, no plugin."""
    app = TokdashApp()

    async def _main():
        async with app.run_test(size=size) as pilot:
            await quiesce(pilot, 0.6)
            await body(app, pilot)
        return app

    asyncio.run(_main())
    return app


_LIVE_WORKER = (WorkerState.PENDING, WorkerState.RUNNING)
_PENDING_FLAGS = ("_layout_required", "_scroll_required", "_repaint_required",
                  "_recompose_required", "_refresh_styles_required")


def _busy(app, horizon: float) -> bool:
    """Whether the app still holds work a step could be waiting on.

    That is an unfinished worker, an animation, a queued message, a layout or
    repaint not yet done, a ``call_after_refresh`` not yet run, or a one-shot
    timer no longer than ``horizon``. Interval timers (the 1 s status tick)
    never finish, and a longer one-shot (the 12 s stale re-read) would not have
    fired inside the fixed sleep this replaces either, so neither counts.
    These are Textual 8 internals; pyproject pins textual below 9.
    """
    if any(worker.state in _LIVE_WORKER for worker in app.workers):
        return True
    if app.animator._animations or app.animator._scheduled:
        return True
    nodes = [app]
    for screen in app.screen_stack:
        if screen._dirty_widgets or screen._callbacks:
            return True
        nodes.extend(screen.walk_children(with_self=True))
    for node in nodes:
        if not node._message_queue.empty():
            return True
        if any(getattr(node, flag, False) for flag in _PENDING_FLAGS):
            return True
        for timer in list(node._timers):
            task = timer._task
            if timer._repeat == 0 and task is not None and not task.done() \
                    and timer._interval <= horizon:
                return True
    return False


async def quiesce(pilot, budget: float):
    """Return once the app is idle, or after ``budget`` seconds at the most.

    Idle means three polls running with nothing in ``_busy`` and every message
    the screen was sent handled; the repeat catches a ``call_after_refresh``
    that a finished worker leaves behind. The budget is the fixed sleep this
    used to be, so a step that parks a worker on purpose (a gated fetch) still
    waits exactly that long -- and every other step stops paying for it.

    Only a busy poll sleeps, to hand the worker threads the GIL; an idle one
    just drains the screen. A fixed sleep per poll would cost each step three
    timer ticks, which on Windows are ~15 ms however short the request.
    """
    deadline = time.monotonic() + budget
    quiet = 0
    busy = True
    while quiet < 3 and time.monotonic() < deadline:
        await pilot.pause(0.002 if busy else 0)
        busy = _busy(pilot.app, budget)
        quiet = 0 if busy else quiet + 1


async def settle(pilot, times=3):
    """Let the step settle: ``times`` quarter-seconds is the cap, not the cost."""
    await quiesce(pilot, 0.25 * times)


def screen_lines(app):
    """The screen as the terminal would show it, one string per row."""
    return [strip.text for strip in app.screen._compositor.render_strips(app.screen.size)]


def visible(app, selector: str) -> str:
    """The text a widget actually shows, clipped exactly as the terminal clips it.

    Reading the composed screen rather than the widget's model means these
    assertions fail when something is scrolled out, hidden or too wide -- which
    is the difference between "the value is in the object" and "the user can see
    the value".
    """
    widget = app.query_one(selector)
    if not widget.display:
        return ""
    region = widget.region
    lines = screen_lines(app)
    return "\n".join(
        line[region.x:region.right] for line in lines[region.y:region.bottom]
    )


def rows_of(table: DataTable) -> list[list[str]]:
    """Every row of a table as plain strings.

    A source cell is a badge (app._source_cell): the harness's own tint on an
    accent edge, then the label. The edge is decoration over the same word, so
    it comes off here and a test can ask what a row SAYS. The decoration is
    guarded separately, in test_each_source_keeps_its_own_colour_in_the_tables.
    """
    edge = app_mod._BADGE_EDGE
    return [[str(cell).removeprefix(edge)
             for cell in table.get_row_at(row)]
            for row in range(table.row_count)]


def headers_of(table: DataTable) -> list[str]:
    return [str(column.label) for column in table.columns.values()]


def widths_of(table: DataTable) -> list[int]:
    return [int(column.width or 0) for column in table.columns.values()]


def press(app, widget_id: str):
    app.query_one(f"#{widget_id}", Button).press()


def line_text(app) -> str:
    """The whole visible screen, for 'the note says X' style assertions."""
    return "\n".join(screen_lines(app))

# --------------------------------------------------------------------------- #
# Models: one aggregate table, one ranking per glance
# --------------------------------------------------------------------------- #


def test_models_aggregate_one_row_per_normalized_model(api):
    """The same normalized name in three harnesses is ONE row, with no Harness
    column and no second leaderboard."""

    async def body(app, pilot):
        app.action_show_pane("overview")
        press(app, "ov-tab-models")
        await settle(pilot)
        table = app.query_one("#ov-table", DataTable)
        rows = rows_of(table)
        names = [row[0] for row in rows]
        assert names.count(SHARED_MODEL) == 1, rows
        assert len(names) == 3, rows  # combined_models has three merged rows
        assert not any("harness" in head.lower() for head in headers_of(table))
        shared = next(row for row in rows if row[0] == SHARED_MODEL)
        assert shared[1] == "800.0K"       # the merged token sum, not one source
        assert shared[2] == "80.0%"        # against the GLOBAL total_tokens
        assert shared[3] == "$11.50"
        # The pane carries ONE table: the duplicate cost leaderboard is gone.
        assert len(app.query("#pane-overview DataTable")) == 1
        assert "by cost" not in visible(app, "#ov-cards").lower()

    run(api, body)


def test_cost_is_a_sort_of_the_same_table(api):
    async def body(app, pilot):
        press(app, "ov-tab-models")
        await settle(pilot)
        by_tokens = rows_of(app.query_one("#ov-table", DataTable))
        assert by_tokens[0][0] == SHARED_MODEL
        app.action_cycle_sort()  # tokens -> cost
        await settle(pilot, 2)
        by_cost = rows_of(app.query_one("#ov-table", DataTable))
        assert by_cost[0][0] == "claude-opus-4-7", by_cost
        assert sorted(row[0] for row in by_cost) == sorted(row[0] for row in by_tokens)
        assert len(app.query("#pane-overview DataTable")) == 1

    run(api, body)


def test_most_used_card_follows_tokens_under_a_cost_sort(api):
    async def body(app, pilot):
        press(app, "ov-tab-models")
        await settle(pilot)
        while app._ov_sort != "cost":
            app.action_cycle_sort()
            await pilot.pause(0.2)
        cards = visible(app, "#ov-cards")
        assert SHARED_MODEL in cards, cards
        assert "claude-opus-4-7" not in cards  # the priciest model is not "most used"
        assert "token-ranked" in cards or "800.0K tokens" in cards

    run(api, body)


def test_search_filters_rows_but_not_the_denominator(api):
    async def body(app, pilot):
        press(app, "ov-tab-models")
        await settle(pilot)
        app._ov_search = "gpt-5-mini"
        app._paint_overview()
        await settle(pilot, 1)
        rows = rows_of(app.query_one("#ov-table", DataTable))
        assert [row[0] for row in rows] == ["gpt-5-mini"]
        assert rows[0][2] == "5.0%", rows  # 50,000 of the global million
        assert "1 of 3" in visible(app, "#ov-table"), headers_of(app.query_one("#ov-table", DataTable))

    run(api, body)


def test_harnesses_keep_measured_zero_apart_from_missing(api):
    async def body(app, pilot):
        app.action_show_pane("overview")
        await settle(pilot)
        table = app.query_one("#ov-table", DataTable)
        rows = {row[0]: row for row in rows_of(table)}
        time_cell = -1  # Est. time is the last column in both shapes
        assert rows["Codex"][time_cell] == "0s", rows        # measured zero
        assert rows["OpenClaw"][time_cell] == "—", rows       # never read
        assert rows["OpenClaw"][1] == "100.0K"               # tokens survive a time gap
        assert "kimi" in visible(app, "#ov-note")

    run(api, body)


def test_harness_detail_names_its_source_and_keeps_kpis_global(api):
    async def body(app, pilot):
        table = app.query_one("#ov-table", DataTable)
        table.focus()
        await pilot.press("enter")
        await settle(pilot)
        assert app._ov_selected == "codex"
        header = headers_of(table)[0]
        assert "Codex" in header and TODAY.isoformat() in header, header
        assert rows_of(table)  # apps["codex"].models
        assert "stay" in visible(app, "#ov-detail").lower() or "global" in visible(app, "#ov-detail").lower()
        await pilot.press("escape")
        await settle(pilot, 1)
        assert app._ov_selected is None

    run(api, body)


def test_activity_strip_reports_the_buckets_the_payload_carries(api):
    async def body(app, pilot):
        await settle(pilot)
        text = visible(app, "#ov-activity")
        assert "2 hour buckets" in text, text
        assert "2026-10-18T09" in text
        assert "server local time" in text     # never the client's guessed zone
        assert "0 tokens" in text              # a real zero hour stays visible

    run(api, body)


def test_long_range_omits_the_chart_instead_of_inventing_one(api):
    async def body(app, pilot):
        api.usage = {**USAGE, "sparkline": None}
        app.action_set_range("all")
        await settle(pilot)
        assert app._win.request() == ("all", None, None)
        assert "Activity chart unavailable" in visible(app, "#ov-activity")
        # The totals stay; only the chart is withheld.
        assert "1.0M" in visible(app, "#ov-cards")

    run(api, body)


# --------------------------------------------------------------------------- #
# One shared range, and no stale paint under a new label
# --------------------------------------------------------------------------- #


def test_range_shortcuts_and_stepping(api):
    async def body(app, pilot):
        yesterday = TODAY - datetime.timedelta(days=1)
        assert app._win.request() == ("today", TODAY.isoformat(), TODAY.isoformat())
        app.action_set_range("yesterday")
        await settle(pilot, 1)
        assert app._win.request() == ("today", yesterday.isoformat(), yesterday.isoformat())
        app.action_set_range("month")
        await settle(pilot, 1)
        app.action_shift_range(-1)
        await settle(pilot, 1)
        moved = app._win
        assert moved.date_to < TODAY
        assert moved.date_from.day == 1 and moved.date_to.day == 30, moved
        app.action_shift_range(1)
        await settle(pilot, 1)
        assert app._win.date_to == TODAY  # back to the current PARTIAL month
        app.action_set_range("all")
        await settle(pilot, 1)
        app.action_shift_range(-1)
        assert app._win.all_time and app._win.request() == ("all", None, None)

    run(api, body)


def test_more_picker_resolves_a_preset_to_explicit_dates(api):
    async def body(app, pilot):
        press(app, "range-more")
        await settle(pilot, 1)
        items = list(app.screen.query("ListItem"))
        assert len(items) == 11  # the four shortcuts and the seven windows behind them
        # "Last Month" is a COMPLETE month, never the current one a day short.
        app._on_preset("lastmonth")
        await settle(pilot, 1)
        assert app._win.date_from == datetime.date(2026, 9, 1)
        assert app._win.date_to == datetime.date(2026, 9, 30)
        period, date_from, date_to = app._win.request()
        assert (date_from, date_to) == ("2026-09-01", "2026-09-30")
        assert period != "lastMonth"  # never a label the resolver would forget

    # A 120-column strip has room for every preset, so "More..." never appears;
    # the overflow picker is an 80-column reality.
    run(api, body, size=(80, 24))


def test_a_late_reply_cannot_be_painted_under_new_dates(api):
    """The generation guard: switch ranges mid-flight and the abandoned answer
    must never reach the screen."""
    import time

    def stamp(*args, **kwargs):
        _, date_from, date_to = args[0], args[1], args[2]
        time.sleep(0.5)
        return FetchOutcome(
            value={**USAGE, "range": {"from": date_from, "to": date_to,
                                      "days": 1, "recognized": True}},
            status="hit", age_seconds=0.0,
        )

    api.hooks["usage"] = stamp

    async def body(app, pilot):
        await settle(pilot, 1)  # a slow Today load is in flight
        app.action_set_range("yesterday")
        await settle(pilot, 4)
        yesterday = (TODAY - datetime.timedelta(days=1)).isoformat()
        assert app._win.date_from.isoformat() == yesterday
        assert app._ov_usage["range"]["from"] == yesterday, app._ov_usage["range"]
        strip = visible(app, "#range-dates")  # the ONE shared window label
        assert yesterday in strip, strip
        assert TODAY.isoformat() not in strip, strip

    run(api, body)


def test_stale_snapshot_is_said_out_loud(api):
    async def body(app, pilot):
        await settle(pilot)
        api.status["usage"] = "stale"
        app.action_refresh()
        await settle(pilot, 4)
        assert "last computed snapshot" in visible(app, "#ov-note")

    run(api, body)

# --------------------------------------------------------------------------- #
# Sessions: one merged list, partial truth, whole-session detail
# --------------------------------------------------------------------------- #

SESSION_SOURCES = ("codex", "claude", "kimi")  # kimi's source is unreadable


def _sessions_body(app, pilot, sources=SESSION_SOURCES):
    app._sn_sources = sources
    app.action_show_pane("sessions")


def test_sessions_fan_out_keeps_partial_truth(api):
    """A source that did not answer is named, not counted as zero sessions."""

    async def body(app, pilot):
        api.failing_sources = ("kimi",)
        _sessions_body(app, pilot)
        await settle(pilot, 4)
        note = visible(app, "#sn-note")
        # The count that stays is the one that is NEWS: three tools were asked,
        # two answered. (Which tools are in force is the head's line to say.)
        assert "2 of 3 tools answered" in note, note
        assert "Partial" in note and "kimi" not in note.split("Partial")[1][:3]
        assert "unavailable" in note and "r Retry" in note, note
        rows = rows_of(app.query_one("#sn-table", DataTable))
        assert len(rows) == 3, rows  # codex 2 + claude 1; the failure adds none

    run(api, body)


def test_identical_session_ids_in_two_sources_stay_two_rows(api):
    """Identity is the (tool, session_id) PAIR, never the bare id."""

    async def body(app, pilot):
        api.failing_sources = ()
        _sessions_body(app, pilot, ("codex", "claude"))
        await settle(pilot, 4)
        table = app.query_one("#sn-table", DataTable)
        keys = [k.value for k in table.rows.keys()]
        assert ("codex", "shared-id") in keys, keys
        assert ("claude", "shared-id") in keys, keys
        assert len(keys) == len(set(keys))
        # The two rows are visibly different sessions of different sources.
        rows = rows_of(table)
        # The session cell is "project / name  id" (the id rides after the name
        # so two tools' identical ids never read as one session), so the filter
        # reads the name the cell starts with.
        assert {"Codex", "Claude Code"} == {
            row[0] for row in rows
            if row[1].split()[0] in ("other", "reports")
        }, rows

    run(api, body)


def test_session_detail_is_the_whole_session_and_asks_for_no_dates(api):
    """/api/session takes tool + id only; the pane must say so and send no range."""

    async def body(app, pilot):
        api.failing_sources = ()
        _sessions_body(app, pilot, ("codex", "claude"))
        await settle(pilot, 4)
        table = app.query_one("#sn-table", DataTable)
        table.focus()
        # The list is grouped by tool, so the newest row of the window is not
        # necessarily row 0: move to the session this test is about by key.
        position = next(
            index for index, stored in enumerate(table.rows.keys())
            if getattr(stored, "value", stored) == ("claude", "shared-id")
        )
        table.move_cursor(row=position)
        await pilot.press("enter")
        await settle(pilot, 4)
        assert app._sn_detail_key == ("claude", "shared-id"), app._sn_detail_key
        detail = visible(app, "#sn-detail")
        assert "Entire session" in detail, detail
        assert "Claude Code · shared-id" in detail
        # The detail read carried no dates at all.
        name, args, kwargs = api.last("session_detail")
        assert list(args) == ["claude", "shared-id"], args
        assert not kwargs.get("date_from") and not kwargs.get("date_to")
        # Timing was asked for the same scope: tool + id, no dates.
        speed = api.last("session_speed")
        assert speed is not None and list(speed[1]) == ["claude", "shared-id"], speed
        # A turn with no timestamp prints a dash, never an invented time.
        assert "—" in detail

    run(api, body)


def test_search_box_takes_the_letters_that_are_also_shortcuts(api):
    """/ then typing "q", "r" or a digit must TEXT, not quit, reload or jump."""

    async def body(app, pilot):
        api.failing_sources = ()
        _sessions_body(app, pilot, ("codex", "claude"))
        await settle(pilot, 4)
        app.action_begin_search()
        await settle(pilot, 1)
        box = app.query_one("#sn-search", Input)
        assert box.display and box.has_focus
        for ch in "qr5t":
            await pilot.press(ch)
        await settle(pilot, 1)
        assert box.value == "qr5t", box.value
        rows = rows_of(app.query_one("#sn-table", DataTable))
        assert rows == [], rows  # nothing matches, and the app never quit
        await pilot.press("backspace", "backspace", "backspace", "backspace")
        await settle(pilot, 1)
        assert len(rows_of(app.query_one("#sn-table", DataTable))) == 3

    run(api, body)


def test_sessions_and_speed_keep_separate_source_selections(api):
    """Sessions fans out over its own sources; Speed filters by its own."""

    async def body(app, pilot):
        api.failing_sources = ()
        # Both selections are made the way a reader makes them: h, then the
        # chooser. Setting the two fields by hand would prove the model holds two
        # values and say nothing about whether either control can be reached.
        app.action_show_pane("sessions")
        await settle(pilot, 3)
        await pilot.press("h")
        await settle(pilot, 1)
        await pilot.press("down")  # the first supported source
        await pilot.press("enter")
        await settle(pilot, 4)
        sessions_choice = app._sn_sources
        assert sessions_choice == (SESSION_TOOLS[0],), sessions_choice
        app.action_show_pane("speed")
        await settle(pilot, 3)
        await pilot.press("h")
        await settle(pilot, 1)
        await pilot.press("down")  # "All measured sources"
        await pilot.press("down")  # codex, which has rows
        await pilot.press("down")  # kimi, which has NONE -- and is still offered
        await pilot.press("enter")
        await settle(pilot, 4)
        assert app._sp_source == "kimi", app._sp_source
        sent = api.last("speed")
        assert sent[2]["source"] == "kimi", sent
        assert "source kimi" in visible(app, "#sp-note"), visible(app, "#sp-note")
        # Back to Sessions: its fan-out is untouched by Speed's filter.
        app.action_show_pane("sessions")
        await settle(pilot, 3)
        assert app._sn_sources == sessions_choice
        # Sessions still stands where the reader left it: one tool in force, and
        # the head says so in the same words the chips use.
        assert "session tools" in visible(app, "#sn-scope"), visible(app, "#sn-scope")

    run(api, body)

# --------------------------------------------------------------------------- #
# Stats: an annual view that owns its window
# --------------------------------------------------------------------------- #

TRAILING_FROM = "2025-10-19"


def test_stats_names_its_own_window_and_ignores_the_shared_range(api):
    async def body(app, pilot):
        app.action_set_range("lastmonth")
        await settle(pilot, 1)
        app.action_show_pane("stats")
        await settle(pilot, 3)
        head = visible(app, "#st-scope")
        assert "Trailing 365 Days" in head, head
        assert f"{TRAILING_FROM} .. {TODAY.isoformat()}" in head, head
        # The shared range did not leak in, and Stats did not move it.
        assert app._win.date_to == datetime.date(2026, 9, 30)
        assert api.last("stats")[1][0] is None  # None = the route's trailing year

    run(api, body)


def test_stats_year_picker_walks_the_ladder_and_asks_for_that_year(api):
    """The key drives the ladder both ways, and every rung asks for its own year.

    The year the reader is STANDING IN used to be unreachable: 'p' stepped from
    the trailing window straight into last year and every press after that went
    further back, with nothing to come back with.
    """

    async def body(app, pilot):
        app.action_show_pane("stats")
        await settle(pilot, 3)
        ladder, heads = [], []
        for _ in range(4):
            await pilot.press("p")
            await settle(pilot, 2)
            ladder.append(app._stats_win.request())
            heads.append(visible(app, "#st-scope"))
        assert ladder == [2026, 2025, 2024, 2023], ladder
        assert api.last("stats")[1][0] == 2023
        # The in-progress year is named for the days it can actually answer.
        assert "2026 to date" in heads[0], heads[0]
        assert "2023  (2023-01-01 .. 2023-12-31)" in heads[3], heads[3]
        # 'P' walks back up and comes to rest on the trailing rung: a year the
        # reader has not lived through is not a window.
        for _ in range(6):
            await pilot.press("P")
            await settle(pilot, 2)
        assert app._stats_win.request() is None
        assert "Trailing 365 Days" in visible(app, "#st-scope")

    run(api, body)


def test_stats_reads_messages_and_never_the_deprecated_sessions(api):
    """stats.messages is the field; stats.sessions is a legacy alias of it."""

    async def body(app, pilot):
        app.action_show_pane("stats")
        await settle(pilot, 3)
        cards = visible(app, "#st-cards")
        assert "TOTAL MESSAGES" in cards and "5,000" in cards, cards
        assert "777" not in cards, cards
        assert "annual window only" in visible(app, "#st-summary")

    run(api, body)


def test_a_lapsed_streak_is_not_reported_as_current(api):
    async def body(app, pilot):
        api.stats = copy.deepcopy(STATS_TRAILING)
        api.stats["stats"]["current_streak"] = 0
        app.action_show_pane("stats")
        await settle(pilot, 3)
        summary = visible(app, "#st-summary")
        assert "no streak ending today or yesterday" in summary, summary
        cards = visible(app, "#st-cards")
        assert "LONGEST STREAK" in cards and "30" in cards, cards

    run(api, body)


def test_every_month_of_the_annual_window_is_reachable_at_80_columns(api):
    """No month is silently trimmed to make the year fit an 80-column pane."""

    async def body(app, pilot):
        app.action_show_pane("stats")
        await settle(pilot, 3)
        seen = {visible(app, "#st-calendar")}
        for _ in range(20):  # walk to the oldest month
            app.action_shift_range(-1)
            await settle(pilot, 1)
            seen.add(visible(app, "#st-calendar"))
        assert any("October 2025" in text for text in seen), sorted(seen)[-1]
        for _ in range(60):  # and back to the newest
            app.action_shift_range(1)
            await settle(pilot, 1)
            seen.add(visible(app, "#st-calendar"))
        assert any("October 2026" in text for text in seen)
        # Stepping never leaves the window: the rungs are counted, not guessed.
        assert app._stats_month is None or 0 <= app._stats_month <= 13

    run(api, body, size=(80, 24))


def test_enter_on_a_day_moves_the_shared_window_and_opens_the_overview(api):
    async def body(app, pilot):
        app.action_show_pane("stats")
        await settle(pilot, 3)
        table = app.query_one("#st-days", DataTable)
        rows = rows_of(table)
        assert rows, "the day table is empty"
        wanted = rows[0][0]
        table.focus()
        await pilot.press("enter")
        await settle(pilot, 3)
        assert app._active_pane() == "overview"
        assert app._win.custom and app._win.date_from == app._win.date_to
        assert app._win.date_from.isoformat() == wanted, (app._win, wanted)
        assert wanted in visible(app, "#range-dates")
        # Overview then asks for exactly that one day.
        sent = api.last("usage")  # fetch_usage(period, date_from, date_to, refresh=)
        assert (sent[1][1], sent[1][2]) == (wanted, wanted), sent

    run(api, body)

# --------------------------------------------------------------------------- #
# Speed: server measurements, comparison keys, and no arithmetic of our own
# --------------------------------------------------------------------------- #


def test_speed_keeps_two_measurement_methods_as_two_rows(api):
    """Same source, same model, two timing kinds: never averaged into one."""

    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 4)
        table = app.query_one("#sp-table", DataTable)
        rows = rows_of(table)
        assert len(rows) == 2, rows
        rates = sorted(row[headers_of(table).index("tok/s")] for row in rows)
        assert rates == ["41.0", "88.4"], rates
        keys = [k.value for k in table.rows.keys()]
        assert len(set(keys)) == 2, keys  # the comparison keys stay distinct
        assert "separate methods stay separate rows" in visible(app, "#sp-detail")
        note = visible(app, "#sp-note")
        assert "cache ready" in note, note
        assert "kimi (timing_unavailable)" in note, note

    run(api, body)


def test_speed_combined_uses_the_servers_union_rate_and_drops_the_harness_column(api):
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 4)
        press(app, "sp-lens-combined")
        await settle(pilot, 3)
        table = app.query_one("#sp-table", DataTable)
        heads = headers_of(table)
        assert "Harness" not in heads, heads
        assert "Sources" in heads, heads
        rows = rows_of(table)
        assert len(rows) == 1, rows  # the payload's merged model
        assert rows[0][heads.index("tok/s")] == "101.2", rows
        assert "claude,codex" in rows[0][heads.index("Sources")], rows
        detail = visible(app, "#sp-detail")
        assert "UNION of busy time" in detail, detail
        assert "no pooled per-stream rate exists" in detail, detail

    run(api, body)


def test_speed_trend_keeps_null_buckets_as_gaps(api):
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 4)
        press(app, "sp-mode-trend")
        await settle(pilot, 4)
        sent = api.last("speed")
        assert sent[2]["view"] == "trend", sent
        assert sent[2]["cache_only"] is True, sent  # a follow-up never queues work
        table = app.query_one("#sp-table", DataTable)
        rows = rows_of(table)
        assert len(rows) == 2, rows
        rate_col = headers_of(table).index("tok/s per stream")
        assert rows[0][rate_col] == "88.0", rows
        assert rows[1][rate_col] == "\u2014", rows  # null stays a gap, not a zero
        detail = visible(app, "#sp-detail")
        assert "duration-weighted" in detail and "30+ responses" in detail, detail

    run(api, body)


def test_hour_of_day_asks_for_one_exact_key_never_a_keyless_request(api):
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 4)
        press(app, "sp-mode-hour")
        await settle(pilot, 4)
        sent = api.last("speed")
        assert sent[2]["view"] == "time-of-day", sent
        assert sent[2]["source"] == "codex", sent
        assert sent[2]["model"] == SHARED_MODEL, sent
        assert sent[2]["measurement_kind"] == "response_window", sent
        assert sent[2]["token_basis"] == "output_including_reasoning", sent
        table = app.query_one("#sp-table", DataTable)
        rows = rows_of(table)
        assert len(rows) == 24, rows  # the server's 24 hours, gaps included
        rates = {row[0]: row[1] for row in rows}
        assert rates["09:00"] == "88.4", rates
        assert rates["10:00"] == "0.0", rates      # a measured zero stays zero
        assert rates["03:00"] == "\u2014", rates   # and an unmeasured hour is null
        detail = visible(app, "#sp-detail")
        assert "days with measurements: 6" in detail, detail
        assert "an hour that measured nothing is a gap" in detail, detail

    run(api, body)


def test_build_progress_from_the_job_events_reaches_the_note(api):
    """A progress event is a NOTE with numbers, and never a re-read.

    ``_on_speed_event`` is the very entry point the SSE reader calls through
    ``call_from_thread``, so this is the stream's own path into the pane.
    """

    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 4)
        assert len(api.called("speed")) == 1
        # What following a queued build actually looks like: the pane knows the
        # job it subscribed for and the subscription token it subscribed under
        # (an event from an older build, or a stale stream, is dropped).
        app._sp_following = True
        app._sp_follow_job = "job-9"
        app._sp_follow_epoch = app._sp_follow_token
        app._on_speed_event({"id": "job-9", "state": "building",
                             "completed_inputs": 3, "total_inputs": 9})
        await settle(pilot, 2)
        note = visible(app, "#sp-note")
        assert "building 3/9" in note, note     # the numbers the server sent
        assert "last-good rows shown" in note, note
        assert len(api.called("speed")) == 1, api.called("speed")  # progress is free
        app._on_speed_event({"id": "job-9", "state": "error"})
        await settle(pilot, 2)
        assert "build failed" in visible(app, "#sp-note")

    run(api, body)


def test_a_build_stream_that_ends_early_is_a_lost_connection_not_a_rebuild_loop(api):
    """A stream that closes mid-build is named once, and never re-subscribed."""

    async def body(app, pilot):
        api.speed_across = copy.deepcopy(SPEED_ACROSS)
        api.speed_across["cache"] = {"state": "building", "job_id": "job-7",
                                     "updated_at": None}
        api.follow_events = [{"state": "building", "completed_inputs": 4,
                              "total_inputs": 9}]  # and then the stream just stops
        app.action_show_pane("speed")
        await settle(pilot, 5)
        note = visible(app, "#sp-note")
        assert "update connection ended" in note, note
        assert "r Reload" in note, note
        reads = len(api.called("speed"))
        follows = len([c for c in api.calls if c[0] == "job_status"])
        app._paint_speed()   # every later paint would re-arm the subscription
        await settle(pilot, 5)
        assert len(api.called("speed")) == reads, "a dropped stream was re-subscribed"
        assert follows == 0  # no status polling behind the stream's back either

    run(api, body)


def test_build_completion_re_reads_once_and_stops_claiming_a_build(api):
    """The completion re-read is cache-only, so it cannot re-queue the build."""

    async def body(app, pilot):
        rebuilt = copy.deepcopy(SPEED_ACROSS)  # what the service holds once built
        api.speed_across = copy.deepcopy(SPEED_ACROSS)
        api.speed_across["cache"] = {"state": "building", "job_id": "job-7",
                                     "updated_at": None}
        api.follow_events = [
            {"state": "building", "completed_inputs": 3, "total_inputs": 9},
            {"state": "ready", "completed_inputs": 9, "total_inputs": 9},
        ]
        served: list[object] = []

        def serve(*args, **kwargs):
            # The build lands between reads: the first read is the building
            # snapshot, the cache-only re-read is shown the rebuilt one.
            served.append(kwargs.get("cache_only"))
            if len(served) < 2:
                return None
            return FetchOutcome(value=copy.deepcopy(rebuilt), status="hit",
                                age_seconds=1.0)

        api.hooks["speed"] = serve
        app.action_show_pane("speed")
        await settle(pilot, 5)
        speed_reads = api.called("speed")
        assert len(speed_reads) == 2, speed_reads   # completion re-read EXACTLY once
        assert served[0] is False and served[1] is True, served
        last = speed_reads[-1][2]
        assert last["cache_only"] is True, last      # and can not queue a new build
        assert last["refresh"] is False, last
        note = visible(app, "#sp-note")
        assert "building" not in note, note          # the pane says it is finished
        assert "cache ready" in note, note

    run(api, body)


def test_manual_reload_asks_for_a_rebuild_and_cache_reads_never_do(api):
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 4)
        first = api.last("speed")[2]
        assert first["refresh"] is False, first
        assert first["cache_only"] is False, first
        app.action_refresh()
        await settle(pilot, 4)
        again = api.last("speed")[2]
        assert again["refresh"] is True, again
        assert again["cache_only"] is False, again  # never both: cache_only wins server-side
        press(app, "sp-mode-trend")  # a follow-up read is cache-only
        await settle(pilot, 4)
        trend = [c for c in api.called("speed") if c[2].get("view") == "trend"][-1]
        assert trend[2]["cache_only"] is True and trend[2]["refresh"] is False

    run(api, body)

# --------------------------------------------------------------------------- #
# Report: one page, two scans, and the denominators that go with them
# --------------------------------------------------------------------------- #


def test_report_agents_read_their_sessions_from_active_time(api):
    """The Sessions column is active-time's, and the clock is not the sum."""

    async def body(app, pilot):
        app.action_show_pane("report")
        await settle(pilot, 4)
        text = visible(app, "#rp-body")
        assert "Tokens 1.0M" in text, text  # usage scan
        assert "Agent  1h 00m clock · 1h 30m sum" in text, text
        rows = [ln for ln in text.splitlines() if ln.strip().startswith(("Codex", "Claude"))]
        assert len(rows) == 2, rows
        assert "3" in rows[0].split(), rows[0]      # by_tool.session_count
        assert "2" in rows[1].split(), rows[1]
        assert "0s" in rows[0], rows[0]              # codex measured zero time
        # A source active time could not read is absent, not a zero row.
        assert "kimi" not in text.lower(), text

    run(api, body)


def test_report_keeps_the_two_scans_denominators_apart(api):
    """Attribution divides by the analytics scan, the headline by the usage scan."""

    async def body(app, pilot):
        app.action_show_pane("report")
        await settle(pilot, 4)
        # The summary names the usage scan as the headline's denominator.
        summary = visible(app, "#rp-body")
        assert "Headline totals are the usage scan" in summary, summary
        press(app, "rp-tab-projects")
        await settle(pilot, 3)
        text = visible(app, "#rp-body")
        # Attribution divides the ANALYTICS scan, named above the rows it ranks.
        assert "shares divide the analytics scan total 900.0K" in text, text
        assert "tokdash" in text and "500.0K" in text, text
        assert "unattributed" in text and "200.0K" in text, text
        # The two scans do not add up, and the page says so instead of hiding it.
        assert "differs from the scan total by +100,000 tokens" in text, text

    run(api, body)


def test_report_when_uses_the_full_hour_grid_and_the_named_weekday(api):
    """The WHOLE facet: 24 hours, 7 named weekdays, and the share denominator.

    The piped report prints the three busiest hours and one weekday because a
    report is a summary. A PANE is where the rest of the facet lives, so the
    claim this test has to actually carry is that the quiet end of the day and
    the other six weekdays are reachable here -- not just the peak line.
    """

    async def body(app, pilot):
        app.action_show_pane("report")
        await settle(pilot, 4)
        press(app, "rp-tab-when")
        await settle(pilot, 3)
        text = visible(app, "#rp-body")
        assert "peak hour 09:00" in text, text
        assert "09:00" in text and "400.0K" in text, text
        # The weekday bucket key is "weekday" + "name"; a "day" key would read
        # as an empty facet and print a dash where Monday belongs.
        assert "busiest weekday Monday" in text, text
        assert "night share" in text, text
        # Every hour of the clock, including the ones nobody clicked on.
        for hour in range(24):
            assert f"{hour:02d}:00" in text, (hour, text)
        for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                    "Saturday", "Sunday"):
            assert day in text, (day, text)
        # Shares divide the analytics scan total, named above the rows.
        assert "analytics scan total" in text, text

    run(api, body)


def test_report_surfaces_a_source_that_only_half_read(api):
    """A window whose scan half-failed is never a quiet zero."""

    async def body(app, pilot):
        api.usage = copy.deepcopy(USAGE)
        api.usage["source_errors"] = [
            {"source": "kimi", "error": "unreadable"}, "grok",
        ]
        app.action_show_pane("report")
        await settle(pilot, 4)
        text = visible(app, "#rp-body")
        assert "2 source(s) failed this window: kimi, grok" in text, text

    run(api, body)


def test_report_names_a_soft_failure_rather_than_dashing_it_out(api):
    """The analytics facet failed to read; that is a sentence, not an em dash."""

    async def body(app, pilot):
        api.insights = RuntimeError("scan exploded")
        app.action_show_pane("report")
        await settle(pilot, 4)
        # Usage still paints (it is the hard source); the facet failure is a
        # warning and the facets print as dashes beside it.
        text = visible(app, "#rp-body")
        assert "analytics scan failed" in text, text

    run(api, body)


# --------------------------------------------------------------------------- #
# Quota: whose window, how old, and what was never observed
# --------------------------------------------------------------------------- #


def test_quota_folds_accounts_and_keeps_a_measured_zero(api):
    async def body(app, pilot):
        app.action_show_pane("quota")
        await settle(pilot, 4)
        table = app.query_one("#quota-table", DataTable)
        rows = rows_of(table)
        text = "\n".join(" | ".join(r) for r in rows)
        assert "claude@default" in text, text      # >1 account: the account travels
        assert "@academic" in text, text
        assert "62.3% left" in text, text          # 100 - 37.7 used
        assert "0.0% left" in text, text           # a measured zero, not a dash
        assert "unlimited" in text, text           # codex's unlimited 5-hour
        assert "· local" in text, text             # a log-read window may lag
        assert "unavailable" in text, text         # kimi: status-only row
        kimi = [r for r in rows if r[0] == "kimi"]
        assert kimi and kimi[0][2] == "\u2014", kimi  # never a zero-usage claim

    run(api, body)


def test_quota_names_the_master_switch_and_the_missing_history(api):
    async def body(app, pilot):
        api.quota = copy.deepcopy(QUOTA_STATE)
        api.quota["enabled"] = False
        api.quota_history = {"series": []}
        app.action_show_pane("quota")
        await settle(pilot, 4)
        note = visible(app, "#quota-note")
        assert "quota tracking is off" in note, note
        assert "no 24h quota history yet" in note, note

    run(api, body)


def test_quota_names_network_polling_that_was_never_consented(api):
    """Tracking on but nobody said yes: the rows are local, and it says so."""

    async def body(app, pilot):
        api.quota = copy.deepcopy(QUOTA_STATE)
        api.quota["consent"] = {k: False for k in api.quota["consent"]}
        app.action_show_pane("quota")
        await settle(pilot, 4)
        note = visible(app, "#quota-note")
        assert "network quota polling is not consented" in note, note

    run(api, body)


def test_the_only_quota_network_call_is_the_key_you_press(api):
    async def body(app, pilot):
        app.action_show_pane("overview")
        await settle(pilot, 3)
        app.action_poll_quota()          # nowhere near the Quota tab
        await settle(pilot, 2)
        assert not api.calls_poll, api.calls_poll
        app.action_show_pane("quota")
        await settle(pilot, 4)
        before = len(api.called("quota"))
        app.action_poll_quota()
        await settle(pilot, 5)
        assert api.calls_poll == [{"include_network": True}], api.calls_poll
        # The poll is followed by a state read, so the pane shows what the poll
        # stored rather than a success word over stale rows.
        assert len(api.called("quota")) > before, api.called("quota")
        assert api.last("quota")[2]["refresh"] is True, api.last("quota")

    run(api, body)


def test_a_disabled_poll_is_reported_as_disabled_not_as_a_success(api):
    async def body(app, pilot):
        api.poll_result = {"disabled": True}
        app.action_show_pane("quota")
        await settle(pilot, 4)
        app.action_poll_quota()
        await settle(pilot, 4)
        note = visible(app, "#quota-note")
        assert "quota tracking disabled" in note, note
        assert "poll ok" not in note, note

    run(api, body)


# --------------------------------------------------------------------------- #
# Pricing: a mirror of the price book, never an editor and never a guess
# --------------------------------------------------------------------------- #


def test_pricing_prints_a_missing_rate_as_a_gap_not_a_zero(api):
    async def body(app, pilot):
        app.action_show_pane("pricing")
        await settle(pilot, 4)
        table = app.query_one("#pr-table", DataTable)
        heads = headers_of(table)
        rows = {r[0]: r for r in rows_of(table)}
        full = rows[SHARED_MODEL]
        assert full[heads.index("Input")] == "1.2500", full
        # gpt-5-mini records input and output only. A rate that was never
        # recorded is not a free model, and 0.0000 would say it is.
        mini = rows["gpt-5-mini"]
        assert mini[heads.index("Cache read")] == "\u2014", mini
        assert mini[heads.index("Cache write")] == "\u2014", mini
        assert "read-only" in visible(app, "#pr-note")

    run(api, body)


def test_pricing_names_the_active_version_and_warns_about_an_override(api):
    async def body(app, pilot):
        app.action_show_pane("pricing")
        await settle(pilot, 4)
        note = visible(app, "#pr-note")
        assert "version 2026-09-01" in note, note
        assert "source override" in note, note
        # An override is not the shipped book: it replaces it and freezes it.
        assert "fully replaces the bundled baseline" in note, note
        assert "1 aliases" in note, note

    run(api, body)


def test_pricing_search_reaches_a_model_through_its_alias(api):
    async def body(app, pilot):
        app.action_show_pane("pricing")
        await settle(pilot, 4)
        box = app.query_one("#pr-search", Input)
        box.focus()
        box.value = "gpt-5-codex"          # the alias, not the canonical name
        await settle(pilot, 3)
        rows = rows_of(app.query_one("#pr-table", DataTable))
        assert [r[0] for r in rows] == [SHARED_MODEL], rows
        assert "1 of 2 models" in visible(app, "#pr-note")

    run(api, body)


# --------------------------------------------------------------------------- #
# Geometry: the same meaning at 120x36, 100x30 and 80x24, keys only
# --------------------------------------------------------------------------- #

SIZES = [(120, 36), (100, 30), (80, 24)]


@pytest.mark.parametrize("size", SIZES)
def test_every_view_opens_by_key_and_keeps_its_figures(api, size):
    async def body(app, pilot):
        for key in ("f1", "f2", "f3", "f4", "f5", "f6", "f7", "f1"):
            await pilot.press(key)
            await settle(pilot, 3)
        cards = visible(app, "#ov-cards")
        assert "1.0M" in cards, cards        # tokens, whole
        assert "$12.50" in cards, cards      # cost, whole
        assert "75.0%" in cards, cards       # cache hit, whole
        table = app.query_one("#ov-table", DataTable)
        assert table.row_count, "no harness rows at " + str(size)
        # Nothing is drawn past the last column the terminal has.
        for line in screen_lines(app):
            assert len(line) <= size[0], (size, line)

    run(api, body, size=size)


def test_fit_columns_drops_optionals_before_it_touches_a_number():
    spec = [
        ("name", "Name", 24, False), ("tokens", "Tokens", 8, False),
        ("cost", "Cost", 10, False), ("msgs", "Msgs", 8, True),
        ("time", "Est. time", 14, True),
    ]
    wide = fit_columns(spec, 120)
    assert [c[0] for c in wide] == [c[0] for c in spec], wide
    tight = fit_columns(spec, 46)
    keys = [c[0] for c in tight]
    assert "tokens" in keys and "cost" in keys, tight   # numbers keep their home
    assert "time" not in keys and "msgs" not in keys, tight
    assert all(width >= 6 for _, _, width in tight), tight
    assert sum(w for _, _, w in tight) + 2 * len(tight) <= 46, tight


def test_a_long_table_scrolls_and_the_cursor_stays_on_screen(api):
    """24 server hours in a 30-row terminal: the cursor never walks off-view."""

    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 4)
        press(app, "sp-mode-hour")
        await settle(pilot, 4)
        table = app.query_one("#sp-table", DataTable)
        table.focus()
        for _ in range(20):
            await pilot.press("down")
            await settle(pilot, 1)
        row = table.cursor_coordinate.row
        assert row >= 15, row  # the keyboard actually walked
        shown = visible(app, "#sp-table")
        label = str(table.get_row_at(row)[0])
        assert label in shown, (label, row, shown)

    run(api, body, size=(100, 30))


def test_closing_a_detail_returns_you_to_the_row_you_were_on(api):
    async def body(app, pilot):
        app._sn_sources = ("codex", "claude")
        app.action_show_pane("sessions")
        await settle(pilot, 4)
        table = app.query_one("#sn-table", DataTable)
        table.focus()
        table.move_cursor(row=2)
        await settle(pilot, 1)
        await pilot.press("enter")
        await settle(pilot, 4)
        assert app._sn_detail_key is not None
        await pilot.press("escape")
        await settle(pilot, 3)
        assert app._sn_detail_key is None
        assert table.cursor_coordinate.row == 2, table.cursor_coordinate
        assert app._sn_selected_key is not None

    run(api, body, size=(100, 30))


def test_a_monochrome_terminal_loses_the_tints_not_the_figures(api):
    async def body(app, pilot):
        app.no_color = True
        app._repaint_pane("overview")
        await settle(pilot, 3)
        cards = visible(app, "#ov-cards")
        assert "1.0M" in cards and "$12.50" in cards, cards
        assert "gpt-5.1-codex" in cards, cards   # the name, not a colour swatch
        cal = app.query_one("#st-calendar", Static)
        assert cal is not None

    run(api, body)


def test_the_live_window_is_named_in_exact_dates_at_every_width(api):
    async def body(app, pilot):
        dates = visible(app, "#range-dates")
        assert TODAY.isoformat() in dates, dates
        app._on_preset("7d")  # the chooser's answer: the row a pick hands back
        await settle(pilot, 2)
        dates = visible(app, "#range-dates")
        assert "2026-10-12" in dates and TODAY.isoformat() in dates, dates

    for size in SIZES:
        run(api, body, size=size)
from tokdash.tui.ranges import PRESETS as _ALL_PRESETS  # noqa: E402


# --------------------------------------------------------------------------- #
# startup: the range the command line asked for
# --------------------------------------------------------------------------- #


def _open(api, initial_period, body, size=(120, 36)):
    """Start the app the way ``tokdash tui --period <token>`` does, then look."""
    app = TokdashApp(initial_period=initial_period)

    async def _main():
        async with app.run_test(size=size) as pilot:
            await quiesce(pilot, 0.6)
            await settle(pilot, 3)
            await body(app, pilot)

    asyncio.run(_main())
    return app


def test_the_period_flag_opens_on_the_dates_it_names(api):
    """``tokdash tui --period week`` may not open on Today.

    run_tui validates the token and hands it to the app. Dropping it on the way
    in shows one day's figures under a claim of a week, which is the same
    silent truncation the HTTP resolver was fixed for.
    """
    async def body(app, pilot):
        dates = visible(app, "#range-dates")
        week_ago = (TODAY - datetime.timedelta(days=6)).isoformat()
        assert week_ago in dates and TODAY.isoformat() in dates, dates
        assert app._win.preset == "7d", app._win
        call = api.last("usage")
        assert call is not None
        assert call[1][:3] == ("today", week_ago, TODAY.isoformat()), call
        # the button for the live window is lit, so the strip agrees with it
        assert app.query_one("#range-7d", Button).has_class("-selected")

    _open(api, "week", body)


def test_a_period_length_the_presets_do_not_own_still_reaches_the_panes(api):
    """--period 14 is a trailing fortnight, not the nearest button's window."""
    async def body(app, pilot):
        dates = visible(app, "#range-dates")
        start = (TODAY - datetime.timedelta(days=13)).isoformat()
        assert start in dates and TODAY.isoformat() in dates, dates
        lit = [p.key for p in _ALL_PRESETS
               if app.query_one(f"#range-{p.key}", Button).has_class("-selected")]
        assert lit == [], lit
        call = api.last("usage")
        assert call[1][1] == start, call

    _open(api, "14", body)


def test_period_all_opens_on_every_recorded_session(api):
    async def body(app, pilot):
        assert app._win.all_time
        assert "All Time" in visible(app, "#range-dates")
        assert app.query_one("#range-all", Button).has_class("-selected")
        call = api.last("usage")
        assert call[1][:3] == ("all", None, None), call

    _open(api, "all", body)

# --------------------------------------------------------------------------- #
# selected state: a row of identical buttons must show which one is in force
# --------------------------------------------------------------------------- #

SELECTED_FILL = "#b9a7ff"  # the lavender fill of the approved design

RANGE_IDS = [f"range-{preset.key}" for preset in _ALL_PRESETS]


def lit(app, widget_ids: list[str]) -> list[str]:
    """Which of these buttons the terminal will paint as the current choice."""
    return [
        widget_id
        for widget_id in widget_ids
        if app.query_one(f"#{widget_id}", Button).has_class("-selected")
    ]


def test_the_range_strip_marks_the_preset_that_names_the_window(api):
    """Ten look-alike buttons are only usable if one of them looks chosen.

    Textual owns ``-active`` for a 200 ms press flash and removes it on a timer,
    so an app that paints ``Button.active`` shows NOTHING persistent: every
    preset looks as selected as every other, and the only clue to the live
    window is the date text. ``-selected`` is the app's own class.
    """

    async def body(app, pilot):
        assert lit(app, RANGE_IDS) == ["range-today"]
        press(app, "range-thisweek")  # a shortcut that is ON the strip
        await settle(pilot, 4)
        assert lit(app, RANGE_IDS) == ["range-thisweek"], lit(app, RANGE_IDS)
        await pilot.press("d")
        await settle(pilot, 3)
        assert lit(app, RANGE_IDS) == ["range-yesterday"], lit(app, RANGE_IDS)
        # Stepping off that preset's own window leaves no button lit on purpose:
        # the exact dates carry the window then, and a lit shortcut would lie.
        await pilot.press("right_square_bracket")
        await settle(pilot, 3)
        assert lit(app, RANGE_IDS) == [], lit(app, RANGE_IDS)

    run(api, body)


def test_every_button_group_marks_the_choice_it_is_showing(api):
    """Subview, report section, speed mode and speed lens all wear the fill."""

    overview = ["ov-tab-harnesses", "ov-tab-models"]
    sections = ["rp-tab-summary", "rp-tab-when", "rp-tab-projects"]
    modes = ["sp-mode-across", "sp-mode-trend", "sp-mode-hour"]
    lenses = ["sp-lens-stream", "sp-lens-combined"]

    async def body(app, pilot):
        assert lit(app, overview) == ["ov-tab-harnesses"]
        press(app, "ov-tab-models")
        await settle(pilot, 2)
        assert lit(app, overview) == ["ov-tab-models"], lit(app, overview)

        app.action_show_pane("report")
        await settle(pilot, 4)
        assert lit(app, sections) == ["rp-tab-summary"], lit(app, sections)
        press(app, "rp-tab-when")
        await settle(pilot, 2)
        assert lit(app, sections) == ["rp-tab-when"], lit(app, sections)

        app.action_show_pane("speed")
        await settle(pilot, 5)
        assert lit(app, modes) == ["sp-mode-across"], lit(app, modes)
        assert lit(app, lenses) == ["sp-lens-stream"], lit(app, lenses)
        press(app, "sp-mode-trend")
        await settle(pilot, 5)
        assert lit(app, modes) == ["sp-mode-trend"], lit(app, modes)
        assert lit(app, lenses) == ["sp-lens-stream"], lit(app, lenses)
        press(app, "sp-lens-combined")
        await settle(pilot, 5)
        assert lit(app, lenses) == ["sp-lens-combined"], lit(app, lenses)
        assert lit(app, modes) == ["sp-mode-trend"], lit(app, modes)

    run(api, body)


def test_the_selected_class_is_painted_not_just_set(api):
    """A class nobody styles is an invisible feature; the fill is the feature."""
    css = TokdashApp.CSS
    for selector in ("#rangebar Button.-selected", ".toggle Button.-selected"):
        assert selector in css, selector
    selected = css[css.index(".toggle Button.-selected"):]
    assert SELECTED_FILL in selected[:240], selected[:240]


def test_the_annual_calendar_pages_only_over_the_months_it_asked_for(api):
    """A named calendar year is twelve months, and every one stays reachable.

    Whole weeks pad the grid edges, so a month list built from the CELLS grows a
    December into the neighbouring January. Paging to that phantom month paints an
    all-blank grid under a heading that names the year, which reads as a month of
    nothing recorded. The fake answers a year request with that year's own days,
    so a calendar left over from the previous window cannot pass either.
    """
    year = TODAY.year - 1

    async def body(app, pilot):
        app.action_show_pane("stats")
        await settle(pilot, 4)
        # Two rungs now: trailing 365 days, then the calendar year in progress,
        # then the year this test wants. (It used to be one press straight past
        # the year the reader is standing in.)
        await pilot.press("p")
        await settle(pilot, 4)
        assert app._stats_win.request() == TODAY.year, app._stats_win
        await pilot.press("p")
        await settle(pilot, 4)
        assert app._stats_win.request() == year, app._stats_win
        start, end = app._stats_win.bounds(app._today)
        cal = app_mod.views.stats_calendar(
            app._stats_payload or {},
            window_from=start.isoformat(),
            window_to=end.isoformat(),
            today=app._today.isoformat(),
        )
        months = [str(month["month"]) for month in cal["month_view"]]
        assert months == [f"{year}-{m:02d}" for m in range(1, 13)], months
        grid = visible(app, "#st-calendar")
        assert f"December {year}" in grid, grid
        days = rows_of(app.query_one("#st-days"))
        assert days and all(row[0].startswith(f"{year}-") for row in days), days
        # Every month is one keystroke closer, so none is silently unreachable.
        app.action_stats_month(-13)
        await settle(pilot, 2)
        assert f"January {year}" in visible(app, "#st-calendar")
        assert f"month 1 of {len(months)}" in visible(app, "#st-calendar")

    run(api, body, size=(100, 30))


def test_no_table_prints_a_header_it_cannot_fit(api):
    """A column narrower than its own label prints a header that lies.

    Speed's hour column asked for 18 cells and named itself in 19, so the pane
    read "Hour (server clock" -- the one word saying WHOSE clock it is, cut off.
    The same class of cut can happen to any of the six tables at any width, so
    the law is checked across all of them.
    """
    tables = {
        "overview": "#ov-table",
        "sessions": "#sn-table",
        "stats": "#st-days",
        "speed": "#sp-table",
        "quota": "#quota-table",
        "pricing": "#pr-table",
    }

    async def body(app, pilot):
        for pane, selector in tables.items():
            app.action_show_pane(pane)
            await settle(pilot, 5)
            table = app.query_one(selector, DataTable)
            header = (visible(app, selector).splitlines() or [""])[0]
            for column in table.columns.values():
                assert str(column.label) in header, (pane, column.label, header)

    run(api, body, size=(100, 30))


def test_the_hour_view_names_the_clock_it_is_using(api):
    """The specific cut: 'Hour (server clock)' in a column of 18."""

    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 5)
        app.query_one("#sp-mode-hour", Button).press()
        await settle(pilot, 5)
        head = (visible(app, "#sp-table").splitlines() or [""])[0]
        assert "Hour (server clock)" in head, head
        assert "Hour (server clock " not in head.replace("Hour (server clock)", ""), head

    run(api, body, size=(100, 30))


def test_a_turn_row_keeps_a_gap_between_the_clock_and_the_model(api):
    """The timestamp field was exactly 19 wide, so the model ran into it.

    The turns live in their own table now, so the collision this guards against
    is a table one: the clock and the model are separate cells, and neither can
    eat the other's width.
    """

    async def body(app, pilot):
        app.action_show_pane("sessions")
        await settle(pilot, 5)
        app.query_one("#sn-table").focus()
        await pilot.press("enter")
        await settle(pilot, 5)
        turns = app.query_one("#sn-turns", DataTable)
        rows = rows_of(turns)
        assert rows, "the turns table painted nothing"
        row = rows[0]
        assert any(cell.endswith("11:00:00") for cell in row), row
        assert any(cell == "claude-opus-4-7" for cell in row), row
        shown = visible(app, "#sn-turns")
        assert "11:00:00" in shown and "claude-opus-4-7" in shown, shown
        assert "11:00:00claude" not in shown, shown

    run(api, body, size=(100, 30))


SEARCH_BOXES = (("overview", "#ov-search"), ("sessions", "#sn-search"),
                ("pricing", "#pr-search"))


def test_a_search_box_shows_the_characters_the_user_typed(api):
    """A frame with nowhere to paint the text is a search box with no value.

    The box is three rows: a round border top and bottom and ONE line between
    them. Vertical padding inside that box eats the line, so the pane filters,
    the model holds the query, and the terminal still shows an empty field.
    """

    async def body(app, pilot):
        for pane, box in SEARCH_BOXES:
            app.action_show_pane(pane)
            await settle(pilot, 4)
            await pilot.press("slash")
            for char in "gpt":
                await pilot.press(char)
            await settle(pilot, 2)
            painted = visible(app, box)
            assert "gpt" in painted, (pane, painted)
            assert app.query_one(box, Input).value == "gpt", pane

    run(api, body, size=(80, 24))


def test_a_pane_switch_hands_the_keyboard_to_the_pane_being_shown(api):
    """A search box keeps focus when its pane is hidden.

    The next keystroke then belongs to a widget the user cannot see: "/" never
    reaches the app, and the new pane looks like it has no search at all.
    """

    async def body(app, pilot):
        await pilot.press("slash")  # overview search, as a user would open it
        await settle(pilot, 1)
        assert isinstance(app.focused, Input)
        app.action_show_pane("pricing")
        await settle(pilot, 4)
        assert not isinstance(app.focused, Input), app.focused
        await pilot.press("slash")
        for char in "gpt":
            await pilot.press(char)
        await settle(pilot, 2)
        assert app._pr_search == "gpt", app._pr_search
        assert "gpt" in visible(app, "#pr-search")

    run(api, body, size=(100, 30))


def test_a_word_too_long_for_its_column_looks_cut(api):
    """Silent clipping turns one value into another.

    Speed's combined lens carries timing-kind names longer than any column the
    terminal can hold. Cut with a trace the reader can see, never mid-word with
    the word left looking complete.
    """

    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 5)
        app.query_one("#sp-lens-combined", Button).press()
        await settle(pilot, 5)
        painted = visible(app, "#sp-table")
        assert "…" in painted, painted
        assert "respon " not in painted, painted

    run(api, body, size=(80, 24))

# --------------------------------------------------------------------------- #
# card captions: a crop that lands mid-word reads as a broken build
# --------------------------------------------------------------------------- #

def _card_lines(cards, pane_width, mono=False):
    per_row = 6 if pane_width >= 116 else 3 if pane_width >= 76 else 2
    block = app_mod._card_block(cards, per_row, pane_width, mono)
    return str(block).splitlines()


@pytest.mark.parametrize("pane_width", [118, 98, 78, 58])
def test_a_card_caption_never_breaks_in_the_middle_of_a_word(pane_width):
    """Six cards share one row at 120 columns, so a caption has 16 cells.

    A sub-caption the user cannot finish reading is worse than none: "prompt-
    token we..." leaves the denominator open, which is exactly the confusion
    the caption exists to close. So the short wording has to be short.
    """
    import tokdash.tui.views as views_mod

    cards = views_mod.kpi_cards(copy.deepcopy(USAGE), copy.deepcopy(ACTIVE))
    assert cards, "the fixture has to feed the headline row"
    lines = _card_lines(cards, pane_width)
    cropped = [ln for ln in lines if ln.rstrip().endswith("\u2026")]
    assert not cropped, (pane_width, cropped)


@pytest.mark.parametrize("pane_width", [118, 98, 78, 58])
def test_every_card_keeps_its_full_value_at_every_width(pane_width):
    """Cropping a caption is a wording decision; cropping a FIGURE is a bug."""
    import tokdash.tui.views as views_mod

    cards = views_mod.kpi_cards(copy.deepcopy(USAGE), copy.deepcopy(ACTIVE))
    painted = "\n".join(_card_lines(cards, pane_width))
    for card in cards:
        assert str(card["value"]) in painted, (pane_width, card["key"], card["value"])


def test_the_monochrome_cards_still_separate_their_figures():
    """No tints in a mono terminal: the geometry alone has to group the row."""
    import tokdash.tui.views as views_mod

    cards = views_mod.kpi_cards(copy.deepcopy(USAGE), copy.deepcopy(ACTIVE))
    painted = "\n".join(_card_lines(cards, 118, mono=True))
    for label in ("TOTAL TOKENS", "TOTAL COST", "TOTAL MESSAGES",
                  "EST. AGENT TIME", "CACHE HIT RATE", "MOST USED MODEL"):
        assert label in painted, label


# --------------------------------------------------------------------------- #
# round 5 (visual parity pass): the room a table's own scrollbar takes, and the
# sentences that may not be cut to buy a row
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("size", [(120, 36), (100, 30), (80, 24)])
def test_a_scrolling_table_never_pays_for_its_scrollbar_with_a_column(api, size):
    """A vertical scrollbar arrives AFTER the columns are fitted.

    Sizing the columns to the full pane and then growing the bar takes a cell from
    the right-hand column, and the figure sitting there loses its last character
    with nothing on screen to say so. The fit keeps that cell, so the table scrolls
    sideways never.
    """
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 3)
        press(app, "sp-mode-hour")
        await settle(pilot, 3)
        table = app.query_one("#sp-table", DataTable)
        assert table.row_count > 0
        # A vertical scrollbar arrives after the fit and takes a cell from the
        # right; the table answers with a HORIZONTAL one when the fit did not
        # keep that cell, and a sideways-scrolling numeric column is a figure the
        # reader has to guess at. Rows the viewport scrolled out of are not
        # missing -- the sideways bar is the thing that must never appear.
        assert not table.show_horizontal_scrollbar, (size, visible(app, "#sp-table"))
        last = len(table.columns) - 1
        # One row of the content region is the header, the rest is the viewport.
        top = int(table.scroll_y)
        window = max(1, int(table.content_region.height) - 1)
        for row in range(top, min(top + window, table.row_count)):
            value = str(table.get_row_at(row)[last])
            if value and value != "\u2014":
                assert value in visible(app, "#sp-table"), (size, row, value)
        # And the row under the cursor is always inside what the widget drew.
        table.move_cursor(row=min(3, table.row_count - 1))
        await settle(pilot, 1)
        label = str(table.get_row_at(table.cursor_row)[0])
        assert label in visible(app, "#sp-table"), (size, label)

    run(api, body, size=size)


@pytest.mark.parametrize("size", [(120, 36), (100, 30), (80, 24)])
def test_an_opened_row_keeps_the_sentence_naming_its_denominator(api, size):
    """Enter is asked because of that sentence; the row's height never eats it.

    The two lenses divide by different things, and a caption packed to three rows
    used to deliver "summed tokens over the" and stop -- a rate standing on screen
    with no denominator, which is the one thing this view may not do.
    """
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 3)
        press(app, "sp-lens-combined")
        await settle(pilot, 2)
        table = app.query_one("#sp-table", DataTable)
        table.focus()
        await pilot.press("enter")
        await settle(pilot, 2)
        detail = " ".join(visible(app, "#sp-detail").split())
        assert "UNION of busy time" in detail, (size, detail)

    run(api, body, size=size)


def test_the_speed_strip_wraps_instead_of_losing_a_fact(api):
    """The strip packs, and packing is not deleting.

    At eighty columns the series line and the source filter do not share a row, so
    the strip takes a second one. Dropping the filter instead left a pane claiming
    "all measured" while a filter was on screen -- two places to read the same
    fact, and only one of them updated.
    """
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 3)
        strip = app.query_one("#sp-note", Static)
        note = " ".join(visible(app, "#sp-note").split())
        # The fixture's speed payload names a harness with no measurement, so the
        # strip has a filter fact, a warning fact and a cache fact to place.
        assert "source all measured" in note, note
        assert "no measured speed: kimi" in note, note
        assert "cache ready" in note, note
        # None of it may be replaced by a tally of what gave way.
        assert "widen to read" not in note, note
        assert strip.region.height >= 1, strip.region

    run(api, body, size=(80, 24))


# --------------------------------------------------------------------------- #
# round 5 (visual parity, second pass): the picture's own labels
# --------------------------------------------------------------------------- #

def test_a_squeezed_chart_still_names_the_ends_of_its_axis():
    """Twenty-four hours at eighty columns leaves two cells per column.

    Keys that do not fit their own column are dropped rather than cut, which is
    right -- "00:" is not a shortened hour -- but a series with no ends at all is
    a shape the reader cannot place in time. The first and last key get the whole
    row instead.
    """
    from tokdash.tui.charts import column_chart

    entries = [{"label": f"{h:02d}:00", "value": float(h % 5 + 1), "text": ""}
               for h in range(24)]
    drawn = column_chart(entries, width=76, height=3, style="series", unit="tok/s")
    axis = drawn["lines"][-1]
    painted = "".join(run.text for run in axis)
    assert painted.strip().startswith("00:00"), repr(painted)
    assert painted.strip().endswith("23:00"), repr(painted)


@pytest.mark.parametrize("size", [(120, 36), (100, 30)])
def test_a_caption_row_never_ends_in_the_middle_of_a_fact(api, size):
    """A number split across two rows reads as two wrong numbers.

    "peak 612.0K tokens · 1" over "bucket measured 0 tokens" is what packing the
    facts as ONE string does: the row breaks at the column, not at the fact.
    """
    async def body(app, pilot):
        band = app.query_one("#ov-activity")
        lines = str(band.render()).splitlines()
        tail = [ln for ln in lines if "\u00b7" in ln]
        assert tail, lines
        for line in tail:
            assert not line.rstrip().endswith(("\u00b7", "1", "0")), (size, line)
        assert "widen to read" not in "\n".join(lines), lines

    run(api, body, size=size)
