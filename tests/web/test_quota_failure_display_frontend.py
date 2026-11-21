"""A quota card must not misdescribe a failed poll.

Seen on a Claude card (2026-11-21):

- Anthropic refused the network the poll came from (HTTP 403 "Request not allowed") and
  the card said the sign-in had expired and to open Claude Code, though the token worked.
"""
from __future__ import annotations

import json
from pathlib import Path

from support.frontend import extract_js_function, index_source, needs_node, run_node

STUBS = """
const LABELS = {
  quotaStaleToken: '{app} sign-in expired',
  quotaNetworkBlocked: '{app} refused this network',
  quotaProviderIssue: '{status} at {time}',
  quotaResetAfterReading: 'reset {time}, before it',
  quotaRemainingPct: '{pct}% left', quotaUnlimited: 'Unlimited', na: 'n/a',
  resetsInDays: 'resets in {n} day{s}', resetsInHours: 'resets in {n} hour{s}',
  resetsInMinutes: 'resets in {n} minute{s}', resetsSoon: 'resets soon',
  sourceSession: 'local logs', sourceSessionTitle: 'from session files',
};
function t(key) { return LABELS[key] || key; }
function quotaProviderLabel(key) { return key === 'claude' ? 'Claude Code' : key; }
function formatRelativeAgo() { return '3 minutes ago'; }
function formatEpochSeconds(value) { return value ? `@${value}` : '—'; }
function quotaWindowLabel(provider, bucket) { return bucket.bucket; }
function quotaRemainingPercent(bucket) {
  return bucket.used_percent == null ? null : 100 - Number(bucket.used_percent);
}
function quotaRemainingColor() { return 'live-colour'; }
function makeEl(tag) {
  return { tag, children: [], className: '', style: {}, textContent: '', title: '',
           appendChild(child) { this.children.push(child); return child; } };
}
const document = { createElement: makeEl };
"""

NOTICE_FUNCTIONS = ["function appendQuotaStatusNoticeFor(card, providerKey, detail, statusAt) {"]

NOW = 1_795_276_800
DAY = 86_400


def _run(tmp_path: Path, name: str, functions: list[str], expression: str, value):
    src = index_source()
    harness = tmp_path / f"{name}.js"
    harness.write_text(
        STUBS
        + "\n".join(extract_js_function(src, signature) for signature in functions)
        + "\nconst input = JSON.parse(process.argv[2]);\n"
        + "Date.now = () => input.now * 1000;\n"
        + f"process.stdout.write(JSON.stringify({expression}));\n",
        encoding="utf-8",
    )
    return json.loads(run_node(harness, json.dumps(value)))


def _notice(tmp_path: Path, detail: str):
    return _run(
        tmp_path,
        f"notice-{detail}",
        NOTICE_FUNCTIONS,
        "(() => { const card = makeEl('card');"
        "appendQuotaStatusNoticeFor(card, 'claude', input.detail, 1795276493);"
        "const box = card.children[0];"
        "return { text: box.textContent, amber: box.style.color === '#D97706' }; })()",
        {"detail": detail, "now": NOW},
    )


@needs_node
def test_a_refused_network_is_not_reported_as_an_expired_sign_in(tmp_path):
    notice = _notice(tmp_path, "network_blocked")

    # Still a call to action (the user has to fix something outside Tokdash), but about the
    # network: the sign-in works, so "open Claude Code to refresh it" cannot help.
    assert notice == {"text": "Claude Code refused this network", "amber": True}


@needs_node
def test_an_expired_sign_in_keeps_its_own_notice(tmp_path):
    assert _notice(tmp_path, "stale_token") == {"text": "Claude Code sign-in expired", "amber": True}


@needs_node
def test_an_unrecognised_failure_still_names_itself(tmp_path):
    assert _notice(tmp_path, "toString") == {"text": "toString at 3 minutes ago", "amber": False}

