from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from .model_normalization import normalize_model_name
from .pricing import PricingDatabase
from .sources.openclaw import get_usage_for_days as get_session_usage_days
from .sources.openclaw import get_usage_for_month as get_session_usage_month
from .sources.openclaw import get_usage_for_year as get_session_usage_year
from .sources.coding_tools import CodingToolsUsageTracker


# ============================================================
# BACKEND CONFIGURATION
# ============================================================
# Local coding-tools parsers have no tokscale runtime dependency.
USE_LOCAL_CODING_TOOLS_BACKEND = True
# ============================================================


def run_tokscale_json(period_args: list[str]) -> Dict[str, Any]:
    """Optional fallback (tokscale backend)."""
    result = subprocess.run(
        ["bunx", "tokscale@latest", "models", "--json", "--no-spinner"] + period_args,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Tokscale failed: {result.stderr.strip()}")
    if not result.stdout.strip():
        raise RuntimeError("Tokscale returned empty output")
    return json.loads(result.stdout)


def _date_range_from_args(period_args: list[str]) -> tuple[Optional[datetime], Optional[datetime]]:
    if "--today" in period_args:
        start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)

    since = None
    until = None
    local_tz = datetime.now().astimezone().tzinfo or timezone.utc
    try:
        if "--since" in period_args:
            since = datetime.strptime(period_args[period_args.index("--since") + 1], "%Y-%m-%d").replace(tzinfo=local_tz)
        if "--until" in period_args:
            # CLI args are inclusive; tracker expects [since, until) exclusive.
            until = (
                datetime.strptime(period_args[period_args.index("--until") + 1], "%Y-%m-%d").replace(tzinfo=local_tz)
                + timedelta(days=1)
            )
    except Exception:
        return None, None

    return since, until


def run_local_coding_tools_json(period_args: list[str]) -> Dict[str, Any]:
    """Collect coding-tool entries using the in-process local parsers."""
    since, until = _date_range_from_args(period_args)
    tracker = CodingToolsUsageTracker()
    tracker.collect(since, until)
    return tracker.to_json()


def parse_entries_json(data: Dict[str, Any]) -> Dict[str, Any]:
    """Parse tokscale-compatible entries JSON and aggregate by app/model."""
    entries = data.get("entries", [])
    pricing_db = PricingDatabase()

    apps: Dict[str, Any] = {}
    all_models_dict: Dict[tuple[str, str], Any] = {}

    for entry in entries:
        source = entry.get("source", "unknown")
        model = entry.get("model", "unknown")
        provider = entry.get("provider", "")
        full_model_name = f"{provider}/{model}" if provider else model

        input_raw = int(entry.get("input", 0) or 0)
        tokens_out = int(entry.get("output", 0) or 0)
        cache_read = int(entry.get("cacheRead", 0) or 0)
        cache_write = int(entry.get("cacheWrite", 0) or 0)

        # Reporting semantics: cacheWrite is billable input (not discounted cache).
        tokens_in = input_raw + cache_write
        tokens_cache = cache_read
        total_tokens = tokens_in + tokens_out + tokens_cache

        # Suppress fully empty rows.
        if total_tokens == 0:
            continue

        # Recompute cost from local pricing DB for consistency.
        cost = pricing_db.get_cost(full_model_name, input_raw, tokens_out, cache_read, cache_write)
        messages = int(entry.get("messageCount", 0) or 1)

        if source not in apps:
            apps[source] = {
                "tokens": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "tokens_cache": 0,
                "cost": 0.0,
                "messages": 0,
                "models_dict": {},
            }

        app_ref = apps[source]
        app_ref["tokens"] += total_tokens
        app_ref["tokens_in"] += tokens_in
        app_ref["tokens_out"] += tokens_out
        app_ref["tokens_cache"] += tokens_cache
        app_ref["cost"] += cost
        app_ref["messages"] += messages

        model_ref = app_ref["models_dict"].setdefault(
            full_model_name,
            {
                "name": full_model_name,
                "tokens": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "tokens_cache": 0,
                "cost": 0.0,
                "messages": 0,
            },
        )
        model_ref["tokens"] += total_tokens
        model_ref["tokens_in"] += tokens_in
        model_ref["tokens_out"] += tokens_out
        model_ref["tokens_cache"] += tokens_cache
        model_ref["cost"] += cost
        model_ref["messages"] += messages

        global_key = (source, full_model_name)
        g = all_models_dict.setdefault(
            global_key,
            {
                "source": source,
                "name": full_model_name,
                "tokens": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "tokens_cache": 0,
                "cost": 0.0,
                "messages": 0,
            },
        )
        g["tokens"] += total_tokens
        g["tokens_in"] += tokens_in
        g["tokens_out"] += tokens_out
        g["tokens_cache"] += tokens_cache
        g["cost"] += cost
        g["messages"] += messages

    for app_data in apps.values():
        app_data["models"] = sorted(app_data["models_dict"].values(), key=lambda x: x["cost"], reverse=True)
        del app_data["models_dict"]

    all_models = sorted(all_models_dict.values(), key=lambda x: x["cost"], reverse=True)
    return {
        "total_cost": sum(x["cost"] for x in all_models),
        "total_tokens": sum(x["tokens"] for x in all_models),
        "total_messages": sum(x["messages"] for x in all_models),
        "apps": apps,
        "all_models": all_models,
    }


def period_to_days(period: str) -> int:
    try:
        return max(1, int(period))
    except ValueError:
        mapping = {
            "today": 1,
            "3days": 3,
            "week": 7,
            "14days": 14,
            "month": 30,
        }
        return mapping.get(period, 1)


def period_to_range_args(period: str) -> list[str]:
    if period == "month":
        now_local = datetime.now().astimezone()
        start_date = now_local.replace(day=1).date()
        end_date = now_local.date()
        return ["--since", start_date.strftime("%Y-%m-%d"), "--until", end_date.strftime("%Y-%m-%d")]

    days = period_to_days(period)
    if days == 1:
        return ["--today"]
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=days - 1)
    return ["--since", start_date.strftime("%Y-%m-%d"), "--until", end_date.strftime("%Y-%m-%d")]


def get_session_data(period: str) -> Dict[str, Any]:
    if period == "month":
        return get_session_usage_month()
    days = period_to_days(period)
    return get_session_usage_days(days)


def get_openclaw_data(period: str) -> Dict[str, Any]:
    """Compatibility wrapper: OpenClaw usage comes from local session logs."""
    return get_session_data(period)


def _has_visible_token_usage(row: Dict[str, Any]) -> bool:
    """A row is visible if any of input/output/cacheRead/cacheWrite token dimensions are non-zero."""
    tokens_in = int(row.get("tokens_in", row.get("input", 0)) or 0)
    tokens_out = int(row.get("tokens_out", row.get("output", 0)) or 0)
    cache_read = int(row.get("cache_read", row.get("cacheRead", 0)) or 0)
    cache_write = int(row.get("cache_write", row.get("cacheWrite", 0)) or 0)

    # Some sources expose only aggregate cache tokens.
    if cache_read == 0 and cache_write == 0:
        cache_total = int(row.get("tokens_cache", 0) or 0)
        cache_read = cache_total

    return (tokens_in + tokens_out + cache_read + cache_write) > 0


def _normalize_model_name(name: str) -> str:
    """Compatibility wrapper for model canonicalization."""
    return normalize_model_name(name)


def _contributions_from_entries(entries: list[dict]) -> list[dict]:
    by_date: Dict[str, dict] = {}

    for e in entries:
        ts_ms = int(e.get("timestamp", 0) or 0)
        if ts_ms <= 0:
            continue
        dt = datetime.fromtimestamp(ts_ms / 1000, timezone.utc).astimezone()
        date = dt.strftime("%Y-%m-%d")

        day = by_date.setdefault(
            date,
            {
                "date": date,
                "totals": {"tokens": 0, "cost": 0.0, "messages": 0},
                "intensity": 0,
                "tokenBreakdown": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "reasoning": 0},
                "sources": [],
            },
        )

        input_raw = int(e.get("input", 0) or 0)
        output_t = int(e.get("output", 0) or 0)
        cache_r = int(e.get("cacheRead", 0) or 0)
        cache_w = int(e.get("cacheWrite", 0) or 0)
        reasoning = int(e.get("reasoning", 0) or 0)
        cost = float(e.get("cost", 0.0) or 0.0)

        # Reporting semantics: cacheWrite counts as billable input.
        input_t = input_raw + cache_w
        total = input_t + output_t + cache_r + reasoning

        day["totals"]["tokens"] += total
        day["totals"]["cost"] += cost
        day["totals"]["messages"] += 1

        tb = day["tokenBreakdown"]
        tb["input"] += input_t
        tb["output"] += output_t
        tb["cacheRead"] += cache_r
        tb["cacheWrite"] += 0
        tb["reasoning"] += reasoning

        day["sources"].append(
            {
                "source": e.get("source", "unknown"),
                "modelId": e.get("model", "unknown"),
                "providerId": e.get("provider", "") or "unknown",
                "tokens": {
                    "input": input_t,
                    "output": output_t,
                    "cacheRead": cache_r,
                    "cacheWrite": 0,
                    "reasoning": reasoning,
                },
                "cost": cost,
                "messages": 1,
            }
        )

    return [by_date[k] for k in sorted(by_date.keys())]


def get_tools_data(period: str) -> Dict[str, Any]:
    period_args = period_to_range_args(period)
    backend_json = (
        run_local_coding_tools_json(period_args) if USE_LOCAL_CODING_TOOLS_BACKEND else run_tokscale_json(period_args)
    )
    return parse_entries_json(backend_json)


def compute_usage(period: str) -> Dict[str, Any]:
    openclaw_data = get_openclaw_data(period)
    coding_data = get_tools_data(period)

    coding_apps = {k: v for k, v in coding_data.get("apps", {}).items() if k.lower() != "openclaw"}
    coding_models = [
        m
        for m in coding_data.get("all_models", [])
        if (m.get("source") or "").lower() != "openclaw" and _has_visible_token_usage(m)
    ]

    total_tokens = openclaw_data["total_tokens"] + sum(v.get("tokens", 0) for v in coding_apps.values())
    total_cost = openclaw_data["total_cost"] + sum(v.get("cost", 0.0) for v in coding_apps.values())

    by_tool = {name: {"tokens": data["tokens"], "cost": data["cost"]} for name, data in coding_apps.items()}
    by_tool["openclaw"] = {"tokens": openclaw_data["total_tokens"], "cost": openclaw_data["total_cost"]}

    openclaw_models = sorted(
        [{"name": k, **v} for k, v in openclaw_data["models"].items() if _has_visible_token_usage(v)],
        key=lambda x: x.get("cost", 0.0),
        reverse=True,
    )

    combined_by_model: Dict[str, dict] = {}

    def add_row(row: dict):
        if not _has_visible_token_usage(row):
            return
        key = _normalize_model_name(row.get("name", "unknown"))
        cur = combined_by_model.setdefault(
            key,
            {
                "name": key,
                "tokens": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "tokens_cache": 0,
                "cost": 0.0,
                "messages": 0,
            },
        )
        cur["tokens"] += int(row.get("tokens", 0) or 0)
        cur["tokens_in"] += int(row.get("tokens_in", 0) or 0)
        cur["tokens_out"] += int(row.get("tokens_out", 0) or 0)
        cur["tokens_cache"] += int(row.get("tokens_cache", 0) or 0)
        cur["cost"] += float(row.get("cost", 0.0) or 0.0)
        cur["messages"] += int(row.get("messages", 0) or 0)

    for r in coding_models:
        add_row(r)
    for r in openclaw_models:
        add_row(r)

    combined_models = sorted(combined_by_model.values(), key=lambda x: x.get("cost", 0.0), reverse=True)

    return {
        "period": period,
        "total_tokens": total_tokens,
        "total_cost": round(total_cost, 2),
        "by_tool": by_tool,
        "apps": coding_apps,
        "coding_apps": coding_apps,
        "coding_models": coding_models,
        "top_models": combined_models[:5],
        "openclaw_models": openclaw_models,
        "combined_models": combined_models,
        "timestamp": datetime.now().isoformat(),
    }


def compute_stats(year: Optional[int] = None) -> Dict[str, Any]:
    """Contribution graph and stats (OpenClaw + coding tools)."""
    session_data = get_session_usage_year(year) if year else get_session_usage_days(365)
    ocl_map = {c.get("date"): c for c in session_data.get("contributions", [])}

    if USE_LOCAL_CODING_TOOLS_BACKEND:
        if year:
            start_date = datetime(year, 1, 1)
            end_date = datetime(year, 12, 31)
            period_args = ["--since", start_date.strftime("%Y-%m-%d"), "--until", end_date.strftime("%Y-%m-%d")]
        else:
            period_args = period_to_range_args("365")

        coding_entries = run_local_coding_tools_json(period_args).get("entries", [])
        coding_contribs = _contributions_from_entries(coding_entries)
    else:
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as f:
            temp_path = f.name
        args = ["bunx", "tokscale@latest", "graph", "--no-spinner", "--output", temp_path]
        if year:
            args.extend(["--year", str(year)])
        result = subprocess.run(args, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(f"Tokscale graph failed: {result.stderr.strip()}")
        with open(temp_path, "r", encoding="utf-8") as f:
            coding_contribs = json.load(f).get("contributions", [])
        os.unlink(temp_path)

    # Remove tokscale/openclaw duplicate if fallback mode.
    for day in coding_contribs:
        day["sources"] = [s for s in day.get("sources", []) if s.get("source", "").lower() != "openclaw"]

    coding_map = {c.get("date"): c for c in coding_contribs}
    all_dates = sorted(set(ocl_map.keys()) | set(coding_map.keys()))

    merged = []
    for date in all_dates:
        ocl_day = ocl_map.get(date)
        c_day = coding_map.get(date)
        if ocl_day and c_day:
            o_tb = ocl_day.get("tokenBreakdown") or {}
            c_tb = c_day.get("tokenBreakdown") or {}
            merged.append(
                {
                    "date": date,
                    "totals": {
                        "tokens": int((ocl_day.get("totals") or {}).get("tokens", 0))
                        + int((c_day.get("totals") or {}).get("tokens", 0)),
                        "cost": float((ocl_day.get("totals") or {}).get("cost", 0.0))
                        + float((c_day.get("totals") or {}).get("cost", 0.0)),
                        "messages": int((ocl_day.get("totals") or {}).get("messages", 0))
                        + int((c_day.get("totals") or {}).get("messages", 0)),
                    },
                    "intensity": max(int(ocl_day.get("intensity") or 0), int(c_day.get("intensity") or 0)),
                    "tokenBreakdown": {
                        "input": int(o_tb.get("input", 0)) + int(c_tb.get("input", 0)),
                        "output": int(o_tb.get("output", 0)) + int(c_tb.get("output", 0)),
                        "cacheRead": int(o_tb.get("cacheRead", 0)) + int(c_tb.get("cacheRead", 0)),
                        "cacheWrite": int(o_tb.get("cacheWrite", 0)) + int(c_tb.get("cacheWrite", 0)),
                        "reasoning": int(o_tb.get("reasoning", 0)) + int(c_tb.get("reasoning", 0)),
                    },
                    "sources": (ocl_day.get("sources") or []) + (c_day.get("sources") or []),
                }
            )
        elif ocl_day:
            merged.append(ocl_day)
        elif c_day:
            merged.append(c_day)

    model_costs: Dict[str, float] = {}
    for day in merged:
        for s in day.get("sources", []):
            model = s.get("modelId", "unknown")
            model_costs[model] = model_costs.get(model, 0.0) + float(s.get("cost", 0.0) or 0.0)
    favorite_model = max(model_costs.items(), key=lambda x: x[1])[0] if model_costs else "N/A"

    total_tokens = sum(int((d.get("totals") or {}).get("tokens", 0)) for d in merged)
    total_cost = sum(float((d.get("totals") or {}).get("cost", 0.0)) for d in merged)
    total_messages = sum(int((d.get("totals") or {}).get("messages", 0)) for d in merged)
    active_days = len(merged)

    if merged:
        first_date = datetime.strptime(merged[0]["date"], "%Y-%m-%d").date()
        last_date = datetime.strptime(merged[-1]["date"], "%Y-%m-%d").date()
        total_days_span = (last_date - first_date).days + 1
    else:
        total_days_span = 0

    return {
        "meta": {"source": "merged"},
        "summary": {"totalTokens": total_tokens, "totalCost": total_cost, "activeDays": active_days, "totalDays": total_days_span},
        "contributions": merged,
        "stats": {
            "favorite_model": favorite_model,
            "total_tokens": total_tokens,
            "sessions": total_messages,
            "current_streak": 0,
            "longest_streak": 0,
            "active_days": active_days,
            "total_days": total_days_span,
        },
        "timestamp": datetime.now().isoformat(),
    }
