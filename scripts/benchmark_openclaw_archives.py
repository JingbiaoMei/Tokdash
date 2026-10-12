#!/usr/bin/env python3
"""Compare OpenClaw parsing/cache latency with a baseline checkout.

Run from the project root:
    python3 scripts/benchmark_openclaw_archives.py --baseline /path/to/baseline

Only generated transcripts and temporary usage databases are opened. JSON output
includes raw samples, timings, peak Python allocations, and regression checks.
Regression checks alternate both readers in one process to control scheduling noise.
"""
from __future__ import annotations

import argparse
import gc
import json
import math
import os
from pathlib import Path
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc


def summarize(samples):
    ordered = sorted(samples)
    return {
        "median_ms": statistics.median(samples),
        "p95_ms": ordered[math.ceil(len(ordered) * .95) - 1],
        "samples_ms": samples,
    }


def peak_rss_mib():
    try:
        import resource
    except ImportError:  # Windows has no getrusage; Python allocations still work.
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024 if sys.platform == "darwin" else 1024)


def paired_file_worker(args):
    """Alternate readers in one process to control CPU/allocator scheduling noise."""
    import importlib.util

    sys.path.insert(0, str(Path(args.repo) / "src"))
    from tokdash.sources import openclaw

    name = "tokdash.sources._openclaw_benchmark_baseline"
    spec = importlib.util.spec_from_file_location(name, args.baseline / "src/tokdash/sources/openclaw.py")
    baseline = importlib.util.module_from_spec(spec)
    sys.modules[name] = baseline
    spec.loader.exec_module(baseline)
    plain = sorted((args.fixture / "plain").glob("*.jsonl*"))[0]
    compressed = sorted((args.fixture / "compressed").glob("*.jsonl*"))[0]
    cases = [("baseline_plain", baseline.parse_session_file, plain),
             ("candidate_plain", openclaw.parse_session_file, plain),
             ("candidate_compressed", openclaw.parse_session_file, compressed)]
    expected = baseline.parse_session_file(str(plain))
    for _, parse, path in cases:
        assert parse(str(path)) == expected
    samples = {name: [] for name, _, _ in cases}
    for block in range(args.rounds):
        for name, parse, path in (cases if block % 2 == 0 else list(reversed(cases))):
            value = None
            gc.collect()
            start = time.perf_counter()
            for _ in range(args.warm_repeats):
                value = parse(str(path))
            samples[name].append((time.perf_counter() - start) * 1000 / args.warm_repeats)
            assert value == expected
    return {name: summarize(values) for name, values in samples.items()}


def paired_warm_worker(args):
    """Compare warm paths under the same scheduler, with separate verified DBs."""
    import importlib.util

    sys.path.insert(0, str(Path(args.repo) / "src"))
    from tokdash.pricing import PricingDatabase
    from tokdash.sources import openclaw

    name = "tokdash.sources._openclaw_benchmark_baseline"
    spec = importlib.util.spec_from_file_location(name, args.baseline / "src/tokdash/sources/openclaw.py")
    baseline = importlib.util.module_from_spec(spec)
    sys.modules[name] = baseline
    spec.loader.exec_module(baseline)
    pricing = PricingDatabase()
    dirs = [str(args.fixture / "plain")]
    cases = [("baseline_plain", baseline), ("candidate_plain", openclaw)]
    data_dir = Path(os.environ["TOKDASH_DATA_DIR"])

    def unexpected_parse(*unused, **kwargs):
        raise AssertionError("Paired warm request parsed a transcript")

    for label, reader in cases:
        os.environ["TOKDASH_USAGE_DB_PATH"] = str(data_dir / (label + ".sqlite3"))
        usage = reader.get_session_usage(dirs, pricing_db=pricing)
        assert usage["total_messages"] == args.files * args.rows_per_file
        reader.parse_session_file = unexpected_parse
    results = {}
    for metric in ("warm_corpus", "warm_store", "warm_usage"):
        samples = {label: [] for label, _ in cases}
        for block in range(args.rounds):
            for label, reader in (cases if block % 2 == 0 else list(reversed(cases))):
                os.environ["TOKDASH_USAGE_DB_PATH"] = str(data_dir / (label + ".sqlite3"))
                if metric == "warm_corpus":
                    operation = lambda: reader.collect_session_corpus(dirs)
                elif metric == "warm_store":
                    operation = lambda: reader._sync_openclaw_store(dirs, pricing)
                else:
                    operation = lambda: reader.get_session_usage(dirs, pricing_db=pricing)
                gc.collect()
                start = time.perf_counter()
                for _ in range(args.warm_repeats):
                    value = operation()
                samples[label].append((time.perf_counter() - start) * 1000 / args.warm_repeats)
                if metric == "warm_usage":
                    assert value["total_messages"] == args.files * args.rows_per_file
        results[metric] = {label: summarize(values) for label, values in samples.items()}
    return results


def paired_corpus_worker(args):
    """Compare genuinely cold parser caches under one scheduler/allocator."""
    import importlib.util

    sys.path.insert(0, str(Path(args.repo) / "src"))
    from tokdash.sources import openclaw

    name = "tokdash.sources._openclaw_benchmark_baseline"
    spec = importlib.util.spec_from_file_location(name, args.baseline / "src/tokdash/sources/openclaw.py")
    baseline = importlib.util.module_from_spec(spec)
    sys.modules[name] = baseline
    spec.loader.exec_module(baseline)
    cases = [("baseline_plain", baseline, "plain"), ("candidate_plain", openclaw, "plain"),
             ("candidate_compressed", openclaw, "compressed")]
    samples = {label: [] for label, _, _ in cases}
    for block in range(args.rounds):
        for label, reader, layout in (cases if block % 2 == 0 else list(reversed(cases))):
            baseline._ENTRY_CACHE.clear()
            openclaw._ENTRY_CACHE.clear()
            value = None
            gc.collect()
            dirs = [str(args.fixture / layout)]
            start = time.perf_counter()
            value = reader.collect_session_corpus(dirs)
            samples[label].append((time.perf_counter() - start) * 1000)
            assert len(value.rows) == args.files * args.rows_per_file and not value.failed
            assert sum(row['input_raw'] + row['cache_write'] + row['output'] + row['cache_read']
                       for row in value.rows) == args.files * args.rows_per_file * 180
    return {label: summarize(values) for label, values in samples.items()}


def build_fixtures(root, args):
    from zstandard import ZstdCompressor

    rng = random.Random(219)
    plain, compressed = root / "plain", root / "compressed"
    plain.mkdir()
    compressed.mkdir()
    raw_bytes = compressed_bytes = 0
    for index in range(args.files):
        archived = index < args.archive_files
        name = f"{index:04d}.jsonl" + (".deleted.2026-09-26T18-03-25.494Z.fixture" if archived else "")
        rows = [{"type": "session", "id": f"s{index}", "cwd": "/synthetic"}]
        for row in range(args.rows_per_file):
            # Mix repeated prose and deterministic varied text rather than timing
            # an unrealistically tiny archive of identical messages.
            varied = "" if args.high_compression else "".join(
                rng.choices("abcdefghijklmnopqrstuvwxyz ", k=args.payload_bytes // 4))
            text = (("Synthetic assistant response with tool context. " *
                     (args.payload_bytes // 46 + 1))[:args.payload_bytes - len(varied)] + varied)
            rows.append({
                "type": "message", "id": f"m{index}-{row}", "timestamp": "2026-10-12T12:00:00Z",
                "message": {"role": "assistant", "provider": "fixture", "model": "fixture-model",
                            "timestamp": "2026-10-12T12:00:00Z", "content": text,
                            "usage": {"input": 100, "output": 20, "cacheRead": 50, "cacheWrite": 10}},
            })
            if args.high_compression:
                # Identical large records stress output expansion. Headerless
                # message ids remain countable via the source's fallback key.
                rows[-1].pop("id")
        payload = b"".join((json.dumps(row) + "\n").encode() for row in rows)
        (plain / name).write_bytes(payload)
        raw_bytes += len(payload)
        if archived:
            blob = ZstdCompressor().compress(payload)
            (compressed / (name + ".zst")).write_bytes(blob)
            compressed_bytes += len(blob)
        else:
            (compressed / name).write_bytes(payload)
            compressed_bytes += len(payload)
    return {"uncompressed_bytes": raw_bytes, "mixed_bytes": compressed_bytes,
            "messages": args.files * args.rows_per_file}


def worker(args):
    sys.path.insert(0, str(Path(args.repo) / "src"))
    from tokdash.pricing import PricingDatabase
    from tokdash.sources import openclaw

    directory = Path(args.fixture) / args.layout
    dirs = [str(directory)]
    expected = args.files * args.rows_per_file
    pricing = PricingDatabase()
    results = {}

    def measure(name, operation, *, cold=False, repeat=1):
        samples = []
        # One untimed call warms filesystem pages, imports and pricing. "Cold"
        # below specifically means a missing parser cache, not a dropped OS cache.
        operation()
        for _ in range(args.rounds):
            if cold:
                openclaw._ENTRY_CACHE.clear()
            gc.collect()
            start = time.perf_counter()
            for _ in range(repeat):
                value = operation()
            samples.append((time.perf_counter() - start) * 1000 / repeat)
            if name.endswith("corpus"):
                assert len(value.rows) == expected and not value.failed
            elif name.endswith("usage"):
                assert value["total_messages"] == expected
                assert value["total_tokens"] == expected * 180
            elif name == "cold_file":
                assert len(value) == args.rows_per_file + 1
        results[name] = summarize(samples)

    # An archive-shaped filename with plain content tests the existing archive
    # path; comparing its compressed counterpart measures the decoding overhead.
    single = sorted(directory.glob("*.jsonl*"))[0]
    measure("cold_file", lambda: openclaw.parse_session_file(
        str(single), unavailable=openclaw._OpenClawReadUnavailable))
    measure("cold_corpus", lambda: openclaw.collect_session_corpus(dirs), cold=True)
    store = openclaw._sync_openclaw_store(dirs, pricing)
    assert len(store.query_entries(sources=["openclaw"])) == expected
    # Both warm paths must do zero transcript parsing and zero decompression.
    def unexpected_parse(*unused, **kwargs):
        raise AssertionError("Warm OpenClaw request parsed a transcript")
    openclaw.parse_session_file = unexpected_parse
    measure("warm_corpus", lambda: openclaw.collect_session_corpus(dirs), repeat=args.warm_repeats)
    measure("warm_store", lambda: openclaw._sync_openclaw_store(dirs, pricing), repeat=args.warm_repeats)
    measure("warm_usage", lambda: openclaw.get_session_usage(dirs, pricing_db=pricing), repeat=args.warm_repeats)
    results["peak_rss_mib"] = peak_rss_mib()
    return results


def memory_worker(args):
    sys.path.insert(0, str(Path(args.repo) / "src"))
    from tokdash.sources import openclaw

    gc.collect()
    tracemalloc.start()
    corpus = openclaw.collect_session_corpus([str(Path(args.fixture) / args.layout)])
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(corpus.rows) == args.files * args.rows_per_file and not corpus.failed
    return {"peak_python_mib": peak / (1024 * 1024)}


def run_child(args, root, repo, layout, memory=False):
    env = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix="openclaw-store-", dir=args.temp_dir) as data_dir:
        env.update(TOKDASH_DATA_DIR=data_dir, TOKDASH_USAGE_DB_PATH=str(Path(data_dir) / "usage.sqlite3"),
                   TOKDASH_USAGE_DB="1", OPENCLAW_HOME=str(root / "unused-home"))
        command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--repo", str(repo),
                   "--layout", layout, "--fixture", str(root), "--files", str(args.files),
                   "--rows-per-file", str(args.rows_per_file), "--rounds", str(args.rounds),
                   "--warm-repeats", str(args.warm_repeats)]
        if memory:
            command.append("--memory")
        if args.paired_file_only:
            command.extend(["--paired-file-only", "--baseline", str(args.baseline.resolve())])
        if args.paired_warm_only:
            command.extend(["--paired-warm-only", "--baseline", str(args.baseline.resolve())])
        if args.paired_corpus_only:
            command.extend(["--paired-corpus-only", "--baseline", str(args.baseline.resolve())])
        result = subprocess.run(command, cwd=repo, env=env, text=True, capture_output=True, check=True)
        return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path, default=Path("output/benchmarks/openclaw-zstd.json"))
    parser.add_argument("--files", type=int, default=48)
    parser.add_argument("--archive-files", type=int, default=12)
    parser.add_argument("--rows-per-file", type=int, default=400)
    parser.add_argument("--payload-bytes", type=int, default=2048)
    parser.add_argument("--high-compression", action="store_true", help="Stress memory with large identical message content")
    parser.add_argument("--rounds", type=int, default=9)
    parser.add_argument("--blocks", type=int, default=2)
    parser.add_argument("--warm-repeats", type=int, default=20)
    parser.add_argument("--max-regression-percent", type=float, default=10)
    parser.add_argument("--noise-floor-ms", type=float, default=.25)
    parser.add_argument("--temp-dir", type=Path, help="Filesystem for generated transcripts and temporary databases")
    paired = parser.add_mutually_exclusive_group()
    paired.add_argument("--paired-file-only", action="store_true", help="Control single-file noise by alternating both readers in one process")
    paired.add_argument("--paired-warm-only", action="store_true", help="Control warm-path noise with paired readers and separate databases")
    paired.add_argument("--paired-corpus-only", action="store_true", help="Control cold-corpus noise with paired readers and empty parser caches")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--memory", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--repo", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--fixture", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--layout", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        result = (paired_corpus_worker(args) if args.paired_corpus_only else
                  paired_warm_worker(args) if args.paired_warm_only else
                  paired_file_worker(args) if args.paired_file_only else
                  memory_worker(args) if args.memory else worker(args))
        print(json.dumps(result))
        return
    if args.baseline is None:
        parser.error("--baseline is required")
    if args.temp_dir is not None:
        args.temp_dir = args.temp_dir.resolve()
    if not (0 < args.archive_files <= args.files and args.rows_per_file > 0 and args.payload_bytes >= 4
            and args.rounds > 0 and args.blocks > 0 and args.warm_repeats > 0):
        parser.error("positive corpus/repetition sizes and 1 <= archive-files <= files are required")
    candidate = Path(__file__).resolve().parents[1]
    baseline = args.baseline.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    cases = [("baseline_plain", baseline, "plain"), ("candidate_plain", candidate, "plain"),
             ("candidate_compressed", candidate, "compressed")]
    collected = {name: {} for name, _, _ in cases}
    # Use the machine's temporary filesystem, not output/ on a mounted Windows
    # drive: SQLite fsync latency there would dominate every warm-store sample.
    # --temp-dir can override this for a deliberate filesystem comparison.
    with tempfile.TemporaryDirectory(prefix="openclaw-fixtures-", dir=args.temp_dir) as temporary:
        root = Path(temporary)
        fixture = build_fixtures(root, args)
        if args.paired_warm_only:
            measurements = run_child(args, root, candidate, "plain")
            comparisons = []
            for metric, values in measurements.items():
                before = values["baseline_plain"]["median_ms"]
                after = values["candidate_plain"]["median_ms"]
                limit = max(before * args.max_regression_percent / 100, args.noise_floor_ms)
                comparisons.append({"metric": metric, "baseline_ms": before, "candidate_ms": after,
                                    "change_percent": (after / before - 1) * 100, "passed": after - before <= limit})
            report = {"baseline": str(baseline), "candidate": str(candidate), "fixture": fixture,
                      "measurements": measurements, "plain_path_comparisons": comparisons,
                      "passed": all(row["passed"] for row in comparisons),
                      "timing_scope": "Both readers alternate in one process with separate verified databases; warm requests may not parse any transcript."}
            args.output.write_text(json.dumps(report, indent=2) + "\n")
            for row in comparisons:
                print(f"{row['metric']}: {row['baseline_ms']:.3f} -> {row['candidate_ms']:.3f} ms ({row['change_percent']:+.1f}%)")
            print(f"Results: {args.output}")
            raise SystemExit(0 if report["passed"] else 1)
        if args.paired_file_only or args.paired_corpus_only:
            measurements = run_child(args, root, candidate, "plain")
            before = measurements["baseline_plain"]["median_ms"]
            after = measurements["candidate_plain"]["median_ms"]
            limit = max(before * args.max_regression_percent / 100, args.noise_floor_ms)
            report = {"baseline": str(baseline), "candidate": str(candidate), "fixture": fixture,
                      "measurements": measurements, "change_percent": (after / before - 1) * 100,
                      "passed": after - before <= limit,
                      "timing_scope": ("Both readers alternate in one process with parser caches cleared before every sample."
                                       if args.paired_corpus_only else
                                       "Both readers alternate in one process; each sample is a batch average.")}
            args.output.write_text(json.dumps(report, indent=2) + "\n")
            label = "corpus" if args.paired_corpus_only else "file"
            print(f"Paired {label} parsing: {before:.3f} -> {after:.3f} ms ({report['change_percent']:+.1f}%)")
            print(f"Compressed equivalent: {measurements['candidate_compressed']['median_ms']:.3f} ms")
            print(f"Results: {args.output}")
            raise SystemExit(0 if report["passed"] else 1)
        for block in range(args.blocks):
            for name, repo, layout in (cases if block % 2 == 0 else list(reversed(cases))):
                result = run_child(args, root, repo, layout)
                for metric, values in result.items():
                    if not isinstance(values, dict):
                        collected[name].setdefault(metric, []).append(values)
                        continue
                    collected[name].setdefault(metric, []).extend(values["samples_ms"])
        measurements = {name: {metric: max(values) if metric == "peak_rss_mib" and values[0] is not None
                              else None if metric == "peak_rss_mib" else summarize(values)
                              for metric, values in metrics.items()}
                        for name, metrics in collected.items()}
        for name, repo, layout in cases:
            measurements[name].update(run_child(args, root, repo, layout, memory=True))
        paired_args = argparse.Namespace(**vars(args))
        paired_args.paired_file_only = True
        paired_files = run_child(paired_args, root, candidate, "plain")
        paired_args.paired_file_only = False
        paired_args.paired_warm_only = True
        paired_warm = run_child(paired_args, root, candidate, "plain")
        paired_args.paired_warm_only = False
        paired_args.paired_corpus_only = True
        paired_corpora = run_child(paired_args, root, candidate, "plain")
    comparisons = []
    for metric, reference in measurements["baseline_plain"].items():
        if not isinstance(reference, dict):
            continue
        if metric == "cold_corpus":
            before = paired_corpora["baseline_plain"]["median_ms"]
            after = paired_corpora["candidate_plain"]["median_ms"]
        elif metric == "cold_file":
            before = paired_files["baseline_plain"]["median_ms"]
            after = paired_files["candidate_plain"]["median_ms"]
        elif metric in paired_warm:
            before = paired_warm[metric]["baseline_plain"]["median_ms"]
            after = paired_warm[metric]["candidate_plain"]["median_ms"]
        else:
            before = reference["median_ms"]
            after = measurements["candidate_plain"][metric]["median_ms"]
        limit = max(before * args.max_regression_percent / 100, args.noise_floor_ms)
        comparisons.append({"metric": metric, "baseline_ms": before, "candidate_ms": after,
                            "change_percent": (after / before - 1) * 100, "passed": after - before <= limit,
                            "scope": "paired readers in one process"})
    report = {
        "python": platform.python_version(), "platform": platform.platform(),
        "baseline": str(baseline), "candidate": str(candidate),
        "fixture": fixture, "parameters": {key: value for key, value in vars(args).items()
                                            if key not in {"repo", "fixture", "layout", "worker", "memory"}},
        "measurements": measurements, "paired_file_measurements": paired_files,
        "paired_warm_measurements": paired_warm,
        "paired_corpus_measurements": paired_corpora,
        "plain_path_comparisons": comparisons,
        "passed": all(row["passed"] for row in comparisons),
        "timing_scope": "Parser caches cold, OS filesystem cache warm; workloads executed sequentially in alternating order. RSS measured without tracemalloc in timing workers; Python allocations measured in separate workers.",
    }
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")
    for row in comparisons:
        print(f"{row['metric']:12s}: {row['baseline_ms']:.3f} -> {row['candidate_ms']:.3f} ms "
              f"({row['change_percent']:+.1f}%) {'PASS' if row['passed'] else 'FAIL'}")
    print(f"Results: {args.output}")
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
