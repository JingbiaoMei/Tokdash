#!/usr/bin/env bash
# Recheck all affected cases after an Agent Time-only optimization.
set -uo pipefail
baseline=${1:?Usage: scripts/recheck_agent_sparkline_performance.sh BASELINE OUTPUT PREVIOUS_OUTPUT PREVIOUS_REVISION}
output_dir=${2:?Output directory required}
previous_output=${3:?Complete previous campaign required}
previous_revision=${4:?Previous tested revision required}
python_bin=${TOKDASH_BENCH_PYTHON:-python3}
browser_python=${TOKDASH_BROWSER_PYTHON:-/home/howard/anaconda3/envs/webapptest/bin/python}
mkdir -p "$output_dir"
if ! PYTHONPATH=src "$python_bin" -m pytest > "$output_dir/pytest.log" 2>&1; then
    printf 'Tests failed; see %s/pytest.log\n' "$output_dir"
    exit 1
fi
printf 'Complete test suite passed.\n'
status=0
for workload in regular dense; do
    rows=300
    if [[ $workload == dense ]]; then rows=1500; fi
    printf 'Starting %s Agent Time benchmark.\n' "$workload"
    if "$python_bin" scripts/benchmark_sparkline_aggregation.py --baseline "$baseline" \
        --rows-per-day "$rows" --kinds active --repeats 96 --output "$output_dir/$workload" \
        > "$output_dir/$workload-summary.log" 2>&1; then
        printf '%s Agent Time benchmark passed.\n' "$workload"
    else
        printf '%s Agent Time benchmark completed with failed checks.\n' "$workload"
        status=1
    fi
done
if ! "$browser_python" scripts/verify_overview_sparklines.py --baseline "$baseline" \
    --repeats 32 --output "$output_dir/browser" --server-python "$python_bin" \
    > "$output_dir/browser-summary.log" 2>&1; then status=1; fi
if ! "$python_bin" scripts/summarize_sparkline_benchmarks.py "$output_dir" --baseline "$baseline" \
    --unchanged "$previous_output" --unchanged-revision "$previous_revision" \
    > "$output_dir/comparison-summary.log" 2>&1; then status=1; fi
printf 'Validation complete; reports in %s (exit status %s).\n' "$output_dir" "$status"
exit "$status"
