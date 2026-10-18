#!/usr/bin/env python3
"""Compare Sessions cache-hit overhead with the pre-#240 cache key.

Runs only signature discovery; session assembly is replaced by a marker. Source
logs and the live usage DB are never parsed or written. Fixtures/results live in
output/ by default. Run from the project root with PYTHONPATH=src.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path
from unittest.mock import patch


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("output/benchmarks/session-response-cache"))
    parser.add_argument("--files", type=int, nargs="+", default=[100, 1000])
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--real-tools", nargs="*", default=[])
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    os.environ["TOKDASH_DATA_DIR"] = str(args.output / "isolated-data")
    os.environ["TOKDASH_WARM_ON_START"] = "0"
    from tokdash import api, sessions

    original_key = api._session_response_cache_key

    def baseline_key(tool, period, date_from, date_to, include_review_sessions, **kwargs):
        return api._window_cache_key(
            f"sessions_{tool.strip().lower()}_{period}_{date_from}_{date_to}_{include_review_sessions}",
            date_from, date_to,
        )

    results = []

    def measure(label, tool):
        row = {"case": label, "tool": tool, "arms": {}}
        for arm, key_fn, sig_ttl in (
            ("baseline", baseline_key, api._SIG_TTL),
            ("synchronous_inventory", original_key, 0),
            ("candidate", original_key, api._SIG_TTL),
        ):
            api._clear_cache()
            with patch.object(api, "_SIG_TTL", sig_ttl), patch.object(api, "_session_response_cache_key", key_fn), patch.object(
                api, "get_sessions_data", return_value={"marker": True}
            ):
                started = time.perf_counter()
                api.get_sessions(tool, "all")
                row["arms"][arm] = {"first_ms": (time.perf_counter() - started) * 1000}
                samples = []
                for _ in range(args.repetitions):
                    started = time.perf_counter()
                    assert api.get_sessions(tool, "all") == {"marker": True}
                    samples.append((time.perf_counter() - started) * 1000)
                row["arms"][arm].update(samples_ms=samples, median_ms=statistics.median(samples))
        results.append(row)
        report = {"method": "in-process Sessions cache hits; real signature discovery, mocked assembly", "results": results}
        (args.output / "benchmark.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(row), flush=True)

    for count in args.files:
        root = args.output / "fixtures" / str(count)
        for index in range(count):
            log = root / str(index // 100) / f"rollout-{index}.jsonl"
            if not log.exists():
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text("{}\n")
        with patch.object(sessions.clientpaths, "codex_sessions_dir", return_value=root), patch.object(
            sessions.clientpaths, "codex_archived_sessions_dir", return_value=root / "archive"
        ), patch.object(sessions, "_codex_state_signature", return_value=()):
            measure(f"synthetic-{count}", "codex")
    for tool in args.real_tools:
        measure("local-source-metadata", tool)


if __name__ == "__main__":
    main()
