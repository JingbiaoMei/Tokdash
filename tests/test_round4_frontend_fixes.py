"""Round-3 reviewer fixes: XSS escapes, chart body-transform, tab race, filter state, token reset.

These six defects were found on the branch after the first merge review:
unescaped log strings reached innerHTML, the chart entry animation animated
<body> (making it the containing block for every position:fixed overlay), the
tab `.active` swap was deferred into an anime onComplete that stop() never
fires, the sessions tool filter lived only as a pill class, and an empty
period silently returned from the token-composition renderer.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

import tokdash

STATIC = Path(tokdash.__file__).parent / "static"
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CHARTS_JS = (STATIC / "js" / "animations" / "charts.js").read_text(encoding="utf-8")
TRANSITIONS_JS = (STATIC / "js" / "animations" / "transitions.js").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# P1 — timeline timestamps / model names must be escaped at every sink
# ---------------------------------------------------------------------------

def _sink(pattern: str, *, count: int = 1) -> None:
    found = re.findall(pattern, INDEX_HTML)
    assert len(found) == count, f"expected {count} of {pattern!r}, found {len(found)}"


def test_session_chat_timestamp_is_escaped():
    _sink(r"renderSessionChat[\s\S]{0,4000}?escapeHtml\(String\(m\.timestamp", count=1)


def test_drawer_timeline_timestamp_is_escaped():
    _sink(r"renderDrawerMessages[\s\S]{0,4000}?escapeHtml\(String\(m\.timestamp", count=1)


def test_apps_breakdown_model_name_escaped_in_text_and_title_attr():
    # the model-name cell: title attribute AND text node both escaped
    row = INDEX_HTML[INDEX_HTML.index("function updateAppsBreakdown"):]
    row = row[: row.index("function updateCombinedModelsTable")]
    assert row.count('escapeHtml(String(model.name ?? \'\'))') == 2, (
        "apps breakdown must escape model.name in both the title attribute and the text node"
    )


def test_combined_models_table_name_escaped():
    row = INDEX_HTML[INDEX_HTML.index("function updateCombinedModelsTable"):]
    row = row[: row.index("function ", row.index("function updateCombinedModelsTable") + 10)
              if "function " in row[10:] else len(row)]
    assert "escapeHtml(String(model.name ?? ''))" in row or "escapeHtml(String(m.name ?? ''))" in row


def test_day_details_model_and_date_escaped():
    day = INDEX_HTML[INDEX_HTML.index("function showDayDetails"):]
    assert "escapeHtml(String(src.modelId ?? ''))" in day
    assert "escapeHtml(String(day.date ?? ''))" in day


def test_no_bare_timestamp_interpolations_remain():
    # every ${...timestamp...} template interpolation inside the renderers must
    # sit behind escapeHtml; bare ones are the regression.
    for name in ("renderSessionChat", "renderDrawerMessages"):
        start = INDEX_HTML.index(f"function {name}")
        body = INDEX_HTML[start:start + 6000]
        for m in re.finditer(r"\$\{([^}]*timestamp[^}]*)\}", body, re.IGNORECASE):
            assert "escapeHtml" in m.group(1), f"{name}: unescaped timestamp sink: {m.group(0)}"


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

def test_tab_active_class_swap_is_synchronous():
    fn = TRANSITIONS_JS[TRANSITIONS_JS.index("function animateTabTransition"):]
    fn = fn[: fn.index("export default")]
    on_complete = fn[fn.index("onComplete"):]
    assert ".classList.remove('active')" not in on_complete
    assert ".classList.add('active')" not in on_complete
    assert "outEl.classList.remove('active')" in fn
    assert "inEl.classList.add('active')" in fn


def test_stop_releases_inline_styles_of_cancelled_animations():
    # stop() skips onComplete, so stopTabTransitions must clear the styles the
    # killed animations owned or panels strand at opacity 0.
    assert "export function stopTabTransitions" in TRANSITIONS_JS
    stop_fn = TRANSITIONS_JS[TRANSITIONS_JS.index("export function stopTabTransitions"):]
    assert "clearTransitionStyles()" in stop_fn[:600]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_rapid_tab_switching_keeps_exactly_one_active_panel(tmp_path):
    """Drive the real animateTabTransition with anime v4's stop() semantics."""
    body = TRANSITIONS_JS
    body = re.sub(r"^import .*?;$", "", body, flags=re.M)
    body = body.replace("export function", "function").replace("export default", "// default export removed")

    harness = """
// --- stand-ins ---------------------------------------------------------------
function respectsReducedMotion() { return false; }
const liveAnims = [];
function animate(el, opts) {
  const a = { stopped: false, stop() { this.stopped = true; /* anime v4: no onComplete on stop */ } };
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


# ---------------------------------------------------------------------------
# P2 — sessions tool filter is state, not a pill class
# ---------------------------------------------------------------------------

def test_sessions_filter_state_variable_exists_and_drives_pills():
    assert "let sessionToolFilter = 'all';" in INDEX_HTML
    pills = INDEX_HTML[INDEX_HTML.index("function buildSessionQuickFilterPills"):]
    pills = pills[: pills.index("function ", 10)]
    assert "const previous = sessionToolFilter;" in pills
    # rebuild re-applies filtering instead of only re-highlighting the pill
    assert "filterSessionPanels(previous, active);" in pills


def test_filter_session_panels_updates_state():
    fn = INDEX_HTML[INDEX_HTML.index("function filterSessionPanels"):]
    fn = fn[: fn.index("function ", 10)]
    assert re.search(r"sessionToolFilter = toolName \|\| 'all';", fn)


def test_sessions_rerender_reapplies_search():
    fn = INDEX_HTML[INDEX_HTML.index("function renderSessionsTab"):]
    fn = fn[: fn.index("\n    function ", 10)]
    assert "filterSessionsBySearch" in fn, "renderSessionsTab must re-apply the search term after rebuild"


# ---------------------------------------------------------------------------
# P2 — empty periods reset the token composition bar
# ---------------------------------------------------------------------------

def test_token_composition_resets_on_empty_data():
    fn = INDEX_HTML[INDEX_HTML.index("function updateTokenCompositionBar"):]
    fn = fn[: fn.index("\n    function ", 10)]
    assert "resetTokenComposition()" in fn
    assert re.search(r"if \(!data\) \{\s*resetTokenComposition\(\); return; \}", fn)
    assert re.search(r"\}\s*else \{(?:\s*//[^\n]*)*\s*resetTokenComposition\(\);", fn), (
        "total == 0 must take an explicit reset branch, not silently return"
    )


def test_reset_zeroes_bars_and_values():
    fn = INDEX_HTML[INDEX_HTML.index("function resetTokenComposition"):]
    fn = fn[: fn.index("\n    function ", 10)]
    for bar in ("compPromptBar", "compCacheBar", "compOutputBar", "compReasoningBar"):
        assert bar in fn
    assert "'—'" in fn or '"—"' in fn
