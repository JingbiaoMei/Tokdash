# Kimi handoff: Tokdash Companion HTML concept

## Status

Historical note: the first in-session attempt with the exact custom `kimi` Codex agent on
2026-07-25 failed before starting because its configured provider rejected the `reasoning`
parameter:

```text
InvalidParameter: reasoning is not supported by current model
```

No fallback agent was used in that attempt. Kimi subsequently produced
[`UI_CONCEPT.html`](UI_CONCEPT.html) through Howard's separate workflow. Howard is requesting
another Kimi iteration. Codex must keep the documents current but perform no companion
implementation until that iteration is approved.

## Prompt

```text
You are working in `/mnt/h/Developing/Agent/Tokdash_Project/tokdash`.

You own only
`docs/local/20260725_companion_app/UI_CONCEPT.html` for this task. You are not alone in
the codebase: do not revert or overwrite other agents' changes, and adapt if the directory
or adjacent files appear while you work.

Create a polished, self-contained HTML/CSS/JS design prototype for Howard to review before
we build native Tokdash companion apps. The product is a lightweight menu-bar app on macOS
and notification-area app on Windows that reads Tokdash's existing local HTTP API.

Read these files first:

- `docs/local/20260725_companion_app/COMPANION_APP_PLAN.md`
- `docs/local/20260725_companion_app/UI_CONTENT_SPEC.md`
- `src/tokdash/api.py`
- relevant response rendering in `src/tokdash/static/index.html`

Use the real API terminology and realistic fake data.

Requirements:

- Produce one local HTML file with no build step and no required network assets.
- Show two deliberately platform-native variants, selectable in the page:
  - macOS menu-bar popover following Apple's current translucent/Liquid Glass direction;
  - Windows notification-area flyout following current Microsoft Fluent principles.
- Do not make the two variants skins of one generic SaaS card.
- Include the tiny closed menu-bar/notification-area presence and the open
  popover/flyout in realistic desktop context.
- Implement two views inside the same transient surface:
  - Usage: connection state, Today cost/tokens, Month context, and one activity line.
  - Quota: subscription windows ordered by lowest remaining percentage, reset times, and
    notification status.
  Keep Open Dashboard, Refresh, Settings, and freshness persistent across the views.
- Make the local/private connection legible without marketing copy.
- Add in-page controls to switch platform and data state: healthy, loading, offline,
  busy/partial, and empty.
- Include a Settings concept with opt-in low-quota notifications, initially using a 20%
  remaining threshold.
- If useful, add compact/detail density, but keep the recommended default obvious.
- Add a short Design decisions / questions annotation area outside the OS frame.
- Use fake data only. Do not make real API requests.
- Make the prototype readable at a typical laptop resolution and keyboard-accessible
  enough for design review.

Aesthetic direction: precision instrument — quiet, native, and dense only where useful.
Avoid generic dashboard styling, purple gradients, giant cards, excessive rounding, and
decorative motion. On macOS, glass must create hierarchy without reducing contrast. On
Windows, use Fluent spacing, typography, focus treatment, and Acrylic/Mica cues.

When complete, report the path and explain:

1. the default content hierarchy;
2. meaningful macOS/Windows differences;
3. any design decision that still needs Howard's input.

Do not edit any other file.
```
