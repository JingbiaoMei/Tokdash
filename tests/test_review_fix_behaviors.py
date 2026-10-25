"""Behavior regressions for interrupted animations."""
import json
import subprocess
from pathlib import Path

import tokdash

from support.frontend import needs_node


STATIC = Path(tokdash.__file__).parent / "static"
pytestmark = needs_node


def run_js(tmp_path, source):
    script = tmp_path / "probe.mjs"
    script.write_text(source, encoding="utf-8")
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


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
