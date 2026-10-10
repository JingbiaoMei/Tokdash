#!/usr/bin/env bash
# Run after an unrestricted complete campaign; keep both sets of raw results.
set -uo pipefail
baseline=${1:?Usage: scripts/check_sparkline_affinity.sh BASELINE OUTPUT COMPLETE_CAMPAIGN [CPU]}
output_dir=${2:?Specify a separate output directory}
complete_dir=${3:?Specify the completed full campaign}
cpu=${4:-0}
python_bin=${TOKDASH_BENCH_PYTHON:-python3}

# Optional kernel notification lets this follow an owned running campaign
# without polling it or overlapping its timed workload.
if [[ -n ${TOKDASH_BENCH_WAIT_PID:-} ]]; then
    "$python_bin" - "$TOKDASH_BENCH_WAIT_PID" <<'PY'
import os, select, sys
try:
    descriptor = os.pidfd_open(int(sys.argv[1]))
except ProcessLookupError:
    pass
else:
    select.select([descriptor], [], [])
    os.close(descriptor)
PY
fi

mkdir -p "$output_dir"
cp "$complete_dir/pytest.log" "$output_dir/pytest.log"
ln -s "$(realpath "$complete_dir/browser")" "$output_dir/browser"
status=0
for workload in regular dense; do
    rows=${TOKDASH_BENCH_REGULAR_ROWS:-300}
    if [[ $workload == dense ]]; then rows=${TOKDASH_BENCH_DENSE_ROWS:-1500}; fi
    printf 'Starting controlled %s comparison: both workers on CPU %s.\n' "$workload" "$cpu"
    if "$python_bin" scripts/benchmark_sparkline_aggregation.py --baseline "$baseline" \
        --cpu "$cpu" --rows-per-day "$rows" --repeats 96 --output "$output_dir/$workload" \
        > "$output_dir/$workload-summary.log" 2>&1; then
        printf '%s controlled comparison passed.\n' "$workload"
    else
        printf '%s controlled comparison completed with failed checks.\n' "$workload"
        status=1
    fi
done
if ! "$python_bin" scripts/summarize_sparkline_benchmarks.py "$output_dir" --baseline "$baseline" \
    > "$output_dir/comparison-summary.log" 2>&1; then status=1; fi
printf 'Controlled comparisons complete; unrestricted reports remain in %s (status %s).\n' "$complete_dir" "$status"
exit "$status"
