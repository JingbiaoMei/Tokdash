from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .dateutil import parse_date_range
from .model_normalization import normalize_model_name
from .pricing import PricingDatabase
from .sources.openclaw import get_usage_for_days as get_session_usage_days
from .sources.openclaw import get_usage_for_month as get_session_usage_month
from .sources.openclaw import get_usage_for_range as get_session_usage_range
from .sources.openclaw import get_usage_for_year as get_session_usage_year
from .sources.coding_tools import CodingToolsUsageTracker
from .usage_store import (
    UsageDatabaseSchemaTooNewError,
    UsageEntryStore,
    build_source_signature,
    persistent_pricing_signature,
    persistent_usage_db_enabled,
    public_usage_entry,
)


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


def _usage_store_sources(tracker: CodingToolsUsageTracker) -> list[str]:
    return [
        name
        for name, parser in tracker.parsers.items()
        if getattr(parser, "sync_capability").mode != "source_native_db"
    ]


def _usage_store_live_sources(tracker: CodingToolsUsageTracker) -> list[str]:
    return [
        name
        for name, parser in tracker.parsers.items()
        if getattr(parser, "sync_capability").mode == "source_native_db"
    ]


def _collect_parser_file(parser: Any, file_sig: tuple[str, int, int]) -> list[dict[str, Any]]:
    original_file_signatures = parser._file_signatures
    try:
        parser._file_signatures = lambda: (file_sig,)
        return parser._parse_all()
    finally:
        parser._file_signatures = original_file_signatures


def _complete_jsonl_tail(path: str, start_offset: int) -> tuple[str, int]:
    with open(path, "rb") as handle:
        handle.seek(max(0, int(start_offset)))
        data = handle.read()
    if not data:
        return "", int(start_offset)
    last_newline = data.rfind(b"\n")
    if last_newline < 0:
        return "", int(start_offset)
    safe_offset = int(start_offset) + last_newline + 1
    return data[: last_newline + 1].decode("utf-8"), safe_offset


def _collect_parser_tail(parser: Any, file_sig: tuple[str, int, int], start_offset: int) -> tuple[list[dict[str, Any]], int]:
    path = file_sig[0]
    if not str(path).endswith(".jsonl"):
        raise ValueError("tail append is only enabled for JSONL files")
    text, safe_offset = _complete_jsonl_tail(path, start_offset)
    if not text:
        return [], safe_offset

    tmp_path = None
    original_file_signatures = parser._file_signatures
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=Path(path).suffix, delete=False) as handle:
            handle.write(text)
            tmp_path = handle.name
        stat = Path(tmp_path).stat()
        parser._file_signatures = lambda: ((str(tmp_path), int(stat.st_mtime_ns), int(stat.st_size)),)
        return parser._parse_all(), safe_offset
    finally:
        parser._file_signatures = original_file_signatures
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _sync_usage_store(tracker: CodingToolsUsageTracker) -> tuple[UsageEntryStore, list[str]]:
    """Bring the persistent usage cache up to date for this tracker's sources.

    Three identities, kept apart on purpose:

    * source identity — the file paths, mtimes and sizes each parser reports,
      plus the safe offset a tail append stopped at;
    * parse identity — each parser's explicit ``persistent_parser_version``,
      so adding or changing one parser cannot invalidate another's rows;
    * pricing identity — the effective pricing content and implementation,
      applied by repricing the stored billing inputs. It is NOT part of any
      parse signature, so a rate edit never reparses a source log.

    See docs/development/technical-notes/USAGE_CACHE_IDENTITY.md.
    """
    store = UsageEntryStore()
    selected = _usage_store_sources(tracker)
    pricing = persistent_pricing_signature(tracker.pricing_db)
    # Rates first: rows land at the current pricing whether they were already
    # cached (repriced here) or inserted by the syncs below. Parsing happens
    # outside the store lock, so each sync also declares the pricing it parsed
    # under; a sync that lands after another process has moved the database on
    # drops the stored identity rather than committing costs under someone
    # else's (see UsageEntryStore._drop_stale_pricing_identity).
    # This connect also doubles as the too-new-schema guard for every path below:
    # it runs BEFORE the parsers enumerate their source files, so a mismatched
    # database costs one open here rather than a full discovery scan per request.
    # Keep it ahead of the loop -- moving it after would reintroduce that scan.
    store.apply_pricing(pricing, tracker.pricing_db)
    for name in selected:
        parser = tracker.parsers[name]
        capability = getattr(parser, "sync_capability")
        files = parser._file_signatures()
        parser_sig = parser.persistent_parser_signature()
        if capability.mode == "file_replace":
            store.sync_files(
                name,
                files,
                parser=parser_sig,
                pricing_identity=pricing,
                parse_file_entries=lambda file_sig, parser=parser: _collect_parser_file(parser, file_sig),
                parse_file_tail_entries=(
                    (lambda file_sig, start_offset, parser=parser: _collect_parser_tail(parser, file_sig, start_offset))
                    if capability.append_jsonl
                    else None
                ),
            )
            continue
        if capability.mode != "source_replace":
            continue
        signature = build_source_signature(
            files=files,
            parser=parser_sig,
            extra=parser.runtime_config_signature(),
        )
        store.sync_source(
            name,
            signature,
            lambda parser=parser: parser.collect(None, None),
            pricing_identity=pricing,
        )
    # If any sync above dropped the identity, this rebuilds every cost from the
    # stored billing inputs so the request does not serve a mixed table. It is a
    # single meta lookup when nothing raced, which is the normal case.
    store.apply_pricing(pricing, tracker.pricing_db)
    return store, selected


def _collect_live_coding_entries(
    tracker: CodingToolsUsageTracker,
    since: Optional[datetime],
    until: Optional[datetime],
    sources: list[str],
) -> list[dict[str, Any]]:
    if not sources:
        return []
    tracker.collect(since, until, sources)
    # Live rows carry the same private billing provenance stored rows do; strip
    # it here so the live and cached paths hand callers identical shapes.
    return [public_usage_entry(entry) for entry in tracker.to_json().get("entries", [])]


def _merge_parsed_usage(parts: list[Dict[str, Any]]) -> Dict[str, Any]:
    apps: Dict[str, Any] = {}
    all_models_dict: Dict[tuple[str, str], Any] = {}

    for part in parts:
        for row in part.get("all_models", []) or []:
            source = str(row.get("source") or "unknown")
            name = str(row.get("name") or "unknown")
            tokens = int(row.get("tokens", 0) or 0)
            if tokens == 0:
                continue
            tokens_in = int(row.get("tokens_in", 0) or 0)
            tokens_out = int(row.get("tokens_out", 0) or 0)
            tokens_cache = int(row.get("tokens_cache", 0) or 0)
            cost = float(row.get("cost", 0.0) or 0.0)
            messages = int(row.get("messages", 0) or 0)

            app_ref = apps.setdefault(
                source,
                {
                    "tokens": 0,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "tokens_cache": 0,
                    "cost": 0.0,
                    "messages": 0,
                    "models_dict": {},
                },
            )
            app_ref["tokens"] += tokens
            app_ref["tokens_in"] += tokens_in
            app_ref["tokens_out"] += tokens_out
            app_ref["tokens_cache"] += tokens_cache
            app_ref["cost"] += cost
            app_ref["messages"] += messages

            model_ref = app_ref["models_dict"].setdefault(
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
            model_ref["tokens"] += tokens
            model_ref["tokens_in"] += tokens_in
            model_ref["tokens_out"] += tokens_out
            model_ref["tokens_cache"] += tokens_cache
            model_ref["cost"] += cost
            model_ref["messages"] += messages

            global_ref = all_models_dict.setdefault(
                (source, name),
                {
                    "source": source,
                    "name": name,
                    "tokens": 0,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "tokens_cache": 0,
                    "cost": 0.0,
                    "messages": 0,
                },
            )
            global_ref["tokens"] += tokens
            global_ref["tokens_in"] += tokens_in
            global_ref["tokens_out"] += tokens_out
            global_ref["tokens_cache"] += tokens_cache
            global_ref["cost"] += cost
            global_ref["messages"] += messages

    for app_data in apps.values():
        app_data["models"] = sorted(app_data["models_dict"].values(), key=lambda x: x["cost"], reverse=True)
        del app_data["models_dict"]
        for model_ref in app_data["models"]:
            model_ref["cache_hit_rate"] = cache_hit_rate(model_ref["tokens_in"], model_ref["tokens_cache"])
        app_data["cache_hit_rate"] = cache_hit_rate(app_data["tokens_in"], app_data["tokens_cache"])

    all_models = sorted(all_models_dict.values(), key=lambda x: x["cost"], reverse=True)
    for row in all_models:
        row["cache_hit_rate"] = cache_hit_rate(row["tokens_in"], row["tokens_cache"])

    total_in = sum(x["tokens_in"] for x in all_models)
    total_cache = sum(x["tokens_cache"] for x in all_models)
    return {
        "total_cost": sum(x["cost"] for x in all_models),
        "total_tokens": sum(x["tokens"] for x in all_models),
        "total_messages": sum(x["messages"] for x in all_models),
        "cache_hit_rate": cache_hit_rate(total_in, total_cache),
        "apps": apps,
        "all_models": all_models,
    }


def _merge_contribution_days(parts: list[list[dict]]) -> list[dict]:
    by_date: Dict[str, dict] = {}
    for contributions in parts:
        for src_day in contributions:
            date = src_day.get("date")
            if not date:
                continue
            day = by_date.setdefault(
                str(date),
                {
                    "date": str(date),
                    "totals": {"tokens": 0, "cost": 0.0, "messages": 0},
                    "intensity": 0,
                    "tokenBreakdown": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "reasoning": 0},
                    "sources": [],
                },
            )
            src_totals = src_day.get("totals") or {}
            day["totals"]["tokens"] += int(src_totals.get("tokens", 0) or 0)
            day["totals"]["cost"] += float(src_totals.get("cost", 0.0) or 0.0)
            day["totals"]["messages"] += int(src_totals.get("messages", 0) or 0)
            day["intensity"] = max(int(day.get("intensity") or 0), int(src_day.get("intensity") or 0))

            src_tb = src_day.get("tokenBreakdown") or {}
            tb = day["tokenBreakdown"]
            tb["input"] += int(src_tb.get("input", 0) or 0)
            tb["output"] += int(src_tb.get("output", 0) or 0)
            tb["cacheRead"] += int(src_tb.get("cacheRead", 0) or 0)
            tb["cacheWrite"] += int(src_tb.get("cacheWrite", 0) or 0)
            tb["reasoning"] += int(src_tb.get("reasoning", 0) or 0)
            day["sources"].extend(src_day.get("sources") or [])

    return [by_date[k] for k in sorted(by_date.keys())]


def run_local_coding_tools_json(period_args: list[str]) -> Dict[str, Any]:
    """Collect coding-tool entries using the in-process local parsers."""
    since, until = _date_range_from_args(period_args)
    tracker = CodingToolsUsageTracker()
    if persistent_usage_db_enabled():
        try:
            store, stored_sources = _sync_usage_store(tracker)
            entries = store.query_entries(sources=stored_sources, since=since, until=until)
            entries.extend(_collect_live_coding_entries(tracker, since, until, _usage_store_live_sources(tracker)))
            entries.sort(key=lambda e: int(e.get("timestamp", 0) or 0))
            return {"entries": entries}
        except UsageDatabaseSchemaTooNewError:
            # A newer database is terminal for this build; reparsing the logs
            # instead would hide the skew behind a permanent full-history reparse.
            raise
        except Exception:
            # The persistent DB is a cache. If it is corrupt or temporarily
            # unavailable, preserve current behavior by falling back to the live
            # parsers for this request.
            pass
    tracker.collect(since, until)
    data = tracker.to_json()
    data["entries"] = [public_usage_entry(entry) for entry in data.get("entries", [])]
    return data


def cache_hit_rate(tokens_in: Any, tokens_cache: Any) -> Optional[float]:
    """Token-weighted prompt cache-hit rate.

    The faithful definition shared across providers (DeepSeek prompt_cache_hit /
    prompt_tokens, Anthropic cache_read / (input + cache_creation + cache_read),
    OpenAI cached / prompt, Gemini cached / promptTokenCount) is the share of
    *prompt input* tokens served from cache. In tokdash's normalized model the
    denominator is ``tokens_in + tokens_cache`` because ``tokens_in`` already folds
    cacheWrite into billable input (input_raw + cacheWrite) and ``tokens_cache`` is
    the cacheRead hit count. Output and reasoning tokens are never cacheable and are
    excluded. Returns ``None`` when there is no prompt input (render as ``n/a``);
    never average per-row ratios — aggregate the raw token sums then call this.
    """
    num = int(tokens_cache or 0)
    den = int(tokens_in or 0) + num
    if den <= 0:
        return None
    return round(num / den, 4)


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
        reasoning = int(entry.get("reasoning", 0) or 0)

        # Reporting semantics: cacheWrite is billable input (not discounted cache).
        tokens_in = input_raw + cache_write
        tokens_cache = cache_read
        # Reasoning tokens are billable output for o-series / extended-thinking
        # models, so they belong in the headline totals (matches ccusage's
        # `totalTokens` definition and keeps Overview consistent with Stats).
        total_tokens = tokens_in + tokens_out + tokens_cache + reasoning

        # Suppress fully empty rows.
        if total_tokens == 0:
            continue

        # Prefer parser-provided cost when present (pi-agent ships per-message
        # cost.total in the log; Hermes ships actual_cost_usd / estimated_cost_usd
        # with subscription-aware precedence). Falling back to pricing-DB recompute
        # only when the parser couldn't determine cost keeps Overview/API costs
        # aligned with the per-source semantics and with the Stats tab, which
        # already trusts entry["cost"].
        entry_cost = float(entry.get("cost") or 0.0)
        # costAuthoritative marks a recorded zero as the source's own
        # billing decision (a free request): accept it instead of
        # guessing a price from the token buckets.
        if entry_cost > 0 or entry.get("costAuthoritative") is True:
            cost = entry_cost
        else:
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
        for model_ref in app_data["models"]:
            model_ref["cache_hit_rate"] = cache_hit_rate(model_ref["tokens_in"], model_ref["tokens_cache"])
        app_data["cache_hit_rate"] = cache_hit_rate(app_data["tokens_in"], app_data["tokens_cache"])

    all_models = sorted(all_models_dict.values(), key=lambda x: x["cost"], reverse=True)
    for m in all_models:
        m["cache_hit_rate"] = cache_hit_rate(m["tokens_in"], m["tokens_cache"])

    # Tools-only (excludes openclaw, which is merged later in compute_usage) token-
    # weighted aggregate, kept alongside the totals for any direct consumer of this fn.
    tools_in = sum(x["tokens_in"] for x in all_models)
    tools_cache = sum(x["tokens_cache"] for x in all_models)
    return {
        "total_cost": sum(x["cost"] for x in all_models),
        "total_tokens": sum(x["tokens"] for x in all_models),
        "total_messages": sum(x["messages"] for x in all_models),
        "cache_hit_rate": cache_hit_rate(tools_in, tools_cache),
        "apps": apps,
        "all_models": all_models,
    }


def period_to_days(period: str) -> int:
    # ~100 years: a finite stand-in for "all time" that period_to_range_args can
    # turn into a concrete --since/--until window covering every transcript.
    ALL_TIME_DAYS = 36500
    try:
        return max(1, int(period))
    except ValueError:
        mapping = {
            "today": 1,
            "3days": 3,
            "week": 7,
            "14days": 14,
            "month": 30,
            "year": 365,
            "all": ALL_TIME_DAYS,
        }
        # Named periods we don't recognise previously fell through to 1 (today),
        # which silently truncated `?period=all` / `?period=year` to a single day
        # and looked like a massive undercount. Default to all-time instead so an
        # unknown period over-reports (visibly wrong) rather than under-reports.
        return mapping.get(period, ALL_TIME_DAYS)


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


def get_openclaw_data_for_range(date_from: str, date_to: str) -> Dict[str, Any]:
    """Get OpenClaw data for a date range specified as strings (YYYY-MM-DD)."""
    since, until = parse_date_range(date_from, date_to)
    return get_session_usage_range(since, until)


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
    if USE_LOCAL_CODING_TOOLS_BACKEND:
        since, until = _date_range_from_args(period_args)
        return get_tools_data_for_range(since, until)
    return parse_entries_json(run_tokscale_json(period_args))


def get_tools_data_for_range(since: Optional[datetime], until: Optional[datetime]) -> Dict[str, Any]:
    if USE_LOCAL_CODING_TOOLS_BACKEND:
        tracker = CodingToolsUsageTracker()
        if persistent_usage_db_enabled():
            try:
                store, stored_sources = _sync_usage_store(tracker)
                store_data = store.aggregate_entries(sources=stored_sources, since=since, until=until)
                live_entries = _collect_live_coding_entries(tracker, since, until, _usage_store_live_sources(tracker))
                live_data = parse_entries_json({"entries": live_entries})
                result = _merge_parsed_usage([store_data, live_data])
                # Only the live (non-stored) sources were just collected, so this
                # lists the sources that failed to read — the UI/API can then show
                # "unavailable" instead of a zero that reads as "no usage".
                result["source_errors"] = [e["source"] for e in tracker.source_errors]
                return result
            except UsageDatabaseSchemaTooNewError:
                # Fail-open covers a sick cache, not an unreadable one: a newer
                # schema never heals on retry, so degrading here would reparse the
                # full history on every request.
                raise
            except Exception:
                # Keep the DB fail-open: serving correctness should not depend on
                # cache health while this backend is still evolving.
                pass
        tracker.collect(since, until)
        result = parse_entries_json(tracker.to_json())
        result["source_errors"] = [e["source"] for e in tracker.source_errors]
        return result

    since_str = since.astimezone().strftime("%Y-%m-%d")
    until_str = (until.astimezone() - timedelta(microseconds=1)).strftime("%Y-%m-%d")
    return parse_entries_json(run_tokscale_json(["--since", since_str, "--until", until_str]))


def get_tools_data_for_range_str(date_from: str, date_to: str) -> Dict[str, Any]:
    """Get tools data for a date range specified as strings (YYYY-MM-DD)."""
    since, until = parse_date_range(date_from, date_to)
    return get_tools_data_for_range(since, until)


def get_tools_contributions_for_range(since: Optional[datetime], until: Optional[datetime]) -> list[dict]:
    if USE_LOCAL_CODING_TOOLS_BACKEND:
        tracker = CodingToolsUsageTracker()
        if persistent_usage_db_enabled():
            try:
                store, stored_sources = _sync_usage_store(tracker)
                store_days = store.contribution_days(sources=stored_sources, since=since, until=until)
                live_entries = _collect_live_coding_entries(tracker, since, until, _usage_store_live_sources(tracker))
                live_days = _contributions_from_entries(live_entries)
                return _merge_contribution_days([store_days, live_days])
            except UsageDatabaseSchemaTooNewError:
                raise
            except Exception:
                pass
        tracker.collect(since, until)
        return _contributions_from_entries(tracker.to_json().get("entries", []))

    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as f:
        temp_path = f.name
    args = ["bunx", "tokscale@latest", "graph", "--no-spinner", "--output", temp_path]
    if since and until:
        since_str = since.astimezone().strftime("%Y-%m-%d")
        until_str = (until.astimezone() - timedelta(microseconds=1)).strftime("%Y-%m-%d")
        args.extend(["--since", since_str, "--until", until_str])
    result = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"Tokscale graph failed: {result.stderr.strip()}")
    with open(temp_path, "r", encoding="utf-8") as f:
        coding_contribs = json.load(f).get("contributions", [])
    os.unlink(temp_path)
    return coding_contribs


def compute_usage(period: str, date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict[str, Any]:
    # If specific dates are provided, use them instead of period
    if date_from and date_to:
        openclaw_data = get_openclaw_data_for_range(date_from, date_to)
        coding_data = get_tools_data_for_range_str(date_from, date_to)
    else:
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

    by_tool = {
        name: {
            "tokens": data["tokens"],
            "cost": data["cost"],
            "tokens_in": data.get("tokens_in", 0),
            "tokens_cache": data.get("tokens_cache", 0),
            "cache_hit_rate": cache_hit_rate(data.get("tokens_in", 0), data.get("tokens_cache", 0)),
        }
        for name, data in coding_apps.items()
    }
    ocl_in = openclaw_data.get("total_tokens_in", 0)
    ocl_cache = openclaw_data.get("total_tokens_cache", 0)
    by_tool["openclaw"] = {
        "tokens": openclaw_data["total_tokens"],
        "cost": openclaw_data["total_cost"],
        "tokens_in": ocl_in,
        "tokens_cache": ocl_cache,
        "cache_hit_rate": cache_hit_rate(ocl_in, ocl_cache),
    }

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
    for row in combined_models:
        row["cache_hit_rate"] = cache_hit_rate(row["tokens_in"], row["tokens_cache"])
    total_messages = openclaw_data["total_messages"] + sum(v.get("messages", 0) for v in coding_apps.values())

    # Header "Average Cache Hit Rate": token-weighted across every tool + openclaw.
    global_in = sum(r["tokens_in"] for r in combined_models)
    global_cache = sum(r["tokens_cache"] for r in combined_models)

    return {
        "period": period,
        "total_tokens": total_tokens,
        "total_cost": round(total_cost, 2),
        "total_messages": total_messages,
        "cache_hit_rate": cache_hit_rate(global_in, global_cache),
        "by_tool": by_tool,
        "apps": coding_apps,
        "coding_apps": coding_apps,
        "coding_models": coding_models,
        "top_models": combined_models[:5],
        "openclaw_models": openclaw_models,
        "combined_models": combined_models,
        # Sources that failed to read this collection (empty when all succeeded):
        # lets the UI and API consumers tell a failed source apart from a source
        # that genuinely had no usage in range.
        "source_errors": coding_data.get("source_errors", []),
        "timestamp": datetime.now().isoformat(),
    }


def _current_period_range(period: str) -> tuple[datetime, datetime]:
    now_local = datetime.now().astimezone()
    local_tz = now_local.tzinfo or timezone.utc

    if period == "month":
        since_local = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        days = period_to_days(period)
        today_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        start_date = today_midnight.date() - timedelta(days=days - 1)
        since_local = datetime.combine(start_date, datetime.min.time(), tzinfo=local_tz)

    return since_local.astimezone(timezone.utc), now_local.astimezone(timezone.utc)


def previous_period_range(period: str) -> tuple[datetime, datetime]:
    current_since, current_until = _current_period_range(period)
    if period == "month":
        prev_until = current_since
        prev_until_local = prev_until.astimezone()
        prev_month_anchor = prev_until_local - timedelta(days=1)
        prev_since_local = prev_month_anchor.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return prev_since_local.astimezone(timezone.utc), prev_until

    if period_to_days(period) == 1:
        prev_since = current_since - timedelta(days=1)
        prev_until = current_since
        return prev_since, prev_until

    days = period_to_days(period)
    prev_until = current_since
    prev_since = prev_until - timedelta(days=days)
    return prev_since, prev_until


def _compute_previous_usage(period: str, date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict[str, Any]:
    # If specific dates are provided, calculate previous period based on date range
    if date_from and date_to:
        current_since, current_until = parse_date_range(date_from, date_to)
        duration = current_until - current_since
        prev_until = current_since
        prev_since = prev_until - duration

        openclaw_data = get_session_usage_range(prev_since, prev_until)
        coding_data = get_tools_data_for_range(prev_since, prev_until)
    else:
        since, until = previous_period_range(period)
        openclaw_data = get_session_usage_range(since, until)
        coding_data = get_tools_data_for_range(since, until)

    coding_apps = {k: v for k, v in coding_data.get("apps", {}).items() if k.lower() != "openclaw"}

    total_tokens = openclaw_data["total_tokens"] + sum(v.get("tokens", 0) for v in coding_apps.values())
    total_cost = openclaw_data["total_cost"] + sum(v.get("cost", 0.0) for v in coding_apps.values())
    total_messages = openclaw_data["total_messages"] + sum(v.get("messages", 0) for v in coding_apps.values())

    return {
        "total_tokens": total_tokens,
        "total_cost": round(total_cost, 2),
        "total_messages": total_messages,
    }


def pct_change(current: float, previous: float) -> Optional[float]:
    if previous == 0:
        return None
    return round(((current - previous) / previous) * 100, 1)


def compute_usage_with_comparison(period: str, date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict[str, Any]:
    current = compute_usage(period, date_from, date_to)
    previous = _compute_previous_usage(period, date_from, date_to)

    current["comparison"] = {
        "tokens_prev": previous["total_tokens"],
        "cost_prev": previous["total_cost"],
        "messages_prev": previous["total_messages"],
        "tokens_pct": pct_change(current["total_tokens"], previous["total_tokens"]),
        "cost_pct": pct_change(current["total_cost"], previous["total_cost"]),
        "messages_pct": pct_change(current["total_messages"], previous["total_messages"]),
    }
    return current


def compute_stats(year: Optional[int] = None) -> Dict[str, Any]:
    """Contribution graph and stats (OpenClaw + coding tools)."""
    session_data = get_session_usage_year(year) if year else get_session_usage_days(365)
    ocl_map = {c.get("date"): c for c in session_data.get("contributions", [])}

    if year:
        start_date = datetime(year, 1, 1)
        end_date = datetime(year, 12, 31)
        period_args = ["--since", start_date.strftime("%Y-%m-%d"), "--until", end_date.strftime("%Y-%m-%d")]
    else:
        period_args = period_to_range_args("365")

    since, until = _date_range_from_args(period_args)
    coding_contribs = get_tools_contributions_for_range(since, until)

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
