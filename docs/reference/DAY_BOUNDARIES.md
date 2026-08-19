# Day boundaries — why a provider's dashboard shows a different number

Tokdash cuts every day at **your machine's local midnight**. Date ranges, the "Today"
and "Yesterday" presets, the daily totals, and the activity heatmap all use the host
timezone, read fresh on each request, so it follows DST.

Timestamps are stored as epoch milliseconds — absolute instants, no timezone — and the
local boundary is applied when they are bucketed. Nothing in the interface displays UTC.

## Why a provider's own page disagrees

Provider usage pages generally bucket by **UTC** midnight. OpenAI's Codex usage profile
does. At UTC+1 that puts their day boundary at 01:00 your time; at UTC+8, 08:00. So work
you do in that band is filed under one date by Tokdash and the neighbouring date by the
provider.

Concretely, at UTC+1, Tokdash's "16 Aug" spans:

```
2026-08-16 00:00 local  ..  2026-08-17 00:00 local
= 2026-08-15 23:00 UTC  ..  2026-08-16 23:00 UTC
```

The provider's "16 Aug" spans `00:00 UTC .. 24:00 UTC`. Same underlying calls, different
cuts. On one measured day this was a 7% difference in the daily total; the wider your
offset from UTC, the wider the band that moves.

## Two other reasons the numbers will not match

**Coverage.** Tokdash reads the session logs on *this machine*. A provider's page is
server-side and account-wide, so it also counts work from other machines you are signed
in on, and from cloud tasks that never touch local disk. This gap cannot be closed
locally, and it is unbounded.

**Token definitions differ per provider.** Codex reports `total_tokens` as
`input_tokens + output_tokens`, with reasoning tokens counted *inside* output. Tokdash
carries reasoning as its own figure and adds it to the total, so its Codex token count
runs slightly above the provider's — on the order of 0.1–0.5% for typical usage, more on
reasoning-heavy days. Costs are unaffected.

Because of these, aligning only the day boundary would not make the two agree. Tokdash
does not offer a UTC view for that reason: it would produce a second number that still
did not reconcile, with no way to tell which difference you were looking at.

## What to do

For a like-for-like check against a provider page, compare a **week or a month** rather
than a single day. The boundary displacement is a fixed band at each end, so it washes
out as the window grows, leaving only the coverage and definition differences above.
