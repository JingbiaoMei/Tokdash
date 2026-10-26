"""Every element the dashboard looks up by a literal id is in the page's markup.

A renamed or deleted element turns `document.getElementById('x')` into null, and
the code that reads it either throws or quietly paints nothing. Several test files
used to keep their own hand-written list of ids that had to exist; this one check
covers every lookup the shipped script makes, so a new panel is covered the day it
is added and no list can fall behind.
"""
from __future__ import annotations

import re

from support.frontend import STATIC_DIR, index_source


LOOKUP = re.compile(r"""getElementById\(\s*(['"])([A-Za-z][\w-]*)\1\s*\)""")
ID_ATTRIBUTE = re.compile(r"""\bid\s*=\s*(['"])([^'"]+)\1""")

# Built by script at runtime rather than declared in the markup.
SCRIPT_BUILT_IDS = {"quotaServerBlocks", "serverInlineEditor"}


def _scripts() -> dict[str, str]:
    html = index_source()
    scripts = {"index.html": "\n".join(re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S))}
    for path in sorted(STATIC_DIR.rglob("*.js")):
        if path.name == "anime.esm.js":  # vendored library
            continue
        scripts[path.relative_to(STATIC_DIR).as_posix()] = path.read_text(encoding="utf-8")
    return scripts


def _looked_up_ids() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for name, text in _scripts().items():
        for match in LOOKUP.finditer(text):
            found.setdefault(match.group(2), set()).add(name)
    return found


def _markup_ids() -> set[str]:
    markup = re.sub(r"<script\b[^>]*>.*?</script>", "", index_source(), flags=re.S)
    return {match.group(2) for match in ID_ATTRIBUTE.finditer(markup)}


def test_every_id_the_script_looks_up_is_in_the_markup() -> None:
    looked_up = _looked_up_ids()
    assert len(looked_up) > 100, "the lookup scan found almost nothing; has the page moved?"
    markup = _markup_ids()
    missing = {
        element_id: sorted(where)
        for element_id, where in looked_up.items()
        if element_id not in markup and element_id not in SCRIPT_BUILT_IDS
    }
    assert not missing, f"looked up by id but not in the markup: {missing}"


def test_the_script_built_ids_are_still_built_and_still_looked_up() -> None:
    """The exemptions above must not outlive the code they describe."""
    looked_up = _looked_up_ids()
    script = _scripts()["index.html"]
    for element_id in SCRIPT_BUILT_IDS:
        assert element_id in looked_up, f"{element_id} is no longer looked up; drop its exemption"
        assert element_id not in _markup_ids(), f"{element_id} is in the markup now; drop its exemption"
        built = re.search(rf"""\bid\s*=\s*(['"]){element_id}\1|\.id\s*=\s*['"]{element_id}['"]""", script)
        assert built, f"{element_id} is neither in the markup nor built by the script"
