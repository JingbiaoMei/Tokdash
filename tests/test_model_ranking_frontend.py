"""Dashboard-side half of the model-ranking contract.

The server now ranks every model array by tokens. Two places in the page can
undo that: the multi-server merger re-sorts the arrays it combines, and the
"Top Models by Cost" chart reads one of them. Both fixtures below use models
whose token order and cost order disagree, since agreement is what let the
original defect through.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import tokdash


INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"


def _extract_js_function(src: str, signature: str) -> str:
    start = src.find(signature)
    assert start >= 0, f"{signature} not found"
    depth = 0
    for index in range(src.find("{", start), len(src)):
        if src[index] == "{":
            depth += 1
        elif src[index] == "}":
            depth -= 1
            if depth == 0:
                return src[start : index + 1]
    raise AssertionError(f"unterminated function: {signature}")


def _run_js(tmp_path: Path, name: str, script: str, value=None):
    harness = tmp_path / f"{name}.js"
    harness.write_text(script, encoding="utf-8")
    argv = ["node", str(harness)]
    if value is not None:
        argv.append(json.dumps(value))
    result = subprocess.run(argv, check=True, capture_output=True, encoding="utf-8")
    return json.loads(result.stdout)


def _payload(models: list[tuple[str, int, float]]) -> dict:
    rows = [{"name": n, "tokens": t, "cost": c} for n, t, c in models]
    return {
        "total_tokens": sum(t for _, t, _ in models),
        "total_cost": sum(c for _, _, c in models),
        "total_messages": len(models),
        "top_models": rows,
        "combined_models": rows,
        "apps": {"editor": {"tokens": 1, "cost": 1, "models": rows}},
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_multi_server_merge_keeps_token_ranking(tmp_path):
    source = INDEX_HTML.read_text(encoding="utf-8")
    function = _extract_js_function(source, "function combineUsagePayloads(list) {")

    # Halved across two servers so the merge, not either input, fixes the order.
    # cheap-workhorse ends on 10k tokens for $1; pricey-boutique on 4k for $9.
    first = _payload([("cheap-workhorse", 5_000, 0.5), ("pricey-boutique", 2_000, 4.5)])
    second = _payload([("cheap-workhorse", 5_000, 0.5), ("pricey-boutique", 2_000, 4.5)])

    script = (
        function
        + "\nconst input = JSON.parse(process.argv[2]);\n"
        + "const out = combineUsagePayloads(input);\n"
        + "process.stdout.write(JSON.stringify({\n"
        + "  top: out.top_models, combined: out.combined_models, app: out.apps.editor.models,\n"
        + "}));\n"
    )
    out = _run_js(tmp_path, "merge_rank", script, [first, second])

    for key in ("top", "combined", "app"):
        rows = out[key]
        assert [r["name"] for r in rows] == ["cheap-workhorse", "pricey-boutique"], key
        assert rows[0]["tokens"] == 10_000 and rows[0]["cost"] == 1.0
        # Fixture guard: a cost sort would have reversed this list.
        assert rows[0]["cost"] < rows[1]["cost"], key


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_cost_chart_picks_its_own_top_five_by_cost(tmp_path):
    source = INDEX_HTML.read_text(encoding="utf-8")
    function = _extract_js_function(source, "function updateModelChart(topModels) {")

    # Six models. By tokens the last one is bottom of the list; by cost it is top.
    # The panel is titled "Top Models by Cost", so it must lead with the $12 model
    # and must not simply render the token-ranked array it was handed.
    models = [
        {"name": "cheap-workhorse", "tokens": 10_000, "cost": 1.0},
        {"name": "ocl-big", "tokens": 9_000, "cost": 0.5},
        {"name": "codex-heavy", "tokens": 8_000, "cost": 2.0},
        {"name": "mid-runner", "tokens": 7_000, "cost": 5.0},
        {"name": "pricey-boutique", "tokens": 4_000, "cost": 9.0},
        {"name": "ocl-small", "tokens": 3_000, "cost": 12.0},
    ]

    script = (
        "let modelChart = null;\n"
        "const captured = {};\n"
        "class Chart { constructor(ctx, config) { captured.config = config; } }\n"
        "const document = { getElementById: () => ({ getContext: () => ({}) }) };\n"
        "const t = (key) => key;\n"
        "const getChartPalette = () => ['#000000'];\n"
        "const formatCurrency = (value) => String(value);\n"
        + function
        + "\nconst input = JSON.parse(process.argv[2]);\n"
        + "updateModelChart(input);\n"
        + "process.stdout.write(JSON.stringify({\n"
        + "  labels: captured.config.data.labels, values: captured.config.data.datasets[0].data,\n"
        + "}));\n"
    )
    out = _run_js(tmp_path, "cost_chart", script, models)

    assert out["labels"] == [
        "ocl-small",
        "pricey-boutique",
        "mid-runner",
        "codex-heavy",
        "cheap-workhorse",
    ]
    assert out["values"] == [12.0, 9.0, 5.0, 2.0, 1.0]
    # The cheapest model is excluded, which is the whole point of a cost panel.
    assert "ocl-big" not in out["labels"]


def test_cost_chart_is_fed_the_full_model_list():
    """Five models by tokens are not the five by cost, so the chart needs them all."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    call = re.search(r"updateModelChart\((.*?)\);", source, re.S)
    assert call, "updateModelChart call site not found"
    assert "combined_models" in call.group(1), (
        "the cost chart must read combined_models; top_models is capped at five by tokens"
    )
