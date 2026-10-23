#!/usr/bin/env bash
# Re-render the two reference screens (Overview, Speed) and rasterise them.
#   scripts/tui-rev5-visuals.sh [case ...]      # default: the parity set
# Captures are the app's OWN frames (Textual pilot), so the .txt files are the
# terminal's cells and the .png files are its colours. Chromium needs a real
# terminal: run this from the elevated shell, not the sandbox.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
PY="${PY:-/home/howard/anaconda3/bin/python3}"
PW="${PW:-/home/howard/anaconda3/envs/webapptest/bin/python}"
CAPS=(
  60-overview-populated-120x36 62-overview-populated-100x30 63-overview-populated-80x24
  61-overview-populated-models-120x36 64-overview-activity-120x36
  70-speed-across-populated-120x36 71-speed-across-populated-100x30 72-speed-across-populated-80x24
  74-speed-trend-populated-120x36 75-speed-trend-populated-100x30 76-speed-trend-populated-80x24
  77-speed-hour-populated-120x36 78-speed-hour-populated-100x30 79-speed-hour-populated-80x24
  73-speed-combined-populated-100x30 81-speed-detail-populated-100x30
  83-speed-trend-month-populated-120x36 84-speed-trend-month-populated-100x30
  85-speed-trend-month-populated-80x24
)
if [ "$#" -gt 0 ]; then CAPS=("$@"); fi
TOKDASH_TUI_NO_REMOTE=1 PYTHONPATH=src:tests "$PY" scripts/capture_tui_renders.py "${CAPS[@]}" || exit 1
svgs=()
for c in "${CAPS[@]}"; do svgs+=("output/tui-rev5-captures/$c.svg"); done
"$PW" output/vischeck/svg2png.py "${svgs[@]}"
