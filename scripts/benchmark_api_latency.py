#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_BASES = [
    "stable=http://127.0.0.1:55423",
    "dev=http://127.0.0.1:55619",
]

DEFAULT_PERIODS = ["today", "month", "year"]


def default_endpoint_specs(periods: list[str] | None = None) -> list[str]:
    selected_periods = periods or DEFAULT_PERIODS
    endpoints = [
        "health=/health",
        "dashboard=/",
        "stats=/api/stats",
    ]
    for period in selected_periods:
        endpoints.extend(
            [
                f"usage_{period}=/api/usage?period={period}",
                f"tools_{period}=/api/tools?period={period}",
                f"openclaw_{period}=/api/openclaw?period={period}",
                f"sessions_codex_{period}=/api/sessions?tool=codex&period={period}",
                f"sessions_codex_review_{period}=/api/sessions?tool=codex&period={period}&include_review_sessions=true",
                f"sessions_claude_{period}=/api/sessions?tool=claude&period={period}",
                f"sessions_opencode_{period}=/api/sessions?tool=opencode&period={period}",
                f"sessions_pi_{period}=/api/sessions?tool=pi_agent&period={period}",
            ]
        )
    return endpoints


DEFAULT_ENDPOINTS = default_endpoint_specs()


def parse_named_value(value: str, *, default_name: str = "") -> tuple[str, str]:
    if "=" in value:
        name, raw = value.split("=", 1)
        name = name.strip()
        raw = raw.strip()
        if not name or not raw:
            raise argparse.ArgumentTypeError(f"Invalid NAME=VALUE pair: {value!r}")
        return name, raw
    if not default_name:
        raise argparse.ArgumentTypeError(f"Expected NAME=VALUE pair: {value!r}")
    raw = value.strip()
    if not raw:
        raise argparse.ArgumentTypeError(f"Empty value for {default_name!r}")
    return default_name, raw


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * pct))
    return ordered[max(0, min(index, len(ordered) - 1))]


def summarize_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [float(sample["latency_ms"]) for sample in samples if sample.get("ok")]
    statuses: dict[str, int] = {}
    for sample in samples:
        status = str(sample.get("status") or sample.get("error") or "unknown")
        statuses[status] = statuses.get(status, 0) + 1
    if not latencies:
        return {
            "ok": False,
            "count": len(samples),
            "ok_count": 0,
            "statuses": statuses,
            "min_ms": None,
            "median_ms": None,
            "p95_ms": None,
            "max_ms": None,
            "bytes_median": None,
        }
    byte_sizes = [int(sample.get("bytes") or 0) for sample in samples if sample.get("ok")]
    return {
        "ok": len(latencies) == len(samples),
        "count": len(samples),
        "ok_count": len(latencies),
        "statuses": statuses,
        "min_ms": round(min(latencies), 2),
        "median_ms": round(statistics.median(latencies), 2),
        "p95_ms": round(_percentile(latencies, 0.95), 2),
        "max_ms": round(max(latencies), 2),
        "bytes_median": int(statistics.median(byte_sizes)) if byte_sizes else 0,
    }


def fetch_once(base_url: str, endpoint: str, timeout: float) -> dict[str, Any]:
    url = base_url.rstrip("/") + endpoint
    started = time.perf_counter()
    request = Request(url, headers={"Accept": "application/json,text/html;q=0.9,*/*;q=0.8"})
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
            status = int(response.status)
        return {
            "ok": 200 <= status < 400,
            "status": status,
            "latency_ms": (time.perf_counter() - started) * 1000,
            "bytes": len(body),
        }
    except HTTPError as exc:
        body = exc.read()
        return {
            "ok": False,
            "status": int(exc.code),
            "latency_ms": (time.perf_counter() - started) * 1000,
            "bytes": len(body),
            "error": f"HTTP {exc.code}",
        }
    except (TimeoutError, URLError, OSError) as exc:
        return {
            "ok": False,
            "status": None,
            "latency_ms": (time.perf_counter() - started) * 1000,
            "bytes": 0,
            "error": type(exc).__name__,
        }


def run_benchmark(
    bases: list[tuple[str, str]],
    endpoints: list[tuple[str, str]],
    *,
    warmups: int,
    repeats: int,
    timeout: float,
    pause: float,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for base_name, base_url in bases:
        for endpoint_name, endpoint in endpoints:
            for _ in range(max(0, warmups)):
                fetch_once(base_url, endpoint, timeout)
                if pause > 0:
                    time.sleep(pause)
            samples = []
            for _ in range(max(1, repeats)):
                samples.append(fetch_once(base_url, endpoint, timeout))
                if pause > 0:
                    time.sleep(pause)
            results.append(
                {
                    "base": base_name,
                    "base_url": base_url,
                    "endpoint": endpoint_name,
                    "path": endpoint,
                    "summary": summarize_samples(samples),
                    "samples": samples,
                }
            )
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "warmups": warmups,
        "repeats": repeats,
        "timeout": timeout,
        "results": results,
    }


def markdown_table(report: dict[str, Any]) -> str:
    lines = [
        "| base | endpoint | ok | status | median ms | p95 ms | max ms | bytes |",
        "|---|---|---:|---|---:|---:|---:|---:|",
    ]
    for row in report["results"]:
        summary = row["summary"]
        statuses = ", ".join(f"{key}:{value}" for key, value in sorted(summary["statuses"].items()))
        lines.append(
            "| {base} | {endpoint} | {ok_count}/{count} | {statuses} | {median} | {p95} | {max_ms} | {bytes_median} |".format(
                base=row["base"],
                endpoint=row["endpoint"],
                ok_count=summary["ok_count"],
                count=summary["count"],
                statuses=statuses,
                median=summary["median_ms"],
                p95=summary["p95_ms"],
                max_ms=summary["max_ms"],
                bytes_median=summary["bytes_median"],
            )
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark Tokdash HTTP API endpoint latency.")
    parser.add_argument("--base", action="append", default=[], help="Named base URL, e.g. stable=http://127.0.0.1:55423")
    parser.add_argument("--endpoint", action="append", default=[], help="Named endpoint, e.g. usage=/api/usage?period=today")
    parser.add_argument(
        "--period",
        action="append",
        default=[],
        help="Default period to benchmark when --endpoint is omitted. Repeatable; defaults to today/month/year.",
    )
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--pause", type=float, default=0.05)
    parser.add_argument("--output", default="", help="Optional JSON output path")
    parser.add_argument("--markdown-output", default="", help="Optional Markdown table output path")
    args = parser.parse_args()

    bases = [parse_named_value(item) for item in (args.base or DEFAULT_BASES)]
    endpoint_specs = args.endpoint or default_endpoint_specs(args.period or None)
    endpoints = [parse_named_value(item) for item in endpoint_specs]
    report = run_benchmark(
        bases,
        endpoints,
        warmups=args.warmups,
        repeats=args.repeats,
        timeout=args.timeout,
        pause=args.pause,
    )
    table = markdown_table(report)
    print(table)

    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.markdown_output:
        path = Path(args.markdown_output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(table + "\n", encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
