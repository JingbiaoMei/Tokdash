#!/usr/bin/env python3
"""Export the complete paired sparkline comparison as reviewable JSON and CSV.

Run from the repo root after scripts/check_overview_sparklines.sh completes.
"""
import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import statistics
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--unchanged", type=Path, help="Earlier complete campaign for unchanged Usage/native paths")
    parser.add_argument("--unchanged-revision", help="Revision tested by the earlier complete campaign")
    args = parser.parse_args()
    if bool(args.unchanged) != bool(args.unchanged_revision):
        parser.error("--unchanged and --unchanged-revision must be supplied together")
    root = args.output
    rows = []
    datasets = {}
    for workload in ("regular", "dense"):
        report = json.loads((root / workload / "aggregation-performance.json").read_text())
        assert report["repeats"] == 96, workload
        source_reports = {case: str(root / workload / "aggregation-performance.json") for case in report["cases"]}
        if args.unchanged:
            prior_path = args.unchanged / workload / "aggregation-performance.json"
            prior = json.loads(prior_path.read_text())
            assert prior["repeats"] == 96 and len(prior["cases"]) == 30
            assert prior["rows"] == report["rows"] and prior["corpus"] == report["corpus"]
            assert len(report["cases"]) == 10 and all(case.startswith("active/") for case in report["cases"])
            unaffected = {case: value for case, value in prior["cases"].items() if not case.startswith("active/")}
            report["cases"] = {**unaffected, **report["cases"]}
            source_reports.update({case: str(prior_path) for case in unaffected})
        assert len(report["cases"]) == 30, workload
        datasets[workload] = {key: report[key] for key in ("rows", "corpus", "gc_mode", "method")}
        for case, values in report["cases"].items():
            rows.append({
                "workload": workload, "case": case,
                "baseline_median_ms": values["baseline"]["median_ms"],
                "candidate_median_ms": values["candidate"]["median_ms"],
                "baseline_p95_ms": values["baseline"]["p95_ms"],
                "candidate_p95_ms": values["candidate"]["p95_ms"],
                "p95_delta_ms": values["candidate"]["p95_ms"] - values["baseline"]["p95_ms"],
                "budget_ms": values["budget_ms"],
                "baseline_cpu_p95_ms": values["cpu_baseline"]["p95_ms"],
                "candidate_cpu_p95_ms": values["cpu_candidate"]["p95_ms"],
                "baseline_bytes": values["bytes_baseline"], "candidate_bytes": values["bytes_candidate"],
                "latency_pass": values["pass"], "correctness_pass": values["correctness_pass"],
                "totals_equal": values["totals_equal"],
                "legacy_half_cent_rounding": values["baseline_rounding_drift"],
                "source_report": source_reports[case],
            })
    browser = json.loads((root / "browser/browser-performance.json").read_text())
    assert browser["repeats_per_range"] == 32 and len(browser["cases"]) == 10
    diagnostics = {}
    for case, values in browser["cases"].items():
        rows.append({
            "workload": "browser", "case": case,
            "baseline_median_ms": values["baseline"]["median_ms"],
            "candidate_median_ms": values["candidate"]["median_ms"],
            "baseline_p95_ms": values["baseline"]["p95_ms"],
            "candidate_p95_ms": values["candidate"]["p95_ms"],
            "p95_delta_ms": values["candidate"]["p95_ms"] - values["baseline"]["p95_ms"],
            "budget_ms": values["budget_ms"], "latency_pass": values["latency_pass"],
            "baseline_settled_p95_ms": values["settled_baseline"]["p95_ms"],
            "candidate_settled_p95_ms": values["settled_candidate"]["p95_ms"],
            "settled_budget_ms": values["settled_budget_ms"],
            "settled_pass": values["settled_pass"], "requests_equal": values["requests_equal"],
        })
        diagnostics[case] = {}
        for label in ("baseline", "candidate"):
            samples = browser["diagnostics"][case][label]
            tasks = [task["duration_ms"] for sample in samples for task in sample["long_tasks"]]
            diagnostics[case][label] = {
                "total_long_tasks": len(tasks), "max_long_task_ms": max(tasks, default=0),
                "median_api_bytes": statistics.median(
                    sum(resource["bytes"] for resource in sample["resources"]) for sample in samples),
            }
    visual = json.loads((root / "browser/browser-visual.json").read_text())
    assert len(visual["screens"]) == 20
    assert {screen["width"] for screen in visual["screens"]} == {390, 1440}
    assert set(visual["presets"]) == set(browser["cases"])
    curve_checks = sum(len(screen["data"]["curves"]) for screen in visual["screens"])
    assert curve_checks == 120
    visual_pass = not visual["errors"] and all(
        curve["reason"] == "drawn" and curve["path"]
        for screen in visual["screens"] for curve in screen["data"]["curves"])
    log = (root / "pytest.log").read_text()
    tests = re.search(r"(\d+) passed, (\d+) skipped", log)
    assert tests, "Complete test suite did not pass"
    passed = visual_pass and not browser["errors"] and all(
        row["latency_pass"] and row.get("correctness_pass", True)
        and row.get("settled_pass", True) and row.get("requests_equal", True) for row in rows)
    report = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "baseline_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=args.baseline, text=True).strip(),
        "candidate_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "tests": {"passed": int(tests[1]), "skipped": int(tests[2])},
        "visual_checks": curve_checks, "screenshots": len(visual["screens"]),
        "backend_paired_samples": 96 * 60, "browser_paired_samples": 32 * 10,
        "gate": browser["gate"], "datasets": datasets,
        "unchanged_paths_revision": args.unchanged_revision,
        "campaign_note": "Usage/native results retained from the complete campaign; all Agent Time cases and browser checks rerun after the interval lifetime change." if args.unchanged else "All cases from one complete campaign.",
        "all_pass": passed, "cases": rows, "browser_diagnostics": diagnostics,
        "limits": "Synthetic warm-cache workloads on this machine; native source discovery is stubbed equally. Browser fixtures isolate rendering and requests from database aggregation. Timings do not guarantee identical performance on every installation.",
    }
    (root / "comparison.json").write_text(json.dumps(report, indent=2) + "\n")
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with (root / "comparison.csv").open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({key: value for key, value in report.items() if key not in ("cases", "browser_diagnostics")}, indent=2))
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
