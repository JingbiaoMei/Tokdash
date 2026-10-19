#!/usr/bin/env python3
"""Rasterise the Textual SVG captures so the palette can be eyeballed.

    python3 scripts/captures_to_png.py [case-name ...]

Reads output/tui-rev5-captures/*.svg (written by capture_tui_renders.py) and
writes a same-named .png beside it. Chrome renders Textual's SVG correctly;
ImageMagick's SVG engine drops most of the text, and Playwright's screenshot
call times out on this machine (see the wsl-playwright skill). The
--disable-gpu pair is that skill's WSL2 guidance.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAPTURES = ROOT / "output" / "tui-rev5-captures"
BROWSER_ARGS = ["--headless=new", "--disable-gpu",
                "--disable-features=Vulkan,VizDisplayCompositor",
                "--hide-scrollbars", "--force-device-scale-factor=1"]


def svg_size(svg: Path) -> tuple[int, int]:
    match = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg.read_text("utf-8"))
    if not match:
        raise SystemExit(f"{svg}: no viewBox to size the window from")
    return round(float(match.group(1))), round(float(match.group(2)))


def main() -> int:
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if chrome is None:
        raise SystemExit("google-chrome/chromium not found on PATH")
    wanted = set(sys.argv[1:])
    svgs = sorted(p for p in CAPTURES.glob("*.svg") if not wanted or p.stem in wanted)
    if not svgs:
        raise SystemExit(f"nothing to convert in {CAPTURES}")
    for svg in svgs:
        width, height = svg_size(svg)
        out = svg.with_suffix(".png")
        subprocess.run(
            [chrome, *BROWSER_ARGS, f"--window-size={width},{height}",
             f"--screenshot={out}", svg.as_uri()],
            check=True, capture_output=True,
        )
        print(f"{svg.stem:38} {width}x{height} -> {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
