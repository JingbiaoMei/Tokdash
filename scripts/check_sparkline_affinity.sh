#!/usr/bin/env bash
# Run after an unrestricted complete campaign; keep both sets of raw results.
set -uo pipefail
baseline=${1:?Usage: scripts/check_sparkline_affinity.sh BASELINE OUTPUT COMPLETE_CAMPAIGN [CPU]}
output_dir=${2:?Specify a separate output directory}
complete_dir=${3:?Specify the completed full campaign}
cpu=${4:-0}
python_bin=${TOKDASH_BENCH_PYTHON:-python3}
browser_python=${TOKDASH_BROWSER_PYTHON:-/home/howard/anaconda3/envs/webapptest/bin/python}
browser_repeats=${TOKDASH_BROWSER_BENCH_REPEATS:-64}

# Optional kernel notification lets this follow an owned running campaign
# without polling it or overlapping its timed workload.
if [[ -n ${TOKDASH_BENCH_WAIT_PID:-} ]]; then
    if ! "$python_bin" - "$TOKDASH_BENCH_WAIT_PID" <<'PY'
import ctypes, errno, os, select, sys
pid = int(sys.argv[1])
open_descriptor = getattr(os, "pidfd_open", None)
if open_descriptor is None:
    # Some conda builds omit os.pidfd_open despite a supporting host libc.
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.pidfd_open
    function.argtypes = [ctypes.c_int, ctypes.c_uint]
    function.restype = ctypes.c_int
    descriptor = function(pid, 0)
    if descriptor < 0 and ctypes.get_errno() != errno.ESRCH:
        raise OSError(ctypes.get_errno(), "pidfd_open failed")
else:
    try:
        descriptor = open_descriptor(pid)
    except ProcessLookupError:
        descriptor = -1
if descriptor >= 0:
    select.select([descriptor], [], [])
    os.close(descriptor)
PY
    then
        printf 'Completion wait failed; no benchmark was started.\n'
        exit 1
    fi
fi

mkdir -p "$output_dir"
cp "$complete_dir/pytest.log" "$output_dir/pytest.log"
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
printf 'Starting fresh desktop/mobile checks and %s paired browser samples per range.\n' "$browser_repeats"
if ! "$browser_python" scripts/verify_overview_sparklines.py --baseline "$baseline" \
    --repeats "$browser_repeats" --output "$output_dir/browser" --server-python "$python_bin" \
    > "$output_dir/browser-summary.log" 2>&1; then status=1; fi
if ! "$python_bin" scripts/summarize_sparkline_benchmarks.py "$output_dir" --baseline "$baseline" \
    > "$output_dir/comparison-summary.log" 2>&1; then status=1; fi
printf 'Controlled comparisons complete; unrestricted reports remain in %s (status %s).\n' "$complete_dir" "$status"
exit "$status"
