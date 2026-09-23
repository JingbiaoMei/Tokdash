#!/usr/bin/env python3
"""Build the GitHub Release body for a version: the English changelog section, with a
Chinese link prepended when that version has been translated.

Replaces the ``awk`` recipe this guide used to carry. The awk version could not tell
whether a Chinese section exists, so it would link a heading that was never written; this
one omits the line instead, which is also the signal during a release that the Chinese
translation is still missing.

Run from the repo root::

    python3 scripts/release_body.py --version 2.6.2
    gh release create "v2.6.2" --title "v2.6.2" --latest -F /tmp/tokdash-release-notes.md
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

CHANGELOG_EN = Path("docs/development/CHANGELOG.md")
RELEASE_NOTES_ZH = Path("src/tokdash/static/release-notes.zh.json")
CHANGELOG_CN_URL = "https://github.com/JingbiaoMei/Tokdash/blob/main/docs/development/CHANGELOG_CN.md"
DEFAULT_OUTPUT = Path("/tmp/tokdash-release-notes.md")


def section_for(version: str, text: str) -> str:
    """One release section: the heading through the line before the next ``## ``.

    The end bound matters. Stopping the search at the next heading is not the same as
    slicing there, and an unbounded slice pastes every older release into the GitHub
    Release body.
    """
    lines = text.splitlines(keepends=True)
    pattern = re.compile(rf"^## {re.escape(version)} - \d{{4}}-\d{{2}}-\d{{2}}\s*$")
    start = next((i for i, line in enumerate(lines) if pattern.match(line)), None)
    if start is None:
        raise SystemExit(f"no section for {version} -- write the entry first")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "".join(lines[start:end]).rstrip() + "\n"


def section(version: str) -> str:
    return section_for(version, CHANGELOG_EN.read_text(encoding="utf-8"))


def anchor(version: str, date: str) -> str:
    """GitHub's heading anchor: lowercase, punctuation dropped, each space a hyphen."""
    slug = f"{version} - {date}".lower()
    slug = re.sub(r"[^a-z0-9 -]", "", slug)
    return slug.replace(" ", "-")


def translated_anchor(version: str) -> str | None:
    if not RELEASE_NOTES_ZH.exists():
        return None
    zh = json.loads(RELEASE_NOTES_ZH.read_text(encoding="utf-8"))
    release = zh.get("releases", {}).get(version)
    if not release:
        return None
    # The date comes from the English heading so both pages anchor identically.
    heading = re.search(
        rf"^## {re.escape(version)} - (\d{{4}}-\d{{2}}-\d{{2}})",
        CHANGELOG_EN.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if not heading:
        return None
    return anchor(version, heading.group(1))


def body(version: str) -> str:
    text = section(version)
    anchor_zh = translated_anchor(version)
    if anchor_zh:
        link = f"{CHANGELOG_CN_URL}#{anchor_zh}"
        text = f"**简体中文：** [查看本版本的中文更新日志]({link})\n\n{text}"
    else:
        print(
            f"note: {version} has no entry in {RELEASE_NOTES_ZH}; the release body stays "
            "English-only (add the Chinese entries and re-run to link it)",
            file=sys.stderr,
        )
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", required=True, help="release version, e.g. 2.6.2")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--stdout", action="store_true", help="print instead of writing a file")
    args = parser.parse_args()

    text = body(args.version)
    if args.stdout:
        # The body carries CJK once a version is translated, and the Windows console
        # defaults to cp1252 -- a Chinese link line would raise UnicodeEncodeError.
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(text)
        return 0
    args.output.write_text(text, encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:  # piping into head/less is a normal way to read a body
        sys.exit(0)
