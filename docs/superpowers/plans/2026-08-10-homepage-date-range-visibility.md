# Homepage Date-Range Visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the homepage's active reporting period immediately visible while keeping the existing date filtering, default range, and request behavior unchanged.

**Architecture:** Keep Flatpickr and the existing committed/pending date state as the source of truth. Add a button-style B1 presentation in front of the existing input, maintain one explicit `activeQuickRange` presentation state, and synchronize the semantic label, exact localized dates, and quick-button pressed state whenever a range is committed or the language changes.

**Tech Stack:** Static HTML/CSS/JavaScript, Flatpickr, pytest, Node.js syntax/pure-function harnesses.

## Global Constraints

- Initial load remains `Today`; this change must not alter default dates.
- Date filtering, API query parameters, apply/cancel semantics, auto-refresh, and `Last updated` meaning remain unchanged.
- The range presentation must not issue API requests; only existing committed selections may refresh dashboard data.
- Known presets display their localized semantic label; calendar-applied selections display localized `Custom range`.
- Preserve English/Chinese localization, keyboard activation, focus visibility, dark themes, and narrow-screen layouts.
- Add no runtime dependency and make no Python API, parser, cache, database, or schema change.
- Preserve the configured Git identity `Su <nostarsbutmyeyes@gmail.com>` for every commit.
- Keep `.superpowers/` untracked and unstaged.

---

### Task 1: Add deterministic date-range presentation helpers

**Files:**
- Create: `tests/test_date_range_control_frontend.py`
- Modify: `src/tokdash/static/index.html:3660-3760`

**Interfaces:**
- Consumes: local JavaScript `Date` values and `currentLang`.
- Produces: `sameLocalDate(left, right) -> boolean`, `formatDateRangeDate(date, lang = currentLang) -> string`, and `formatDateRangeTriggerText(startDate, endDate, lang = currentLang) -> string`.

- [ ] **Step 1: Write the failing formatter tests**

Create `tests/test_date_range_control_frontend.py` with the existing frontend-test import/extraction pattern and these assertions:

```python
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
def test_date_range_trigger_text_is_localized_and_deterministic(tmp_path: Path) -> None:
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
```

- [ ] **Step 2: Run the test and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_date_range_control_frontend.py
```

Expected: failure because `sameLocalDate`, `formatDateRangeDate`, and `formatDateRangeTriggerText` do not exist.

- [ ] **Step 3: Add the minimal pure helpers**

Add beside the existing date-range helpers in `src/tokdash/static/index.html`:

```javascript
    function sameLocalDate(left, right) {
      if (!left || !right) return false;
      return left.getFullYear() === right.getFullYear()
        && left.getMonth() === right.getMonth()
        && left.getDate() === right.getDate();
    }

    function formatDateRangeDate(date, lang = currentLang) {
      if (!date) return '';
      if (lang === 'zh') {
        return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日`;
      }
      const month = new Intl.DateTimeFormat('en-US', { month: 'short' }).format(date);
      return `${month} ${date.getDate()}, ${date.getFullYear()}`;
    }

    function formatDateRangeTriggerText(startDate, endDate, lang = currentLang) {
      const start = startDate || endDate;
      const end = endDate || startDate;
      if (!start || !end) return '';
      const startText = formatDateRangeDate(start, lang);
      if (sameLocalDate(start, end)) return startText;
      return `${startText} – ${formatDateRangeDate(end, lang)}`;
    }
```

- [ ] **Step 4: Run the focused tests and lint/type-check the new test**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_date_range_control_frontend.py tests/test_quick_ranges_frontend.py
.venv/bin/python -m ruff check tests/test_date_range_control_frontend.py
.venv/bin/python -m mypy tests/test_date_range_control_frontend.py
```

Expected: all commands exit 0.

- [ ] **Step 5: Commit the formatter unit**

```bash
git add src/tokdash/static/index.html tests/test_date_range_control_frontend.py
git commit -m "feat: add date range presentation helpers"
```

---

### Task 2: Add the accessible B1 range trigger and synchronized preset state

**Files:**
- Modify: `tests/test_date_range_control_frontend.py`
- Modify: `src/tokdash/static/index.html:255-405, 1031-1062, 2280-2590, 3496-3535, 3660-3940`

**Interfaces:**
- Consumes: Task 1's formatters, existing `flatpickrInstance`, `commitDateSelection`, `applyPendingDateSelection`, `getQuickRangeDates`, `applyI18n`, and translation helper `t`.
- Produces: `activeQuickRange`, `syncDateRangeControl()`, `#dateRangeTrigger`, `#dateRangePresetLabel`, `#dateRangeExactLabel`, and pressed states on `.quick-range-btn`.

- [ ] **Step 1: Add failing markup and behavior contracts**

Append these tests to `tests/test_date_range_control_frontend.py`:

```python
def test_date_range_trigger_markup_and_localization_contract() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="dateRangeTrigger"' in source
    assert 'id="dateRangePresetLabel"' in source
    assert 'id="dateRangeExactLabel"' in source
    assert 'class="date-range-calendar-icon"' in source
    assert 'class="date-range-chevron"' in source
    assert 'aria-haspopup="dialog"' in source
    assert source.count("customRange: '") == 2
    assert source.count("selectRange: '") == 2


def test_date_range_state_sync_does_not_fetch() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    sync = _extract_js_function(source, "function syncDateRangeControl() {")
    commit = _extract_js_function(source, "function commitDateSelection(startDate, endDate, options = {}) {")
    i18n = _extract_js_function(source, "function applyI18n() {")

    assert "activeQuickRange || 'customRange'" in sync
    assert "formatDateRangeTriggerText(currentStartDate, currentEndDate)" in sync
    assert "button.setAttribute('aria-pressed', String(isActive));" in sync
    assert "trigger.setAttribute('title', t('selectRange'));" in sync
    assert "fetch(" not in sync
    assert "updateDashboard" not in sync
    assert "rangeKey = null" in commit
    assert "activeQuickRange = rangeKey;" in commit
    assert "syncDateRangeControl();" in commit
    assert "syncDateRangeControl();" in i18n


def test_date_range_open_and_commit_contract() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    assert "positionElement: dateTrigger" in source
    assert "dateTrigger.addEventListener('click', () => flatpickrInstance?.open());" in source
    assert "dateTrigger.setAttribute('aria-expanded', 'true');" in source
    assert "dateTrigger.setAttribute('aria-expanded', 'false');" in source
    assert "rangeKey: range" in source
    assert "let activeQuickRange = 'today';" in source
```

- [ ] **Step 2: Run the contracts and confirm they fail**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_date_range_control_frontend.py
```

Expected: Task 1 remains green; the three integration contracts fail on missing B1 markup and state synchronization.

- [ ] **Step 3: Replace the visible input with the B1 trigger while retaining the native Flatpickr input**

Inside `.range-picker-group`, keep `#dateRangePicker` for Flatpickr but make it presentation-only, then add the visible button:

```html
            <label for="dateRangeTrigger" id="dateRangeControlLabel" class="text-xs font-semibold uppercase tracking-wider" style="color: var(--color-label);" data-i18n="range">Range</label>
            <div class="date-range-control">
              <input type="text" id="dateRangePicker" class="date-range-native-input" readonly tabindex="-1" aria-hidden="true" />
              <button id="dateRangeTrigger" class="date-range-trigger" type="button" aria-haspopup="dialog" aria-expanded="false" aria-labelledby="dateRangeControlLabel dateRangePresetLabel dateRangeExactLabel">
                <svg class="date-range-calendar-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                  <rect x="3" y="5" width="18" height="16" rx="2" stroke="currentColor" stroke-width="1.8"/>
                  <path d="M8 3v4M16 3v4M3 10h18" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
                </svg>
                <span class="date-range-trigger-copy">
                  <strong id="dateRangePresetLabel">Today</strong>
                  <span id="dateRangeExactLabel"></span>
                </span>
                <svg class="date-range-chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                  <path d="m7 10 5 5 5-5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
              </button>
            </div>
```

- [ ] **Step 4: Add responsive, theme-compatible, and focus-visible styles**

Add beside the existing topbar range styles:

```css
    .date-range-control { position: relative; min-width: min(240px, 100%); }
    .date-range-native-input { position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none; }
    .date-range-trigger { width: 100%; min-height: 48px; display: inline-flex; align-items: center; gap: 10px; padding: 7px 10px; border: 1px solid var(--color-border); border-radius: 10px; background: var(--color-surface-glass); color: var(--color-text); cursor: pointer; text-align: left; transition: border-color var(--t-fast) ease, box-shadow var(--t-fast) ease, background var(--t-fast) ease; }
    .date-range-trigger:hover { border-color: var(--color-primary); background: color-mix(in srgb, var(--color-primary) 5%, var(--color-surface-glass)); }
    .date-range-trigger:focus-visible { outline: none; border-color: var(--color-primary); box-shadow: 0 0 0 3px color-mix(in srgb, var(--color-primary) 20%, transparent); }
    .date-range-calendar-icon { flex: 0 0 auto; color: var(--color-primary); }
    .date-range-trigger-copy { flex: 1; min-width: 0; display: grid; gap: 2px; }
    .date-range-trigger-copy strong { font-size: 12px; line-height: 1.2; }
    .date-range-trigger-copy span { overflow: hidden; color: var(--color-muted); font-size: 11px; line-height: 1.2; text-overflow: ellipsis; white-space: nowrap; }
    .date-range-chevron { flex: 0 0 auto; color: var(--color-muted); transition: transform var(--t-fast) ease; }
    .date-range-trigger[aria-expanded="true"] .date-range-chevron { transform: rotate(180deg); }
    .quick-range-btn[aria-pressed="true"] { border-color: color-mix(in srgb, var(--color-primary) 58%, var(--color-border)); background: color-mix(in srgb, var(--color-primary) 12%, transparent); color: var(--color-primary); }
    @media (max-width: 640px) { .date-range-control { width: 100%; min-width: 0; } }
    @media (prefers-reduced-motion: reduce) { .date-range-trigger, .date-range-chevron { transition: none; } }
```

- [ ] **Step 5: Add translations and committed presentation state**

Add to both translation maps:

```javascript
        selectRange: 'Select range',
        customRange: 'Custom range',
```

```javascript
        selectRange: '选择日期范围',
        customRange: '自定义范围',
```

Initialize state beside the existing picker state:

```javascript
    let activeQuickRange = 'today';
```

- [ ] **Step 6: Add one synchronization function**

Add beside the Task 1 helpers:

```javascript
    function syncDateRangeControl() {
      const trigger = document.getElementById('dateRangeTrigger');
      const preset = document.getElementById('dateRangePresetLabel');
      const exact = document.getElementById('dateRangeExactLabel');
      if (!trigger || !preset || !exact) return;

      const presetKey = activeQuickRange || 'customRange';
      preset.textContent = t(presetKey);
      exact.textContent = formatDateRangeTriggerText(currentStartDate, currentEndDate);
      trigger.setAttribute('title', t('selectRange'));
      document.querySelectorAll('.quick-range-btn').forEach((button) => {
        const isActive = button.dataset.range === activeQuickRange;
        button.setAttribute('aria-pressed', String(isActive));
      });
    }
```

Call `syncDateRangeControl()` from `applyI18n()` after translating declarative elements.

- [ ] **Step 7: Wire Flatpickr, commits, and quick buttons without adding requests**

In `initDateRangePicker()`:

```javascript
      const dateTrigger = document.getElementById('dateRangeTrigger');
      if (!dateInput || !dateTrigger) return;
      dateTrigger.addEventListener('click', () => flatpickrInstance?.open());
```

Add `positionElement: dateTrigger` to Flatpickr options. In `onReady`, keep the existing Today date initialization and call `syncDateRangeControl()`. In `onOpen`, set `aria-expanded` to `true`; in every `onClose` path set it to `false` before preserving or restoring pending dates.

Extend the existing commit option destructuring:

```javascript
      const {
        closePicker = false,
        triggerUpdate = true,
        triggerPickerSync = true,
        rangeKey = null,
      } = options;
```

After committed dates are assigned:

```javascript
      activeQuickRange = rangeKey;
      syncDateRangeControl();
```

Keep calendar Apply calls unchanged so their implicit `rangeKey` is `null`. Update quick-range commits only:

```javascript
          commitDateSelection(startDate, endDate, {
            closePicker: true,
            triggerUpdate: true,
            triggerPickerSync: true,
            rangeKey: range,
          });
```

- [ ] **Step 8: Run focused frontend regressions and checks**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_date_range_control_frontend.py tests/test_quick_ranges_frontend.py tests/test_profile_stats_frontend.py tests/test_api_smoke.py
.venv/bin/python -m ruff check tests/test_date_range_control_frontend.py
.venv/bin/python -m mypy tests/test_date_range_control_frontend.py
.venv/bin/python -m compileall -q src tests/test_date_range_control_frontend.py
```

Expected: all commands exit 0, and the existing Node-backed main inline-script syntax test is not skipped when Node is installed.

- [ ] **Step 9: Commit the complete interaction**

```bash
git add src/tokdash/static/index.html tests/test_date_range_control_frontend.py
git commit -m "feat: clarify the active homepage date range"
```

---

### Task 3: Document and visually verify the change

**Files:**
- Modify: `docs/development/CHANGELOG.md`
- Verify: `src/tokdash/static/index.html`
- Verify: `tests/test_date_range_control_frontend.py`

**Interfaces:**
- Consumes: the completed B1 range trigger and existing local dashboard.
- Produces: changelog documentation and PR verification evidence for issue #16.

- [ ] **Step 1: Add the changelog entry**

Under `## Unreleased` → `### Added`, add:

```markdown
- Made the active homepage date range immediately visible with a localized, keyboard-accessible range trigger and synchronized quick-range selection, without changing date filtering or refresh behavior.
```

- [ ] **Step 2: Run the full suite and static checks**

Run:

```bash
TZ=UTC .venv/bin/python -m pytest -q
.venv/bin/python -m ruff check tests/test_date_range_control_frontend.py
.venv/bin/python -m mypy tests/test_date_range_control_frontend.py
.venv/bin/python -m compileall -q src tests/test_date_range_control_frontend.py
git diff --check origin/main...HEAD
```

Expected: all commands exit 0. Report any existing unrelated type-check or timezone-sensitive failure with its exact command and diagnostic instead of hiding it.

- [ ] **Step 3: Visually verify in one reused browser tab**

Start one local server and create one test tab only. Reuse it for every state and close it when finished. Verify:

1. Initial state reads `Today` plus today's localized exact date and marks only Today pressed.
2. The calendar icon, chevron, hover style, keyboard focus ring, and expanded state make the trigger visibly interactive.
3. Last Week updates the semantic label, exact Monday–Sunday dates, and pressed state together.
4. A calendar-applied range reads `Custom range`, shows exact dates, and clears all quick pressed states.
5. Opening and cancelling the picker preserves the committed label and dates.
6. Switching English/Chinese rerenders both semantic and exact-date labels without fetching.
7. At 720 × 900, the trigger remains contained and the page has no horizontal overflow; reset the viewport afterward.
8. Network inspection confirms presentation synchronization adds no request and range commit retains the existing single dashboard refresh.

- [ ] **Step 4: Audit compatibility**

Confirm from the final diff:

- no API, parser, cache, database, or schema file changed;
- default Today and all `getQuickRangeDates()` results remain unchanged;
- `commitDateSelection()` remains the only range commit path;
- `Last updated` still represents response freshness;
- existing Apply/cancel and auto-refresh behavior remains unchanged;
- `.superpowers/` remains untracked and unstaged.

- [ ] **Step 5: Commit documentation**

```bash
git add docs/development/CHANGELOG.md
git commit -m "docs: describe homepage date range clarity"
```

- [ ] **Step 6: Push and create the PR**

Push `feat/homepage-date-range-visibility` to the user's fork and create a PR against `JingbiaoMei/Tokdash:main` with:

```markdown
## Summary
- Makes the active homepage reporting period immediately visible.
- Adds a keyboard-accessible calendar trigger with semantic and exact date labels.
- Synchronizes known quick-range pressed states and marks calendar selections as custom.

## Compatibility
- Default Today, date calculations, API requests, filtering, Apply/cancel semantics, auto-refresh, and Last updated behavior are unchanged.

## Verification
- `TZ=UTC .venv/bin/python -m pytest -q`
- `.venv/bin/python -m ruff check tests/test_date_range_control_frontend.py`
- `.venv/bin/python -m mypy tests/test_date_range_control_frontend.py`
- `.venv/bin/python -m compileall -q src tests/test_date_range_control_frontend.py`
- Desktop and 720px responsive behavior verified in one reused browser tab.

Closes #16
```
