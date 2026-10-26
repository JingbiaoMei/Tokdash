"""Run pieces of the real dashboard JavaScript from a test.

The frontend tests execute the shipped code rather than grep it: they slice the
functions they need out of ``index.html`` (or a module under ``static/js``), wrap
them in a small Node harness over a stub DOM, and read back what the code did.
These are the pieces every such file needs, kept in one place so they cannot
drift apart again (there were 23 copies of the slicer, in 12 versions).
"""
from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest

import tokdash

STATIC_DIR = Path(tokdash.__file__).parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


@lru_cache(maxsize=None)
def index_source() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def extract_js_function(source: str, signature: str) -> str:
    """Slice one complete declaration, from ``signature`` to its closing brace.

    ``signature`` is the start of the declaration as it appears in the source,
    for example ``"function renderOverviewTab("`` or ``"const I18N = {"``.

    The parameter list is skipped before the body is brace-matched: a default
    such as ``options = {}`` puts braces inside parentheses, and counting those
    as the body would cut the function off at the default value.
    """
    start = source.find(signature)
    assert start != -1, f"{signature} not found"

    parens = 0
    body_start = -1
    for index in range(start, len(source)):
        char = source[index]
        if char == "(":
            parens += 1
        elif char == ")":
            parens -= 1
        elif char == "{" and parens == 0:
            body_start = index
            break
    assert body_start != -1, f"no body for {signature}"

    depth = 0
    for index in range(body_start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated declaration: {signature}")


def run_node(script: Path | str, *args: str, timeout: float = 60) -> str:
    """Run a Node script file and return its stdout.

    A non-zero exit fails the test with Node's stderr, which carries the stack
    of whatever the harness threw.
    """
    result = subprocess.run(
        ["node", str(script), *args],
        capture_output=True,
        encoding="utf-8",
        timeout=timeout,
    )
    assert result.returncode == 0, f"node exited {result.returncode}:\n{result.stderr[-4000:]}"
    return result.stdout


def extract_i18n_literal(source: str) -> str:
    """The ``const I18N = {...}`` object literal, every language included."""
    start = source.find("const I18N = {")
    assert start != -1, "const I18N = { not found in index.html"
    end = source.find("\n    };", start)
    assert end != -1, "unterminated I18N object in index.html"
    return source[start : end + 6]
