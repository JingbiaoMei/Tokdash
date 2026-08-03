# Readable Overview Token Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persisted Overview toolbar switch that defaults the filtered Total Tokens KPI to an adaptive `M`/`B` display and reveals the exact count only on hover or keyboard focus.

**Architecture:** Keep the existing `/api/usage` response and date filtering unchanged. Add pure formatting and preference helpers to the existing inline frontend, then wire one Overview-only toolbar switch and one themed exact-value tooltip to the existing `renderOverviewTab()` path. The raw token integer remains the only calculation source; toggling is a local rerender with no network request.

**Tech Stack:** Static HTML/CSS/JavaScript in `src/tokdash/static/index.html`, Python `pytest` contract tests with Node.js harnesses, browser visual verification.

## Global Constraints

- The switch changes only the Overview `Total Tokens` KPI; Profile, Activity Insights, Sessions, charts, tables, deltas, heatmaps, and API responses remain unchanged.
- Readable mode is enabled by default and persisted locally under `tokdash-overview-readable-tokens`.
- Values below `1_000_000` remain exact; values from `1_000_000` through `999_999_999` use `M`; values at or above `1_000_000_000` use `B`.
- Readable values use decimal units, at most one fractional digit, and no trailing `.0`.
- Rounding never changes the selected unit: `999_999_999` renders as `1,000M tokens`, not `1B tokens`.
- The exact localized value appears only while the readable value is hovered or keyboard-focused.
- Toggling never fetches data; the already-loaded raw total is rerendered.
- Local-storage failures fall back to readable mode and never block Overview.
- The control is visible only while Overview is active and must not cause page-level overflow.
- English copy is `Readable tokens`; Chinese copy is `易读 Token`.
- Reuse one browser tab for desktop and responsive validation. Do not open repeated Tokdash windows; reset the viewport and close the internal test tab when finished.
- Preserve the untracked `.superpowers/` directory and do not stage it.

---

## File Structure

- Create `tests/test_readable_tokens_frontend.py`: isolated formatter, persistence, markup, integration, localization, accessibility, and no-refetch contracts.
- Modify `src/tokdash/static/index.html`: pure helpers, persisted state, Overview-only switch, tooltip markup/styles, translations, rendering, and event wiring.
- Modify `docs/development/CHANGELOG.md`: concise user-facing behavior entry.

---

### Task 1: Add Pure Readable-Token Formatting and Preference Semantics

**Files:**
- Create: `tests/test_readable_tokens_frontend.py`
- Modify: `src/tokdash/static/index.html:2808-2813`

**Interfaces:**
- Consumes: existing `formatNumber(num)` and `t(key)` frontend helpers.
- Produces: `normalizeOverviewTokenCount(value) -> number`, `formatReadableTokenCount(value) -> string`, `loadOverviewReadableTokensPreference(storage) -> boolean`, `saveOverviewReadableTokensPreference(enabled, storage) -> void`, and `OVERVIEW_READABLE_TOKENS_STORAGE_KEY`.

- [ ] **Step 1: Add the failing formatter and preference tests**

Create `tests/test_readable_tokens_frontend.py` with the complete harness and boundary cases:

```python
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import tokdash


INDEX_HTML = Path(tokdash.__file__).parent / "static" / "index.html"


def _extract_js_function(source: str, signature: str) -> str:
    start = source.find(signature)
    assert start != -1, f"{signature} not found in index.html"
    depth = 0
    body_start = start + len(signature) - 1 if signature.endswith("{") else source.find("{", start)
    for index in range(body_start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {signature}")


def _run_readable_token_js(tmp_path: Path, expression: str, payload: object) -> object:
    source = INDEX_HTML.read_text(encoding="utf-8")
    functions = "\n".join(
        _extract_js_function(source, signature)
        for signature in (
            "function normalizeOverviewTokenCount(value) {",
            "function formatReadableTokenCount(value) {",
            "function loadOverviewReadableTokensPreference(storage = null) {",
            "function saveOverviewReadableTokensPreference(enabled, storage = null) {",
        )
    )
    key_match = "const OVERVIEW_READABLE_TOKENS_STORAGE_KEY = 'tokdash-overview-readable-tokens';"
    assert key_match in source
    harness = tmp_path / "readable-tokens.js"
    harness.write_text(
        "let currentLang = 'en';\n"
        "const LABELS = { tokensUnit: 'tokens' };\n"
        "function t(key) { return LABELS[key] || key; }\n"
        "function formatNumber(value) { return Number(value || 0).toLocaleString('en-US'); }\n"
        + key_match
        + "\n"
        + functions
        + "\nconst payload = JSON.parse(process.argv[2]);\n"
        + f"const result = {expression};\n"
        + "process.stdout.write(JSON.stringify(result));\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(harness), json.dumps(payload)],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_readable_token_formatter_boundaries(tmp_path: Path) -> None:
    cases = {
        0: "0 tokens",
        -1: "0 tokens",
        842_315: "842,315 tokens",
        999_999: "999,999 tokens",
        1_000_000: "1M tokens",
        1_049_999: "1M tokens",
        1_050_000: "1.1M tokens",
        482_563_219: "482.6M tokens",
        999_999_999: "1,000M tokens",
        1_000_000_000: "1B tokens",
        1_249_000_000: "1.2B tokens",
    }
    result = _run_readable_token_js(
        tmp_path,
        "Object.fromEntries(payload.map(value => [String(value), formatReadableTokenCount(value)]))",
        list(cases),
    )
    assert result == {str(key): value for key, value in cases.items()}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_readable_token_preference_defaults_and_fails_soft(tmp_path: Path) -> None:
    expression = """
(() => {
  const writes = [];
  const missing = { getItem: () => null, setItem: (key, value) => writes.push([key, value]) };
  const disabled = { getItem: () => '0', setItem: (key, value) => writes.push([key, value]) };
  const broken = { getItem: () => { throw new Error('blocked'); }, setItem: () => { throw new Error('blocked'); } };
  saveOverviewReadableTokensPreference(false, missing);
  saveOverviewReadableTokensPreference(true, missing);
  saveOverviewReadableTokensPreference(true, broken);
  return {
    missing: loadOverviewReadableTokensPreference(missing),
    disabled: loadOverviewReadableTokensPreference(disabled),
    broken: loadOverviewReadableTokensPreference(broken),
    writes,
  };
})()
"""
    assert _run_readable_token_js(tmp_path, expression, None) == {
        "missing": True,
        "disabled": False,
        "broken": True,
        "writes": [
            ["tokdash-overview-readable-tokens", "0"],
            ["tokdash-overview-readable-tokens", "1"],
        ],
    }
```

- [ ] **Step 2: Run the new tests and confirm the expected failure**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_readable_tokens_frontend.py
```

Expected: both tests fail because `normalizeOverviewTokenCount`, `formatReadableTokenCount`, and the preference helpers are absent.

- [ ] **Step 3: Add the minimal pure helpers**

In `src/tokdash/static/index.html`, immediately after `formatNumber(num)`, add:

```javascript
    const OVERVIEW_READABLE_TOKENS_STORAGE_KEY = 'tokdash-overview-readable-tokens';

    function normalizeOverviewTokenCount(value) {
      const number = Number(value);
      return Number.isFinite(number) && number > 0 ? Math.round(number) : 0;
    }

    function formatReadableTokenCount(value) {
      const tokens = normalizeOverviewTokenCount(value);
      if (tokens < 1_000_000) return `${formatNumber(tokens)} ${t('tokensUnit')}`;
      const useBillions = tokens >= 1_000_000_000;
      const divisor = useBillions ? 1_000_000_000 : 1_000_000;
      const suffix = useBillions ? 'B' : 'M';
      const rounded = Math.round((tokens / divisor) * 10) / 10;
      const locale = currentLang === 'zh' ? 'zh-CN' : 'en-US';
      const compact = rounded.toLocaleString(locale, {
        minimumFractionDigits: 0,
        maximumFractionDigits: 1,
      });
      return `${compact}${suffix} ${t('tokensUnit')}`;
    }

    function loadOverviewReadableTokensPreference(storage = null) {
      try {
        const target = storage || window.localStorage;
        return target.getItem(OVERVIEW_READABLE_TOKENS_STORAGE_KEY) !== '0';
      } catch (_error) {
        return true;
      }
    }

    function saveOverviewReadableTokensPreference(enabled, storage = null) {
      try {
        const target = storage || window.localStorage;
        target.setItem(OVERVIEW_READABLE_TOKENS_STORAGE_KEY, enabled ? '1' : '0');
      } catch (_error) {
        // The display preference is optional; private/blocked storage stays default-on.
      }
    }
```

- [ ] **Step 4: Run the focused tests and lint the new Python file**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_readable_tokens_frontend.py
.venv/bin/python -m ruff check tests/test_readable_tokens_frontend.py
```

Expected: both tests pass and Ruff reports `All checks passed!`.

- [ ] **Step 5: Commit the pure formatting unit**

```bash
git add src/tokdash/static/index.html tests/test_readable_tokens_frontend.py
git commit -m "feat: add readable Overview token formatting"
```

---

### Task 2: Add the Overview-Only Switch and Exact-Value Tooltip

**Files:**
- Modify: `tests/test_readable_tokens_frontend.py`
- Modify: `src/tokdash/static/index.html:187-420, 1034-1068, 1091-1098, 2340-2355, 2616-2630, 3280-3335, 3686-3705, 6050-6070, 8500-8545`

**Interfaces:**
- Consumes: Task 1's formatter and preference helpers plus existing `lastUsageResponse`, `formatNumber`, `fitKpiValue`, `applyI18n`, and `activateDashboardTab` behavior.
- Produces: `overviewReadableTokens`, `overviewTotalTokensRaw`, `renderOverviewTokenTotal(value)`, `syncOverviewReadableTokensToggle()`, `setOverviewReadableTokens(enabled)`, `#readableTokensToggle`, `#totalTokensWrap`, and `#totalTokensExact`.

- [ ] **Step 1: Add failing markup and integration contracts**

Append these tests to `tests/test_readable_tokens_frontend.py`:

```python
def test_readable_token_switch_markup_and_tooltip_contract() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    compact = "".join(source.split())
    assert 'id="readableTokensToggle"' in source
    assert 'role="switch"' in source
    assert 'aria-checked="true"' in source
    assert 'id="totalTokensWrap"' in source
    assert 'id="totalTokensExact"' in source
    assert 'role="tooltip"' in source
    assert ".overview-readable-tokens-toggle[hidden]{display:none;}" in compact
    assert "#totalTokens:hover+.overview-token-exact-tooltip" in compact
    assert "#totalTokens:focus-visible+.overview-token-exact-tooltip" in compact
    assert source.count("readableTokens: '") == 2


def test_readable_token_render_and_toggle_do_not_refetch() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    renderer = _extract_js_function(source, "function renderOverviewTokenTotal(value = overviewTotalTokensRaw) {")
    setter = _extract_js_function(source, "function setOverviewReadableTokens(enabled) {")
    tab_activation = _extract_js_function(source, "function activateDashboardTab(tab) {")
    overview = _extract_js_function(source, "function renderOverviewTab(data) {")
    i18n = _extract_js_function(source, "function applyI18n() {")

    assert "overviewTotalTokensRaw = normalizeOverviewTokenCount(value);" in renderer
    assert "formatReadableTokenCount(overviewTotalTokensRaw)" in renderer
    assert "formatNumber(overviewTotalTokensRaw)" in renderer
    assert "totalTokensExact" in renderer
    assert "aria-describedby" in renderer
    assert "saveOverviewReadableTokensPreference" in setter
    assert "renderOverviewTokenTotal();" in setter
    assert "fetch(" not in setter
    assert "updateDashboard" not in setter
    assert "toggle.hidden = tab !== 'overview';" in tab_activation
    assert "renderOverviewTokenTotal(data.total_tokens);" in overview
    assert "renderOverviewTokenTotal();" in i18n


def test_readable_token_scope_preserves_other_token_views() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    assert source.count("formatReadableTokenCount(") == 2
    assert "document.getElementById('totalTokens').textContent = formatNumber(data.total_tokens);" not in source
    for existing in (
        "document.getElementById('statTotalTokens').textContent = formatNumber",
        "document.getElementById('monthTotalTokens').textContent = formatNumber",
        "document.getElementById('sessionModalTotal').textContent = formatNumber",
        "formatProfileMetricNumber(summary.recordedTokens",
    ):
        assert existing in source
```

- [ ] **Step 2: Run the contracts and confirm they fail**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_readable_tokens_frontend.py
```

Expected: the Task 1 tests pass; the three new tests fail on missing switch, tooltip, and renderer contracts.

- [ ] **Step 3: Add switch and tooltip styles**

Near the existing `.topbar-actions` and KPI styles in `src/tokdash/static/index.html`, add:

```css
    .overview-readable-tokens-toggle{display:inline-flex;align-items:center;gap:7px;cursor:pointer;}
    .overview-readable-tokens-toggle[hidden]{display:none;}
    .overview-readable-tokens-track{position:relative;width:28px;height:16px;border-radius:999px;background:var(--color-border);transition:background var(--t-fast) ease;}
    .overview-readable-tokens-track::after{content:"";position:absolute;top:2px;left:2px;width:12px;height:12px;border-radius:50%;background:var(--color-bg);box-shadow:0 1px 4px rgba(15,23,42,.22);transition:transform var(--t-fast) ease;}
    .overview-readable-tokens-toggle[aria-checked="true"] .overview-readable-tokens-track{background:var(--color-primary);}
    .overview-readable-tokens-toggle[aria-checked="true"] .overview-readable-tokens-track::after{transform:translateX(12px);}
    .overview-token-value-wrap{position:relative;width:max-content;max-width:100%;}
    .overview-token-exact-tooltip{position:absolute;z-index:70;left:0;bottom:calc(100% + 8px);padding:6px 8px;border-radius:7px;background:var(--color-text);color:var(--color-bg);font-size:10px;font-weight:750;line-height:1.2;white-space:nowrap;opacity:0;visibility:hidden;pointer-events:none;transform:translateY(3px);transition:opacity var(--t-fast) ease,transform var(--t-fast) ease,visibility var(--t-fast) ease;box-shadow:0 8px 22px rgba(15,23,42,.22);}
    .overview-token-exact-tooltip::after{content:"";position:absolute;left:18px;top:100%;border:4px solid transparent;border-top-color:var(--color-text);}
    #totalTokens:hover+.overview-token-exact-tooltip,#totalTokens:focus-visible+.overview-token-exact-tooltip{opacity:1;visibility:visible;transform:translateY(0);}
    @media(max-width:640px){.overview-readable-tokens-toggle{max-width:100%;}.overview-token-exact-tooltip{max-width:min(260px,calc(100vw - 40px));overflow:hidden;text-overflow:ellipsis;}}
    @media(prefers-reduced-motion:reduce){.overview-readable-tokens-track,.overview-readable-tokens-track::after,.overview-token-exact-tooltip{transition:none;}}
```

- [ ] **Step 4: Add the toolbar switch and KPI tooltip markup**

Inside `.topbar-actions`, immediately before `#refreshBtn`, add:

```html
            <button id="readableTokensToggle" class="btn btn-ghost compact-control overview-readable-tokens-toggle" type="button" role="switch" aria-checked="true" aria-label="Readable tokens">
              <span data-i18n="readableTokens">Readable tokens</span>
              <span class="overview-readable-tokens-track" aria-hidden="true"></span>
            </button>
```

Replace only the Overview Total Tokens value element with:

```html
          <div id="totalTokensWrap" class="overview-token-value-wrap" data-readable="true">
            <div id="totalTokens" class="text-3xl sm:text-4xl font-extrabold mt-2" style="color: var(--color-primary);" tabindex="0" aria-describedby="totalTokensExact">-</div>
            <div id="totalTokensExact" class="overview-token-exact-tooltip" role="tooltip">-</div>
          </div>
```

- [ ] **Step 5: Add translations and initialized state**

Add `readableTokens` beside `totalTokens` in both translation maps:

```javascript
        readableTokens: 'Readable tokens',
```

```javascript
        readableTokens: '易读 Token',
```

After the Task 1 helpers, initialize the state:

```javascript
    let overviewReadableTokens = loadOverviewReadableTokensPreference();
    let overviewTotalTokensRaw = null;
```

- [ ] **Step 6: Add the renderer and switch synchronization**

Immediately before `fitOverviewKpis()`, add:

```javascript
    function renderOverviewTokenTotal(value = overviewTotalTokensRaw) {
      const valueElement = document.getElementById('totalTokens');
      const wrapper = document.getElementById('totalTokensWrap');
      const tooltip = document.getElementById('totalTokensExact');
      if (!valueElement || !wrapper || !tooltip) return;
      if (value === null) {
        valueElement.textContent = '-';
        valueElement.removeAttribute('tabindex');
        valueElement.removeAttribute('aria-describedby');
        valueElement.removeAttribute('aria-label');
        tooltip.textContent = '-';
        tooltip.hidden = true;
        return;
      }
      overviewTotalTokensRaw = normalizeOverviewTokenCount(value);
      const exact = `${formatNumber(overviewTotalTokensRaw)} ${t('tokensUnit')}`;
      valueElement.textContent = overviewReadableTokens
        ? formatReadableTokenCount(overviewTotalTokensRaw)
        : formatNumber(overviewTotalTokensRaw);
      wrapper.dataset.readable = String(overviewReadableTokens);
      tooltip.textContent = exact;
      tooltip.hidden = !overviewReadableTokens;
      if (overviewReadableTokens) {
        valueElement.tabIndex = 0;
        valueElement.setAttribute('aria-describedby', 'totalTokensExact');
        valueElement.setAttribute('aria-label', exact);
      } else {
        valueElement.removeAttribute('tabindex');
        valueElement.removeAttribute('aria-describedby');
        valueElement.removeAttribute('aria-label');
      }
      fitKpiValue(valueElement);
    }

    function syncOverviewReadableTokensToggle() {
      const toggle = document.getElementById('readableTokensToggle');
      if (!toggle) return;
      toggle.setAttribute('aria-checked', String(overviewReadableTokens));
      toggle.setAttribute('aria-label', t('readableTokens'));
    }

    function setOverviewReadableTokens(enabled) {
      overviewReadableTokens = Boolean(enabled);
      saveOverviewReadableTokensPreference(overviewReadableTokens);
      syncOverviewReadableTokensToggle();
      renderOverviewTokenTotal();
    }
```

In `renderOverviewTab(data)`, replace the direct Total Tokens assignment with:

```javascript
      renderOverviewTokenTotal(data.total_tokens);
```

Keep `fitOverviewKpis()` in place so cost/messages and the compact token value retain existing overflow protection.

- [ ] **Step 7: Wire localization, tab visibility, and click behavior**

In `applyI18n()`, before `renderActivityInsights()`, add:

```javascript
      syncOverviewReadableTokensToggle();
      renderOverviewTokenTotal();
```

In `activateDashboardTab(tab)`, immediately after activating the content, add:

```javascript
      const toggle = document.getElementById('readableTokensToggle');
      if (toggle) toggle.hidden = tab !== 'overview';
```

Near the existing `#refreshBtn` listener, add:

```javascript
    document.getElementById('readableTokensToggle')?.addEventListener('click', () => {
      setOverviewReadableTokens(!overviewReadableTokens);
    });
```

This listener must not call `updateDashboard`, `updateDashboardByDateRange`, or `fetch`.

- [ ] **Step 8: Run focused frontend regressions**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_readable_tokens_frontend.py tests/test_profile_stats_frontend.py tests/test_quick_ranges_frontend.py tests/test_api_smoke.py
.venv/bin/python -m ruff check tests/test_readable_tokens_frontend.py
```

Expected: all tests pass, including the existing main-inline-script Node syntax check, date-range contracts, Overview Profile behavior, and API smoke suite.

- [ ] **Step 9: Commit the complete UI behavior**

```bash
git add src/tokdash/static/index.html tests/test_readable_tokens_frontend.py
git commit -m "feat: add Overview readable token switch"
```

---

### Task 3: Document, Visually Verify, and Run Final Regression Checks

**Files:**
- Modify: `docs/development/CHANGELOG.md`
- Verify: `src/tokdash/static/index.html`
- Verify: `tests/test_readable_tokens_frontend.py`

**Interfaces:**
- Consumes: the completed formatter, switch, renderer, tooltip, and tests from Tasks 1–2.
- Produces: user-facing changelog text and final PR verification evidence.

- [ ] **Step 1: Add the changelog entry**

Under `## Unreleased` → `### Added` in `docs/development/CHANGELOG.md`, add:

```markdown
- Added a persisted `Readable tokens` switch to Overview. Filtered totals use adaptive M/B units by default, while hover or keyboard focus reveals the exact localized count; disabling the switch restores the previous exact-number display.
```

- [ ] **Step 2: Run the feature and adjacent frontend suite**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_readable_tokens_frontend.py tests/test_profile_stats_frontend.py tests/test_quick_ranges_frontend.py tests/test_api_smoke.py
```

Expected: exit code 0. The Node-backed inline-script syntax test must not be skipped when `node` is available.

- [ ] **Step 3: Run lint, compilation, and the relevant type check**

Run:

```bash
.venv/bin/python -m ruff check tests/test_readable_tokens_frontend.py
.venv/bin/python -m compileall -q src tests/test_readable_tokens_frontend.py
.venv/bin/python -m mypy tests/test_readable_tokens_frontend.py
```

Expected: Ruff, compilation, and mypy exit 0. Also confirm the repository still has no configured JavaScript/TypeScript type-check command:

```bash
rg -n "typescript|tsc|jsconfig|typecheck|type-check" pyproject.toml .github scripts
```

Expected: no configured JS/TS type-check command. Report this limitation rather than claiming a JavaScript type checker ran; the inline script is syntax-checked through Node-backed pytest.

- [ ] **Step 4: Run the full repository suite in the stable test timezone**

Run:

```bash
TZ=UTC .venv/bin/python -m pytest -q
```

Expected: the current baseline is `756 passed, 3 skipped` before adding these tests; the final pass count increases by the new readable-token tests. If the default Asia/Hong_Kong environment is also checked, `tests/test_period_semantics.py::test_previous_period_range_month_uses_full_previous_calendar_month` is the known unrelated timezone-sensitive failure and must be reported exactly if it remains.

- [ ] **Step 5: Start one synthetic local preview without scanning user history**

Start a single server once and keep its session alive for the complete visual check:

```bash
TOKDASH_WARM_ON_START=0 TOKDASH_USAGE_DB=0 .venv/bin/python -c '
from tokdash import api
import uvicorn

api.compute_usage_with_comparison = lambda *_args, **_kwargs: {
    "total_tokens": 482_563_219,
    "total_cost": 128.40,
    "total_messages": 1_046,
    "cache_hit_rate": 0.52,
    "top_models": [],
    "by_tool": {},
    "combined_models": [],
    "apps": {},
    "openclaw_models": [],
    "comparison": {},
    "timestamp": "2026-08-03T12:00:00+00:00",
}
api.compute_stats = lambda *_args, **_kwargs: {"contributions": []}
api.get_codex_activity_insights = lambda: {
    "recorded_chats": {"value": 0, "coverage": {}},
    "reasoning": {"most_used": None, "distribution": [], "coverage": {}},
    "tools": {"total_calls": 0, "most_used": None, "distribution": [], "coverage": {}},
}
uvicorn.run(api.app, host="127.0.0.1", port=55427, log_level="warning")
'
```

Expected: one local server on `http://127.0.0.1:55427/`, with no usage database writes and no scan of the user's Codex history.

- [ ] **Step 6: Visually verify in exactly one browser tab**

Use the browser-control skill and keep one tab reference for the whole check. Do not call tab creation a second time.

Verify in that same tab:

1. Default value is `482.6M tokens` and the switch is on.
2. The exact `482,563,219 tokens` tooltip is absent at rest.
3. Hovering the Token value alone reveals the exact tooltip.
4. Keyboard focus reveals the same tooltip and the value has an accessible exact label.
5. Clicking the switch changes the value to `482,563,219` without any new `/api/usage` request.
6. Reloading preserves the off state; re-enable it before finishing.
7. Switching to Sessions hides the control; returning to Overview restores it.
8. Change the existing tab viewport to `720 × 900`; confirm wrapped controls, contained tooltip, and no page-level horizontal overflow.
9. Reset the viewport, keep no test tab, stop the synthetic server, and do not open another Tokdash window.

- [ ] **Step 7: Audit compatibility from the final diff**

Run:

```bash
git diff origin/main...HEAD -- src/tokdash/static/index.html tests/test_readable_tokens_frontend.py docs/development/CHANGELOG.md
git diff --check origin/main...HEAD
git status --short
```

Confirm:

- no Python API, session parser, database, or schema file changed for this display feature;
- `renderOverviewTab()` still reads the filtered `data.total_tokens` value;
- the toggle path contains no fetch or dashboard update;
- exact raw tokens remain used for deltas and all calculations;
- other token views still use their existing formatters;
- `.superpowers/` remains untracked and unstaged.

- [ ] **Step 8: Commit documentation**

```bash
git add docs/development/CHANGELOG.md
git commit -m "docs: describe readable Overview tokens"
```

- [ ] **Step 9: Prepare the PR update but do not push without confirmation**

Use this verification summary:

```markdown
## What changed
- Added an Overview-only `Readable tokens` switch, enabled by default and persisted locally.
- Added adaptive exact/M/B formatting for the currently filtered Total Tokens KPI.
- Added a hover/focus tooltip for the exact localized count without additional API requests.

## Compatibility
- Token aggregation, date filtering, API responses, deltas, Profile, Activity Insights, Sessions, tables, charts, and heatmaps are unchanged.
- Turning the switch off restores the previous exact-number display.

## Verification
- `.venv/bin/python -m pytest -q tests/test_readable_tokens_frontend.py tests/test_profile_stats_frontend.py tests/test_quick_ranges_frontend.py tests/test_api_smoke.py`
- `TZ=UTC .venv/bin/python -m pytest -q`
- `.venv/bin/python -m ruff check tests/test_readable_tokens_frontend.py`
- `.venv/bin/python -m mypy tests/test_readable_tokens_frontend.py`
- `.venv/bin/python -m compileall -q src tests/test_readable_tokens_frontend.py`
- Desktop and 720px responsive behavior verified in one reused browser tab.
```
