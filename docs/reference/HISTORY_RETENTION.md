# History retention — why Tokdash's past months can shrink (and how to prevent it)

Tokdash now keeps a local SQLite usage index at `~/.tokdash/usage.sqlite3` by default.
The index is for performance: it stores parsed usage rows so repeated dashboard/API reads do
not have to reparse every client log. Source logs still matter. If a client deletes old logs
before Tokdash has indexed them, that usage is still gone. A month you looked at weeks ago can
quietly read lower than it did then when the DB is disabled, stale, or never saw the deleted
files.

This is not a Tokdash bug; it is the client doing housekeeping. The good news: only **two**
of the supported clients delete history by default, and **both can be turned off with a
one-line config change**. Everything else keeps history indefinitely.

---

## TL;DR — the only two you need to fix

| Client | Auto-deletes? | Default | Fix (keep history) |
| --- | --- | --- | --- |
| **Claude Code** | **Yes**, at startup | sessions **> 30 days** | add `"cleanupPeriodDays": 3650` to `~/.claude/settings.json` |
| **Gemini CLI** | **Yes**, at startup | sessions **> 30 days** | set `general.sessionRetention.enabled: false` in `~/.gemini/settings.json` and any project `.gemini/settings.json` |

Apply those two and your Tokdash history stops eroding. Details and the full client survey
below.

---

## A real example

On 2026-06-01 the same April total for `claude-opus-4-6` had fallen from a screenshot taken
~May 11 to the current dashboard:

| | ~May 11 | 2026-06-01 |
| --- | --- | --- |
| Messages | 3,755 | 1,095 |
| Tokens | 615,037,255 | 83,944,974 |
| Cost | **$528.30** | **$73.80** |

Cross-checked with **ccusage** reading the same files — it reported the same shrunken
numbers, confirming the data (not the parser) had changed. Claude Code's 30-day cleanup had
deleted the older April transcripts; only sessions that were resumed (and thus had a fresh
file timestamp) survived.

---

## How the mechanism works

- Tokdash reads files like `~/.claude*/projects/**/*.jsonl` and syncs a local SQLite
  usage index. You can force this with `tokdash db sync`, run a loop with `tokdash db watch`,
  or enable the serve-time loop with `TOKDASH_USAGE_DB_WATCH=1`.
- Claude Code and Gemini CLI run a **startup cleanup** that hard-deletes session files older
  than a retention window (default **30 days**), based on the file's last-modified time.
- **Continuing** a session (sending a new message) refreshes its file timestamp, so
  actively-used sessions survive; standalone sessions you never reopen age out and are
  deleted.
- With `TOKDASH_USAGE_DB_DURABLE=1` (default), rows Tokdash has already indexed are kept when
  a source file disappears. With `TOKDASH_USAGE_DB_DURABLE=0`, the DB strictly mirrors current
  source files.
- Once a file is deleted before Tokdash has indexed it, the tokens/cost it held are gone from
  Tokdash. **Already-deleted, never-indexed data is unrecoverable** — these settings only
  prevent *future* loss.

---

## Full client survey (verified against source + docs, 2026-06-02)

| Client | Auto-deletes by default? | Default window | Storage | How to disable / status |
| --- | --- | --- | --- | --- |
| **Claude Code** | 🔴 **Yes** (startup) | 30 days | JSONL | `cleanupPeriodDays` in `settings.json` (set high, e.g. `3650`) |
| **Gemini CLI** | 🔴 **Yes** (startup) | 30 days | JSONL | `general.sessionRetention.enabled: false` (or raise `maxAge`) in `~/.gemini/settings.json` and any project `.gemini/settings.json` |
| Hermes | 🟡 Capable, **off by default** | 90 days *if* enabled | SQLite | leave `sessions.auto_prune: false` (the default) |
| Codex CLI | 🟢 No | indefinite | JSONL | nothing to do (retention feature unimplemented) |
| OpenCode | 🟢 No | indefinite | SQLite | nothing to do |
| Kimi CLI | 🟢 No | indefinite | JSONL | nothing to do (a web-only "archive" flag never deletes data) |
| Pi | 🟢 No | indefinite | JSONL | nothing to do |
| GitHub Copilot CLI | 🟢 No | indefinite | JSONL + SQLite | nothing to do (manual `/session prune` only) |
| OpenClaw | 🟢 No | indefinite | JSONL | nothing to do (a session reset/delete renames the file to `.reset`/`.deleted` but keeps it on disk — Tokdash still reads it) |
| Goose | 🟡 own retention unmeasured; **Tokdash follows the database** | unverified | SQLite | no Goose setting measured — but deleting a session in Goose takes its `usage_ledger` rows with it (`ON DELETE CASCADE`), so that usage leaves the Tokdash index at the next sync and the durable mode does not override it |
| Roo Code | 🟢 No | indefinite | JSON per task | nothing to do; and the reverse is also true — delete a task directory and Tokdash keeps the rows it already indexed, so the usage stays on the dashboard |

Legend: 🔴 erodes by default · 🟡 can erode if you opt in · 🟢 durable by default.

Goose and Roo Code were added after the 2026-06-02 survey above, so neither row is a
retention audit: the Goose row states only what deleting a session does to Tokdash's
index, and Roo's own pruning was not measured.

---

## The fixes in detail

### Claude Code
Add this key to your **existing** `~/.claude/settings.json` (don't replace the file), and to
any alternate `CLAUDE_CONFIG_DIR` you use:

```json
{
  "cleanupPeriodDays": 3650
}
```

`3650` (≈10 years) effectively disables auto-deletion. This setting also governs cleanup of
orphaned subagent worktrees, so a very large value keeps those around too — harmless in
practice, but pick a window that comfortably exceeds the history you care about.

### Gemini CLI
Disable session retention in `~/.gemini/settings.json`:

```json
{
  "general": {
    "sessionRetention": {
      "enabled": false
    }
  }
}
```

Gemini CLI also supports workspace settings at `<project>/.gemini/settings.json`, and those
override user settings. If a project has a workspace settings file, apply the same
`general.sessionRetention` change there too.

(To extend rather than disable, set `general.sessionRetention.maxAge` to e.g. `"3650d"`.)

### Hermes
Already safe by default (`sessions.auto_prune: false`). Just don't enable auto-prune or run
`hermes sessions prune` if you want to keep everything.

---

## What the SQLite index does and does not solve

The persistent usage DB is a local performance index, not a raw-log archive or billing ledger:

1. **It makes warm reads fast.** Repeated Overview, Stats, OpenClaw, and supported session
   queries can use indexed SQL instead of scanning every log file.
2. **It can retain rows Tokdash has already indexed.** The default durable mode keeps cached
   rows when a source file temporarily disappears or a parser returns no rows. For a
   file-per-task source that is a permanent outcome rather than a temporary one: deleting a
   Roo Code task directory leaves its usage on the dashboard. A source that owns one
   database instead follows that database's contents, so deleting a Goose session does
   remove its usage at the next sync.
3. **It cannot reconstruct logs it never saw.** If Claude Code or Gemini CLI deletes a file
   before Tokdash syncs it, the DB has nothing to preserve.
4. **It is still derived data.** Parser fixes, pricing updates, and explicit resyncs may
   change computed totals. Keep the original client logs when you need audit-grade history.

---

## Defense in depth (optional)

If long-term history is critical, you can also keep an additive backup of the raw log
directories — e.g. a periodic `rsync` (without `--delete`) of `~/.claude/projects/` into a
backup location. Tokdash's per-message deduplication means reading both the live and
backed-up copies never double-counts. For most users, the two config changes above are
enough.
