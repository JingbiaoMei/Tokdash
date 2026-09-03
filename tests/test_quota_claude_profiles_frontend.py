"""The Claude card must separate two subscriptions on the same machine.

A `~/.claude-academic` install reports its own windows, and every surface that names them
(card groups, window labels, chart legend) has to say which subscription it means, or two
different 5-hour windows both read as "Claude 5-hour".

The card decides its groups, its plan line and its headings from one `claudeCardView()` call,
so the tests below exercise that view rather than the helpers behind it: a card whose title
and body were decided separately could (did) drop the plan, or drop a failing install's
notice, by having the two halves disagree.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import tokdash  # type: ignore[import-untyped]

INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"

# A stand-in for the dashboard's globals: translations, the element factory, and a bar
# renderer cheap enough to assert against. Rendering tests care about which headings and
# notices appear around the bars, not about the bars' own markup.
STUBS = """
const LABELS = {
  quotaProfileDefault: 'Default', windowFiveHour: '5-hour', windowWeekly: 'Weekly',
  quotaNoData: 'No quota snapshots yet.', quotaProviderIssue: '{status} at {time}',
  quotaStaleToken: '{app} sign-in expired', na: 'n/a',
};
function t(key) { return LABELS[key] || key; }
function quotaProviderLabel(key) { return key === 'claude' ? 'Claude Code' : key; }
function formatRelativeAgo() { return '3 minutes ago'; }
function makeEl(tag) {
  return { tag, children: [], className: '', style: {}, textContent: '',
           appendChild(child) { this.children.push(child); return child; } };
}
const document = { createElement: makeEl };
function renderQuotaBucketRow(bucket) {
  return { tag: 'bar', textContent: bucket.bucket, children: [] };
}
// Headings are the only uppercase elements and notices the only rounded ones, which is
// enough for the render assertions to tell the three kinds of child apart.
const kind = (el) => (/uppercase/.test(el.className || '') ? 'heading'
  : /rounded/.test(el.className || '') ? 'notice' : el.tag);
const shape = (card) => card.children.map((el) => [kind(el), el.textContent]);
"""

# Everything the card's view is built from, in dependency order.
VIEW_FUNCTIONS = [
    "function isQuotaUsageBucket(bucket) {",
    "function claudeBucketPrefix(account) {",
    "function claudeBucketKind(account, bucket) {",
    "function claudeProfileSeriesName(account) {",
    "function claudeCardAccounts(buckets, provider) {",
    "function claudeProfileGroups(buckets, provider) {",
    "function claudeProfileGroupLabel(group, showPlans) {",
    "function claudeCardView(buckets, provider) {",
    "function quotaSubtitleEl(text, tight) {",
    "function appendQuotaStatusNoticeFor(card, providerKey, detail, statusAt) {",
    "function renderClaudeBuckets(card, view, providerKey) {",
]


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


def _view_code(src: str) -> str:
    return "\n".join(_extract_js_function(src, signature) for signature in VIEW_FUNCTIONS)


def _run(tmp_path: Path, name: str, code: str, expression: str, value):
    harness = tmp_path / f"{name}.js"
    harness.write_text(
        STUBS
        + code
        + "\nconst input = JSON.parse(process.argv[2]);\n"
        + f"process.stdout.write(JSON.stringify({expression}));\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(harness), json.dumps(value)], check=True, capture_output=True, encoding="utf-8"
    )
    return json.loads(result.stdout)


def _card(tmp_path: Path, src: str, name: str, provider):
    """Render one provider payload the way the card body does, and return its children."""
    return _run(
        tmp_path,
        name,
        _view_code(src),
        "(() => { const card = makeEl('card');"
        "const buckets = (input.buckets || []).filter(isQuotaUsageBucket);"
        "const rendered = renderClaudeBuckets(card, claudeCardView(buckets, input), 'claude');"
        "return { rendered, children: shape(card) }; })()",
        provider,
    )


def _window(account, bucket, used=40.0):
    return {"account": account, "bucket": bucket, "used_percent": used, "bucket_label": "Session"}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_a_siblings_windows_are_named_after_its_directory(tmp_path):
    src = INDEX_HTML.read_text(encoding="utf-8")
    code = _view_code(src) + "\n" + "\n".join(
        [
            _extract_js_function(src, "function quotaWindowLabel(provider, bucket) {"),
            _extract_js_function(src, "function quotaSeriesLabel(item) {"),
        ]
    )
    windows = [
        _window("default", "session"),
        {"account": "default", "bucket": "weekly_all", "used_percent": 10.0},
        _window("academic", "academic_session", 20.0),
        {"account": "academic", "bucket": "academic_weekly_all", "used_percent": 5.0},
        # A per-model window keeps the label the API gave it.
        {
            "account": "academic",
            "bucket": "academic_weekly_scoped_opus",
            "used_percent": 5.0,
            "bucket_label": "Opus",
        },
    ]
    series = [
        {"provider": "claude", "account": "default", "bucket": "session", "bucket_label": "Session"},
        {
            "provider": "claude",
            "account": "academic",
            "bucket": "academic_session",
            "bucket_label": "Session",
        },
        {
            "provider": "claude",
            "account": "academic",
            "bucket": "academic_weekly_all",
            "bucket_label": "Weekly All",
        },
    ]
    result = _run(
        tmp_path,
        "labels",
        code,
        "{ windows: input.map((b) => quotaWindowLabel('claude', b)), "
        "series: [1].map(() => 0) }",
        windows,
    )
    legend = _run(
        tmp_path,
        "legend",
        code,
        "input.map((item) => quotaSeriesLabel(item))",
        series,
    )

    assert result["windows"] == ["5-hour", "Weekly", "5-hour", "Weekly", "Opus"]
    # The legend has to tell two subscriptions' windows apart, and the default install's
    # label must not change for everyone who has one install.
    assert legend == ["Claude 5-hour", "Claude-academic 5-hour", "Claude-academic Weekly"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_a_stored_bucket_id_reads_back_as_the_window_it_measures(tmp_path):
    """The prefix the backend writes and the label code that reads it are one convention.

    The stored id is built in Python and taken apart in JavaScript, so the two are checked
    against each other here rather than each trusting its own arithmetic.
    """
    from tokdash.sources.quota.claude import ClaudeProfile

    src = INDEX_HTML.read_text(encoding="utf-8")
    profiles = [ClaudeProfile("default", Path("."), True)] + [
        ClaudeProfile(name, Path(f"~/.claude-{name}"))
        for name in ("academic", "lab", "weekly_all")  # the last owns a window-name prefix
    ]
    pairs = [
        {"account": profile.name, "bucket": f"{profile.bucket_prefix}{kind}"}
        for profile in profiles
        for kind in ("session", "weekly_all", "weekly_scoped_opus")
    ]
    kinds = _run(
        tmp_path,
        "prefix-parity",
        _view_code(src),
        "input.map((p) => claudeBucketKind(p.account, p.bucket))",
        pairs,
    )

    assert kinds == [
        kind for _profile in profiles for kind in ("session", "weekly_all", "weekly_scoped_opus")
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_the_title_only_shows_a_plan_every_shown_install_shares(tmp_path):
    """The plan line and the group headings are one decision, made from one group list."""
    src = INDEX_HTML.read_text(encoding="utf-8")
    same = {
        "plan": "Max 5x",
        "buckets": [_window("default", "session"), _window("academic", "academic_session")],
        "accounts": [{"account": "default", "plan": "Max 5x"}, {"account": "academic", "plan": "Max 5x"}],
    }
    differs = {
        "plan": "Max 5x",
        "buckets": [_window("default", "session"), _window("academic", "academic_session")],
        "accounts": [{"account": "default", "plan": "Max 5x"}, {"account": "academic", "plan": "Pro"}],
    }
    # The case that lost the plan outright: a second install is listed but has nothing to
    # show, so only one group renders -- and the title had stopped naming the plan because
    # it keyed off the payload list rather than what was on screen.
    silent = {
        "plan": "Max 5x",
        "buckets": [_window("default", "session")],
        "accounts": [{"account": "default", "plan": "Max 5x"}, {"account": "lab", "plan": "Pro"}],
    }
    # Two installs shown, only one of them reports a plan: the title stays quiet and the
    # heading that has a plan says it.
    half = {
        "plan": "Max 5x",
        "buckets": [_window("default", "session")],
        "accounts": [
            {"account": "default", "plan": "Max 5x"},
            {"account": "lab", "status_detail": "fetch_error", "status_at": 1_782_907_200},
        ],
    }
    cases = [same, differs, silent, half]
    views = _run(
        tmp_path,
        "plans",
        _view_code(src),
        "input.map((p) => { const view = claudeCardView((p.buckets || []).filter(isQuotaUsageBucket), p);"
        "return { plan: view.plan, showPlans: view.showPlans,"
        " headings: view.groups.map((g) => claudeProfileGroupLabel(g, view.showPlans)) }; })",
        cases,
    )

    assert views[0]["plan"] == "Max 5x"
    assert views[0]["headings"] == ["Default", "academic"]
    assert views[1]["plan"] == ""  # neither install's plan describes the other
    assert views[1]["headings"] == ["Default · Max 5x", "academic · Pro"]
    assert views[2]["plan"] == "Max 5x"
    assert views[2]["headings"] == ["Default"]
    assert views[3]["plan"] == ""
    assert views[3]["headings"] == ["Default · Max 5x", "lab"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_one_install_renders_exactly_as_it_did_before_profiles(tmp_path):
    src = INDEX_HTML.read_text(encoding="utf-8")
    card = _card(
        tmp_path,
        src,
        "one-install",
        {
            "buckets": [_window("default", "session"), _window("default", "weekly_all", 10.0)],
            "accounts": [{"account": "default", "plan": "Max 5x"}],
        },
    )

    assert card["rendered"] == 1
    # One install needs no heading over its own bars, and no plan duplicated from the title.
    assert card["children"] == [["bar", "session"], ["bar", "weekly_all"]]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_each_install_gets_its_own_heading_bars_and_failure(tmp_path):
    src = INDEX_HTML.read_text(encoding="utf-8")
    card = _card(
        tmp_path,
        src,
        "three-installs",
        {
            "buckets": [
                _window("academic", "academic_session", 20.0),
                _window("default", "session", 75.0),
            ],
            "accounts": [
                {"account": "default", "plan": "Max 5x"},
                {"account": "academic", "plan": "Pro"},
                # Signed in, never polled successfully: no bars, but a real problem.
                {"account": "lab", "plan": "Pro", "status_detail": "stale_token", "status_at": 1_782_907_200},
            ],
        },
    )

    # Default install first, siblings by name, and a `api` status row is never a window bar.
    assert card["children"] == [
        ["heading", "Default · Max 5x"],
        ["bar", "session"],
        ["heading", "academic · Pro"],
        ["bar", "academic_session"],
        ["heading", "lab · Pro"],
        ["notice", "Claude Code sign-in expired"],
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_every_failing_install_is_named_when_none_of_them_reports(tmp_path):
    """Both tokens expired: the card has no window rows at all, and still has to say which
    install is broken rather than print one generic line."""
    src = INDEX_HTML.read_text(encoding="utf-8")
    card = _card(
        tmp_path,
        src,
        "all-failing",
        {
            "buckets": [],
            "status": "stale_token",
            "status_detail": "stale_token",
            "accounts": [
                {"account": "default", "plan": "Max 5x", "status_detail": "stale_token", "status_at": 1_782_907_200},
                {"account": "academic", "plan": "Pro", "status_detail": "fetch_error", "status_at": 1_782_907_200},
            ],
        },
    )

    assert card["rendered"] == 2
    assert card["children"] == [
        ["heading", "Default · Max 5x"],
        ["notice", "Claude Code sign-in expired"],
        ["heading", "academic · Pro"],
        ["notice", "fetch_error at 3 minutes ago"],
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_the_card_holds_its_own_error_when_no_installs_are_named(tmp_path):
    """An older payload, or one without credential-scan consent, names no installs; the
    card's own error still reaches the one install it can be about."""
    src = INDEX_HTML.read_text(encoding="utf-8")
    card = _card(
        tmp_path,
        src,
        "no-account-list",
        {"buckets": [_window("default", "session")], "status_detail": "fetch_error", "status_at": 1_782_907_200},
    )

    assert card["children"] == [
        ["bar", "session"],
        ["notice", "fetch_error at 3 minutes ago"],
    ]
