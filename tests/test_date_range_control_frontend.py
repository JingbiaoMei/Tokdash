from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import tokdash  # type: ignore[import-untyped]

INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"


def _extract_js_function(source: str, signature: str) -> str:
    start = source.find(signature)
    assert start != -1, f"{signature} not found in index.html"
    depth = 0
    for index in range(source.find("{", start), len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {signature}")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_date_range_trigger_text_is_localized_and_deterministic(
    tmp_path: Path,
) -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    functions = "\n".join(
        _extract_js_function(source, signature)
        for signature in (
            "function sameLocalDate(left, right) {",
            "function formatDateRangeDate(date, lang = currentLang) {",
            "function formatDateRangeTriggerText(startDate, endDate, lang = currentLang) {",
        )
    )
    harness = tmp_path / "date-range-control.js"
    harness.write_text(
        functions
        + "\nconst cases = JSON.parse(process.argv[2]);\n"
        + "const result = cases.map(({ start, end, lang }) => "
        + "formatDateRangeTriggerText(new Date(`${start}T12:00:00`), new Date(`${end}T12:00:00`), lang));\n"
        + "process.stdout.write(JSON.stringify(result));\n",
        encoding="utf-8",
    )
    cases = [
        {"start": "2026-08-10", "end": "2026-08-10", "lang": "en"},
        {"start": "2026-08-03", "end": "2026-08-09", "lang": "en"},
        {"start": "2026-08-10", "end": "2026-08-10", "lang": "zh"},
        {"start": "2026-08-03", "end": "2026-08-09", "lang": "zh"},
    ]
    result = subprocess.run(
        ["node", str(harness), json.dumps(cases)],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    assert json.loads(result.stdout) == [
        "Aug 10, 2026",
        "Aug 3, 2026 – Aug 9, 2026",
        "2026年8月10日",
        "2026年8月3日 – 2026年8月9日",
    ]
