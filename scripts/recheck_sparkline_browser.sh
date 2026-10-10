#!/usr/bin/env bash
# Full tests and fresh browser measurements after a frontend-only change.
set -uo pipefail
baseline=${1:?Usage: scripts/recheck_sparkline_browser.sh BASELINE OUTPUT BACKEND_CAMPAIGN}
output_dir=${2:?Specify a separate output directory}
backend_dir=${3:?Specify the completed controlled backend campaign}
python_bin=${TOKDASH_BENCH_PYTHON:-python3}
browser_python=${TOKDASH_BROWSER_PYTHON:-/home/howard/anaconda3/envs/webapptest/bin/python}
browser_repeats=${TOKDASH_BROWSER_BENCH_REPEATS:-64}
mkdir -p "$output_dir"
printf 'Running complete tests for the updated frontend.\n'
if ! PYTHONPATH=src "$python_bin" -m pytest > "$output_dir/pytest.log" 2>&1; then
    printf 'Complete suite failed; see %s/pytest.log\n' "$output_dir"
    exit 1
fi
printf 'Complete suite passed; starting all-preset physical checks and %s paired browser samples.\n' "$browser_repeats"
status=0
if ! "$browser_python" scripts/verify_overview_sparklines.py --baseline "$baseline" \
    --repeats "$browser_repeats" --output "$output_dir/browser" --server-python "$python_bin" \
    > "$output_dir/browser-summary.log" 2>&1; then status=1; fi
if ! "$python_bin" scripts/summarize_sparkline_benchmarks.py "$output_dir" --baseline "$baseline" \
    --backend-campaign "$backend_dir" > "$output_dir/comparison-summary.log" 2>&1; then status=1; fi
printf 'Frontend comparison complete; all backend raw results retained in %s (status %s).\n' "$backend_dir" "$status"
exit "$status"
