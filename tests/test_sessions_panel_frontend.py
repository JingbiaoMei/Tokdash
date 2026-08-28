from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

import tokdash

INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"
PANELS = ("codex", "claude", "opencode", "pi_agent", "mimo", "kimi", "dsh", "reasonix", "zcode", "kilocode", "omp", "grok", "hermes", "antigravity_cli", "cline", "combined")


def _extract_js_function(src: str, signature: str) -> str:
    start = src.find(signature)
    assert start != -1, f"{signature} not found in index.html"
    depth = 0
    for j in range(src.find("{", start), len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start : j + 1]
    raise AssertionError(f"unterminated JS function: {signature}")


def _run(fn_src: str, args: list) -> object:
    import json as _json
    program = (
        f"{fn_src}\n"
        "const out = sessionPanelShouldOpen("
        + ", ".join(_json.dumps(a) for a in args)
        + ");\nconsole.log(JSON.stringify(out));\n"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        check=True,
    )
    return __import__("json").loads(result.stdout.strip())


@pytest.fixture(scope="module")
def fn_src() -> str:
    source = INDEX_HTML.read_text(encoding="utf-8")
    return _extract_js_function(source, "function sessionPanelShouldOpen(override, data) {")


def test_default_is_collapsed(fn_src):
    # Howard, 2026-08-24: every section starts collapsed, even with data.
    assert _run(fn_src, [None, {"sessions": [{"session_id": "1"}], "error": None}]) is False
    assert _run(fn_src, [None, {"sessions": [], "error": None}]) is False
    assert _run(fn_src, [None, None]) is False


def test_fetch_error_without_sessions_stays_open(fn_src):
    # Failures must stay visible: an errored panel with no sessions defaults open.
    assert _run(fn_src, [None, {"sessions": [], "error": "boom"}]) is True


def test_error_with_sessions_is_collapsed(fn_src):
    # Stale-but-present data is the normal degraded path; collapse it like data.
    assert _run(fn_src, [None, {"sessions": [{"session_id": "1"}], "error": "boom"}]) is False


def test_user_override_wins(fn_src):
    # True = user collapsed; false = user expanded. Either beats the default.
    assert _run(fn_src, [True, {"sessions": [{"session_id": "1"}]}]) is False
    assert _run(fn_src, [False, {"sessions": [{"session_id": "1"}]}]) is True
    assert _run(fn_src, [True, {"sessions": []}]) is False
    assert _run(fn_src, [False, {"sessions": []}]) is True


def test_every_panel_has_collapsible_markup():
    source = INDEX_HTML.read_text(encoding="utf-8")
    for panel in PANELS:
        assert f'data-panel-details="{panel}"' in source, panel
        assert f'id="{panel}PanelCount"' in source, panel
        assert f'id="{panel}PanelKpis"' in source, panel
        assert f'id="{panel}PanelTokens"' in source, panel
        assert f'id="{panel}PanelCost"' in source, panel
        assert f'id="{panel}PanelLast"' in source, panel
    # Panels start collapsed: no static `open` attribute on any of them.
    for m in __import__("re").finditer(r"<details class=\"session-panel\" data-panel-details=\"\w+\">", source):
        assert "open" not in source[m.end() : m.end() + 40]
    assert source.count('id="sessionsExpandAll"') == 1
    assert source.count('id="sessionsCollapseAll"') == 1


def test_i18n_keys_in_both_languages():
    source = INDEX_HTML.read_text(encoding="utf-8")
    for key in ("panelTokens", "panelLast", "sessionsExpandAll", "sessionsCollapseAll"):
        assert len(re.findall(rf"^\s+{key}: ", source, re.MULTILINE)) == 6, key


def test_sessions_panels_have_logos_and_hide_when_empty():
    """Panel headers reuse the Overview brand identity (icon-only; combined
    gets none) and zero-session harnesses are hidden, with a tab-level empty
    state as the fallback."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    panels = set(re.findall(r'data-panel-details="(\w+)"', source))
    assert len(panels) >= 15, panels
    # Icon-only identity injected once per panel header, combined excluded.
    assert 'createToolIdentity(panel, { iconOnly: true })' in source
    assert 'function createToolBrandIcon(tool, meta)' in source
    assert '!details.querySelector("summary .tool-identity")' in source
    assert 'panel !== "combined"' in source
    # Empty-range panels are hidden only when the range has no sessions and
    # the fetch did not fail. Assert on the guard pieces, not the formatting.
    hide_line = next(l for l in source.splitlines() if "panelEl.parentElement.style.display" in l)
    assert "!sessions.length" in hide_line, hide_line
    assert "!data?.error" in hide_line, hide_line
    assert '"none"' in hide_line, hide_line
    # Tab-level empty state and the expand/collapse row share the same gate.
    assert 'id="sessionsEmptyState"' in source
    assert 'data-i18n="sessionsEmptyRange"' in source
    assert 'id="sessionsPanelToolbar"' in source
    # `hidden` alone is beaten by Tailwind's display utilities on the flex row;
    # the inline display is what actually hides it, and both must gate on
    # showEmpty.
    toolbar_line = next(l for l in source.splitlines() if "toolbarEl.hidden" in l)
    assert "showEmpty" in toolbar_line, toolbar_line
    display_line = next(l for l in source.splitlines() if "toolbarEl.style.display" in l)
    assert "showEmpty" in display_line, display_line
