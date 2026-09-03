"""The Claude card must separate two subscriptions on the same machine.

A `~/.claude-academic` install reports its own windows, and every surface that names them
(card groups, window labels, chart legend) has to say which subscription it means, or two
different 5-hour windows both read as "Claude 5-hour".
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import tokdash  # type: ignore[import-untyped]

INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"

STUBS = (
    "function t(key) { return LABELS[key] || key; }\n"
    "const LABELS = { quotaProfileDefault: 'Default', windowFiveHour: '5-hour', windowWeekly: 'Weekly' };\n"
    "function quotaProviderLabel(key) { return key === 'claude' ? 'Claude Code' : key; }\n"
)


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


def _run(tmp_path: Path, name: str, functions: list[str], expression: str, value):
    harness = tmp_path / f"{name}.js"
    harness.write_text(
        STUBS
        + "\n".join(functions)
        + "\nconst input = JSON.parse(process.argv[2]);\n"
        + f"process.stdout.write(JSON.stringify({expression}));\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(harness), json.dumps(value)],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def _claude_functions(src: str) -> list[str]:
    return [
        _extract_js_function(src, "function claudeBucketPrefix(account) {"),
        _extract_js_function(src, "function claudeProfileSeriesName(account) {"),
        _extract_js_function(src, "function claudeProfileAccounts(provider) {"),
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_a_siblings_windows_are_named_after_its_directory(tmp_path):
    src = INDEX_HTML.read_text(encoding="utf-8")
    functions = _claude_functions(src) + [
        _extract_js_function(src, "function antigravityWindowLabel(bucket) {"),
        _extract_js_function(src, "function quotaWindowLabel(provider, bucket) {"),
        _extract_js_function(src, "function quotaSeriesLabel(item) {"),
        _extract_js_function(src, "function quotaProviderCardLabel(providerKey, provider) {"),
    ]
    windows = [
        {"account": "default", "bucket": "session", "bucket_label": "Session"},
        {"account": "default", "bucket": "weekly_all", "bucket_label": "Weekly All"},
        {"account": "academic", "bucket": "academic_session", "bucket_label": "Session"},
        {"account": "academic", "bucket": "academic_weekly_all", "bucket_label": "Weekly All"},
        {
            "account": "academic",
            "bucket": "academic_weekly_scoped_opus",
            "bucket_label": "Opus",
        },
    ]
    series = [
        {"provider": "claude", "account": "default", "bucket": "session", "bucket_label": "Session"},
        {"provider": "claude", "account": "academic", "bucket": "academic_session", "bucket_label": "Session"},
        {"provider": "claude", "account": "academic", "bucket": "academic_weekly_all", "bucket_label": "Weekly All"},
    ]
    cards = [
        {"buckets": [{"account": "academic"}], "profiles": [{"account": "academic"}]},
        {"buckets": [{"account": "default"}, {"account": "academic"}], "profiles": []},
        {"buckets": [{"account": "default"}], "profiles": [{"account": "default"}]},
    ]
    result = _run(
        tmp_path,
        "claude-labels",
        functions,
        "{ windows: input.windows.map((b) => quotaWindowLabel('claude', b)), "
        "series: input.series.map((item) => quotaSeriesLabel(item)), "
        "cards: input.cards.map((p) => quotaProviderCardLabel('claude', p)) }",
        {"windows": windows, "series": series, "cards": cards},
    )

    assert result["windows"] == ["5-hour", "Weekly", "5-hour", "Weekly", "Opus"]
    # The legend has to tell two subscriptions' windows apart, and the default install's
    # label must not change for everyone who has one install.
    assert result["series"] == [
        "Claude 5-hour",
        "Claude-academic 5-hour",
        "Claude-academic Weekly",
    ]
    assert result["cards"] == ["Claude Code (academic)", "Claude Code", "Claude Code"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_only_the_card_plan_that_fits_all_installs_is_shown(tmp_path):
    src = INDEX_HTML.read_text(encoding="utf-8")
    plan_fn = _extract_js_function(src, "function quotaProviderPlanLabel(providerKey, provider) {")

    same = {"plan": "Max 5x", "profiles": [{"account": "default", "plan": "Max 5x"}, {"account": "academic", "plan": "Max 5x"}]}
    differs = {"plan": "Max 5x", "profiles": [{"account": "default", "plan": "Max 5x"}, {"account": "academic", "plan": "Pro"}]}
    single = {"plan": "Max 5x", "profiles": [{"account": "default", "plan": "Max 5x"}]}

    assert _run(tmp_path, "claude-plan", [plan_fn], "input.map((p) => quotaProviderPlanLabel('claude', p))", [same, differs, single]) == [
        "Max 5x",
        "",  # the default install's plan does not describe the academic one
        "Max 5x",
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_profile_groups_order_headings_and_surface_own_failures(tmp_path):
    src = INDEX_HTML.read_text(encoding="utf-8")
    functions = _claude_functions(src) + [
        _extract_js_function(src, "function isQuotaUsageBucket(bucket) {"),
        _extract_js_function(src, "function claudeProfileGroups(provider) {"),
        _extract_js_function(src, "function claudeProfileGroupLabel(group, showPlans) {"),
    ]
    provider = {
        "buckets": [
            {"account": "academic", "bucket": "academic_session", "used_percent": 20},
            {"account": "default", "bucket": "session", "used_percent": 75},
            {"account": "default", "bucket": "api", "status": "ok"},
        ],
        "profiles": [
            {"account": "default", "plan": "Max 5x"},
            {"account": "academic", "plan": "Pro"},
            {"account": "lab", "plan": "Pro", "status_detail": "stale_token", "status_at": 1782907200},
            {"account": "signed-out"},  # readable directory, nothing polled, nothing wrong
        ],
    }

    headings = _run(
        tmp_path,
        "claude-groups",
        functions,
        "{ plain: claudeProfileGroups(input).map((group) => ["
        "claudeProfileGroupLabel(group, false), "
        "group.rows.map((row) => row.bucket), group.notice]), "
        "apart: claudeProfileGroups(input).map((group) => claudeProfileGroupLabel(group, true)) }",
        provider,
    )

    # Default install first; the non-default ones keep their directory names; a `api`
    # status row is not a window; an install with neither windows nor a problem is dropped;
    # an install that only has a failure still gets its heading and its own notice.
    assert headings["plain"] == [
        ["Default", ["session"], ""],
        ["academic", ["academic_session"], ""],
        ["lab", [], "stale_token"],
    ]
    # When the installs are on different plans each heading has to say so, because the
    # card title's plan line is suppressed for exactly that case.
    assert headings["apart"] == ["Default · Max 5x", "academic · Pro", "lab · Pro"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_the_claude_card_renders_one_group_of_bars_per_install(tmp_path):
    """Rendering is checked with a stand-in for the bar row: the point is which headings
    appear, in what order, and that a broken install's notice lands under its own heading
    rather than at the bottom of the card."""
    src = INDEX_HTML.read_text(encoding="utf-8")
    functions = _claude_functions(src) + [
        _extract_js_function(src, "function isQuotaUsageBucket(bucket) {"),
        _extract_js_function(src, "function claudeProfileGroups(provider) {"),
        _extract_js_function(src, "function claudeProfileGroupLabel(group, showPlans) {"),
        _extract_js_function(src, "function quotaSubtitleEl(text, tight) {"),
        _extract_js_function(src, "function appendQuotaStatusNoticeFor(card, providerKey, detail, statusAt) {"),
        _extract_js_function(src, "function renderClaudeBuckets(card, provider) {"),
    ]
    harness = tmp_path / "claude-card.js"
    harness.write_text(
        "const LABELS = { quotaProfileDefault: 'Default', quotaStaleToken: '{app} sign-in expired', quotaProviderIssue: '{status} at {time}', na: 'n/a' };\n"
        "function t(key) { return LABELS[key] || key; }\n"
        "function quotaProviderLabel(key) { return key === 'claude' ? 'Claude Code' : key; }\n"
        "function formatRelativeAgo() { return '2 minutes ago'; }\n"
        "function makeEl(tag) {\n"
        "  return { tag, children: [], className: '', style: {}, textContent: '',\n"
        "           appendChild(child) { this.children.push(child); return child; } };\n"
        "}\n"
        "const document = { createElement: makeEl };\n"
        "function renderQuotaBucketRow(bucket) { return { tag: 'bar', textContent: bucket.bucket, children: [] }; }\n"
        + "\n".join(functions)
        + "\nconst card = makeEl('card');\n"
        "renderClaudeBuckets(card, JSON.parse(process.argv[2]));\n"
        "const kind = (child) => (/uppercase/.test(child.className || '') ? 'subtitle'"
        " : /rounded/.test(child.className || '') ? 'notice' : 'bar');\n"
        "process.stdout.write(JSON.stringify(card.children.map((child) => [kind(child), child.textContent])));\n",
        encoding="utf-8",
    )
    provider = {
        "buckets": [
            {"account": "academic", "bucket": "academic_session", "used_percent": 20},
            {"account": "default", "bucket": "session", "used_percent": 75},
        ],
        "profiles": [
            {"account": "default", "plan": "Max 5x"},
            {"account": "academic", "plan": "Pro"},
            {"account": "lab", "plan": "Pro", "status_detail": "stale_token", "status_at": 1782907200},
        ],
    }

    result = subprocess.run(
        ["node", str(harness), json.dumps(provider)], capture_output=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr

    assert json.loads(result.stdout) == [
        ["subtitle", "Default · Max 5x"],
        ["bar", "session"],
        ["subtitle", "academic · Pro"],
        ["bar", "academic_session"],
        ["subtitle", "lab · Pro"],
        ["notice", "Claude Code sign-in expired"],
    ]
