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
        encoding="utf-8",
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
        [
            "minimax",
            {
                "detected": True,
                "plan": "one-region-plan",
                "buckets": [{"account": "global"}, {"account": "cn"}],
            },
        ],
    ]
    result = subprocess.run(
        ["node", str(harness), json.dumps(cases)],
        capture_output=True,
        encoding="utf-8",
        check=True,
    )

    assert json.loads(result.stdout) == [
        "",
        "",
        "Intermediate",
        "not detected",
        "",
        "",
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_minimax_china_region_moves_from_bucket_to_card_title(tmp_path):
    src = INDEX_HTML.read_text(encoding="utf-8")
    card_fn = _extract_js_function(
        src, "function quotaProviderCardLabel(providerKey, provider) {"
    )
    window_fn = _extract_js_function(
        src, "function quotaWindowLabel(provider, bucket) {"
    )
    series_fn = _extract_js_function(
        src, "function quotaSeriesLabel(item) {"
    )
    groups_fn = _extract_js_function(
        src, "function miniMaxBucketGroups(buckets) {"
    )
    harness = tmp_path / "minimax-region-label.js"
    harness.write_text(
        "function t(key) { return key === 'quotaRegionChina' ? 'China' : key; }\n"
        + "function quotaProviderLabel(key) { return key; }\n"
        + card_fn
        + "\n"
        + window_fn
        + "\n"
        + series_fn
        + "\n"
        + groups_fn
        + "\nconst cases = JSON.parse(process.argv[2]);\n"
        + "const titles = cases.map((buckets) => quotaProviderCardLabel('minimax', { buckets }));\n"
        + "const oldBucket = quotaWindowLabel('minimax', "
        + "{ account: 'cn', bucket: 'cn_general_5h', bucket_label: 'General · 5-hour (Mainland China)' });\n"
        + "const weeklyBucket = quotaWindowLabel('minimax', "
        + "{ account: 'cn', bucket: 'cn_general_7d', bucket_label: 'General · Weekly' });\n"
        + "const nestedGeneral = quotaWindowLabel('minimax', "
        + "{ account: 'global', bucket: 'global_text_general_5h', bucket_label: 'Text General · 5-hour' });\n"
        + "const nestedSeries = quotaSeriesLabel("
        + "{ provider: 'minimax', account: 'global', bucket: 'global_text_general_5h', "
        + "bucket_label: 'Text General · 5-hour' });\n"
        + "const groups = miniMaxBucketGroups(["
        + "{ account: 'cn', bucket: 'cn-row' }, { account: 'global', bucket: 'global-row' }"
        + "]).map((group) => [group.account, group.rows.map((row) => row.bucket)]);\n"
        + "process.stdout.write(JSON.stringify({ titles, oldBucket, weeklyBucket, nestedGeneral, nestedSeries, groups }));\n",
        encoding="utf-8",
    )
    cases = [
        [{"account": "cn"}],
        [{"account": "global"}],
        [{"account": "global"}, {"account": "cn"}],
    ]
    result = subprocess.run(
        ["node", str(harness), json.dumps(cases)],
        capture_output=True,
        encoding="utf-8",
        check=True,
    )

    assert json.loads(result.stdout) == {
        "titles": ["MiniMax (China)", "MiniMax", "MiniMax"],
        "oldBucket": "windowFiveHour",
        "weeklyBucket": "windowWeekly",
        "nestedGeneral": "Text General · 5-hour",
        "nestedSeries": "MiniMax Text General · 5-hour",
        "groups": [["global", ["global-row"]], ["cn", ["cn-row"]]],
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_kimi_legacy_plan_bucket_is_labeled_weekly(tmp_path):
    src = INDEX_HTML.read_text(encoding="utf-8")
    window_fn = _extract_js_function(
        src, "function quotaWindowLabel(provider, bucket) {"
    )
    harness = tmp_path / "kimi-window-label.js"
    harness.write_text(
        "function t(key) { return key; }\n"
        + window_fn
        + "\nprocess.stdout.write(quotaWindowLabel('kimi', "
        + "{ bucket: 'plan', bucket_label: 'Plan usage' }));\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["node", str(harness)],
        capture_output=True,
        encoding="utf-8",
        check=True,
    )

    assert result.stdout == "windowWeekly"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_antigravity_window_label_auto_determined_from_reset_time(tmp_path):
    """Antigravity's fetchAvailableModels returns a single quotaInfo per model - whichever
    window (5-hour OR weekly) currently binds the shared pool. The card label must reflect
    the actual window, not assume 5-hour. The window is inferred from how far out the reset
    falls: a 5-hour window can't reset more than 5h out, so a reset beyond the threshold is
    weekly. Reproduces the 3d22h weekly case that previously rendered a stale "5-hour" label.
    """
    src = INDEX_HTML.read_text(encoding="utf-8")
    window_fn = _extract_js_function(src, "function quotaWindowLabel(provider, bucket) {")
    ag_fn = _extract_js_function(src, "function antigravityWindowLabel(bucket) {")
    harness = tmp_path / "antigravity-window-label.js"
    harness.write_text(
        "function t(key) { return key; }\n"
        + ag_fn
        + "\n"
        + window_fn
        + "\nconst cases = JSON.parse(process.argv[2]);\n"
        + "process.stdout.write(JSON.stringify(cases.map((b) => quotaWindowLabel('antigravity', b))));\n",
        encoding="utf-8",
    )
    captured = 1_782_907_200  # fixed reference; resets_at is absolute epoch seconds
    cases = [
        # 5-hour window: reset 3h out -> 5-hour
        {"bucket": "pool:gemini", "bucket_label": "Gemini Models",
         "captured_at": captured, "resets_at": captured + 3 * 3600},
        # weekly window: reset 3d22h out (the reported scenario) -> Weekly
        {"bucket": "pool:gemini", "bucket_label": "Gemini Models",
         "captured_at": captured, "resets_at": captured + (3 * 24 + 22) * 3600},
        # full 7-day weekly window -> Weekly
        {"bucket": "pool:claude", "bucket_label": "Claude and GPT Models",
         "captured_at": captured, "resets_at": captured + 7 * 24 * 3600},
        # idle model: no reset time -> 5-hour (no regression vs. the old hardcoded label)
        {"bucket": "pool:gemini", "bucket_label": "Gemini Models",
         "captured_at": captured, "resets_at": None},
    ]
    result = subprocess.run(
        ["node", str(harness), json.dumps(cases)],
        capture_output=True,
        encoding="utf-8",
        check=True,
    )

    assert json.loads(result.stdout) == [
        "windowFiveHour",
        "windowWeekly",
        "windowWeekly",
        "windowFiveHour",
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_quota_reset_countdown_uses_companion_single_unit_rule(tmp_path):
    """The quota bar sub-line shows a relative countdown using the companion app's rule:
    a single unit only (no combined "3d 22h 43m" noise). >= 1 day -> days, >= 2 hours ->
    hours, else minutes; sub-minute / past -> "resets soon"; non-finite -> null.

    The tier boundaries below are pinned to the same values asserted by the companion
    suites (macOS testResetsTextIsRelative, Windows ResetsTextForRemaining_Is_Relative).
    All three must agree or the same window reads differently on each surface — which is
    exactly what happened when the web gained a days tier the companions lacked and a
    weekly window read "resets in 3 days" here but "resets in 94 hours" in the flyout.
    """
    src = INDEX_HTML.read_text(encoding="utf-8")
    fn = _extract_js_function(src, "function formatResetCountdownFromSeconds(remaining) {")
    harness = tmp_path / "reset-countdown.js"
    harness.write_text(
        "function t(key) {\n"
        "  const m = { resetsInDays: 'resets in {n} day{s}',\n"
        "              resetsInHours: 'resets in {n} hour{s}',\n"
        "              resetsInMinutes: 'resets in {n} minute{s}',\n"
        "              resetsSoon: 'resets soon' };\n"
        "  return m[key] !== undefined ? m[key] : key;\n"
        "}\n"
        + fn
        + "\nconst cases = JSON.parse(process.argv[2]);\n"
        + "process.stdout.write(JSON.stringify(cases.map(formatResetCountdownFromSeconds)));\n",
        encoding="utf-8",
    )
    cases = [
        3 * 86400 + 22 * 3600,  # 3.9 days -> "3 days"
        4 * 3600 + 32 * 60,     # 4.5 hours -> "4 hours"
        43 * 60,                # 43 min -> "43 minutes"
        60,                     # exactly 1 min -> "1 minute" (singular)
        86400,                  # exactly 1 day -> "1 day" (singular)
        30,                     # <1 min -> resets soon
        -100,                   # past -> resets soon
        None,                   # non-finite (null) -> no countdown
        # Tier boundaries, mirrored by both companion suites.
        7199,                   # max minute value stays under 120
        7200,                   # minute -> hour boundary
        86399,                  # max hour value stays under 24
        129600,                 # 1.5d floors to the whole unit
        7 * 86400,              # full weekly window
    ]
    result = subprocess.run(
        ["node", str(harness), json.dumps(cases)],
        capture_output=True,
        encoding="utf-8",
        check=True,
    )

    assert json.loads(result.stdout) == [
        "resets in 3 days",
        "resets in 4 hours",
        "resets in 43 minutes",
        "resets in 1 minute",
        "resets in 1 day",
        "resets soon",
        "resets soon",
        None,
        "resets in 119 minutes",
        "resets in 2 hours",
        "resets in 23 hours",
        "resets in 1 day",
        "resets in 7 days",
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_unlimited_quota_bucket_remains_visible_without_numeric_percent(tmp_path):
    src = INDEX_HTML.read_text(encoding="utf-8")
    usage_fn = _extract_js_function(
        src, "function isQuotaUsageBucket(bucket) {"
    )
    harness = tmp_path / "quota-usage-bucket.js"
    harness.write_text(
        usage_fn
        + "\nconst cases = JSON.parse(process.argv[2]);\n"
        + "process.stdout.write(JSON.stringify(cases.map(isQuotaUsageBucket)));\n",
        encoding="utf-8",
    )
    cases = [
        {"bucket": "cn_general_7d", "used_percent": None, "unlimited": True},
        {"bucket": "global_general_7d", "used_percent": 0, "unlimited": False},
        {"bucket": "api", "used_percent": None, "unlimited": True},
        {"bucket": "global_general_7d", "used_percent": None, "unlimited": False},
    ]

    result = subprocess.run(
        ["node", str(harness), json.dumps(cases)],
        capture_output=True,
        encoding="utf-8",
        check=True,
    )

    assert json.loads(result.stdout) == [True, True, False, False]
