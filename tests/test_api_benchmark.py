from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


def _load_benchmark_module():
    script = Path(__file__).resolve().parent.parent / "scripts" / "benchmark_api_latency.py"
    spec = importlib.util.spec_from_file_location("benchmark_api_latency", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_benchmark_summary_and_name_parsing():
    bench = _load_benchmark_module()

    assert bench.parse_named_value("dev=http://127.0.0.1:55619") == ("dev", "http://127.0.0.1:55619")
    assert bench.parse_named_value("/health", default_name="health") == ("health", "/health")
    default_names = {bench.parse_named_value(item)[0] for item in bench.default_endpoint_specs()}
    assert {"usage_today", "usage_month", "usage_year"}.issubset(default_names)
    assert {"sessions_codex_today", "sessions_codex_month", "sessions_codex_year"}.issubset(default_names)

    summary = bench.summarize_samples(
        [
            {"ok": True, "status": 200, "latency_ms": 10.0, "bytes": 5},
            {"ok": True, "status": 200, "latency_ms": 20.0, "bytes": 7},
            {"ok": True, "status": 200, "latency_ms": 30.0, "bytes": 9},
        ]
    )

    assert summary["ok"] is True
    assert summary["median_ms"] == 20.0
    assert summary["p95_ms"] == 30.0
    assert summary["bytes_median"] == 7


@pytest.mark.skipif(
    os.environ.get("TOKDASH_RUN_API_BENCHMARK", "").strip().lower() not in {"1", "true", "yes", "on"},
    reason="set TOKDASH_RUN_API_BENCHMARK=1 to compare live stable/dev Tokdash servers",
)
def test_live_stable_dev_api_benchmark():
    bench = _load_benchmark_module()
    stable_url = os.environ.get("TOKDASH_BENCH_STABLE_URL", "http://127.0.0.1:55423")
    dev_url = os.environ.get("TOKDASH_BENCH_DEV_URL", "http://127.0.0.1:55619")
    max_p95_ms = float(os.environ.get("TOKDASH_BENCH_MAX_P95_MS", "5000"))

    report = bench.run_benchmark(
        [("stable", stable_url), ("dev", dev_url)],
        [
            ("health", "/health"),
            ("usage_today", "/api/usage?period=today"),
            ("usage_month", "/api/usage?period=month"),
            ("usage_year", "/api/usage?period=year"),
            ("sessions_codex_today", "/api/sessions?tool=codex&period=today"),
            ("sessions_codex_month", "/api/sessions?tool=codex&period=month"),
            ("sessions_codex_year", "/api/sessions?tool=codex&period=year"),
            ("sessions_claude_today", "/api/sessions?tool=claude&period=today"),
            ("sessions_opencode_today", "/api/sessions?tool=opencode&period=today"),
        ],
        warmups=1,
        repeats=3,
        timeout=30,
        pause=0.05,
    )

    failures = []
    for row in report["results"]:
        summary = row["summary"]
        if not summary["ok"]:
            failures.append(f"{row['base']} {row['endpoint']} status={summary['statuses']}")
            continue
        if summary["p95_ms"] is not None and summary["p95_ms"] > max_p95_ms:
            failures.append(f"{row['base']} {row['endpoint']} p95={summary['p95_ms']}ms")

    assert not failures, bench.markdown_table(report)
