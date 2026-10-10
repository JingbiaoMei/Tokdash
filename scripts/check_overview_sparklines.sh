#!/usr/bin/env bash
# Run from the repository root. Data and timings are synthetic and isolated.
set -uo pipefail

baseline=${1:?Usage: scripts/check_overview_sparklines.sh BASELINE_CHECKOUT [OUTPUT_DIR]}
output_dir=${2:-output/sparkline-validation/year-full}
backend_repeats=${TOKDASH_BENCH_REPEATS:-96}
browser_repeats=${TOKDASH_BROWSER_BENCH_REPEATS:-32}
regular_rows=${TOKDASH_BENCH_REGULAR_ROWS:-300}
dense_rows=${TOKDASH_BENCH_DENSE_ROWS:-1500}
python_bin=${TOKDASH_BENCH_PYTHON:-python3}
browser_python=${TOKDASH_BROWSER_PYTHON:-/home/howard/anaconda3/envs/webapptest/bin/python}
cpu_args=()
if [[ -n ${TOKDASH_BENCH_CPU:-} ]]; then cpu_args=(--cpu "$TOKDASH_BENCH_CPU"); fi

mkdir -p "$output_dir"
printf 'Running complete tests before timing either version.\n'
if ! PYTHONPATH=src "$python_bin" -m pytest > "$output_dir/pytest.log" 2>&1; then
    printf 'Tests failed; see %s/pytest.log\n' "$output_dir"
    exit 1
fi
printf 'Complete test suite passed.\n'

status=0
for workload in regular dense; do
    rows=$regular_rows
    if [[ $workload == dense ]]; then rows=$dense_rows; fi
    printf 'Starting %s backend benchmark: %s rows per day, %s samples per case.\n' "$workload" "$rows" "$backend_repeats"
    if "$python_bin" scripts/benchmark_sparkline_aggregation.py --baseline "$baseline" \
        --rows-per-day "$rows" --repeats "$backend_repeats" "${cpu_args[@]}" --output "$output_dir/$workload" \
        > "$output_dir/$workload-summary.log" 2>&1; then
        printf '%s backend benchmark passed.\n' "$workload"
    else
        printf '%s backend benchmark completed with failed checks.\n' "$workload"
        status=1
    fi
done

printf 'Starting all-preset desktop/mobile checks and paired browser timings.\n'
if "$browser_python" scripts/verify_overview_sparklines.py --baseline "$baseline" \
    --repeats "$browser_repeats" --output "$output_dir/browser" \
    --server-python "$python_bin" > "$output_dir/browser-summary.log" 2>&1; then
    printf 'All-preset browser benchmark passed.\n'
else
    printf 'Browser benchmark completed with failed checks.\n'
    status=1
fi
if "$python_bin" scripts/summarize_sparkline_benchmarks.py "$output_dir" --baseline "$baseline" \
    > "$output_dir/comparison-summary.log" 2>&1; then
    printf 'Complete JSON/CSV comparison exported; all checks passed.\n'
else
    printf 'Comparison contains failed checks or incomplete reports; see %s/comparison-summary.log\n' "$output_dir"
    status=1
fi
printf 'Validation complete; reports in %s (exit status %s).\n' "$output_dir" "$status"
exit "$status"
