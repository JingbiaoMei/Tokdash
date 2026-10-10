#!/usr/bin/env python3
"""Compare real aggregation with a reference checkout using synthetic SQLite rows.

Run from the repo root with --baseline /path/to/reference. No local logs,
credentials or live usage databases are opened. Raw timings go into output/.
"""
from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN
import gc
import json
from itertools import accumulate
import math
import os
from pathlib import Path
import random
import statistics
import subprocess
import sys
import tempfile
import time


def summary(values):
    ordered = sorted(values)
    return {"median_ms": statistics.median(values), "p95_ms": ordered[math.ceil(len(ordered) * .95) - 1]}


WINDOWS = ("today", "yesterday", "last7days", "lastWeek", "last14days", "last4weeks",
           "thisMonth", "lastMonth", "thisYear", "lastYear")


def common_windows(today):
    monday = today - timedelta(days=today.weekday())
    month = today.replace(day=1)
    last_month = month - timedelta(days=1)
    return {"today": (today, today), "yesterday": (today - timedelta(days=1), today - timedelta(days=1)),
            "last7days": (today - timedelta(days=6), today),
            "lastWeek": (monday - timedelta(days=7), monday - timedelta(days=1)),
            "last14days": (today - timedelta(days=13), today), "last4weeks": (today - timedelta(days=27), today),
            "thisMonth": (month, today), "lastMonth": (last_month.replace(day=1), last_month),
            "thisYear": (today.replace(month=1, day=1), today),
            "lastYear": (today.replace(year=today.year-1, month=1, day=1), today.replace(year=today.year-1, month=12, day=31))}


def worker(args):
    sys.path.insert(0, str(Path(args.repo).resolve() / "src"))
    from tokdash import compute
    from tokdash import sessions
    from tokdash.dateutil import parse_date_range
    from tokdash.sources import openclaw
    from tokdash.usage_store import UsageEntryStore, build_source_signature

    today = datetime.now().date()
    cases = common_windows(today)
    corpus_start = today.replace(year=today.year-2, month=1, day=1)
    if args.corpus_days:
        corpus_start = today - timedelta(days=args.corpus_days-1)
    corpus_days = (today - corpus_start).days + 1
    rows = []
    for day in range(corpus_days):
        # Resolve each local midnight separately across DST.
        local, tomorrow = parse_date_range((corpus_start+timedelta(days=day)).isoformat(),
                                           (corpus_start+timedelta(days=day)).isoformat())
        for i in range(args.rows_per_day):
            stamp = local + (tomorrow-local) * (i / args.rows_per_day)
            rows.append({"source": "openclaw" if i % 10 == 0 else "codex", "model": f"model-{i % 5}", "timestamp": int(stamp.timestamp()*1000),
                         "input": 100+i%13, "output": 30, "cacheRead": 200, "cacheWrite": 7, "reasoning": 4,
                         "cost": .001*(i%7+1), "messageCount": i%3+1})
    store = UsageEntryStore(Path(args.database_dir or args.output) / f"{args.label}-usage.sqlite3")
    for source in ("codex", "openclaw"):
        selected = [row for row in rows if row["source"] == source]
        signature = build_source_signature(files=[["synthetic", len(selected), 1]],
                                           parser={"v": 3, "start": corpus_start.isoformat(), "hours_per_day": 24})
        store.sync_source(source, signature, lambda: selected)

    class Tracker:
        parsers = {}
        source_errors = []
    compute.CodingToolsUsageTracker = Tracker
    compute._sync_usage_store = lambda _tracker: (store, ["codex"])
    compute.UsageEntryStore = lambda: store
    compute._usage_store_sources = lambda _tracker: ["codex"]
    compute._collect_live_coding_entries = lambda *unused: []
    compute.get_openclaw_data_for_range = lambda f, t: openclaw._openclaw_usage_from_store(store, *parse_date_range(f, t))
    compute.get_session_usage_range = lambda start, end: openclaw._openclaw_usage_from_store(store, start, end)
    intervals = [(row["timestamp"], row["timestamp"] + 120_000) for row in rows]
    stamps = [row["timestamp"] for row in rows]
    # The synthetic recorded fees are exact integer mills. This independent
    # oracle exposes legacy binary half-cent rounding instead of silently
    # allowing a one-cent drift between aggregation partitions.
    cost_mills = [0, *accumulate(round(row["cost"]*1000) for row in rows)]

    def cost_reference(first, last):
        lo, hi = bisect_left(stamps, int(first.timestamp()*1000)), bisect_left(stamps, int(last.timestamp()*1000))
        mills = cost_mills[hi] - cost_mills[lo]
        cents = float((Decimal(mills) / 1000).quantize(Decimal(".01"), rounding=ROUND_HALF_EVEN))
        return mills, cents

    def active_window(since_ms, until_ms, **unused):
        lo = bisect_right(stamps, since_ms-120_000)
        hi = bisect_left(stamps, until_ms)
        # Exercise each revision's production clipper. Source discovery is
        # stubbed equally; clipping, aggregation and encoding remain timed.
        selected = sessions._clip_intervals(intervals[lo:hi], since_ms, until_ms)
        total = sum(end-start for start, end in selected)
        return {"codex": {"active_ms_sum": total}}, [], selected

    sessions._active_time_window = active_window
    print(json.dumps({"ready": True, "rows": len(rows), "corpus_days": corpus_days,
                      "repo_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=args.repo, text=True).strip(),
                      "corpus_start": str(corpus_start), "corpus_end": str(today)}), flush=True)
    for line in sys.stdin:
        key = json.loads(line)["case"]
        kind, window = key.split("/", 1)
        start, end = cases[window]
        if kind == "native":
            since, until = parse_date_range(str(start), str(end))
            lo = bisect_left(stamps, int(since.timestamp()*1000))
            hi = bisect_left(stamps, int(until.timestamp()*1000))
            native_entries = [row for row in rows[lo:hi] if row["source"] != "openclaw"]
        enabled = gc.isenabled()
        gc_before = [generation["collections"] for generation in gc.get_stats()]
        if args.gc_mode == "isolated":
            gc.disable()
        t = time.perf_counter()
        cpu = time.process_time()
        if kind == "usage":
            data = compute.compute_usage_with_comparison("today", start.isoformat(), end.isoformat())
            fields = ("total_tokens", "total_cost", "total_messages", "cache_hit_rate", "comparison")
        elif kind == "active":
            data = sessions.get_active_time_data("today", start.isoformat(), end.isoformat())
            fields = ("active_ms", "active_ms_sum", "comparison")
        else:
            options = {}
            if args.label == "candidate":
                from tokdash.usage_buckets import bucket_granularity
                options["granularity"] = bucket_granularity(since, until)
            data = compute.parse_entries_json({"entries": native_entries}, **options)
            fields = ("total_tokens", "total_cost", "total_messages", "cache_hit_rate")
        # Include encoding the complete endpoint result, including new buckets.
        encoded_bytes = len(json.dumps(data, separators=(",", ":")).encode())
        elapsed = (time.perf_counter()-t)*1000
        cpu_elapsed = (time.process_time()-cpu)*1000
        gc_delta = [generation["collections"] - before
                    for generation, before in zip(gc.get_stats(), gc_before)]
        if enabled:
            gc.enable()
        oracle = None
        if kind == "usage":
            since, until = parse_date_range(str(start), str(end))
            mills, current_cost = cost_reference(since, until)
            previous_mills, previous_cost = cost_reference(since-(until-since), since)
            oracle = {"current_mills": mills, "previous_mills": previous_mills,
                      "total_cost": current_cost, "cost_prev": previous_cost,
                      "cost_pct": compute.pct_change(current_cost, previous_cost)}
        print(json.dumps({"ms": elapsed, "cpu_ms": cpu_elapsed, "gc_collections": gc_delta,
                          "bytes": encoded_bytes, "oracle": oracle,
                          "totals": {field: data[field] for field in fields}}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline")
    parser.add_argument("--repo")
    parser.add_argument("--label", default="candidate")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--rows-per-day", type=int, default=3000)
    parser.add_argument("--corpus-days", type=int, help="Override full calendar corpus (two previous years plus current year)")
    parser.add_argument("--windows", nargs="+", choices=WINDOWS, default=list(WINDOWS))
    parser.add_argument("--kinds", nargs="+", choices=("usage", "active", "native"), default=["usage", "active", "native"])
    parser.add_argument("--repeats", type=int, default=48)
    parser.add_argument("--output", default="output/sparkline-validation")
    parser.add_argument("--report-name", default="aggregation-performance.json")
    parser.add_argument("--database-dir", help="Override temporary SQLite storage (default: OS temporary filesystem)")
    parser.add_argument("--gc-mode", choices=("normal", "isolated"), default="normal",
                        help="isolated excludes cyclic garbage collection from timed aggregation; browser verification keeps normal GC")
    parser.add_argument("--cpu", type=int, help="Pin both workers to this available CPU for a controlled comparison (Linux)")
    args = parser.parse_args()
    if args.rows_per_day < 1 or args.repeats < 1 or (args.corpus_days is not None and args.corpus_days < 1):
        parser.error("--rows-per-day, --repeats and --corpus-days must be positive")
    if args.cpu is not None:
        if not hasattr(os, "sched_getaffinity") or args.cpu not in os.sched_getaffinity(0):
            parser.error("--cpu must name an available Linux CPU")
        os.sched_setaffinity(0, {args.cpu})
    Path(args.output).mkdir(parents=True, exist_ok=True)
    if args.worker:
        worker(args)
        return
    if not args.baseline:
        parser.error("--baseline is required")
    cases = [f"{kind}/{window}" for kind in args.kinds for window in args.windows]
    results = {label: {"samples": {key: [] for key in cases}, "cpu_samples": {key: [] for key in cases},
                      "gc_collections": {key: [] for key in cases}, "bytes": {}, "totals": {}, "oracle": {}}
               for label in ("baseline", "candidate")}
    processes = {}
    logs = []
    temporary = tempfile.TemporaryDirectory(prefix="tokdash-sparkline-bench-")
    database_dir = Path(args.database_dir or temporary.name).resolve()
    database_dir.mkdir(parents=True, exist_ok=True)
    try:
        for label, repo in (("baseline", args.baseline), ("candidate", Path.cwd())):
            cmd = [sys.executable, str(Path(__file__).resolve()), "--worker", "--repo", str(repo), "--label", label,
                   "--rows-per-day", str(args.rows_per_day), "--output", args.output, "--database-dir", str(database_dir),
                   "--gc-mode", args.gc_mode]
            if args.corpus_days:
                cmd.extend(["--corpus-days", str(args.corpus_days)])
            env = dict(os.environ, TOKDASH_DATA_DIR=str(Path(args.output).resolve()/label), PYTHONPATH=str(Path(repo).resolve()/"src"))
            log = (Path(args.output)/f"{label}-benchmark.log").open("w")
            logs.append(log)
            proc = subprocess.Popen(cmd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=log, text=True, bufsize=1)
            processes[label] = proc
            metadata = json.loads(proc.stdout.readline())
            results[label].update(metadata)
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
                        results[label]["cpu_samples"][key].append(result["cpu_ms"])
                        results[label]["gc_collections"][key].append(result["gc_collections"])
                        results[label]["bytes"][key] = result["bytes"]
                        results[label]["oracle"][key] = result["oracle"]
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
        temporary.cleanup()
    report = {"rows": results["candidate"]["rows"], "repeats": args.repeats,
              "revisions": {label: results[label]["repo_revision"] for label in results},
              "method": "randomized paired processes; complete common calendar ranges; Usage and Active Time including previous comparison; loaded native fallback and agent intervals; complete JSON encoding; 24-hour events; 3 warmups",
              "corpus": {k: results["candidate"][k] for k in ("corpus_days", "corpus_start", "corpus_end")},
              "database_storage": str(database_dir),
              "gc_mode": args.gc_mode,
              "cpu_affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
              "active_interval_clipping": "Each revision's production sessions._clip_intervals; source discovery stubbed equally",
              "gate": "candidate p95 <= baseline p95 + max(10 ms, 10% of baseline p95)",
              "correctness_gate": "Nonmonetary fields match baseline; candidate cents match exact integer-mill oracle; baseline cost drift allowed only within one cent at an exact half-cent boundary",
              "cases": {}, "raw": results}
    for key in results["baseline"]["samples"]:
        before = summary(results["baseline"]["samples"][key])
        after = summary(results["candidate"]["samples"][key])
        budget = max(10, before["p95_ms"]*.1)
        baseline_totals = results["baseline"]["totals"][key]
        candidate_totals = results["candidate"]["totals"][key]
        equal = baseline_totals == candidate_totals
        oracle = results["candidate"]["oracle"][key]
        correctness = equal
        baseline_rounding_drift = False
        if oracle is not None:
            def monetary_fields_match(totals):
                return (totals["total_cost"] == oracle["total_cost"]
                        and totals["comparison"]["cost_prev"] == oracle["cost_prev"]
                        and totals["comparison"]["cost_pct"] == oracle["cost_pct"])

            def nonmonetary(totals):
                copy = json.loads(json.dumps(totals))
                copy.pop("total_cost")
                copy["comparison"].pop("cost_prev")
                copy["comparison"].pop("cost_pct")
                return copy

            def explained_legacy_rounding():
                current_delta = abs(baseline_totals["total_cost"]-oracle["total_cost"])
                previous_delta = abs(baseline_totals["comparison"]["cost_prev"]-oracle["cost_prev"])
                current_ok = current_delta == 0 or (oracle["current_mills"] % 10 == 5 and current_delta <= .010000001)
                previous_ok = previous_delta == 0 or (oracle["previous_mills"] % 10 == 5 and previous_delta <= .010000001)
                return current_ok and previous_ok

            baseline_rounding_drift = not monetary_fields_match(baseline_totals) and explained_legacy_rounding()
            correctness = (monetary_fields_match(candidate_totals) and explained_legacy_rounding()
                           and nonmonetary(baseline_totals) == nonmonetary(candidate_totals))
        report["cases"][key] = {"baseline": before, "candidate": after, "budget_ms": budget,
            "cpu_baseline": summary(results["baseline"]["cpu_samples"][key]),
            "cpu_candidate": summary(results["candidate"]["cpu_samples"][key]),
            "bytes_baseline": results["baseline"]["bytes"][key], "bytes_candidate": results["candidate"]["bytes"][key],
            "pass": after["p95_ms"] <= before["p95_ms"]+budget,
            "totals_equal": equal, "correctness_pass": correctness,
            "cost_reference": oracle, "baseline_rounding_drift": baseline_rounding_drift}
    path = Path(args.output)/args.report_name
    path.write_text(json.dumps(report, indent=2)+"\n")
    print(json.dumps({"report": str(path), "cases": report["cases"]}, indent=2))
    if not all(row["pass"] and row["correctness_pass"] for row in report["cases"].values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
