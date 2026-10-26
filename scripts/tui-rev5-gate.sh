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

"$PY" -m pytest tests/tui/test_tui_app.py tests/tui/test_tui_charts.py \
  tests/tui/test_tui_data.py tests/tui/test_tui_formatting.py tests/tui/test_tui_import_discipline.py \
  tests/tui/test_tui_ranges.py tests/tui/test_tui_remote.py tests/tui/test_tui_report.py \
  tests/tui/test_tui_overview_pane.py tests/tui/test_tui_sessions_pane.py \
  tests/tui/test_tui_stats_pane.py tests/tui/test_tui_report_pane.py \
  tests/tui/test_tui_speed_pane.py tests/tui/test_tui_speed_series.py tests/tui/test_tui_speed_builds.py \
  tests/tui/test_tui_quota_pane.py tests/tui/test_tui_pricing_pane.py \
  tests/tui/test_tui_range_strip.py tests/tui/test_tui_selected_state.py tests/tui/test_tui_navigation.py \
  tests/tui/test_tui_source_colours.py tests/tui/test_tui_layout.py \
  tests/tui/test_tui_resize_refit.py tests/tui/test_tui_footer_fit.py \
  tests/tui/test_tui_terminal_colour.py -q "$@"
status=$?

# The reviewers' regression files, run one pytest process each. They all share
# the module basename, so they cannot share a run. They live under output/ of
# the MAIN checkout -- this is a linked worktree -- until a cleanup sweep moves
# them into docs/internal/output-archive/, so both roots are searched.
# The MAIN checkout, not this linked worktree: its .git is the shared one, and
# walking up from "." instead (an earlier version, without a bound at "/") spun
# forever the day a review folder moved out from under it.
main_root="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"
main_root="${main_root%/.git}"
[ -d "$main_root" ] || main_root="/mnt/h/Developing/Agent/Tokdash_Project/tokdash"

found=0
for dir in tui-rev5-review tui-rev5-review2 tui-rev5-review3 tui-rev5-review4 \
           tui-rev5-review5 tui-rev5-review6; do
  for root in "$main_root/output" "$main_root/docs/internal/output-archive"; do
    # A reviewer's file is usually test_regressions.py; the sixth round shipped
    # test_followup_review.py, and a gate that looks for one name silently
    # drops whichever reviewer did not use it.
    for file in "$root/$dir/test_regressions.py" "$root/$dir/test_followup_review.py"; do
      [ -f "$file" ] || continue
      found=$((found + 1))
      echo "--- ${dir}/$(basename "$file") ---"
      "$PY" -m pytest "$file" -q --tb=line || status=1
    done
  done
done
if [ "$found" = "0" ]; then
  echo "no reviewer regression files found under $main_root -- gate ran without them" >&2
  status=1
fi
exit $status
