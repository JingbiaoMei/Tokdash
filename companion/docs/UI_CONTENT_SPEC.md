# Companion UI content specification

## Design direction

The companion should feel like a quiet instrument supplied by the operating system. It is
not a compressed web dashboard.

Shared anatomy does not mean shared styling:

```text
status + settings
Usage | Quota
  Usage: today hero + month context + one activity line
  Quota: subscription windows + notification status
open dashboard + refresh
freshness
```

The hierarchy and behavior stay aligned across platforms. Materials, spacing, typography,
menus, focus, dismissal, and window shape remain native.

## Closed state

### macOS menu bar

Default: one monochrome template icon.

- Accessible name: `Tokdash`.
- Tooltip/help: `Tokdash — Today $3.42 · 18.7M tokens`.
- Optional later setting: show `$3.42` beside the icon.
- Offline: a stable disconnected variant or small badge; no animation.
- Urgent quota: do not recolor the whole icon. A restrained warning mark is enough.

### Windows notification area

Default: one icon with a unique outline and transparent background.

- Tooltip: `Tokdash — Today $3.42 · 18.7M tokens`.
- Do not attempt a text label in the notification area.
- Use the same high-level normal, warning, and offline states as macOS.
- Single click and keyboard activation open the flyout.
- Right click opens a short native menu: Open Tokdash, Refresh, Settings, Exit.

## Open state

Suggested width:

- macOS: about 340–360 points.
- Windows: about 360–400 effective pixels.

The exact dimensions should follow native control metrics and text scaling.

### 1. Header

Left:

- Tokdash wordmark or small icon.
- Connection line: `Local · Connected`, `Tailscale · Connected`, `Busy`, or `Offline`.

Right:

- Settings button.

Do not show a marketing privacy badge. `Local · Connected` or the configured host is enough.

### 2. View switcher

Use two views inside the same popover/flyout:

- **Usage** — default view; tokens, cost, month context, and activity.
- **Quota** — subscription windows, reset times, and quota notification status.

Keep the header, action row, and freshness footer stable while switching. Do not open a
second window. Use the platform's native compact selector rather than web-style navigation
tabs.

### 3. Usage — Today hero

Primary value: today's estimated USD cost.

```text
TODAY
$3.42
18.7M tokens · 248 messages
12% below yesterday
```

Rules:

- Use `total_cost`, `total_tokens`, and `total_messages`.
- Use `comparison.cost_pct` for the short comparison line.
- If previous-period data is unavailable, omit the comparison rather than showing zero.
- Cost is still labelled as Tokdash's estimate wherever the dashboard uses price estimates;
  do not imply a provider invoice.
- Tokens use compact notation; exact values can appear in accessibility text or a tooltip.

### 4. Usage — Month context

One compact line or small secondary block:

```text
JULY    $48.90    281M tokens
```

Do not add a chart. The full dashboard already provides trends.

### 5. Quota — Subscription windows

When quota tracking is enabled, render every detected provider with usable buckets. Put the
most urgent windows first and keep the first screen concise; additional rows may scroll.

Default selection:

1. Flatten usable buckets across detected providers.
2. Sort by `remaining_percent` ascending.
3. Place the two most urgent windows above the fold.
4. Keep paired primary windows from the same provider understandable; labels must include
   provider and bucket.

Example:

```text
SUBSCRIPTION
Codex · 5-hour       38% left   resets 14:40
███████░░░░░░░░░░░
Claude · weekly      62% left   resets Mon
███████████░░░░░░░
```

Rules:

- The API stores `used_percent` but also returns `remaining_percent`; display remaining.
- Indicate `Estimated` when the provider object says `estimated: true`.
- Show reset time in the user's locale and timezone.
- If quota tracking is disabled, show one quiet row: `Subscription tracking is off` with
  an `Open Dashboard` path to configure it. Do not configure consent in the companion MVP.
- Provider failures produce an inline warning for that row, not a full-surface failure.
- Do not call `/api/quota/refresh` automatically.
- Show whether low-quota notifications are enabled and provide a path to Settings. Do not
  request notification permission merely because the Quota view was opened.

### 6. Usage — Activity line

Use one line, not a table:

```text
Most used today  Codex · gpt-5.6
```

Selection:

- Leading tool by cost from `by_tool`.
- Leading model by cost from `top_models`/`combined_models`.
- Omit when the response is empty.

This is the first item to remove if the surface feels crowded.

### 7. Action row

- Primary: `Open Dashboard`.
- Secondary icon button: `Refresh`.
- macOS may put `Quit Tokdash Companion` in a footer menu or settings menu.
- Windows keeps `Exit` in the right-click menu and settings.

`Refresh` refetches companion data. A provider network poll, if added later, must be a
separate labelled command.

### 8. Freshness

Footer text:

```text
Updated 2 min ago
```

Append `· cached` or `· stale` only when it helps explain why data has not changed. Do not
expose cache implementation terms during normal operation.

## States

### Loading with no prior data

- Keep the real layout.
- Use restrained placeholders for the main values.
- Header says `Connecting…`.
- Do not use a full-screen spinner.

### Refreshing with prior data

- Keep all data visible.
- Rotate or pulse only the Refresh affordance, respecting reduced motion.
- Header stays connected.

### Offline

```text
Tokdash is not reachable
Start Tokdash, or check the server address in Settings.

[Retry]  [Settings]
```

Keep the last in-memory data visible but clearly marked as last updated, if available.

### Busy (`503`)

```text
Tokdash is busy — retrying
Last data from 3 min ago
```

Retry with backoff. Do not translate `503` into “offline.”

### Partial

Show the successful Today/Month sections normally. Replace only the failed quota or month
section with a short inline status and retry it later.

### Empty

```text
No usage recorded today
Month to date  $12.40
```

Zero is valid data. Do not show an error illustration.

### Wrong service

If `/health` does not return `service: "tokdash"`:

```text
This address is not a Tokdash service
```

Do not continue calling usage endpoints.

## Platform treatment

### macOS

- Use the system menu-bar popover behavior through SwiftUI.
- Let standard controls and popovers adopt Liquid Glass on macOS 26.
- Use platform typography and SF Symbols in the production app.
- Avoid custom nested glass cards. Content should sit in one coherent popover.
- Test light/dark appearance, Reduce Transparency, Increase Contrast, and text scaling.

### Windows

- Treat the surface as a transient, light-dismiss flyout anchored near the notification
  area.
- Use Acrylic for the flyout, with a solid fallback for battery saver, high contrast, or
  unsupported configurations.
- Use WinUI typography, spacing, icons, focus visuals, and command placement.
- Avoid macOS-style floating pills and traffic-light decoration.
- Test every taskbar edge, multiple monitors, DPI changes, light/dark mode, and keyboard
  invocation.

## MVP low-quota notifications

- Notifications are opt-in and default off.
- Recommended initial threshold: 20% remaining.
- Evaluate the threshold from already scheduled `/api/quota` reads; notifications must not
  create extra provider polling.
- Notify only on a crossing from above to at-or-below the threshold.
- Deduplicate by provider, account, bucket, reset epoch, and threshold.
- Do not repeat on every background refresh.
- A new reset epoch re-arms the notification.
- Clicking a notification opens the companion directly to Quota.
- Do not notify for Tokdash being offline, busy responses, estimated-data staleness, or
  quota recovery in MVP.
- If a bucket has no reset time, keep it suppressed until there is an explicit product
  rule for re-arming it.

## Prototype review checklist

- Can the three product questions be answered in under five seconds?
- Does each variant look native before reading its platform label?
- Is Today still clearly primary when quota data is urgent?
- Should the activity line survive, or is it noise?
- Is the offline recovery path obvious?
- Does glass/material improve hierarchy without lowering contrast?
- Would the menu-bar/tray icon remain useful without showing live numbers?
