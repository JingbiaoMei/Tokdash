#!/usr/bin/env python3
"""
OpenClaw cron job: Tokdash combined usage report (OpenClaw + coding tools).

Intended for:
- manual usage (run once)
- cron jobs (scheduled reports)

Repo location: docs/agents/openclaw_reporting/openclaw_cron_job.py

It reads the Tokdash API:
  GET /api/usage?period=<period>
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Tuple


def format_num(n: Any) -> str:
    try:
        return f"{int(n or 0):,}"
    except Exception:
        return "0"


def fetch_usage(base_url: str, period: str, timeout_s: int) -> Dict[str, Any]:
    base = base_url.rstrip("/")
    url = f"{base}/api/usage?{urllib.parse.urlencode({'period': period})}"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise RuntimeError("Tokdash API returned non-object JSON.")
        return data
    except urllib.error.URLError as e:
        raise RuntimeError(f"Failed to reach Tokdash API at {url}: {e}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse Tokdash API response as JSON: {e}")


def period_labels(period: str) -> Tuple[str, str]:
    label_map = {
        "today": "Past 1 Day",
        "3days": "Past 3 Days",
        "week": "Past 7 Days",
        "14days": "Past 14 Days",
        "month": "Current Month",
    }
    zh_label_map = {
        "today": "过去 1 天",
        "3days": "过去 3 天",
        "week": "过去 7 天",
        "14days": "过去 14 天",
        "month": "当月",
    }

    if period.isdigit():
        return f"Past {period} Days", f"过去 {period} 天"

    return label_map.get(period, f"Period: {period}"), zh_label_map.get(period, f"周期：{period}")


def build_report(data: Dict[str, Any], *, period: str, lang: str, max_models: int) -> str:
    label, zh_label = period_labels(period)

    openclaw_models = data.get("openclaw_models", []) or []
    coding_apps = data.get("coding_apps", data.get("apps", {})) or {}
    coding_models = data.get("coding_models", []) or []

    openclaw_total = sum(float(m.get("cost", 0.0) or 0.0) for m in openclaw_models)
    coding_total = sum(float(v.get("cost", 0.0) or 0.0) for v in coding_apps.values())
    grand_total = float(data.get("total_cost", openclaw_total + coding_total) or 0.0)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def limited(items):
        if max_models <= 0:
            return items
        return items[:max_models]

    lines = []

    # English
    en = []
    en.append(f"📊 Combined {label} Token Usage Report (tokdash)")
    en.append(f"Generated: {ts}")
    en.append(f"Summary: Total ${grand_total:.2f} (${openclaw_total:.2f} OpenClaw + ${coding_total:.2f} Coding Tools)")
    en.append("")

    en.append("1) OpenClaw Session Usage")
    if not openclaw_models:
        en.append("  - No usage found.")
    else:
        for m in limited(sorted(openclaw_models, key=lambda x: float(x.get('cost', 0.0) or 0.0), reverse=True)):
            en.append(
                f"  - {m.get('name', 'unknown')}: "
                f"{format_num(m.get('tokens_in', 0))} In, "
                f"{format_num(m.get('tokens_out', 0))} Out, "
                f"{format_num(m.get('tokens_cache', 0))} Cache, "
                f"${float(m.get('cost', 0.0) or 0.0):.2f}"
            )
    en.append("")

    en.append("2) Coding Tools Usage")
    if not coding_apps:
        en.append("  - No usage found.")
    else:
        en.append("  Breakdown by Tool:")
        for tool, s in sorted(coding_apps.items(), key=lambda x: float(x[1].get("cost", 0.0) or 0.0), reverse=True):
            en.append(f"  - {tool.upper()}: ${float(s.get('cost', 0.0) or 0.0):.2f} ({int(s.get('messages', 0) or 0)} msgs)")
        en.append("  Models (Aggregated):")
        for m in limited(sorted(coding_models, key=lambda x: float(x.get("cost", 0.0) or 0.0), reverse=True)):
            model_name = m.get("name") or m.get("model") or "unknown"
            en.append(
                f"  - {model_name}: "
                f"{format_num(m.get('tokens_in', 0))} In, "
                f"{format_num(m.get('tokens_out', 0))} Out, "
                f"{format_num(m.get('tokens_cache', 0))} Cache, "
                f"${float(m.get('cost', 0.0) or 0.0):.2f}"
            )

    # Chinese
    zh = []
    zh.append(f"📊 {zh_label}代币用量综合报告 (tokdash)")
    zh.append(f"生成时间：{ts}")
    zh.append(f"摘要：总计 ${grand_total:.2f} (OpenClaw ${openclaw_total:.2f} + 编程工具 ${coding_total:.2f})")
    zh.append("")

    zh.append("1) OpenClaw 会话使用情况")
    if not openclaw_models:
        zh.append("  - 未发现用量。")
    else:
        for m in limited(sorted(openclaw_models, key=lambda x: float(x.get('cost', 0.0) or 0.0), reverse=True)):
            zh.append(
                f"  - {m.get('name', 'unknown')}: "
                f"{format_num(m.get('tokens_in', 0))} 输入, "
                f"{format_num(m.get('tokens_out', 0))} 输出, "
                f"{format_num(m.get('tokens_cache', 0))} 缓存, "
                f"${float(m.get('cost', 0.0) or 0.0):.2f}"
            )
    zh.append("")

    zh.append("2) 编程工具使用情况")
    if not coding_apps:
        zh.append("  - 未发现用量。")
    else:
        zh.append("  详细分类:")
        for tool, s in sorted(coding_apps.items(), key=lambda x: float(x[1].get("cost", 0.0) or 0.0), reverse=True):
            zh.append(f"  - {tool.upper()}: ${float(s.get('cost', 0.0) or 0.0):.2f} ({int(s.get('messages', 0) or 0)} 条消息)")
        zh.append("  模型汇总:")
        for m in limited(sorted(coding_models, key=lambda x: float(x.get("cost", 0.0) or 0.0), reverse=True)):
            model_name = m.get("name") or m.get("model") or "unknown"
            zh.append(
                f"  - {model_name}: "
                f"{format_num(m.get('tokens_in', 0))} 输入, "
                f"{format_num(m.get('tokens_out', 0))} 输出, "
                f"{format_num(m.get('tokens_cache', 0))} 缓存, "
                f"${float(m.get('cost', 0.0) or 0.0):.2f}"
            )

    if lang == "en":
        lines.extend(en)
    elif lang == "zh":
        lines.extend(zh)
    else:
        lines.extend(en)
        lines.append("\n" + "=" * 40 + "\n")
        lines.extend(zh)

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Tokdash combined usage report")
    p.add_argument("--base-url", type=str, default="http://127.0.0.1:55423", help="Tokdash base URL (default: http://127.0.0.1:55423)")
    p.add_argument("--period", type=str, default="today", help='Usage period: "today", "week", "month", "3days", "14days", or an integer N (days)')
    p.add_argument("--timeout", type=int, default=20, help="HTTP timeout seconds (default: 20)")
    p.add_argument("--lang", type=str, default="both", choices=["en", "zh", "both"], help="Report language (default: both)")
    p.add_argument("--max-models", type=int, default=20, help="Max models to print per section (0 = all, default: 20)")
    p.add_argument("--output", type=str, default="", help="Write report to a file instead of stdout")
    args = p.parse_args(argv)

    try:
        data = fetch_usage(args.base_url, args.period, args.timeout)
        report = build_report(data, period=args.period, lang=args.lang, max_models=args.max_models)
    except Exception as e:
        print(f"Report failed: {e}", file=sys.stderr)
        return 1

    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(report + "\n")
        except Exception as e:
            print(f"Failed to write {args.output!r}: {e}", file=sys.stderr)
            return 2
        return 0

    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
