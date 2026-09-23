#!/usr/bin/env python3
"""Regenerate ``docs/development/CHANGELOG_CN.md`` from the shipped Chinese release notes.

One Chinese source of truth: ``src/tokdash/static/release-notes.zh.json`` holds the
translated What's new entries, and this script writes the GitHub-rendered record that a
release body can link to. Nothing is retyped, so the in-app view and the docs page cannot
drift apart.

Two inputs, deliberately:

* ``release-notes.zh.json``  -- the Chinese prose, index-aligned with the English
  ``release-notes.json``. Versions absent here are untranslated and stay English in the
  app, so they are absent from this page too.
* ``docs/development/CHANGELOG.md`` -- release dates and the ``#NNN`` refs, which the
  in-app view carries by design nowhere (it renders plain text, so a PR number would
  show up literally and never link).

Run from the repo root::

    python3 scripts/changelog_cn.py            # write the page
    python3 scripts/changelog_cn.py --check    # exit 1 if it is stale (CI / release gate)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from collections import OrderedDict

RELEASE_NOTES_ZH = Path("src/tokdash/static/release-notes.zh.json")
RELEASE_NOTES_EN = Path("src/tokdash/static/release-notes.json")
CHANGELOG_EN = Path("docs/development/CHANGELOG.md")
CHANGELOG_CN = Path("docs/development/CHANGELOG_CN.md")

SECTION_LABELS = {"added": "新增", "changed": "调整", "fixed": "修复"}
HEADING_PATTERN = re.compile(r"^## (\d+\.\d+\.\d+) - (\d{4}-\d{2}-\d{2})\s*$")

# A changelog bullet ends with its refs: `(#40)`, `(#48, thanks @handle)`, or
# `(#42, closes #41, thanks @handle)`. Only the bare `#N` tokens in that trailing group
# name PRs: `closes #41` names an issue, and a `#42` inside prose is not a ref at all.
# Labeling an issue 相关 PR on the Chinese page is a small lie, so the parse is strict.
REF_GROUP = re.compile(r"\(([^()]*(?:#\d+)[^()]*)\)\s*$")
NON_PR_KEYWORD = re.compile(r"^(?:closes?|fix(?:e[sd])?|resolve[sd]?|reported|via|see)\b", re.IGNORECASE)
BARE_REF = re.compile(r"^#(\d+)$")
ENGLISH_LINK = "https://github.com/JingbiaoMei/Tokdash/blob/main/docs/development/CHANGELOG.md"

HEADER = """\
# 更新日志（简体中文）

本页面由 `scripts/changelog_cn.py` 生成，内容来自应用内“更新日志”所用的
`src/tokdash/static/release-notes.zh.json`，请勿手工编辑。

- 中文条目面向使用者；每一条改动背后的完整实现推理仍记录在英文
  [CHANGELOG.md]({english_link})，本页面在每节末尾附上对应的 PR。
- 未收录的版本表示尚未翻译，应用内同样回落英文。
""".format(english_link=ENGLISH_LINK)


def pr_refs(line: str) -> "list[str]":
    """PR numbers from one bullet's trailing ref group, empty when it has none."""
    group = REF_GROUP.search(line.strip())
    if not group:
        return []
    refs = []
    for part in group.group(1).split(","):
        part = part.strip()
        if NON_PR_KEYWORD.match(part):
            continue
        bare = BARE_REF.match(part)
        if bare and bare.group(1) not in refs:
            refs.append(bare.group(1))
    return refs


def english_metadata() -> "OrderedDict[str, dict]":
    """Release date and PR refs per version, read from the English changelog."""
    meta: "OrderedDict[str, dict]" = OrderedDict()
    current = None
    for line in CHANGELOG_EN.read_text(encoding="utf-8").splitlines():
        heading = HEADING_PATTERN.match(line)
        if heading:
            current = heading.group(1)
            meta[current] = {"date": heading.group(2), "refs": []}
            continue
        if current:
            for ref in pr_refs(line):
                if ref not in meta[current]["refs"]:
                    meta[current]["refs"].append(ref)
    return meta


def build() -> str:
    zh = json.loads(RELEASE_NOTES_ZH.read_text(encoding="utf-8"))
    en = json.loads(RELEASE_NOTES_EN.read_text(encoding="utf-8"))
    meta = english_metadata()

    # Follow the English release order (newest first), not the JSON key order, so a
    # backfill cannot land in the middle of the page.
    order = [release["version"] for release in en["releases"]]
    translations = zh["releases"]
    missing = [version for version in translations if version not in order]
    if missing:
        raise SystemExit(f"{RELEASE_NOTES_ZH}: versions not in the English notes: {missing}")

    parts = [HEADER]
    for version in order:
        if version not in translations:
            continue
        release = next(r for r in en["releases"] if r["version"] == version)
        date = meta.get(version, {}).get("date") or release["date"]
        refs = meta.get(version, {}).get("refs", [])
        parts.append(f"\n## {version} - {date}\n")
        for section in release["sections"]:
            items = translations[version].get(section["type"], [])
            label = SECTION_LABELS.get(section["type"], section["type"])
            parts.append(f"\n### {label}\n")
            for item in items:
                parts.append(f"\n- {item}")
            parts.append("\n")
        if refs:
            joined = "、".join(f"#{ref}" for ref in refs)
            parts.append(f"\n相关 PR：{joined}\n")
    return "".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the page is out of date")
    args = parser.parse_args()

    generated = build()
    if args.check:
        existing = CHANGELOG_CN.read_text(encoding="utf-8") if CHANGELOG_CN.exists() else ""
        if existing != generated:
            print(f"{CHANGELOG_CN} is stale; run: python3 scripts/changelog_cn.py", file=sys.stderr)
            return 1
        return 0

    CHANGELOG_CN.write_text(generated, encoding="utf-8")
    versions = json.loads(RELEASE_NOTES_ZH.read_text(encoding="utf-8"))["releases"]
    print(f"wrote {CHANGELOG_CN} ({len(versions)} releases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
