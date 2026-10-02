"""Tests for XSS sink regression invariant across static frontend assets.

Structural tripwire enforcing that every assignment to innerHTML/outerHTML,
invocation of insertAdjacentHTML, or document.write routes all template
interpolations (${...}) through escapeHtml(), approved formatters, or trusted literals.
Closes upstream issue #155.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

import pytest
import tokdash

STATIC_DIR = Path(tokdash.__file__).parent / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

# Vendors / third-party libraries exempt from internal invariant checks
EXEMPT_FILES = {"anime.esm.js"}


def is_safe_interpolation(expr: str) -> Tuple[bool, str]:
    """Check if a template literal interpolation ${expr} is demonstrably safe against XSS.

    Returns (is_safe, reason).
    """
    expr = expr.strip()

    # 1. HTML Escaping wrapper
    if expr.startswith("escapeHtml(") and expr.endswith(")"):
        return True, "escapeHtml wrapper"

    # 2. Path resolution helpers (strictly static asset/base prefixes)
    if re.fullmatch(r"window\.tokdash(?:AssetWithBase|Path)\(['\"][^'\"]+['\"]\)", expr):
        return True, "safe path prefix resolver with static asset path"

    # 3. Translation functions and static translation identifiers
    if re.fullmatch(r"t\([^)]+\)", expr):
        return True, "translation helper"
    if expr.startswith("t(") and ".replace(" in expr:
        # Extract each .replace(..., ...) argument taking into account nested parens
        pos = 0
        replacements_found = 0
        while True:
            rpos = expr.find(".replace(", pos)
            if rpos == -1:
                break
            p_start = rpos + len(".replace(")
            p_depth = 1
            i = p_start
            in_q = None
            comma_pos = -1
            while i < len(expr) and p_depth > 0:
                c = expr[i]
                if in_q:
                    if c == "\\":
                        i += 2
                        continue
                    if c == in_q:
                        in_q = None
                else:
                    if c in ("'", '"'):
                        in_q = c
                    elif c == "(":
                        p_depth += 1
                    elif c == ")":
                        p_depth -= 1
                        if p_depth == 0:
                            break
                    elif c == "," and p_depth == 1:
                        comma_pos = i
                i += 1
            if comma_pos != -1:
                replacements_found += 1
                arg = expr[comma_pos + 1 : i].strip()
                if not (
                    re.fullmatch(r"String\([a-zA-Z0-9_.]+\)", arg)
                    or re.fullmatch(r"\d+", arg)
                    or arg.startswith("escapeHtml(")
                ):
                    return False, f"untrusted replacement argument in translation: {arg}"
            pos = i

        if replacements_found > 0:
            return True, "translation with verified safe replacements"
        return False, "invalid replace structure in translation"

    if expr in ("loading", "minute"):
        return True, "known translation string identifier"

    # 4. Numeric, currency, rate, and date formatting helpers
    safe_formatters = (
        "formatTokenCount",
        "formatCurrency",
        "formatHitRate",
        "formatNumber",
        "formatPct",
        "formatRelativeTime",
        "formatDate",
        "serverTimeOfDay",
    )
    for fn in safe_formatters:
        if expr.startswith(f"{fn}(") and expr.endswith(")"):
            return True, f"safe formatter {fn}"

    # 5. Explicit numeric casts / methods
    if re.fullmatch(r"Number\([^)]+\)(\.toLocaleString\(\))?", expr):
        return True, "Number cast"
    if re.fullmatch(r"parseInt\([^)]+\)", expr) or re.fullmatch(r"parseFloat\([^)]+\)", expr):
        return True, "parseInt/parseFloat"
    if re.fullmatch(r"[\d\s+\-*/%()]+", expr) or re.fullmatch(r"idx\s*\+\s*\d+", expr):
        return True, "numeric arithmetic expression"

    # 6. Known safe visual constants / palettes / class variables
    if expr in (
        "c",
        "cardStyle",
        "roleBadge",
        "roleLabel",
        "cardBorder",
        "roleColor",
        "argsChip",
        "reasoningHtml",
    ):
        return True, "safe UI component/style variable"
    if expr == "metaChips || '<span class=\"text-slate-500 font-mono\">no content recorded</span>'":
        return True, "safe fallback chips HTML"
    if expr == "glyphs[icon] || glyphs.cross":
        return True, "SVG path dictionary lookup"
    if expr.startswith("monthNames[") and expr.endswith("]"):
        return True, "month name constant array lookup"
    if expr in ("iconClass", "label"):
        return True, "refresh button state class/label"

    # 7. Boolean / ternary class expressions (e.g. show.has(i) ? 'strong' : '')
    if re.fullmatch(r"show\.has\([^)]+\)\s*\?\s*['\"][^'\"]*['\"]\s*:\s*['\"][^'\"]*['\"]", expr):
        return True, "safe ternary class string"
    if re.fullmatch(r"show\.has\([^)]+\)\s*\?\s*[a-zA-Z0-9_]+\s*:\s*['\"][^'\"]*['\"]", expr):
        return True, "safe ternary label string"

    # 8. Nested template literals with escapeHtml (e.g. conditional badge / workspace icon)
    if "`" in expr:
        inner_matches = re.findall(r"\$\{([^}]+)\}", expr)
        if inner_matches and all(is_safe_interpolation(m)[0] for m in inner_matches):
            return True, "safe nested template with escaped content"

    return False, f"unapproved or dynamic expression: {expr}"


def extract_sink_interpolations(source: str) -> List[Tuple[int, str, str]]:
    """Scan JavaScript/HTML source for innerHTML, outerHTML, insertAdjacentHTML, and document.write sinks.

    Returns a list of (line_number, sink_type, interpolation_expression).
    """
    lines = source.splitlines()
    line_offsets = []
    curr = 0
    for l in lines:
        line_offsets.append(curr)
        curr += len(l) + 1

    def get_line_no(offset: int) -> int:
        for i, o in enumerate(line_offsets):
            if o > offset:
                return i
        return len(line_offsets)

    sinks: List[Tuple[int, int, str, str]] = []
    pos = 0
    sink_pattern = re.compile(
        r"(\.(?:innerHTML|outerHTML)\s*(\+?=)|\.insertAdjacentHTML\s*\(|document\.write\s*\()"
    )
    while True:
        m = sink_pattern.search(source, pos)
        if not m:
            break
        sink_pos = m.start()
        raw_match = m.group(1)
        if "insertAdjacentHTML" in raw_match:
            sink_type = "insertAdjacentHTML"
        elif "document.write" in raw_match:
            sink_type = "document.write"
        elif "outerHTML" in raw_match:
            sink_type = "outerHTML"
        else:
            sink_type = "innerHTML"

        sinks.append((sink_pos, get_line_no(sink_pos), sink_type, raw_match))
        pos = sink_pos + len(raw_match)

    interpolations: List[Tuple[int, str, str]] = []

    for sp, lno, st, mstr in sinks:
        expr_start = sp + len(mstr)
        i = expr_start
        if st in ("insertAdjacentHTML", "document.write"):
            paren_depth = 1  # already opened '(' in mstr
            in_quote = None
            while i < len(source):
                ch = source[i]
                if in_quote:
                    if ch == "\\":
                        i += 2
                        continue
                    if ch == in_quote:
                        in_quote = None
                else:
                    if ch in ("'", '"', "`"):
                        in_quote = ch
                    elif ch == "(":
                        paren_depth += 1
                    elif ch == ")":
                        paren_depth -= 1
                        if paren_depth == 0:
                            i += 1
                            break
                i += 1
            raw_expr = source[expr_start:i]
        else:
            in_quote = None
            paren_depth = 0
            bracket_depth = 0
            brace_depth = 0
            while i < len(source):
                ch = source[i]
                if in_quote:
                    if ch == "\\":
                        i += 2
                        continue
                    if ch == in_quote:
                        in_quote = None
                else:
                    if ch in ("'", '"', "`"):
                        in_quote = ch
                    elif ch == "(":
                        paren_depth += 1
                    elif ch == ")":
                        paren_depth = max(0, paren_depth - 1)
                    elif ch == "[":
                        bracket_depth += 1
                    elif ch == "]":
                        bracket_depth = max(0, bracket_depth - 1)
                    elif ch == "{":
                        brace_depth += 1
                    elif ch == "}":
                        if brace_depth > 0:
                            brace_depth -= 1
                        else:
                            break
                    elif ch == ";" and paren_depth == 0 and bracket_depth == 0 and brace_depth == 0:
                        break
                    elif ch == "\n" and paren_depth == 0 and bracket_depth == 0 and brace_depth == 0:
                        next_line = source[i + 1 :].lstrip()
                        if next_line and not (
                            next_line.startswith(".")
                            or next_line.startswith("+")
                            or next_line.startswith("?")
                            or next_line.startswith(":")
                        ):
                            break
                i += 1
            raw_expr = source[expr_start:i]

        # Extract all ${...} interpolations in raw_expr (handling nested braces)
        idx = 0
        while True:
            dollar = raw_expr.find("${", idx)
            if dollar == -1:
                break
            j = dollar + 2
            depth = 1
            in_q = None
            while j < len(raw_expr) and depth > 0:
                c = raw_expr[j]
                if in_q:
                    if c == "\\":
                        j += 2
                        continue
                    if c == in_q:
                        in_q = None
                else:
                    if c in ("'", '"', "`"):
                        in_q = c
                    elif c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                if depth > 0:
                    j += 1
            interp_content = raw_expr[dollar + 2 : j].strip()
            interpolations.append((lno, st, interp_content))
            idx = j + 1

    return interpolations


# ===========================================================================
# Invariant Test Suite
# ===========================================================================


def test_all_innerhtml_interpolations_are_safe():
    """Verify that every ${...} template interpolation in every innerHTML / insertAdjacentHTML sink is safe."""
    target_files = [INDEX_HTML]
    for js_path in STATIC_DIR.rglob("*.js"):
        if js_path.name in EXEMPT_FILES:
            continue
        target_files.append(js_path.read_text(encoding="utf-8"))

    all_found: List[Tuple[int, str, str]] = []
    for content in target_files:
        all_found.extend(extract_sink_interpolations(content))

    # Guard against accidental detector regression: ensure baseline sink count is found
    assert len(all_found) >= 95, (
        f"Expected at least 95 sink interpolations across static frontend assets, found {len(all_found)}"
    )

    failures = []
    for lno, sink_type, expr in all_found:
        safe, reason = is_safe_interpolation(expr)
        if not safe:
            failures.append(f"Line {lno} ({sink_type}): unapproved interpolation '${{{expr}}}' ({reason})")

    assert not failures, "XSS sink invariant violation(s) detected:\n" + "\n".join(failures)


def test_sink_detector_flags_unescaped_interpolations():
    """Verify that the structural tripwire correctly flags unescaped dynamic interpolations."""
    dangerous_snippets = [
        "card.innerHTML = `<div>${session.project}</div>`;",
        "container.innerHTML = `<span>${m.raw_text}</span>`;",
        "tr.insertAdjacentHTML('beforeend', `<td>${user_input}</td>`);",
        "tooltip.innerHTML = `<p>${String(session.id)}</p>`;",
        "header.outerHTML = `<h2>${item.label}</h2>`;",
        "document.write(`<script src='${external_url}'></script>`);",
        "card.innerHTML = ws ? `<span>${ws.raw_name}</span>` : '';",
        "card.innerHTML = ws ? `<span title=\"${escapeHtml(ws.title)}\">${ws.raw_name}</span>` : '';",
        "panel.innerHTML = `<link href=\"${window.tokdashPath(user_supplied_path)}\">`;",
        "banner.innerHTML = `<p>${t('serversReachable').replace('{ok}', unescapedPayload)}</p>`;",
    ]

    for snippet in dangerous_snippets:
        interps = extract_sink_interpolations(snippet)
        assert len(interps) >= 1, f"Expected at least 1 interpolation in {snippet!r}, found {len(interps)}"
        any_flagged = False
        for lno, sink_type, expr in interps:
            safe, reason = is_safe_interpolation(expr)
            if not safe:
                any_flagged = True
                break
        assert any_flagged, f"Expected {snippet!r} to be flagged as unsafe, but all interpolations were classified safe"


def test_historical_named_sinks_remain_explicitly_escaped():
    """Pin the specific named sinks from Round 4 to ensure continuous coverage."""
    # 1. Timeline timestamps
    assert re.search(r"renderSessionChat[\s\S]{0,4000}?escapeHtml\(String\(m\.timestamp", INDEX_HTML)
    assert re.search(r"renderDrawerMessages[\s\S]{0,4000}?escapeHtml\(String\(m\.timestamp", INDEX_HTML)

    # 2. Apps breakdown model name in title attribute and text node
    row = INDEX_HTML[INDEX_HTML.index("function updateAppsBreakdown") :]
    row = row[: row.index("function updateCombinedModelsTable")]
    assert row.count("escapeHtml(String(model.name ?? ''))") == 2

    # 3. Combined models table
    combined = INDEX_HTML[INDEX_HTML.index("function updateCombinedModelsTable") :]
    end_idx = combined.index("function ", 10) if "function " in combined[10:] else len(combined)
    combined = combined[:end_idx]
    assert "escapeHtml(String(model.name ?? ''))" in combined or "escapeHtml(String(m.name ?? ''))" in combined

    # 4. Day details modelId
    day = INDEX_HTML[INDEX_HTML.index("function showDayDetails") :]
    assert "escapeHtml(String(src.modelId ?? ''))" in day


def test_no_bare_risky_identifiers_in_sink_interpolations():
    """Verify that risky log/session fields never appear bare in sink interpolations."""
    all_interps = extract_sink_interpolations(INDEX_HTML)
    risky_props = ("timestamp", "model", "project", "workspace", "session", "prompt", "display_name")

    for lno, sink_type, expr in all_interps:
        expr_clean = expr.strip()
        # Skip pure translation calls such as t('model')
        if re.fullmatch(r"t\([^)]+\)", expr_clean):
            continue

        for prop in risky_props:
            if re.search(rf"\b{prop}\b", expr_clean, re.IGNORECASE):
                assert (
                    "escapehtml" in expr_clean.lower()
                    or "sessiondisplayname" in expr_clean.lower()
                    or "servertimeofday" in expr_clean.lower()
                    or expr_clean.startswith(
                        ("formatTokenCount(", "formatCurrency(", "formatHitRate(", "formatNumber(")
                    )
                ), f"Line {lno} ({sink_type}): bare risky property {prop!r} in interpolation '${{{expr}}}'"
