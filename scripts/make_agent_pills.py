"""Generate white "pill" badges (logo + name) for the README supported-tools strip.

Reads source agent logos from ``docs/assets/agents/`` and writes rounded white
pills to ``docs/assets/agents/pills/``. The white fill + subtle border keeps every
pill (including solid-black logos like Codex/Copilot) visible on both GitHub light
and dark themes.

Run from the repo root. Dependencies are NOT tokdash runtime deps — install them
into a throwaway venv:

    python3 -m venv /tmp/svgvenv
    /tmp/svgvenv/bin/pip install pillow cairosvg
    /tmp/svgvenv/bin/python scripts/make_agent_pills.py

Pass tool keys to rebuild only some pills (``cairosvg`` is imported lazily, so
PNG-sourced pills build with pillow alone):

    python3 scripts/make_agent_pills.py grok mimo
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

AGENTS_DIR = Path("docs/assets/agents")
OUT_DIR = AGENTS_DIR / "pills"
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# (output key, display label, source logo filename) — order matches the README list.
# A ``None`` label makes a logo-only pill: use it for sources that are already a
# wordmark, so the name is not printed twice.
TOOLS = [
    ("opencode", "OpenCode", "opencode.png"),
    ("codex", "Codex", "codex.png"),
    ("claude", "Claude Code", "claude.svg"),
    ("gemini", "Gemini CLI", "gemini.svg"),
    ("antigravity", "Antigravity", "antigravity.png"),
    ("openclaw", "OpenClaw", "openclaw.png"),
    ("kimi", "Kimi CLI", "kimi.png"),
    ("mimo", None, "mimo.svg"),
    ("grok", "Grok Build", "grok.png"),
    ("pi", "Pi", "pi.png"),
    ("omp", "omp", "omp.png"),
    ("kilocode", "Kilo Code", "kilocode.png"),
    ("cline", "Cline", "cline.png"),
    ("copilot", "GitHub Copilot CLI", "copilot.svg"),
    ("hermes", "Hermes", "hermes.png"),
    ("dsh", "DeepSeek Harness", "dsh.svg"),
    ("reasonix", "Reasonix", "reasonix.svg"),
    ("zcode", "ZCode", "zcode.png"),
    ("workbuddy", "WorkBuddy", "workbuddy.png"),
    ("qoder-ide", "Qoder IDE", "qoder.png"),
    ("qoder-cli", "Qoder CLI", "qoder.png"),
]

# Rendered at ~2.4x the README display height (40px) for crispness.
H = 96
LOGO_H = 52
WORDMARK_H = 34  # logo-only pills: wordmark cap height reads like the pill label
PAD_X = 26
GAP = 16
RADIUS = 22
BORDER = 2
FILL = (255, 255, 255, 255)
BORDER_COLOR = (208, 215, 222, 255)  # #d0d7de — defines the pill on white (light theme)
TEXT_COLOR = (31, 35, 40, 255)       # #1f2328 — GitHub default text
FONT_SIZE = 38


def load_logo(path: Path, target_h: int) -> Image.Image:
    """Load a PNG/SVG logo as RGBA, scaled to ``target_h`` px tall."""
    if path.suffix.lower() == ".svg":
        import cairosvg

        png_bytes = cairosvg.svg2png(url=str(path), output_height=target_h * 3)
        img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    else:
        img = Image.open(path).convert("RGBA")
    width = round(img.width * target_h / img.height)
    return img.resize((width, target_h), Image.LANCZOS)


def make_pill(label: str | None, logo: Image.Image) -> Image.Image:
    if label is None:
        width = PAD_X + logo.width + PAD_X
    else:
        font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
        measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        bbox = measure.textbbox((0, 0), label, font=font)
        text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        width = PAD_X + logo.width + GAP + text_w + PAD_X

    img = Image.new("RGBA", (width, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [0, 0, width - 1, H - 1], radius=RADIUS, fill=FILL, outline=BORDER_COLOR, width=BORDER
    )
    img.alpha_composite(logo, (PAD_X, (H - logo.height) // 2))
    if label is not None:
        draw.text(
            (PAD_X + logo.width + GAP, (H - text_h) // 2 - bbox[1]),
            label,
            font=font,
            fill=TEXT_COLOR,
        )
    return img


def main() -> None:
    only = set(sys.argv[1:])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for key, label, fname in TOOLS:
        if only and key not in only:
            continue
        # Wordmarks carry their own name, so give them the full pill height.
        logo = load_logo(AGENTS_DIR / fname, WORDMARK_H if label is None else LOGO_H)
        pill = make_pill(label, logo)
        out = OUT_DIR / f"{key}.png"
        pill.save(out)
        print(f"wrote {out}  ({pill.width}x{pill.height})")


if __name__ == "__main__":
    main()
