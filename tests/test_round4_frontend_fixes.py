"""Round-3 reviewer fixes: chart body-transform, tab race.

These defects were found on the branch after the first merge review: the
chart entry animation animated <body> (making it the containing block for
every position:fixed overlay), and the tab `.active` swap was deferred into an
anime onComplete that stop() never fires. The escaping and sessions filter
fixes from the same review are tested in test_frontend_html_escaping.py and
test_sessions_tab_filters_frontend.py.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


import tokdash

from support.frontend import needs_node


STATIC = Path(tokdash.__file__).parent / "static"
CHARTS_JS = (STATIC / "js" / "animations" / "charts.js").read_text(encoding="utf-8")
TRANSITIONS_JS = (STATIC / "js" / "animations" / "transitions.js").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# P1 — the chart observer must never touch <body> or fixed decorations
# ---------------------------------------------------------------------------

def test_charts_guard_skips_body_and_fixed_canvases():
    assert "isViewportDecoration" in CHARTS_JS
    assert "document.body" in CHARTS_JS
    assert "position === 'fixed'" in CHARTS_JS


def test_charts_guard_used_at_both_entry_points():
    # animateChartEntry and the observeCharts preset loop both call it
    assert CHARTS_JS.count("isViewportDecoration(") >= 3  # def + 2 call sites


def test_chart_entry_animation_clears_residual_transform():
    # anime v4 stop() does not run onComplete, but a finished animation must
    # still drop the inline transform: a residual scale(1) on any ancestor
    # re-creates the fixed-overlay containing-block bug.
    assert "target.style.transform = ''" in CHARTS_JS


# ---------------------------------------------------------------------------
# P2 — tab switching commits .active synchronously, never inside onComplete
# ---------------------------------------------------------------------------

def test_stop_releases_inline_styles_of_cancelled_animations():
    # stop() skips onComplete, so stopTabTransitions must clear the styles the
    # killed animations owned or panels strand at opacity 0.
    assert "export function stopTabTransitions" in TRANSITIONS_JS
    stop_fn = TRANSITIONS_JS[TRANSITIONS_JS.index("export function stopTabTransitions"):]
    assert "clearTransitionStyles()" in stop_fn[:600]


@needs_node
def test_rapid_tab_switching_keeps_exactly_one_active_panel(tmp_path):
    """Drive the real animateTabTransition with anime v4's stop() semantics."""
    body = TRANSITIONS_JS
    body = re.sub(r"^import .*?;$", "", body, flags=re.M)
    body = body.replace("export function", "function").replace("export default", "// default export removed")

    harness = """
// --- stand-ins ---------------------------------------------------------------
function respectsReducedMotion() { return false; }
// anime-stop.js is an import stripped from the harness; replicate it.
function stopAnimation(a) { if (a && typeof a.pause === 'function') a.pause(); return null; }
const liveAnims = [];
function animate(el, opts) {
  // anime 4.0.0 instance: pause() is the stop API, and it never runs onComplete
  const a = { stopped: false, pause() { this.stopped = true; } };
  liveAnims.push(a);
  return a;
}
function createSpring() { return 'spring'; }
function makePanel() {
  const set = new Set();
  return { classList: { add: (c) => set.add(c), remove: (c) => set.delete(c), contains: (c) => set.has(c) },
           style: { opacity: '', transform: '' } };
}
const panels = {};
function panelFor(t) { return panels[t] || (panels[t] = makePanel()); }

// --- extracted module --------------------------------------------------------
%s

// --- the race the reviewer hit: click A..G in a tight loop -------------------
const tabs = ['overview','sessions','stats','report','quota','servers','pricing'];
let current = 'overview';
panelFor(current).classList.add('active');
let bad = 0;
for (let round = 0; round < 4; round++) {
  for (const t of tabs) {
    const out = panelFor(current);
    const inn = panelFor(t);
    animateTabTransition(out === inn ? null : out, inn, null);
    current = t;
    const active = tabs.filter((x) => panelFor(x).classList.contains('active'));
    if (active.length !== 1) { bad++; }
  }
}
const finalActive = tabs.filter((x) => panelFor(x).classList.contains('active'));
console.log(JSON.stringify({ bad, finalActive }));
""" % body

    script = tmp_path / "tab_race.js"
    script.write_text(harness, encoding="utf-8")
    proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    import json
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["bad"] == 0, f"{result['bad']} transitions left != 1 active panel"
    assert len(result["finalActive"]) == 1, f"final state: {result['finalActive']}"
