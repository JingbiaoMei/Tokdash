"""Seeded synthetic payloads for visual dashboard development.

These fixtures deliberately live above the persistence layer.  They exercise the
real dashboard renderers without reading or writing local usage history, credentials,
pricing overrides, or quota snapshots. A fixture seed adds organic variation between
server starts while keeping every request stable and reproducible within one run.
"""

from __future__ import annotations

import calendar
import hashlib
import math
import random
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .compute import cache_hit_rate
from .sessions import SESSION_TOOLS, _include_codex_review_sessions
from .usage_store import model_cost_rank_key, model_rank_key

# Usage sources (Overview, /api/tools) and session tools (Session Explorer,
# Active Time) are genuinely different sets in production: `cursor` and
# `gemini_cli` report token usage but expose no session transcripts, so they
# appear in TOOL_SPECS and not in SESSION_LABELS. The fixture mirrors that split
# rather than hiding it -- SESSION_LABELS is pinned to the real
# `sessions.SESSION_TOOLS` by test_dense_fixture_session_tools_match_production.
TOOL_SPECS = (
    ("codex", "Codex", ("openai/gpt-5.6-sol", "openai/gpt-5.5", "openai/o4-mini")),
    (
        "claude",
        "Claude Code",
        (
            "anthropic/claude-opus-4.1",
            "anthropic/claude-sonnet-4",
            "anthropic/claude-haiku-3.5",
        ),
    ),
    (
        "cursor",
        "Cursor",
        ("cursor/composer-2", "anthropic/claude-sonnet-4", "openai/gpt-5.5"),
    ),
    (
        "gemini_cli",
        "Gemini CLI",
        ("google/gemini-3-pro", "google/gemini-3-flash", "google/gemini-2.5-pro"),
    ),
    (
        "opencode",
        "OpenCode",
        ("openai/gpt-5.5", "anthropic/claude-sonnet-4", "google/gemini-3-flash"),
    ),
    (
        "kimi",
        "Kimi CLI",
        ("moonshot/kimi-k2.6", "moonshot/kimi-k2.5", "moonshot/kimi-code"),
    ),
    ("grok", "Grok Build", ("xai/grok-code-fast-1", "xai/grok-4.1", "xai/grok-4-mini")),
    (
        "cline",
        "Cline",
        ("anthropic/claude-sonnet-4", "openai/gpt-5.5", "google/gemini-3-pro"),
    ),
    (
        "qoder",
        "Qoder IDE",
        ("qoder/quest-pro", "qoder/quest-fast", "anthropic/claude-sonnet-4"),
    ),
    ("zcode", "ZCode", ("zai/glm-5", "zai/glm-4.7", "zai/glm-4.6")),
    (
        "workbuddy",
        "WorkBuddy",
        ("alibaba/qwen3-coder", "alibaba/qwen3-max", "openai/gpt-5.5"),
    ),
    (
        "reasonix",
        "Reasonix",
        ("minimax/minimax-m3", "deepseek/deepseek-v3.2", "moonshot/kimi-k2.6"),
    ),
)

SESSION_LABELS = {
    "codex": "Codex",
    "claude": "Claude Code",
    "opencode": "OpenCode",
    "pi_agent": "Pi",
    "omp": "omp",
    "mimo": "Mimo",
    "kimi": "Kimi",
    "dsh": "DeepSeek Harness",
    "reasonix": "Reasonix",
    "zcode": "ZCode",
    "kilocode": "Kilo Code",
    "grok": "Grok Build",
    "hermes": "Hermes",
    "antigravity_cli": "Antigravity CLI",
    "cline": "Cline",
    "workbuddy": "WorkBuddy",
    "qoder": "Qoder IDE",
}

SESSION_MODELS = (
    "gpt-5.6-sol",
    "claude-opus-4.1",
    "gemini-3-pro",
    "kimi-k2.6",
    "glm-5",
    "qwen3-coder",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rng(seed: int, namespace: str) -> random.Random:
    """Return an order-independent RNG for one fixture surface."""
    digest = hashlib.blake2b(f"{seed}:{namespace}".encode(), digest_size=8).digest()
    return random.Random(int.from_bytes(digest, "big"))


def _days_in_range(range_info: Mapping[str, Any]) -> int:
    try:
        return max(1, int(range_info.get("days", 1)))
    except (TypeError, ValueError):
        return 1


def _model_row(name: str, tokens: int, seed: int) -> dict[str, Any]:
    input_tokens = int(tokens * (0.10 + (seed % 4) * 0.008))
    cache_tokens = int(tokens * (0.68 + (seed % 5) * 0.025))
    output_tokens = max(1, tokens - input_tokens - cache_tokens)
    messages = max(1, tokens // (72_000 + (seed % 7) * 9_000))
    cost = round(
        input_tokens * 0.0000025
        + output_tokens * 0.0000105
        + cache_tokens * 0.00000025,
        6,
    )
    return {
        "name": name,
        "tokens": tokens,
        "tokens_in": input_tokens,
        "tokens_out": output_tokens,
        "tokens_cache": cache_tokens,
        "cost": cost,
        "messages": messages,
        "cache_hit_rate": cache_hit_rate(input_tokens, cache_tokens),
    }


def dense_usage(range_info: Mapping[str, Any], seed: int = 0) -> dict[str, Any]:
    """Return a crowded but internally consistent Overview payload."""
    days = _days_in_range(range_info)
    day_scale = max(1.0, min(days, 366) ** 0.82)
    usage_rng = _rng(
        seed, f"usage:{range_info.get('period_resolved', 'custom')}:{days}"
    )
    apps: dict[str, dict[str, Any]] = {}
    coding_models: list[dict[str, Any]] = []

    for tool_index, (tool, _label, model_names) in enumerate(TOOL_SPECS):
        tool_base = int(
            (154_000_000 - tool_index * 7_400_000)
            * day_scale
            * usage_rng.uniform(0.92, 1.08)
        )
        raw_shares = [
            base * usage_rng.uniform(0.95, 1.05) for base in (0.54, 0.29, 0.17)
        ]
        share_total = sum(raw_shares)
        shares = [share / share_total for share in raw_shares]
        models = [
            _model_row(
                model_name,
                max(1, int(tool_base * shares[model_index])),
                tool_index * 5 + model_index,
            )
            for model_index, model_name in enumerate(model_names)
        ]
        models.sort(key=model_rank_key)
        tokens = sum(row["tokens"] for row in models)
        tokens_in = sum(row["tokens_in"] for row in models)
        tokens_out = sum(row["tokens_out"] for row in models)
        tokens_cache = sum(row["tokens_cache"] for row in models)
        app_row = {
            "tokens": tokens,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tokens_cache": tokens_cache,
            "cost": round(sum(row["cost"] for row in models), 6),
            "messages": sum(row["messages"] for row in models),
            "models": models,
            "cache_hit_rate": cache_hit_rate(tokens_in, tokens_cache),
        }
        apps[tool] = app_row
        coding_models.extend({"source": tool, **row} for row in models)

    # API.md documents coding_models as token-ranked, and the real producer gets
    # that by filtering an already-sorted all_models. Built per tool, this list
    # came out grouped by tool instead.
    coding_models.sort(key=model_rank_key)

    openclaw_models = [
        _model_row(
            "openai/gpt-5.5",
            int(72_000_000 * day_scale * usage_rng.uniform(0.92, 1.08)),
            101,
        ),
        _model_row(
            "anthropic/claude-sonnet-4",
            int(49_000_000 * day_scale * usage_rng.uniform(0.92, 1.08)),
            102,
        ),
        _model_row(
            "google/gemini-3-flash",
            int(37_000_000 * day_scale * usage_rng.uniform(0.92, 1.08)),
            103,
        ),
    ]

    combined: dict[str, dict[str, Any]] = {}
    for row in [*coding_models, *openclaw_models]:
        name = str(row["name"]).split("/")[-1]
        target = combined.setdefault(
            name,
            {
                "name": name,
                "tokens": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "tokens_cache": 0,
                "cost": 0.0,
                "messages": 0,
            },
        )
        for key in ("tokens", "tokens_in", "tokens_out", "tokens_cache", "messages"):
            target[key] += int(row[key])
        target["cost"] += float(row["cost"])

    combined_models = []
    for row in combined.values():
        row["cost"] = round(row["cost"], 6)
        row["cache_hit_rate"] = cache_hit_rate(row["tokens_in"], row["tokens_cache"])
        combined_models.append(row)
    # Imported, not re-derived: a fixture whose ordering can drift from the
    # server's is the one place the drift would go unnoticed.
    combined_models.sort(key=model_rank_key)

    by_tool = {
        tool: {
            "tokens": row["tokens"],
            "tokens_in": row["tokens_in"],
            "tokens_cache": row["tokens_cache"],
            "cost": row["cost"],
            "cache_hit_rate": row["cache_hit_rate"],
        }
        for tool, row in apps.items()
    }
    openclaw_total = sum(row["tokens"] for row in openclaw_models)
    openclaw_input = sum(row["tokens_in"] for row in openclaw_models)
    openclaw_cache = sum(row["tokens_cache"] for row in openclaw_models)
    by_tool["openclaw"] = {
        "tokens": openclaw_total,
        "tokens_in": openclaw_input,
        "tokens_cache": openclaw_cache,
        "cost": round(sum(row["cost"] for row in openclaw_models), 6),
        "cache_hit_rate": cache_hit_rate(openclaw_input, openclaw_cache),
    }

    total_tokens = sum(row["tokens"] for row in by_tool.values())
    total_cost = round(sum(row["cost"] for row in by_tool.values()), 2)
    total_messages = sum(row["messages"] for row in apps.values()) + sum(
        row["messages"] for row in openclaw_models
    )
    previous_factor = usage_rng.uniform(0.86, 1.06)
    return {
        "period": range_info.get("period_resolved", "custom"),
        "range": dict(range_info),
        "timestamp": _now_iso(),
        "total_tokens": total_tokens,
        "total_cost": total_cost,
        "total_messages": total_messages,
        "tokens_in": sum(row["tokens_in"] for row in by_tool.values()),
        "tokens_cache": sum(row["tokens_cache"] for row in by_tool.values()),
        "cache_hit_rate": cache_hit_rate(
            sum(row["tokens_in"] for row in by_tool.values()),
            sum(row["tokens_cache"] for row in by_tool.values()),
        ),
        "apps": apps,
        "coding_apps": apps,
        "by_tool": by_tool,
        "coding_models": coding_models,
        "openclaw_models": openclaw_models,
        "combined_models": combined_models,
        "top_models": combined_models[:5],
        "top_models_by_cost": sorted(combined_models, key=model_cost_rank_key)[:5],
        "comparison": {
            "tokens_prev": int(total_tokens * previous_factor),
            "cost_prev": round(total_cost * previous_factor, 2),
            "messages_prev": int(total_messages * previous_factor),
            "tokens_pct": round((1 / previous_factor - 1) * 100, 1),
            "cost_pct": round((1 / previous_factor - 1) * 100, 1),
            "messages_pct": round((1 / previous_factor - 1) * 100, 1),
        },
        "response_cache": {
            "served_from_cache": False,
            "age_seconds": 0.0,
            "status": "fixture",
        },
        "source_errors": [],
        "fixture": {"name": "dense", "seed": seed},
    }


def dense_tools(range_info: Mapping[str, Any], seed: int = 0) -> dict[str, Any]:
    """Coding-tool usage in `parse_entries_json` shape, plus the route's envelope.

    Derived from the same seeded draw as :func:`dense_usage` so Overview and
    /api/tools never disagree about the same window.
    """
    usage = dense_usage(range_info, seed=seed)
    all_models = usage["coding_models"]
    return {
        "total_cost": round(sum(row["cost"] for row in all_models), 6),
        "total_tokens": sum(row["tokens"] for row in all_models),
        "total_messages": sum(row["messages"] for row in all_models),
        "cache_hit_rate": cache_hit_rate(
            sum(row["tokens_in"] for row in all_models),
            sum(row["tokens_cache"] for row in all_models),
        ),
        "apps": usage["apps"],
        "all_models": all_models,
        "source_errors": [],
        "period": usage["period"],
        "range": dict(range_info),
        "timestamp": _now_iso(),
    }


def dense_openclaw(range_info: Mapping[str, Any], seed: int = 0) -> dict[str, Any]:
    """OpenClaw usage in `sources.openclaw.get_session_usage` shape.

    Note `models` is a mapping keyed by model name with no ``name`` field -- that
    is the real producer's shape, and it differs from every other model list.
    """
    usage = dense_usage(range_info, seed=seed)
    rows = usage["openclaw_models"]
    total_in = sum(row["tokens_in"] for row in rows)
    total_cache = sum(row["tokens_cache"] for row in rows)
    return {
        "total_tokens": sum(row["tokens"] for row in rows),
        "total_cost": round(sum(row["cost"] for row in rows), 6),
        "total_messages": sum(row["messages"] for row in rows),
        "total_tokens_in": total_in,
        "total_tokens_cache": total_cache,
        "cache_hit_rate": cache_hit_rate(total_in, total_cache),
        "models": {
            row["name"]: {key: value for key, value in row.items() if key != "name"}
            for row in rows
        },
        "contributions": _openclaw_contributions(range_info, seed),
        "period": usage["period"],
        "range": dict(range_info),
        "timestamp": _now_iso(),
    }


def _openclaw_contributions(
    range_info: Mapping[str, Any], seed: int
) -> list[dict[str, Any]]:
    """Per-day OpenClaw rows over the requested window, never past today."""
    today = datetime.now().astimezone().date()
    try:
        end = min(date.fromisoformat(str(range_info.get("to"))), today)
    except (TypeError, ValueError):
        end = today
    start = end - timedelta(days=max(0, _days_in_range(range_info) - 1))

    days: list[dict[str, Any]] = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        day_rng = _rng(seed, f"openclaw-day:{day.isoformat()}")
        if day_rng.random() < 0.12:
            continue
        tokens = int(day_rng.triangular(4_000_000, 180_000_000, 41_000_000))
        tokens_in = int(tokens * 0.12)
        tokens_cache = int(tokens * 0.73)
        tokens_out = max(1, tokens - tokens_in - tokens_cache)
        messages = day_rng.randint(12, 210)
        cost = round(
            tokens_in * 0.0000025
            + tokens_out * 0.0000105
            + tokens_cache * 0.00000025,
            6,
        )
        model = SESSION_MODELS[day.toordinal() % len(SESSION_MODELS)]
        days.append(
            {
                "date": day.isoformat(),
                "totals": {"tokens": tokens, "cost": cost, "messages": messages},
                "intensity": 0,
                "tokenBreakdown": {
                    "input": tokens_in,
                    "output": tokens_out,
                    "cacheRead": tokens_cache,
                    "cacheWrite": 0,
                    "reasoning": 0,
                },
                "sources": [
                    {
                        "source": "openclaw",
                        "modelId": model,
                        "providerId": "unknown",
                        "tokens": {
                            "input": tokens_in,
                            "output": tokens_out,
                            "cacheRead": tokens_cache,
                            "cacheWrite": 0,
                            "reasoning": 0,
                        },
                        "cost": cost,
                        "messages": messages,
                    }
                ],
            }
        )
    return days


def _session_row(tool: str, index: int, seed: int = 0) -> dict[str, Any]:
    row_rng = _rng(seed, f"session:{tool}:{index}")
    now = datetime.now(timezone.utc)
    model = SESSION_MODELS[(index + len(tool)) % len(SESSION_MODELS)]
    tokens = int(
        (18_400_000 + (index * 4_719_137) + (len(tool) * 817_000))
        * row_rng.uniform(0.90, 1.10)
    )
    tokens_in = int(
        tokens * (0.10 + (index % 3) * 0.015 + row_rng.uniform(-0.006, 0.006))
    )
    tokens_cache = int(
        tokens * (0.70 + (index % 4) * 0.03 + row_rng.uniform(-0.01, 0.01))
    )
    tokens_out = max(1, tokens - tokens_in - tokens_cache)
    last_seen = now - timedelta(minutes=index * 17 + len(tool) + row_rng.randint(0, 9))
    started = last_seen - timedelta(minutes=23 + index * 7 + row_rng.randint(0, 12))
    return {
        "tool": tool,
        "session_id": f"{tool}-dense-{index + 1:02d}",
        "display_name": (
            "Investigate cross-provider cache reconciliation and responsive table overflow"
            if index % 4 == 0
            else f"Dense fixture session {index + 1}: verify model and token breakdown"
        ),
        "project": (
            "customer-analytics-platform-with-an-intentionally-long-project-name"
            if index % 5 == 0
            else ("tokdash-design-lab" if index % 2 else "agent-runtime-integration")
        ),
        "is_review_session": tool == "codex" and index % 7 == 0,
        "model": model,
        "token_events": 84 + index * 31,
        "tokens_in": tokens_in,
        "tokens_cache": tokens_cache,
        "tokens_out": tokens_out,
        "tokens_reasoning": int(tokens_out * 0.31),
        "tokens": tokens,
        "cache_ratio": round(tokens_cache / tokens, 4),
        "cache_hit_rate": cache_hit_rate(tokens_in, tokens_cache),
        "cost": round(
            tokens_in * 0.0000025 + tokens_out * 0.0000105 + tokens_cache * 0.00000025,
            6,
        ),
        "started_at": started.isoformat(),
        "last_seen_at": last_seen.isoformat(),
        "span_ms": int((last_seen - started).total_seconds() * 1000),
        "active_ms": (19 + index * 3) * 60_000,
        "active_ms_sum": (27 + index * 5) * 60_000,
    }


SESSION_ROWS_PER_TOOL = 18


def dense_sessions(
    tool: str, include_review_sessions: bool | None = None, seed: int = 0
) -> dict[str, Any]:
    # Same rejection as `sessions.get_sessions_data`, so an unsupported tool still
    # renders the dashboard's error state instead of a fabricated session list.
    key = str(tool or "").strip().lower()
    if key not in SESSION_TOOLS:
        raise ValueError(f"Unsupported session tool: {tool}")

    # The real route resolves an unset flag through the configured default rather
    # than treating None as False -- and the response has to report what was
    # actually applied, or the payload contradicts the rows it ships.
    include_review = _include_codex_review_sessions(include_review_sessions)
    rows = [_session_row(key, index, seed) for index in range(SESSION_ROWS_PER_TOOL)]
    if key == "codex" and not include_review:
        rows = [row for row in rows if not row["is_review_session"]]
    return {
        "tool": key,
        "tool_label": SESSION_LABELS.get(key, key.replace("_", " ").title()),
        "period": "fixture",
        "include_review_sessions": include_review,
        "latest_session": rows[0] if rows else None,
        "sessions": rows,
        "summary": {
            "session_count": len(rows),
            "tokens": sum(row["tokens"] for row in rows),
            "cost": round(sum(row["cost"] for row in rows), 6),
            "active_ms": sum(row["active_ms"] for row in rows),
            "active_ms_sum": sum(row["active_ms_sum"] for row in rows),
            "span_ms": sum(row["span_ms"] for row in rows),
            "active_gap_cap_ms": 300_000,
            "active_time_estimated": True,
            "active_time_method": "capped-inter-event-gap",
        },
        "timestamp": _now_iso(),
    }


def dense_session_detail(tool: str, session_id: str, seed: int = 0) -> dict[str, Any]:
    key = str(tool or "").strip().lower()
    if key not in SESSION_TOOLS:
        raise ValueError(f"Unsupported session tool: {tool}")

    # Only ids this fixture actually generates resolve. Fabricating a session for
    # any string meant /api/session could never 404, so the dashboard's
    # "session not found" path was unreachable -- one of the states the fixture
    # exists to exercise.
    known = {
        _session_row(key, index, seed)["session_id"]: index
        for index in range(SESSION_ROWS_PER_TOOL)
    }
    index = known.get(str(session_id))
    if index is None:
        raise FileNotFoundError(
            f"{SESSION_LABELS.get(key, key.title())} session not found: {session_id}"
        )

    detail_rng = _rng(seed, f"detail:{key}:{session_id}")
    session = _session_row(key, index, seed)
    start = datetime.fromisoformat(session["started_at"])
    turns = []
    for index in range(48):
        tokens = int(
            (84_000 + (index * 37_913) % 710_000) * detail_rng.uniform(0.90, 1.10)
        )
        row = _model_row(SESSION_MODELS[index % len(SESSION_MODELS)], tokens, index)
        model_name = row.pop("name")
        turns.append(
            {
                **row,
                "model": model_name,
                "tokens_reasoning": int(row["tokens_out"] * 0.28),
                "turn_index": index + 1,
                "timestamp": (
                    start + timedelta(minutes=index * 3, seconds=index * 7)
                ).isoformat(),
            }
        )
    return {"session": session, "turns": turns, "timestamp": _now_iso()}


def dense_active_time(range_info: Mapping[str, Any], seed: int = 0) -> dict[str, Any]:
    by_tool: dict[str, dict[str, Any]] = {}
    for index, (tool, label) in enumerate(SESSION_LABELS.items()):
        row_rng = _rng(seed, f"active:{tool}")
        by_tool[tool] = {
            "tool_label": label,
            "session_count": 16 + row_rng.randint(0, 5),
            "active_ms": int((215 + index * 19) * row_rng.uniform(0.92, 1.08)) * 60_000,
            "active_ms_sum": int((328 + index * 31) * row_rng.uniform(0.92, 1.08))
            * 60_000,
        }
    active_ms = sum(row["active_ms"] for row in by_tool.values())
    active_ms_sum = sum(row["active_ms_sum"] for row in by_tool.values())
    return {
        "period": range_info.get("period_resolved", "custom"),
        "range": dict(range_info),
        "active_ms": active_ms,
        "active_ms_sum": active_ms_sum,
        "comparison": {
            "active_ms_prev": int(active_ms * 0.88),
            "active_ms_sum_prev": int(active_ms_sum * 0.91),
            "active_ms_pct": 13.6,
            "active_ms_sum_pct": 9.9,
        },
        "by_tool": by_tool,
        "unavailable_tools": [],
        "active_gap_cap_ms": 300_000,
        "active_time_estimated": True,
        "active_time_method": "capped-inter-event-gap",
        "include_review_sessions": False,
        "timestamp": _now_iso(),
    }


def _contribution(day: date, seed: int = 0) -> dict[str, Any]:
    day_rng = _rng(seed, f"stats:{day.isoformat()}")
    active = day_rng.random() > 0.16
    tokens = (
        0
        if not active
        else int(day_rng.triangular(22_000_000, 1_390_000_000, 310_000_000))
    )
    if active and day_rng.random() < 0.018:
        tokens += day_rng.randint(800_000_000, 1_180_000_000)
    input_tokens = int(tokens * day_rng.uniform(0.105, 0.125))
    cache_tokens = int(tokens * day_rng.uniform(0.715, 0.755))
    output_tokens = max(0, tokens - input_tokens - cache_tokens)
    messages = 0 if not active else day_rng.randint(47, 826)
    cost = round(
        input_tokens * 0.0000025
        + output_tokens * 0.0000105
        + cache_tokens * 0.00000025,
        6,
    )
    sources = []
    if active:
        for source_index in range(5):
            source_tokens = int(
                tokens * (0.33 - source_index * 0.045) * day_rng.uniform(0.94, 1.06)
            )
            sources.append(
                {
                    "source": TOOL_SPECS[
                        (day.toordinal() + source_index) % len(TOOL_SPECS)
                    ][0],
                    "modelId": SESSION_MODELS[
                        (day.toordinal() + source_index) % len(SESSION_MODELS)
                    ],
                    "providerId": ("openai", "anthropic", "google", "moonshot", "zai")[
                        source_index
                    ],
                    "tokens": {
                        "input": int(source_tokens * 0.12),
                        "output": int(source_tokens * 0.15),
                        "cacheRead": int(source_tokens * 0.73),
                        "cacheWrite": 0,
                        "reasoning": int(source_tokens * 0.025),
                    },
                    "cost": round(cost * (0.31 - source_index * 0.035), 6),
                    "messages": max(1, int(messages * (0.30 - source_index * 0.03))),
                }
            )
    return {
        "date": day.isoformat(),
        "totals": {"tokens": tokens, "cost": cost, "messages": messages},
        "intensity": 0
        if not active
        else min(7, 1 + int(math.log10(max(1, tokens / 10_000_000)) * 2)),
        "tokenBreakdown": {
            "input": input_tokens,
            "output": output_tokens,
            "cacheRead": cache_tokens,
            "cacheWrite": 0,
            "reasoning": int(output_tokens * 0.26),
        },
        "sources": sources,
    }


def dense_stats(year: int | None = None, seed: int = 0) -> dict[str, Any]:
    today = datetime.now().astimezone().date()
    if year is None:
        end = today
        start = end - timedelta(days=419)
    else:
        start = date(year, 1, 1)
        # Never past today: real `compute_stats` builds the contribution list from
        # dates that actually carry usage, so it cannot emit a future day. Asking
        # the year selector for the current year used to fill the graph through
        # December with fabricated activity.
        end = min(date(year, 12, 31), today)
    contributions = [
        _contribution(start + timedelta(days=index), seed)
        for index in range((end - start).days + 1)
    ]
    active = [row for row in contributions if row["totals"]["tokens"] > 0]
    total_tokens = sum(row["totals"]["tokens"] for row in active)
    total_cost = round(sum(row["totals"]["cost"] for row in active), 6)
    messages = sum(row["totals"]["messages"] for row in active)
    return {
        "stats": {
            "favorite_model": "gpt-5.6-sol",
            "most_used_model": "gpt-5.6-sol",
            "highest_cost_model": "claude-opus-4.1",
            "total_tokens": total_tokens,
            "messages": messages,
            "sessions": messages,
            "current_streak": 6,
            "longest_streak": 37,
            "active_days": len(active),
            "total_days": len(contributions),
        },
        "summary": {
            "totalTokens": total_tokens,
            "totalCost": total_cost,
            "activeDays": len(active),
            "totalDays": len(contributions),
        },
        "contributions": contributions,
        "meta": {"source": "dev-fixture", "synthetic": True, "seed": seed},
        "timestamp": _now_iso(),
    }


def dense_activity_insights(seed: int = 0) -> dict[str, Any]:
    insights_rng = _rng(seed, "activity-insights")
    tool_names = (
        "exec",
        "exec_command",
        "node_repl/js",
        "apply_patch",
        "browser/search",
        "browser/open",
        "write_stdin",
        "wait_agent",
        "request_user_input",
        "send_message",
        "view_image",
        "open_in_codex",
        "automation_update",
        "read_thread",
        "list_threads",
        "create_thread",
        "web/search_query",
        "web/open",
    )
    base_counts = [
        48_931,
        22_481,
        11_307,
        7_942,
        5_319,
        4_877,
        3_115,
        2_487,
        1_936,
        1_507,
        1_103,
        894,
        629,
        474,
        319,
        207,
        188,
        147,
    ]
    counts = [int(value * insights_rng.uniform(0.93, 1.07)) for value in base_counts]
    total_calls = sum(counts)
    distribution = [
        {"name": name, "count": count, "share": round(count / total_calls, 6)}
        for name, count in zip(tool_names, counts)
    ]
    reasoning_counts = {
        "xhigh": int(5_712 * insights_rng.uniform(0.94, 1.06)),
        "high": int(3_841 * insights_rng.uniform(0.94, 1.06)),
        "medium": int(1_936 * insights_rng.uniform(0.94, 1.06)),
        "low": int(812 * insights_rng.uniform(0.94, 1.06)),
        "ultra": int(307 * insights_rng.uniform(0.94, 1.06)),
    }
    reasoning_total = sum(reasoning_counts.values())
    reasoning_distribution = [
        {"effort": effort, "count": count, "share": round(count / reasoning_total, 6)}
        for effort, count in reasoning_counts.items()
    ]
    return {
        "scope": {
            "tool": "codex",
            "local": True,
            "primary_only": True,
            "synthetic": True,
        },
        "recorded_chats": {
            "value": int(1_284 * insights_rng.uniform(0.96, 1.04)),
            "coverage": {
                "primary_files": 1_516,
                "files_with_session_id": 1_493,
                "legacy_unavailable_records": 23,
            },
        },
        "reasoning": {
            "most_used": reasoning_distribution[0],
            "distribution": reasoning_distribution,
            "coverage": {
                "identified_turns": reasoning_total,
                "known_effort_turns": reasoning_total,
                "ambiguous_turns": 0,
                "excluded_records": 11,
            },
        },
        "tools": {
            "total_calls": total_calls,
            "most_used": distribution[0],
            "distribution": distribution,
            "coverage": {
                "named_calls": total_calls,
                "ambiguous_name_calls": 0,
                "excluded_records": 37,
            },
        },
        "timestamp": _now_iso(),
    }


def dense_quota(seed: int = 0) -> dict[str, Any]:
    now = int(datetime.now(timezone.utc).timestamp())
    provider_specs = (
        ("codex", "Pro", (("5h", "5-hour window", 78.4), ("7d", "7-day window", 63.7))),
        (
            "claude",
            "Max",
            (("session", "5-hour session", 91.2), ("weekly", "Weekly", 72.9)),
        ),
        (
            "antigravity",
            "AI Pro",
            (("pro", "Pro models", 54.3), ("flash", "Flash models", 26.8)),
        ),
        (
            "minimax",
            "Coding Plan",
            (("5h", "5-hour window", 18.6), ("weekly", "Weekly", 44.1)),
        ),
        (
            "kimi",
            "Allegretto",
            (("5h", "5-hour window", 37.5), ("plan", "Weekly", 81.3)),
        ),
        (
            "grok",
            "SuperGrok",
            (("2h", "2-hour window", 69.7), ("monthly", "Monthly", 33.4)),
        ),
        (
            "zai",
            "GLM Coding Pro",
            (("5h", "5-hour window", 47.8), ("monthly", "Monthly", 58.9)),
        ),
    )
    providers = {}
    for provider_index, (provider, plan, bucket_specs) in enumerate(provider_specs):
        provider_rng = _rng(seed, f"quota:{provider}")
        buckets = []
        for bucket_index, (bucket, label, used) in enumerate(bucket_specs):
            varied_used = round(
                min(97.0, max(3.0, used + provider_rng.uniform(-4.8, 4.8))), 1
            )
            buckets.append(
                {
                    "account": "default",
                    "bucket": bucket,
                    "bucket_label": label,
                    "used_percent": varied_used,
                    "remaining_percent": round(100 - varied_used, 1),
                    "resets_at": now
                    + (bucket_index + 1) * 18_000
                    + provider_index * 2_100,
                    "captured_at": now - provider_index * 71,
                    "source": f"{provider}_api",
                    "status": "ok",
                    "unlimited": False,
                }
            )
        providers[provider] = {
            "provider": provider,
            "network_enabled": True,
            "plan": plan,
            "buckets": buckets,
            "status": "ok" if provider_index not in {3, 5} else "stale",
            "status_detail": None
            if provider_index not in {3, 5}
            else "Last provider response is older than the configured poll interval.",
            "status_at": now - provider_index * 71,
            "updated_at": now - provider_index * 71,
            "sources": [f"{provider}_api"],
            "estimated": provider == "codex",
            "detected": True,
        }
    return {
        "providers": providers,
        "consent": {
            "credential_scan": False,
            **{f"{provider}_api": False for provider in providers},
        },
        "enabled": True,
        "poll": {
            "enabled": True,
            "network_enabled": False,
            "interval": 1800,
            "interval_source": "dev-fixture",
            "interval_minutes": 30,
            "interval_choices": [15, 30, 60, 120],
            "last_run": now,
            "kill_switch": True,
        },
        "timestamp": now,
    }


def dense_quota_history(granularity: str = "hour", seed: int = 0) -> dict[str, Any]:
    now = int(datetime.now(timezone.utc).timestamp())
    step = 3600 if granularity == "hour" else 86_400
    series = []
    for provider_index, (provider, _plan, bucket_specs) in enumerate(
        (
            ("codex", "Pro", (("5h", "5-hour window", 0), ("7d", "7-day window", 0))),
            (
                "claude",
                "Max",
                (("session", "5-hour session", 0), ("weekly", "Weekly", 0)),
            ),
            (
                "antigravity",
                "AI Pro",
                (("pro", "Pro models", 0), ("flash", "Flash models", 0)),
            ),
            ("kimi", "Allegretto", (("5h", "5-hour window", 0), ("plan", "Weekly", 0))),
        )
    ):
        for bucket_index, (bucket, label, _value) in enumerate(bucket_specs):
            series_rng = _rng(seed, f"quota-history:{granularity}:{provider}:{bucket}")
            phase = series_rng.uniform(0, 17)
            slope = series_rng.uniform(3.35, 4.05)
            points = []
            consumption = []
            for point_index in range(72):
                captured_at = now - (71 - point_index) * step
                used = round(
                    (
                        provider_index * 13
                        + bucket_index * 19
                        + phase
                        + point_index * slope
                    )
                    % 96,
                    1,
                )
                points.append({"captured_at": captured_at, "used_percent": used})
                consumption.append(
                    {
                        "period_start": captured_at,
                        "consumed_percent": round(
                            1.2 + (point_index * 1.7 + phase / 3) % 11,
                            1,
                        ),
                    }
                )
            series.append(
                {
                    "provider": provider,
                    "account": "default",
                    "bucket": bucket,
                    "bucket_label": label,
                    "points": points,
                    "consumption": consumption,
                    "estimated": provider == "codex",
                }
            )
    return {"granularity": granularity, "series": series, "any_estimated": True}


def fixture_year_days(year: int) -> int:
    """Small public helper used by tests to pin leap-year fixture coverage."""
    return 366 if calendar.isleap(year) else 365
