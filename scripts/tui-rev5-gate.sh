#!/usr/bin/env bash
# Revision-5 TUI gate: the branch's own TUI tests, then each reviewer's
# regression file run on its own (they all share the module basename
# `test_regressions`, which pytest's default import mode cannot tell apart).
#
#   scripts/tui-rev5-gate.sh                 # focused TUI gate
#   scripts/tui-rev5-gate.sh -x              # extra pytest args go to run 1
#
# Isolated data only: TOKDASH_DATA_DIR defaults to a throwaway under /tmp so a
# run can never open the live ~/.tokdash usage database.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
export TOKDASH_TUI_NO_REMOTE=1
export TOKDASH_DATA_DIR="${TOKDASH_DATA_DIR:-/tmp/tui-rev5-dev}"
export PYTHONPATH=src:tests
PY="${PY:-python3}"

"$PY" -m pytest tests/test_tui_revision5.py tests/test_tui_app.py tests/test_tui_charts.py \
  tests/test_tui_data.py tests/test_tui_formatting.py tests/test_tui_import_discipline.py \
  tests/test_tui_ranges.py tests/test_tui_remote.py tests/test_tui_report.py \
  tests/test_tui_review_fixes.py tests/test_tui_review_fixes2.py \
  tests/test_tui_review_fixes3.py tests/test_tui_review_fixes4.py -q "$@"
status=$?

repo="$(git rev-parse --show-toplevel 2>/dev/null)"
main_root="${repo}"
while [ -n "$main_root" ] && [ ! -d "$main_root/output/tui-rev5-review" ]; do
  main_root="$(dirname "$main_root")"
done
[ -n "$main_root" ] || main_root="/mnt/h/Developing/Agent/Tokdash_Project/tokdash"

for dir in tui-rev5-review tui-rev5-review2 tui-rev5-review3 tui-rev5-review4; do
  file="$main_root/output/$dir/test_regressions.py"
  [ -f "$file" ] || continue
  echo "--- $dir ---"
  "$PY" -m pytest "$file" -q --tb=line || status=1
done
exit $status
