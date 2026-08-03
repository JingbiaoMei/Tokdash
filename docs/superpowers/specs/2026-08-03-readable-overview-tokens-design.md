# Readable Overview Token Display

**Date:** 2026-08-03
**Status:** Approved design
**Scope:** Overview Total Tokens KPI only

## Summary

Add a `Readable tokens` switch to the Overview toolbar. The switch changes only
the presentation of the Overview `Total Tokens` KPI for the currently selected
date range. It does not change token aggregation, filtering, API responses, or
any token values elsewhere in Tokdash.

Readable mode is enabled by default and remembered locally. It uses an adaptive
unit: millions display as `M`, billions display as `B`, and values below one
million remain exact. When readable mode is active, hovering or keyboard-focusing
the displayed value reveals the localized exact token count.

## User Experience

### Placement

The switch sits in the Overview toolbar beside the existing date-range and
refresh controls:

```text
Overview                         Last 30 Days  Readable tokens [on]  Refresh

Total Tokens
482.6M tokens
```

The toolbar placement was chosen because the switch is a display preference for
the current Overview result, not a second metric inside the KPI card. On narrow
screens the controls may wrap using the toolbar's existing responsive behavior;
the switch must not create page-level horizontal overflow.

### Formatting rules

Readable mode uses decimal token units and at most one fractional digit:

| Raw value | Readable value |
| ---: | --- |
| `842315` | `842,315 tokens` |
| `1000000` | `1M tokens` |
| `482563219` | `482.6M tokens` |
| `999999999` | `1,000M tokens` |
| `1000000000` | `1B tokens` |
| `1249000000` | `1.2B tokens` |

Trailing `.0` is omitted. Rounding does not promote a value into the next unit:
a raw value below one billion remains an `M` value even if one-decimal rounding
produces `1,000M`.

When the switch is off, the KPI preserves today's localized exact-number format,
for example `482,563,219`. The `Total Tokens` label already supplies the unit in
exact mode.

### Exact-value disclosure

In readable mode, the compact value owns a themed tooltip containing the exact
localized value, for example `482,563,219 tokens`. The tooltip is hidden by
default and appears only while the value is hovered or keyboard-focused. It must
remain within the viewport at normal Overview widths.

The exact count is also exposed through an accessible label so the information
does not depend on pointer hover. When readable mode is off, the already-exact
display does not show the redundant tooltip.

### Persistence and localization

- Default state: readable mode enabled.
- Persistence: a dedicated local-storage preference records the user's choice.
- Storage failure: fall back to readable mode without blocking Overview render.
- English label: `Readable tokens`.
- Chinese label: `易读 Token`.
- Tooltip unit uses the existing localized token-unit copy where practical.

## Alternatives Considered

### A. Switch inside the Total Tokens card

This makes the scope immediately obvious and minimizes toolbar work, but adds a
control to an otherwise read-only KPI and competes with the value at narrow card
widths.

### B. Switch in the Overview toolbar — selected

This treats readable formatting as a display preference, keeps the KPI clean,
and leaves enough space for an explicit label. Its scope is intentionally limited
to the Overview Total Tokens KPI even though its placement is global-looking.

### C. Readable/Exact segmented control below the value

This makes both states explicit but increases card height and gives a minor
formatting preference too much visual weight.

## Architecture and Data Flow

No backend or schema change is required.

1. Overview continues to receive `total_tokens` from the existing usage response
   for the active date filter.
2. A pure frontend formatter converts that raw number into the readable value.
3. The Overview renderer chooses readable or exact output from the persisted
   switch state.
4. Changing the switch rerenders the KPI from the already-loaded raw value; it
   does not issue another API request.
5. Changing the date range or refreshing data naturally supplies a new raw value,
   which is formatted using the current switch state.

The existing exact token number remains the source of truth. The compact string
is presentation-only and must never be used for calculations, sorting, deltas,
or subsequent formatting.

## Component Boundaries

### Readable token formatter

A small pure function accepts a raw token count and returns the display string.
It owns the `<1M`, `M`, and `B` thresholds, one-decimal rounding, and removal of
trailing `.0`.

### Overview preference control

The toolbar switch owns preference loading, persistence, `role="switch"`, and
`aria-checked`. It only requests a rerender of the existing Total Tokens value.

### Exact-value tooltip

The Total Tokens value owns its exact localized text and tooltip state. Tooltip
content must be assigned with DOM text APIs or attributes, never untrusted HTML.

## Error Handling

- Missing, non-finite, or negative token totals follow existing Overview fallback
  behavior and render as zero rather than producing `NaN` or an invalid unit.
- Local-storage read/write errors are ignored after choosing the safe default.
- A failed usage request keeps the existing Overview loading/error handling; the
  switch introduces no independent request or error state.
- The formatter must not mutate the raw value or the response object.

## Testing

Automated checks cover:

- formatter boundaries immediately below and at `1M` and `1B`;
- one-decimal rounding and removal of `.0`;
- values that round to `1,000M` without premature promotion to `B`;
- exact-mode preservation of the current localized number;
- default-on state, persistence, and storage-failure fallback;
- switch accessibility and English/Chinese labels;
- hover/focus exact-value disclosure only in readable mode;
- no extra Overview API request when toggling;
- date-range changes reformat the newly filtered total;
- narrow-screen wrapping without page-level overflow;
- existing Overview KPI fitting, delta, date-filter, and refresh contracts.

Visual verification will reuse one Tokdash browser tab for all desktop and
responsive checks. The test viewport will be changed in that tab and reset at
the end; internal test tabs will be closed rather than opening repeated Tokdash
windows.

## Compatibility and Behavior Changes

- Behavior change: the Overview Total Tokens KPI defaults to readable compact
  output instead of the current exact integer.
- Users can restore the previous exact display with the toolbar switch, and that
  choice is remembered.
- Overview token deltas, costs, messages, tables, charts, Profile, Activity
  Insights, Sessions, heatmaps, tooltips outside this KPI, and API responses keep
  their current behavior.
- No database migration, history reparse, or additional network request occurs.
