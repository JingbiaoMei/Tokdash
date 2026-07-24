from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import tokdash
from tokdash.model_normalization import normalize_model_name

INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"

# Cases that have historically drifted between the hand-maintained JS twin in
# index.html and the Python normalizer (especially date-suffix spellings and
# the deliberate non-stripping of 4-digit YYMM / version stamps).
SYNC_CASES = [
    "volcengine-coding-plan/glm-5-2-260617",  # YYMMDD strips -> glm-5.2
    "glm-5-2-20260617",                        # YYYYMMDD strips
    "glm-5-2-2026-06-17",                      # YYYY-MM-DD strips
    "deepseek-v4-flash-2604",                  # 4-digit YYMM must NOT strip
    "mistral-large-2512",                      # version stamp must NOT strip
    "gpt-4o-mini-2024-07-18",
    "model-123456",                            # 6-digit non-date preserved
    "model-2699",                              # 4-digit non-date preserved
    "models:claude-3.7-sonnet-latest",
    "google/gemini-3-pro-preview",
    "gemini-3-flash-a",          # alias -> gemini-3-flash
    "kimi/kimi-k2p6",            # Kimi collapse -> kimi-k2.6
    "k2p6",                      # alias + collapse -> kimi-k2.6
    "kimi-coding/k2p5",          # -> kimi-k2.5
    "infi/kimi-2.5",             # -> kimi-k2.5
    "vol-engine/kimi-2.5",       # -> kimi-k2.5
    "kimi-k2-5",                 # -> kimi-k2.5
    "kimi2.5",                   # -> kimi-k2.5
    "google/gemini-3-pro-medium",   # effort suffix strips -> gemini-3-pro
    "gemini-3-pro-high",            # -> gemini-3-pro (via strip)
    "o3-mini-low",                  # -> o3-mini
]


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


def _extract_js_normalize_model_name(src: str) -> str:
    return _extract_js_function(src, "function normalizeModelName(name) {")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_frontend_normalize_model_name_matches_backend(tmp_path):
    """The JS twin in index.html must agree with the backend normalizer.

    Both copies are hand-maintained and have drifted before: the JS lagged on
    YYMMDD date-suffix stripping, so a snapshot model (glm-5-2-260617) showed
    as a split row with a different label in the client-side grouping than in
    the backend's combined table. This guard extracts the real function from
    the shipped HTML and compares it against the Python source of truth, so
    future drift fails CI instead of silently mismatching frontend/backend
    labels. Skipped when node is absent.
    """
    src = INDEX_HTML.read_text(encoding="utf-8")
    js_fn = _extract_js_normalize_model_name(src)

    harness = tmp_path / "norm.js"
    harness.write_text(
        js_fn
        + "\nconst cases = JSON.parse(process.argv[2]);\n"
        + "const out = {};\n"
        + "for (const c of cases) out[c] = normalizeModelName(c);\n"
        + "process.stdout.write(JSON.stringify(out));\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(harness), json.dumps(SYNC_CASES)],
        capture_output=True,
        text=True,
        check=True,
    )
    js_out = json.loads(result.stdout)

    mismatches = [
        f"{c!r}: python={normalize_model_name(c)!r} js={js_out.get(c)!r}"
        for c in SYNC_CASES
        if normalize_model_name(c) != js_out.get(c)
    ]
    assert not mismatches, "frontend/backend normalizer drift:\n  " + "\n  ".join(mismatches)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_quota_plan_label_does_not_call_detected_providers_undetected(tmp_path):
    src = INDEX_HTML.read_text(encoding="utf-8")
    js_fn = _extract_js_function(
        src, "function quotaProviderPlanLabel(providerKey, provider) {"
    )
    harness = tmp_path / "quota-plan-label.js"
    harness.write_text(
        "function t(key) { return key === 'notDetected' ? 'not detected' : key; }\n"
        + js_fn
        + "\nconst cases = JSON.parse(process.argv[2]);\n"
        + "process.stdout.write(JSON.stringify(cases.map(([key, provider]) => "
        + "quotaProviderPlanLabel(key, provider))));\n",
        encoding="utf-8",
    )
    cases = [
        ["minimax", {"detected": True, "plan": None}],
        ["grok", {"detected": True, "plan": None}],
        ["kimi", {"detected": True, "plan": "Intermediate"}],
        ["minimax", {"detected": False, "plan": None}],
        ["antigravity", {"detected": False, "plan": None}],
    ]
    result = subprocess.run(
        ["node", str(harness), json.dumps(cases)],
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(result.stdout) == ["", "", "Intermediate", "not detected", ""]
