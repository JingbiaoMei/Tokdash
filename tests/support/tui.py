"""Contract-shaped fixtures and pilot helpers shared by the TUI pane tests.

Every payload below is shaped like the endpoint's real response -- field names,
nesting, and the parts that are deliberately ABSENT. The tests built on them
assert MEANING: which field a card reads, what a missing figure prints as, which
population a share divides, and whether a late reply can be painted under a new
label. Shared synthetic rows that flatter every endpoint at once would prove
nothing, so each source gets its own shape. Every figure here is synthetic.

The fetch seams are the ``fetch_*`` names inside ``tokdash.tui.app`` -- the very
names the workers call -- so the workers, the generation guard, the progressive
paints and the failure paths stay under test while the network and the disk do
not. The suite's conftest keeps TOKDASH_TUI_NO_REMOTE on, so no pilot can reach
a live service or the real usage database.

A test module calls ``pytest.importorskip("textual")`` first and then imports
what it needs from here. ``api`` is a pytest fixture: importing it into a test
module is what lets that module's tests ask for it. This module skips on its own
as well, so a textual-free test that imports a payload inside its body is
skipped rather than failed where textual is not installed.

Wait on a condition, never on a fixed sleep: ``settle()`` returns as soon as the
app is idle, and ``wait_for()`` polls a predicate.
"""
from __future__ import annotations

import asyncio
import copy
import datetime
import time

import pytest

textual = pytest.importorskip("textual", reason="the TUI tests need textual")

from textual.widgets import Button, DataTable  # noqa: E402
from textual.worker import WorkerState  # noqa: E402

import tokdash.tui.app as app_mod  # noqa: E402
from tokdash.tui.app import ChoicePicker, TokdashApp  # noqa: E402
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

#: The most a step may take before the test looks anyway. A loaded CI runner
#: (four workers on four cores) has run a step at several times the fixed sleep
#: this replaces, so the sleep is no ceiling. Only a step that parks a worker on
#: purpose -- a gated fetch, a held build stream -- ever waits this out.
PATIENCE = 3.0


def _busy(app, horizon: float) -> bool:
    """Whether the app still holds work a step could be waiting on.

    That is an unfinished worker, an animation, a queued message, a one-shot
    timer no longer than ``horizon``, or -- on the screen being shown -- a layout
    or repaint not yet done or a ``call_after_refresh`` not yet run. A screen
    under a chooser does not repaint until it is uncovered, so its pending
    repaint is not work in hand. Interval timers (the 1 s status tick) never
    finish, and a longer one-shot (the 12 s stale re-read) would not have fired
    inside the fixed sleep this replaces either, so neither counts.
    These are Textual 8 internals; pyproject pins textual below 9.
    """
    if any(worker.state in _LIVE_WORKER for worker in app.workers):
        return True
    if app.animator._animations or app.animator._scheduled:
        return True
    shown = app.screen
    if shown._dirty_widgets or shown._callbacks:
        return True
    for screen in app.screen_stack:
        for node in screen.walk_children(with_self=True):
            if not node._message_queue.empty():
                return True
            if screen is shown and any(getattr(node, flag, False) for flag in _PENDING_FLAGS):
                return True
            if _short_timer(node, horizon):
                return True
    return not app._message_queue.empty() or _short_timer(app, horizon)


def _short_timer(node, horizon: float) -> bool:
    for timer in list(node._timers):
        task = timer._task
        if timer._repeat == 0 and task is not None and not task.done() \
                and timer._interval <= horizon:
            return True
    return False


async def quiesce(pilot, budget: float):
    """Return once the app is idle, or after ``PATIENCE`` seconds at the most.

    Idle means three polls running with nothing in ``_busy`` and every message
    the screen was sent handled; the repeat catches a ``call_after_refresh``
    that a finished worker leaves behind. ``budget`` is the fixed sleep this
    used to be. It decides which one-shot timers the step may be waiting on;
    the wait itself runs to ``PATIENCE``, or to ``budget`` if that is longer.

    Only a busy poll sleeps, to hand the worker threads the GIL; an idle one
    just drains the screen. A fixed sleep per poll would cost each step three
    timer ticks, which on Windows are ~15 ms however short the request.
    """
    deadline = time.monotonic() + max(budget, PATIENCE)
    quiet = 0
    busy = True
    while quiet < 3 and time.monotonic() < deadline:
        await pilot.pause(0.002 if busy else 0)
        busy = _busy(pilot.app, budget)
        quiet = 0 if busy else quiet + 1


async def settle(pilot, times=3):
    """Let the step settle: ``times`` quarter-seconds was the old fixed sleep."""
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
    guarded separately, in test_tui_source_colours.py.
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


_STAMPED_REFITS = (TokdashApp._refit_activity, TokdashApp._refit_speed_series)


def paint_with_refits_flushed_early(app, paint) -> None:
    """Run ``paint`` with the refits it queues run BEFORE its layout lands.

    Textual runs a screen's after-refresh callbacks from ``_on_timer_update``
    whether or not the Layout messages the same paint queued have reached the
    screen, and ``Pilot.pause`` calls it after every poll. A refit run in that gap
    measures the frame the paint itself measured. That is the ordering Windows CI
    met now and then; this helper makes it the ordering every run gets: the
    stamp-guarded refits the paint asks for are held, and run as soon as the
    paint returns, before anything has been laid out.
    """
    held = []
    real = app.call_after_refresh

    def hold(callback, *args, **kwargs):
        if getattr(callback, "__func__", None) in _STAMPED_REFITS:
            held.append((callback, args, kwargs))
            return True
        return real(callback, *args, **kwargs)

    app.call_after_refresh = hold  # the instance attribute shadows the method
    try:
        paint()
    finally:
        del app.call_after_refresh
    assert held, "the paint queued no stamped refit, so nothing was flushed early"
    for callback, args, kwargs in held:
        callback(*args, **kwargs)


def line_text(app) -> str:
    """The whole visible screen, for 'the note says X' style assertions."""
    return "\n".join(screen_lines(app))


# --------------------------------------------------------------------------- #
# more pilot helpers
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


async def wait_for(app, pilot, predicate, timeout=10.0):
    """Thread jobs + chained awaits need more than one pause to settle.

    The timeout is patience, not a cost: a predicate that holds returns at once,
    and a CI runner shared by four workers has taken more than the two seconds
    this used to allow to run one thread job.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await pilot.pause(0.02)
    return predicate()


async def _settled(app, pilot, pane: str = "overview") -> None:
    """Wait for one pane's first paint to land, and fail the test if it never does.

    test_tui_resize_refit.py keeps a variant that answers a bool instead, because
    its tests assert on the answer.
    """
    for _ in range(40):
        if app._state.get(pane) == "ok" and not app._open_loads.get(pane):
            return
        await pilot.pause(0.1)
    raise AssertionError(f"{pane} never settled")


def picker_options(app):
    """The chooser on top of the stack, as the reader is offered it."""
    screen = app.screen
    assert isinstance(screen, ChoicePicker), f"no chooser is open ({screen!r})"
    return screen._options


def body_text(app, selector="#rp-body") -> str:
    """The section's whole text, wrap and clipping aside."""
    return str(app.query_one(selector).render())


def lit(app, widget_ids: list[str]) -> list[str]:
    """Which of these buttons the terminal will paint as the current choice."""
    return [
        widget_id
        for widget_id in widget_ids
        if app.query_one(f"#{widget_id}", Button).has_class("-selected")
    ]


# --------------------------------------------------------------------------- #
# the three sizes a terminal comes in, and the two a resize moves between
# --------------------------------------------------------------------------- #

SIZES = [(120, 36), (100, 30), (80, 24)]
WIDE = (120, 36)
NARROW = (80, 24)


# --------------------------------------------------------------------------- #
# payload builders
# --------------------------------------------------------------------------- #


def month_of_buckets(api, count=30):
    """Turn the fixture's two-hour trend into a month of DAILY buckets.

    Thirty-one days is what a "Last Month" window asks for, and it is the first
    series long enough to want more cells than the Speed picture has beside its
    table. The rates are written distinct so a merged or dropped bucket shows.
    """
    days = [(datetime.date(2026, 9, 1) + datetime.timedelta(days=i)).isoformat()
            for i in range(count)]
    api.speed_trend["granularity"] = "day"
    api.speed_trend["buckets"] = [
        {"key": day, "from": day, "to": day} for day in days
    ]
    api.speed_trend["offered_buckets"] = [{"bucket": "day", "count": count},
                                          {"bucket": "week", "count": count // 7 + 1}]
    for series in api.speed_trend["series"]:
        series.update(per_stream=[30.0 + i for i in range(count)],
                      combined=[60.0 + i for i in range(count)],
                      calls=[40 + i for i in range(count)],
                      parallelism=[1.4] * count, spread={})
    for series in api.speed_trend["model_series"]:
        series.update(combined=[60.0 + i for i in range(count)],
                      calls=[40 + i for i in range(count)],
                      parallelism=[1.2] * count)
    return days
