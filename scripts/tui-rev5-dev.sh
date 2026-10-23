#!/usr/bin/env bash
# Run the revision-5 TUI from this checkout against a THROWAWAY data directory.
#
#   scripts/tui-rev5-dev.sh              # run here
#   scripts/tui-rev5-dev.sh tmux         # start it as a detachable tmux session
#   scripts/tui-rev5-dev.sh tmux myname  # ... under a session name you choose
#
# Why this exists rather than typing it out each time:
#
# * The command is `python3 -m tokdash tui`, NOT `python main.py tui` -- main.py
#   is the legacy entry point and forces `serve`.
#
# * TOKDASH_DATA_DIR is mandatory. Without it the checkout opens the live
#   ~/.tokdash usage database and can migrate it to a schema the installed
#   (pipx) version refuses to read. It defaults to /tmp/tokdash-rev5-demo.
# * NO_COLOR is removed for this run. The dashboard's palette is the design; a
#   NO_COLOR exported anywhere above the pane greys it out, and the top row then
#   reads "colour: off". tokdash honours NO_COLOR by default (see
#   docs/reference/CONFIG.md), so a development run asks for colour explicitly.
# * Inside tmux the palette also depends on the server knowing the client speaks
#   24-bit. Add `set -as terminal-features ',xterm*:truecolour'` to ~/.tmux.conf
#   if the top row says "256 colours".
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

export TOKDASH_DATA_DIR="${TOKDASH_DATA_DIR:-/tmp/tokdash-rev5-demo}"
export PYTHONPATH="$ROOT/src"
export TOKDASH_TUI_COLOR="${TOKDASH_TUI_COLOR:-always}"
unset NO_COLOR
PY="${PY:-/home/howard/anaconda3/bin/python3}"

if [ "${1:-}" = "tmux" ]; then
  session="${2:-tokdash-rev5}"
  if tmux has-session -t "=$session" 2>/dev/null; then
    echo "tmux session '$session' is already running the TUI."
    echo "  attach:   tmux attach -t $session"
    echo "  stop it:  tmux kill-session -t $session"
    exit 0
  fi
  tmux new-session -d -s "$session" -c "$ROOT" \
    "env TOKDASH_DATA_DIR='$TOKDASH_DATA_DIR' PYTHONPATH='$PYTHONPATH' TOKDASH_TUI_COLOR=always '$PY' -m tokdash tui; printf '\nTUI exited (status %s) -- press enter\n' \"\$?\"; read _" || exit 1
  echo "started in tmux session '$session' (data: $TOKDASH_DATA_DIR)"
  echo "  attach:   tmux attach -t $session"
  echo "  stop it:  tmux kill-session -t $session"
  exit 0
fi

exec "$PY" -m tokdash tui
