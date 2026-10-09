#!/usr/bin/env python3
"""Compare real aggregation with a reference checkout using synthetic SQLite rows.

Run from the repo root with --baseline /path/to/reference. No local logs,
credentials or live usage databases are opened. Raw timings go into output/.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import math
import os
from pathlib import Path
import random
import statistics
import subprocess
import sys
import time


def summary(values):
    ordered = sorted(values)
    return {"median_ms": statistics.median(values), "p95_ms": ordered[math.ceil(len(ordered) * .95) - 1]}


def worker(args):
    sys.path.insert(0, str(Path(args.repo).resolve() / "src"))
    from tokdash import compute
    from tokdash import sessions
    from tokdash.dateutil import parse_date_range
    from tokdash.sources import openclaw
    from tokdash.usage_store import UsageEntryStore, build_source_signature

    today = datetime.now().date()
    since, _ = parse_date_range((today - timedelta(days=27)).isoformat(), today.isoformat())
    rows = []
    for day in range(28):
        local = since + timedelta(days=day)
        for i in range(args.rows_per_day):
            stamp = local + timedelta(seconds=i * 86400 / args.rows_per_day)
            rows.append({"source": "openclaw" if i % 10 == 0 else "codex", "model": f"model-{i % 5}", "timestamp": int(stamp.timestamp()*1000),
                         "input": 100+i%13, "output": 30, "cacheRead": 200, "cacheWrite": 7, "reasoning": 4,
                         "cost": .001*(i%7+1), "messageCount": i%3+1})
    store = UsageEntryStore(Path(args.output) / f"{args.label}-usage.sqlite3")
    for source in ("codex", "openclaw"):
        selected = [row for row in rows if row["source"] == source]
        signature = build_source_signature(files=[["synthetic", len(selected), 1]],
                                           parser={"v": 2, "start": since.isoformat(), "hours_per_day": 24})
        store.sync_source(source, signature, lambda: selected)

    class Tracker:
        parsers = {}
        source_errors = []
    compute.CodingToolsUsageTracker = Tracker
    compute._sync_usage_store = lambda _tracker: (store, ["codex"])
    compute._collect_live_coding_entries = lambda *unused: []
    compute.get_openclaw_data_for_range = lambda f, t: openclaw._openclaw_usage_from_store(store, *parse_date_range(f, t))
    cases = {"today": (today, today), "yesterday": (today-timedelta(days=1), today-timedelta(days=1)),
             "week": (today-timedelta(days=6), today), "year": (today-timedelta(days=364), today)}
    intervals = [(row["timestamp"], row["timestamp"] + 120_000) for row in rows]

    def active_window(since_ms, until_ms, **unused):
        selected = [(max(start, since_ms), min(end, until_ms)) for start, end in intervals
                    if start < until_ms and end > since_ms]
        total = sum(end-start for start, end in selected)
        return {"codex": {"active_ms_sum": total}}, [], selected

    sessions._active_time_window = active_window
    sessions._active_time_comparison = lambda *unused, **kwargs: None
    print(json.dumps({"ready": True, "rows": len(rows)}), flush=True)
    for line in sys.stdin:
        key = json.loads(line)["case"]
        kind, window = key.split("/", 1)
        start, end = cases[window]
        t = time.perf_counter()
        if kind == "usage":
            data = compute.compute_usage("today", start.isoformat(), end.isoformat())
            fields = ("total_tokens", "total_cost", "total_messages", "cache_hit_rate")
        else:
            data = sessions.get_active_time_data("today", start.isoformat(), end.isoformat())
            fields = ("active_ms", "active_ms_sum")
        elapsed = (time.perf_counter()-t)*1000
        print(json.dumps({"ms": elapsed, "totals": {field: data[field] for field in fields}}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline")
    parser.add_argument("--repo")
    parser.add_argument("--label", default="candidate")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--rows-per-day", type=int, default=3000)
    parser.add_argument("--repeats", type=int, default=48)
    parser.add_argument("--output", default="output/sparkline-validation")
    parser.add_argument("--report-name", default="aggregation-performance.json")
    args = parser.parse_args()
    if args.rows_per_day < 1 or args.repeats < 1:
        parser.error("--rows-per-day and --repeats must be positive")
    Path(args.output).mkdir(parents=True, exist_ok=True)
    if args.worker:
        worker(args)
        return
    if not args.baseline:
        parser.error("--baseline is required")
    cases = [f"{kind}/{window}" for kind in ("usage", "active") for window in ("today", "yesterday", "week", "year")]
    results = {label: {"samples": {key: [] for key in cases}, "totals": {}}
               for label in ("baseline", "candidate")}
    processes = {}
    logs = []
    try:
        for label, repo in (("baseline", args.baseline), ("candidate", Path.cwd())):
            cmd = [sys.executable, str(Path(__file__).resolve()), "--worker", "--repo", str(repo), "--label", label,
                   "--rows-per-day", str(args.rows_per_day), "--output", args.output]
            env = dict(os.environ, TOKDASH_DATA_DIR=str(Path(args.output).resolve()/label), PYTHONPATH=str(Path(repo).resolve()/"src"))
            log = (Path(args.output)/f"{label}-benchmark.log").open("w")
            logs.append(log)
            proc = subprocess.Popen(cmd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=log, text=True, bufsize=1)
            processes[label] = proc
            results[label]["rows"] = json.loads(proc.stdout.readline())["rows"]
        rng = random.Random(17)
        for iteration in range(args.repeats + 3):
            order = list(cases)
            rng.shuffle(order)
            for key in order:
                labels = ["baseline", "candidate"]
                rng.shuffle(labels)
                for label in labels:
                    proc = processes[label]
                    proc.stdin.write(json.dumps({"case": key}) + "\n")
                    proc.stdin.flush()
                    result = json.loads(proc.stdout.readline())
                    if iteration >= 3:
                        results[label]["samples"][key].append(result["ms"])
                        results[label]["totals"][key] = result["totals"]
    finally:
        for proc in processes.values():
            proc.stdin.close()
        for proc in processes.values():
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for log in logs:
            log.close()
    report = {"rows": results["candidate"]["rows"], "repeats": args.repeats,
              "method": "paired alternating processes; 24-hour events; 3 warmups; same corpus and headline semantics",
              "gate": "candidate p95 <= baseline p95 + max(10 ms, 10% of baseline p95)", "cases": {}, "raw": results}
    for key in results["baseline"]["samples"]:
        before = summary(results["baseline"]["samples"][key])
        after = summary(results["candidate"]["samples"][key])
        budget = max(10, before["p95_ms"]*.1)
        report["cases"][key] = {"baseline": before, "candidate": after, "budget_ms": budget,
            "pass": after["p95_ms"] <= before["p95_ms"]+budget,
            "totals_equal": results["baseline"]["totals"][key] == results["candidate"]["totals"][key]}
    path = Path(args.output)/args.report_name
    path.write_text(json.dumps(report, indent=2)+"\n")
    print(json.dumps({"report": str(path), "cases": report["cases"]}, indent=2))
    if not all(row["pass"] and row["totals_equal"] for row in report["cases"].values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
