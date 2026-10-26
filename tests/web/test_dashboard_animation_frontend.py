"""Dashboard animations that are interrupted must let go of what they animated.

Four ways this has regressed:

- a stopped animation kept writing to its target, because the bundled anime
  4.0.0 has no stop() and createAnimatable() instances have no pause() either,
  so the cache bar's stale width came back over an empty period's reset;
- the tab ``.active`` swap lived in an onComplete that stopping an animation
  never runs, so a fast second click left two panels active, or none;
- stopping a tab transition left the incoming panel at its starting opacity,
  because only onComplete cleared the inline styles;
- the chart entry animation scaled <body> (the wrapper of the #linkyBg
  backdrop canvas), which made it the containing block for every
  position:fixed overlay, and a finished entry left its transform behind.

The stopping tests run the shipped modules against the shipped anime.js engine,
imported from disk, so cancellation is the engine's own. The tab and chart tests
swap in a recording engine whose animations never finish by themselves, which
is exactly the case under test: what the module does before, or without,
onComplete.
"""
import json

from support.frontend import STATIC_DIR as STATIC, needs_node, run_node


pytestmark = needs_node

ANIMATIONS = STATIC / "js" / "animations"

# anime's animate() and createSpring(), as far as the tab and chart modules use
# them. An instance never finishes by itself: pause() is its only stop verb, as
# in anime 4.0.0, and its onComplete runs only if the test calls it.
RECORDING_ENGINE = """
export const started = [];
export function animate(target, params) {
  const instance = { target, params, paused: false, pause() { instance.paused = true; } };
  started.push(instance);
  return instance;
}
export function createSpring() { return 'spring'; }
"""


def run_js(tmp_path, source):
    script = tmp_path / "probe.mjs"
    script.write_text(source, encoding="utf-8")
    return json.loads(run_node(script))


def stage_with_recording_engine(tmp_path, *modules):
    """Copy animation modules into tmp_path as ES modules importing RECORDING_ENGINE as anime."""
    (tmp_path / "anime.mjs").write_text(RECORDING_ENGINE, encoding="utf-8")
    for name in ("anime-stop", "reduced-motion", *modules):
        source = (ANIMATIONS / f"{name}.js").read_text(encoding="utf-8")
        source = source.replace("../anime.esm.js", "./anime.mjs")
        for dependency in ("anime-stop", "reduced-motion"):
            source = source.replace(f"./{dependency}.js", f"./{dependency}.mjs")
        (tmp_path / f"{name}.mjs").write_text(source, encoding="utf-8")


def test_stopped_cache_animation_cannot_restore_previous_width(tmp_path):
    # Use the shipped animation engine, not a fake implementing an invented API.
    (tmp_path / "anime.mjs").write_text((STATIC / "js/anime.esm.js").read_text(encoding="utf-8"), encoding="utf-8")
    for name in ("anime-stop", "reduced-motion", "micro"):
        source = (STATIC / f"js/animations/{name}.js").read_text(encoding="utf-8")
        source = source.replace("../anime.esm.js", "./anime.mjs")
        for dependency in ("anime-stop", "reduced-motion"):
            source = source.replace(f"./{dependency}.js", f"./{dependency}.mjs")
        (tmp_path / f"{name}.mjs").write_text(source, encoding="utf-8")
    result = run_js(tmp_path, """
import {animateCacheHitBar, stopCacheHitBar} from './micro.mjs';
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
// A plain object exercises the same engine scheduling and cancellation in Node.
const bar = {width: '0%', backgroundColor: '#ef4444', style: {}};
animateCacheHitBar(bar, 30);
await wait(100);
stopCacheHitBar(bar);
bar.width = '0%';
await wait(1100);
console.log(JSON.stringify({width: bar.width}));
""")
    assert result == {"width": "0%"}


def test_stop_animation_reaches_both_anime_instance_shapes(tmp_path):
    """pause() first, because the loader and the SSE pulse own their element's
    styles and must not have them reverted out from under them; revert() last,
    because createAnimatable() instances have neither pause() nor stop(), so the
    guard chain used to end in a silent no-op on exactly those."""
    (tmp_path / "anime.mjs").write_text((STATIC / "js/anime.esm.js").read_text(encoding="utf-8"), encoding="utf-8")
    stopper = (STATIC / "js/animations/anime-stop.js").read_text(encoding="utf-8")
    (tmp_path / "anime-stop.mjs").write_text(
        stopper.replace("../anime.esm.js", "./anime.mjs"), encoding="utf-8"
    )
    result = run_js(tmp_path, """
import {animate, createAnimatable, engine} from './anime.mjs';
import {stopAnimation} from './anime-stop.mjs';
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const out = {};

// animate(): the shape the loader rate and the SSE pulse hold. Unchanged: it is
// paused, not reverted, and a detached instance never writes again.
const rate = {value: 0};
const rateAnim = animate(rate, {value: 1, duration: 4000});
stopAnimation(rateAnim);
out.ratePaused = rateAnim.paused === true;
await wait(150);
out.rateDetached = !engine._head;

// createAnimatable(): no pause(), no stop() — only revert().
const driven = {val: 0};
const animatable = createAnimatable(driven, {val: {duration: 4000}});
animatable.val(1);
await wait(80);
out.animatableRegistered = !!engine._head;
stopAnimation(animatable);
await wait(300);
out.animatableDetached = !engine._head;
console.log(JSON.stringify(out));
""")
    assert result == {
        "ratePaused": True,
        "rateDetached": True,
        "animatableRegistered": True,
        "animatableDetached": True,
    }


TAB_SWITCHING = """
import { started } from './anime.mjs';
import { animateTabTransition, stopTabTransitions } from './transitions.mjs';

globalThis.window = { matchMedia: () => ({ matches: false }) };

function element() {
  const classes = new Set();
  return {
    classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c) },
    style: { opacity: '', transform: '' },
  };
}
const tabs = ['overview', 'sessions', 'stats', 'report', 'quota', 'servers', 'pricing'];
const panels = Object.fromEntries(tabs.map((tab) => [tab, element()]));
const buttons = Object.fromEntries(tabs.map((tab) => [tab, element()]));
const activeTabs = () => tabs.filter((tab) => panels[tab].classList.contains('active'));
const holdingStyles = (tab) => panels[tab].style.opacity !== '' || panels[tab].style.transform !== '';

// The race the reviewer hit: click every tab in a tight loop, so each switch
// lands while the previous one is still animating and its onComplete never runs.
let current = 'overview';
panels[current].classList.add('active');
const out = { notOneActive: 0, stranded: [] };
for (let round = 0; round < 4; round++) {
  for (const tab of tabs) {
    animateTabTransition(tab === current ? null : panels[current], panels[tab], buttons[tab]);
    current = tab;
    if (activeTabs().length !== 1) out.notOneActive++;
    out.stranded.push(...tabs.filter((other) => other !== current && holdingStyles(other)));
  }
}
out.finalActive = activeTabs();
out.enteringMidFlight = { ...panels[current].style };
stopTabTransitions();
out.enteringAfterStop = { ...panels[current].style };
out.allStopped = started.length > 0 && started.every((instance) => instance.paused);
console.log(JSON.stringify(out));
"""


def _switch_tabs(tmp_path):
    stage_with_recording_engine(tmp_path, "transitions")
    return run_js(tmp_path, TAB_SWITCHING)


def test_rapid_tab_switching_keeps_exactly_one_active_panel(tmp_path):
    """The .active swap must not wait for an onComplete that a stop never runs."""
    out = _switch_tabs(tmp_path)

    assert out["notOneActive"] == 0, f"{out['notOneActive']} switches left other than one active panel"
    assert out["finalActive"] == ["pricing"]


def test_stopping_tab_transitions_releases_the_styles_they_owned(tmp_path):
    """A stopped entry animation never runs its onComplete, so the stop itself clears its styles.

    Otherwise every panel switched away from mid-animation keeps opacity 0, and
    shows up blank the next time it is opened.
    """
    out = _switch_tabs(tmp_path)

    assert out["enteringMidFlight"] != {"opacity": "", "transform": ""}, (
        "the incoming panel must really be mid-animation, or the checks below prove nothing"
    )
    assert out["stranded"] == [], "panels switched away from mid-animation kept their inline styles"
    assert out["enteringAfterStop"] == {"opacity": "", "transform": ""}
    assert out["allStopped"], "stopTabTransitions must stop every animation it started"


CHART_ENTRY = """
import { started } from './anime.mjs';
import { animateChartEntry, observeCharts } from './charts.mjs';

const element = (parentElement = null) => ({ parentElement, dataset: {}, style: {} });
const body = element();
const pinnedCanvases = new Set();
globalThis.window = globalThis;
globalThis.document = { body, documentElement: element() };
window.matchMedia = () => ({ matches: false });
window.getComputedStyle = (el) => ({ position: pinnedCanvases.has(el) ? 'fixed' : 'static' });
const observed = [];
globalThis.IntersectionObserver = class {
  observe(target) { observed.push(target); }
  unobserve() {}
  disconnect() {}
};

// #linkyBg: a decoration canvas that is a direct child of <body>.
const backdrop = element(body);
// A canvas the stylesheet pins with position: fixed, inside an ordinary div.
const pinnedWrap = element();
const pinned = element(pinnedWrap);
pinnedCanvases.add(pinned);
// An ordinary chart card.
const cardWrap = element();
const card = element(cardWrap);
const names = new Map([
  [body, 'body'], [backdrop, 'backdrop'], [pinnedWrap, 'pinnedWrap'], [pinned, 'pinned'],
  [cardWrap, 'cardWrap'], [card, 'card'],
]);
const name = (el) => names.get(el) || 'unnamed';

const out = {};
// Both entry points: the observer set up over every canvas on the page, then the
// entry animation each canvas would get once it scrolls into view.
observeCharts({ querySelectorAll: () => [backdrop, pinned, card] });
out.observed = observed.map(name);
out.cardWrapBeforeEntry = { ...cardWrap.style };
[backdrop, pinned, card].forEach(animateChartEntry);
out.animated = started.map((instance) => name(instance.target));
out.body = { style: { ...body.style }, dataset: { ...body.dataset } };
out.pinnedWrap = { style: { ...pinnedWrap.style }, dataset: { ...pinnedWrap.dataset } };

const entry = started.find((instance) => instance.target === cardWrap);
if (entry && entry.params.onComplete) entry.params.onComplete();
out.cardWrapAfterEntry = { ...cardWrap.style };
console.log(JSON.stringify(out));
"""


def _enter_charts(tmp_path):
    stage_with_recording_engine(tmp_path, "charts")
    return run_js(tmp_path, CHART_ENTRY)


def test_chart_entry_never_animates_the_page_body_or_a_fixed_canvas(tmp_path):
    """Neither observeCharts nor animateChartEntry may style <body> or a pinned canvas's wrapper.

    Any transform on <body>, even an identity scale(1), makes it the containing
    block for every position:fixed overlay: the settings modal's backdrop then
    opened about 2600px below the viewport.
    """
    out = _enter_charts(tmp_path)

    assert out["observed"] == ["card"]
    assert out["animated"] == ["cardWrap"]
    assert out["body"] == {"style": {}, "dataset": {}}
    assert out["pinnedWrap"] == {"style": {}, "dataset": {}}


def test_a_finished_chart_entry_drops_its_inline_transform(tmp_path):
    """A residual scale(1) on any ancestor is the same containing-block trap as one on <body>."""
    out = _enter_charts(tmp_path)

    assert out["cardWrapBeforeEntry"].get("transform"), "the card must start the entry scaled down"
    assert out["cardWrapAfterEntry"] == {"opacity": "", "transform": ""}
