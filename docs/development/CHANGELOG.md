# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Changed

- The persistent usage cache no longer treats a parser edit or a pricing update as a reason to reread source logs. A cached row used to be validated against one signature that folded in a SHA-1 of the whole `coding_tools.py` module and the complete pricing identity, so adding or changing one parser invalidated every stored coding-tool source (all of them share that file), and adding an unrelated model to the pricing database reparsed every cached log. v2.0.0 shipped both at once and rebuilt the entire cache on upgrade. Three identities are now kept apart: source files, an explicit per-parser `persistent_parser_version`, and pricing. A parser bump invalidates only its own source; the shared `USAGE_ENTRY_FORMAT_VERSION` is the one identity that invalidates all of them, deliberately. DSH still folds in the shared log decoder's own versions, so a decoder bump invalidates DSH and nothing else. Package version, install path and file restamping affect nothing. See `docs/development/technical-notes/USAGE_CACHE_IDENTITY.md`.
- Pricing changes now reprice cached usage rows instead of reparsing them. Each row stores the billing inputs it was priced from — the exact arguments its parser passes to `PricingDatabase.get_cost`, which are not always the displayed token buckets — so a rate edit, an alias change, a newly added model or a removed one is applied by recomputing the stored cost in a single transaction that opens no source log. Costs the provider itself reported (Pi's `usage.cost.total`, Hermes' actual/estimated cost) are stored as fixed and are never recomputed, and provider-qualified then bare-model fallback order is preserved exactly as a live parse resolves it. The new pricing identity is committed in the same transaction as the rows it describes, so it can never advance ahead of them and a failed pass leaves the last good cache servable. Overview, Stats, contributions, `/api/usage`, `/api/tools` and `tokdash export` all observe the new price immediately.
- Entry keys for rows a source does not name itself no longer include cost. A repriced row would otherwise stop matching the same logical entry reparsed out of another file, and the duplicate would be counted twice.

### Fixed

- A usage sync that finishes after another process changed pricing can no longer strand stale costs in the cache. Parsing runs outside the store lock, so a sync could begin under one pricing database, be overtaken by a process that repriced everything to a newer one, and then commit its older costs while the stored pricing identity claimed the newer — after which every later request matched that identity, returned early, and never revisited those rows. Each row-writing transaction now declares the pricing it parsed under and drops the stored identity in that same transaction when the two disagree, so the next request rebuilds the costs from the stored billing inputs without rereading a log. The sync paths also reprice once more after their writes, so a request that hits this heals before it returns rather than leaving the work for the next one.
- A pricing identity can no longer describe rates that were never applied. `PricingDatabase` loads its rates into memory when constructed, but the content identity was derived by rereading the file on demand — so a pricing edit landing between the two paired the new file's identity with the old in-memory rates. The cache stamped that identity against costs computed from the superseded rates, and every later request holding the genuinely-new pricing then matched it and skipped repricing for good. `load()` now parses the rates and computes their identity from a single read and publishes both in one snapshot, so `content_signature()` always describes exactly the rates `get_cost` will apply. `signature()` remains the separate drift detector that reads current files to answer "should I reload?".
- Reads can no longer observe a half-repriced usage cache. A superseded write drops the stored pricing identity as it commits, but the repair runs in a later transaction, and in that window the table really does hold two pricing generations — so a request landing there could total costs computed under different rates. Verifying the identity and then reading would not have closed it either, since a write can commit between the two. The read path now takes both in one snapshot: an identity visible there proves every row in that same snapshot was priced under it, and an absent one makes the reader repair and retry rather than report a mixture. The last retry repairs and reads while holding the write lock, so it always terminates. OpenClaw now takes its model totals and contribution grid from a single snapshot instead of two.
- Reinstalling or upgrading Tokdash no longer rebuilds the usage cache on its own. The parse identity is now free of file paths, mtimes and module content, so a `pipx upgrade` that restamps every installed file — or a wheel that lands at a new path — leaves cached rows alone.

### Migration

- The usage database moves to schema 8, which adds a `billing_json` column to `usage_entries`. Rows written before it have no trustworthy billing provenance, so the migration preserves each one's stored cost as a fixed cost rather than guessing at it. Rows whose source file still exists are rebuilt once by the next sync (their parse signature changed shape at the same time) and come back with real provenance; a durable row whose source file is gone keeps exactly the cost it already reported. Session records, quota history and every other durable table are untouched. This is the last global reparse: after it, ordinary parser additions and pricing updates never trigger one again.

## 2.0.0 - 2026-08-20

### Added

- Added ZCode as a token and cost source. Tokdash copies `$ZCODE_HOME/cli/db/db.sqlite` and its live WAL into a disposable snapshot before reading `model_usage`, so SQLite never creates or changes sidecars beside the source database. Each model request is one entry and retries remain separate billable rows. ZCode's `input_tokens` counts cached and uncached prompt tokens together, so the cached share is split into its own `cacheRead` bucket, and reasoning tokens are displayed disjoint from output while billing at the output rate.
- Added ZCode to the Session Explorer. Turns are read from the same database through the same coherent snapshot the usage parser uses and billed per (turn, model) with the same token rules, top-level sessions only. A turn with no billable tokens still credits its measured time to active time as an activity event, and a boundary turn whose measured work overlaps the selected window is credited even when its session has no in-window token event. A transient read failure surfaces as an error and retries on the next collection instead of blanking the panel or caching an empty result.
- Added a ZCode brand mark to the dashboard and the README supported-tools strip.
- Added Qwen3.8 27B pricing from Alibaba Cloud Model Studio's published rate.

### Fixed

- `tokdash setup` on native Windows can now register its Task Scheduler task. The task definition was written as UTF-8, but `schtasks /Create /XML` hands the file to MSXML as a UTF-16 wide string that refuses a declared UTF-8, so registration failed with "unable to switch the encoding" on every native-Windows setup since v1.0.5 — it failed closed, with no half-installed state. The definition is now declared and written as UTF-16 LE with BOM, matching Task Scheduler's own exports, and read back the same way, including the plain-UTF-8 file a previously failed setup left behind, so an upgrade still recognizes it as setup-owned. A `windows-latest` CI step now renders both task variants and registers them with a real `schtasks` — every unit test fakes `schtasks`, which is exactly why this shipped.
- A single locked or unreadable file no longer errors a whole dashboard view on native Windows. Session scanners and parsers now skip a file they cannot stat or open (for example one held without share-read by the agent itself, antivirus, or the search indexer) instead of letting the error propagate. That skip lasts one request: neither the per-file parser nor the per-tool loader caches a view assembled while a file was locked, so the session comes back on the next request. Both caches are keyed on (path, mtime, size), which for a finished session file never changes again — memoizing the miss would have hidden that session until tokdash restarted. The usage tracker skips a source that fails outright rather than blanking `/api/usage`. A session view that cannot be read for I/O or SQLite reasons degrades to an empty view with a logged warning instead of a 500 — a loader bug still raises, rather than masquerading as "no sessions".
- `tokdash db resync` run while the dashboard is up no longer ends in a raw traceback and an orphaned temp database on Windows: replacing a database another process still holds open is now reported as a clear "stop the running `tokdash serve` and retry" failure, any partially-done rename is undone, and the temp files are removed — and the command exits non-zero for any failed resync, including the existing refusal to replace a populated database with an empty result. The undo runs for any failed rename, not just a held file: a cross-device rename or a volume I/O error reports its own cause instead of blaming the server. If the undo itself cannot finish, the response says so (`rollback_ok: false`) and names the `.bak` left on disk, which is then the only intact copy of the old database. `tokdash uninstall --purge` names the same cause when the file it could not delete is the usage database a running server is holding.
- `tokdash db resync` now repairs a corrupt usage database on native Windows instead of failing on it. `UsageEntryStore._connect` ran schema setup before handing the connection out, so a failure there — the normal path for a corrupt file — escaped past the caller's `closing()` and leaked the handle. On Windows that handle blocked renaming the file, so the resync reported the database as held open by a running `tokdash serve` that was not the cause, on exactly the broken database resync exists to repair. The connection is now closed before the error propagates. Found by the new `windows-latest` CI job.
- Codex session titles are no longer lost when the path to `state_5.sqlite` contains `#` or `%` — on any platform. The read-only SQLite `file:` URI was built by string interpolation, where `#` truncates the path and `%XX` is percent-decoded; it is now built with `Path.as_uri()`, which encodes both.

### Changed

- `/api/usage` now reports the sources that failed to read in a `source_errors` list (the usage tracker's JSON carries it too), so a source that failed mid-collect can be shown as unavailable instead of a zero that reads as "no usage in range".
- Reads of live third-party SQLite databases (OpenCode, Hermes, Mimo) now open read-only first with a read-write fallback, matching what the Antigravity reader already did. A plain read-write open takes a write lock that on native Windows can block the client's own writes for the duration of the read.
- OpenClaw's data location now resolves through the same central path seam as every other client and honors an `OPENCLAW_HOME` override. Its native-Windows location is unverified (no Windows host at research time); if OpenClaw stores its data elsewhere there, point `OPENCLAW_HOME` at it.
- The Pi override now honors the environment variables the Pi coding agent actually reads: `PI_CODING_AGENT_SESSION_DIR` (the session dir) and `PI_CODING_AGENT_DIR` (the agent dir; sessions live under `<dir>/sessions`). The earlier `PI_AGENT_DIR` comma-separated list still works.

## 1.9.0 - 2026-08-19

### Added

- Added Reasonix as a token, cost and session source. Tokdash reads `$REASONIX_HOME/stats/YYYY-MM-DD.jsonl` for per-request usage and `$REASONIX_HOME/projects/*/sessions/*.jsonl` for the Session Explorer (`REASONIX_HOME` defaults to `~/.reasonix`), attributing the `provider/model` pair from Reasonix's own config and pricing it through the existing pricing database. Reasonix's `prompt` field counts cached and uncached input together, so it is split into Tokdash's disjoint `input` and `cacheRead` buckets rather than copied into both. Reasonix records usage per request without a session id, so session rows carry turns, project and timing but no token counts; Overview and Stats hold the full totals.
- Added a Reasonix brand mark to the dashboard and the README supported-tools strip.
- Documented that dashboard days are the host machine's local days, and why a provider's own usage page shows a different number for the same date (`docs/reference/DAY_BOUNDARIES.md`).
- Active time can now use a duration the source measured itself instead of inferring one from the gap between events. Reasonix logs how long each assistant step took, so its sessions exclude the pause between an answer and the next prompt outright rather than billing it up to the idle cap, and a session's first and last steps are both counted. Tools that log only completion instants are unchanged.

### Fixed

- Restored DeepSeek Harness session loading on the live-parse path. `/api/sessions?tool=dsh` raised `NameError` whenever the persistent store was disabled or its read failed, because the fallback loader had been removed.
- The Overview no longer shows one date range's breakdowns under another range's label. The date picker writes its new label before the fetch starts, and the Apps & Models and Combined Models tables were painted from an idle callback, so both could outlive the range they belonged to. They are now cleared the moment the selection moves, and the Overview is visibly marked while it is showing a window other than the one on the label.
- The Agent Time card now shows a loading state instead of an em dash while its own request is in flight. It is fetched after the rest of the KPI row, so a dash there read as "no agent time in this range" rather than "not back yet".

## 1.8.1 - 2026-08-18

### Fixed

- Codex subagent and fork rollouts no longer double-count the parent thread they replay. Codex 0.146+ writes fork files with a single `session_meta` carrying the child's own id and declares ancestry through `forked_from_id` or a top-level `parent_thread_id`, so the previous gate never fired and every replayed event was counted twice. Replay segments are now keyed to the declared parent session so they collapse against the parent's own rows. When the parent is not indexed anywhere the rows survive and are counted once, and sibling forks of the same parent keep exactly one copy between them.
- Codex usage is no longer attributed to `gpt-5.3-codex` when a rollout records no model. That name is a real, selectable model, so it can no longer stand in for "not known yet": rows written before a file's first model signal take the file's own first model, and files with no model signal at all are labelled `unknown` and priced at zero. `thread_settings_applied` now counts as a model source alongside `turn_context`.
- A windowed session read no longer keeps a fork's replayed prefix when the window excluded every one of the parent's files.

### Added

- Codex rollouts moved to `~/.codex/archived_sessions` are now read alongside `sessions/`. Identical copies across the two roots collapse by event key rather than counting twice.

### Changed

- Stored session rows belonging to one session now merge in a single pass rather than pairwise. Folding them one at a time re-keyed and re-copied every turn already merged, which is quadratic in the number of files a session spans — and Claude splits one session across every subagent transcript it spawned, several hundred files for a single session id. Output is unchanged.

## 1.8.0 - 2026-08-14

### Added

- Added DeepSeek Harness (`dsh`) as a token, cost and session source. Tokdash reads `$DSH_HOME/sessions/*/*/session.jsonl.zstd` (or an uncompressed `session.jsonl`, defaulting to `~/.dsh`), decodes its concatenated zstd frames, folds each step's early usage chunk into its finalized message instead of double-counting it, skips the parent-owned prefix of forked sessions, and prices DeepSeek models through the existing pricing database. Sessions appear in the Session Explorer with title, project and active time, and sync into the persistent store like the other file-backed clients.
- Added a DeepSeek Harness brand mark to the dashboard and the README supported-tools strip.
- Added GLM 5.3, DeepSeek V4 Pro 0813 and Grok 4.6 pricing.

### Changed

- Updated the DeepSeek V4 Flash and V4 Pro rates to the current official prices.

## 1.7.0 - 2026-08-14

### Added

- Added an estimated agent time card to the Overview, after Total Messages, with a change against the previous period. It is backed by a new `/api/active-time` endpoint that merges every session tool into one figure and reports each tool separately.
- Added estimated active and agent runtime to sessions and per-tool panels. Active time counts each gap between a stream's token events up to an idle cap (`TOKDASH_ACTIVE_GAP_CAP_SECONDS`, default 300s), so a session left open overnight no longer reads as an all-night session; agent time adds concurrent agents up instead of counting the overlap once.
- Added Kimi to the session explorer, including its persistent cache and per-agent stream timing.
- Added Gemini 3.7 Flash pricing at list price rather than the current launch discount.

### Changed

- Cached session rows are now price-neutral: they hold each turn's billing inputs and are priced when read. Editing a rate reprices Codex, Claude and Kimi immediately instead of marking every unchanged log as changed — on a corpus of 5301 session files, a pricing edit now reparses none of them and rewrites no rows. Two Tokdash builds sharing one database no longer overwrite each other's costs.
- Codex rows written before this are rebuilt once. Codex bills under `provider/model` but stores the bare model name, so a row holding only totals cannot prove which pricing entry applied to it.
- The Overview's total token value drops its unit to save width; the exact count remains in its tooltip and its accessible label.

### Fixed

- Kept the provider's own reported cost for OpenCode and Mimo turns instead of replacing it with Tokdash's estimate.
- Fixed Mimo's fallback loader for SQLite builds without JSON1, which still required a JSON function to exclude imported messages.
- Stopped a dashboard load that was superseded mid-flight from committing its results or reporting its errors under the newly selected range, and made a return to a range whose load was already discarded re-request it.
- Timed Claude subagents as their own streams, so two agents reporting identical usage in the same second are no longer merged into one.
- Stopped an unwindowed loader from treating a user message just outside a window as the nearest token event when JSON1 is unavailable.

## 1.6.4 - 2026-08-12

### Fixed

- Replaced the quick-range horizontal scrollbar with a responsive, keyboard-accessible disclosure that keeps the active preset visible.

## 1.6.3 - 2026-08-12

### Fixed

- Preserved the persistent usage cache across package upgrades when parser and pricing contents are unchanged, while still invalidating cached costs when pricing data or calculation code changes.
- Hid the unused single-server quota history shells in multi-server mode, removing the duplicate empty charts above per-server quota sections.

## 1.6.2 - 2026-08-12

### Added

- Added an in-app **What's new** drawer, backed by release notes that ship with the package and a release checklist guard to keep them current.
- Added compact local brand marks for supported coding tools in the Usage by Tool chart, usage tables, app/model, and combined-session breakdowns.

### Changed

- Unified dashboard loading placeholders with the Profile Activity shimmer while preserving localized status text for assistive technology.
- Refined the Overview Profile preview's typography and insight hierarchy without a nested card surface, while keeping its heatmap geometry unchanged.
- Moved the What's new entry from the Tokdash brand lockup into the dashboard action rail and normalized Codex/Grok marks onto transparent backgrounds.
- Kept quick date ranges compact and on one row, extending from the existing Range control toward the right without stretching individual buttons.

### Fixed

- Prevented gradient theme backgrounds from tiling on long dashboard views, removing the mismatched horizontal seam while preserving the Brutalist theme's intentional grid pattern.
- Kept the current-day Profile Activity cell synchronized with the already-loaded Today Overview totals, avoiding drift between independently aged response caches without adding another API request.
- Shared each local tool icon request across repeated dashboard identities, avoiding duplicate transfers under the no-store static asset policy.
- Allowed dashboards opened through Tailscale Serve to combine Tokdash servers on the same tailnet without per-server CORS configuration; cross-tailnet origins and remote writes remain blocked.

## 1.6.1 - 2026-08-11

### Fixed

- Kept the Settings dropdown above dashboard cards in themes that use blurred surfaces.

## 1.6.0 - 2026-08-10

### Added

- Added multi-server selection to the web dashboard and macOS/Windows companion settings. Reachable server usage is combined for Overview, Sessions, Stats, and companion totals; quota remains grouped by server.
- Added optional browser-side `/health` testing, syntax-only server adding for offline machines, per-server reachability state, and automatic recovery on a later refresh.

### Changed

- Companion settings now persist schema v2 server entries and migrate the prior single `BaseURL`/`baseURL` without resetting other preferences.
- Default Sessions requests now run with two-tool concurrency instead of letting a cold Claude scan block every later tool.

### Fixed

- Startup warming now populates the dashboard's explicit-today Sessions keys and Activity Insights instead of warming unused period-only keys.
- Expired API responses now return stale data immediately while one background refresh recomputes the key; response values and idle per-key locks are bounded, and failed-fill lock cleanup remains atomic with the single-flight registry.
- Persistent Codex and Claude session records now carry indexed time bounds, so narrow date ranges discard historical sessions before Python deserializes their JSON. Explicit parser versions and content-based pricing identities preserve compatible v1.5.9 rows across code changes and wheel restamps; persistent-cache failures are logged before the source-file fallback.

## 1.5.9 - 2026-08-10

### Added

- Made the active homepage date range immediately visible with a localized, keyboard-accessible range trigger and synchronized quick-range selection, without changing date filtering or refresh behavior.

### Fixed

- Companion apps (macOS/Windows) now show multi-day reset countdowns in days, matching the dashboard. A weekly window read "resets in 3 days" on the dashboard but "resets in 94 hours" in the companion flyout, because the days tier added to the dashboard in 1.5.7 was never added to the companions. The tier boundaries are now pinned to the same values in all three test suites.

## 1.5.8 - 2026-08-05

### Fixed

- Antigravity quota refresh no longer fails for every provider when one model's `quota_info` payload is malformed (non-dict). The defensive guard lost when the reset-time lookup was hoisted out of the broad `try`/`except` is restored.

## 1.5.7 - 2026-08-05

### Changed

- Quota bars now show a relative "resets in 3 days" / "resets in 4 hours" / "resets in 43 minutes" countdown (single unit, matching the companion app's rule) plus the absolute reset timestamp, replacing the per-bar captured-at time.

### Fixed

- Antigravity quota now shows the correct window label ("5-hour" vs "Weekly") on the dashboard and companion apps. The single quota window returned by the API is auto-determined from its reset time instead of always assuming 5-hour, so a weekly limit (e.g. resetting in 3 days) no longer renders as "5-hour".
- Antigravity models with an exhausted weekly window (`remainingFraction: null`) are now captured instead of skipped, so the dashboard no longer falls back to stale 5-hour data when the weekly limit is hit.

## 1.5.6 - 2026-08-03

### Added

- Added local Codex Activity Insights to Profile and Overview, including recorded chats, reasoning-effort distribution, structured tool usage, ranked tools, and explicit coverage indicators.
- Activity counts use primary local sessions and exact stable session identifiers; subagents, inferred skills/plugins, and unavailable legacy files are excluded and reflected in coverage.
- Added a persisted `Readable tokens` setting across the dashboard. Token quantities use adaptive K/M/B/T units by default; disabling the setting restores exact localized counts.

### Changed

- Grouped language, light/dark mode, visual theme, readable-token display, and app installation under one accessible Settings panel.

### Fixed

- Excluded Codex guardian and automatic-review sessions from primary activity insights so reasoning-effort summaries reflect user sessions.
- Rejected malformed activity fields and refreshed activity insights with manual dashboard updates.
- Kept the Overview range controls stable after adding readable-token display controls.

## 1.5.5 - 2026-07-31

### Added

- Added official pricing for DeepSeek V4 Flash 0731, including cache-hit, cache-miss, and output rates.

### Changed

- Updated GPT-5.6 Terra and Luna pricing, including their `-pro` mirrors, to the current official OpenAI standard rates.

## 1.5.4 - 2026-07-30

### Fixed

- Deduplicate Codex history copied into ordinary resumed rollout files, preserve the original event dates and explicit thread names, and merge genuine turns across files belonging to the same logical session. Persistent indexing promotes a surviving replay occurrence when a canonical file is rewritten or removed.

## 1.5.3 - 2026-07-29

### Added

- Added a Last Week quick range that selects the previous Monday-through-Sunday calendar week in English and Chinese.

## 1.5.2 - 2026-07-28

### Fixed

- Keep Profile activity tooltips aligned with their heatmap cells across browser zoom, display scaling, scrolling, and viewport edges without hover-position jitter.

## 1.5.1 - 2026-07-28

### Added

- Added a synchronized, locally persisted Milestones switch to the Overview and Profile activity legends. Milestone highlighting defaults to off.

### Changed

- Matched milestone badges and cell glows to each active heatmap theme.
- Reworked the Paper activity palette around parchment, warm copper, and aged-ink tones in light and dark modes.

## 1.5.0 - 2026-07-28

### Added

- Added a Profile activity dashboard under Stats with recorded-token, peak-day, active-day, and streak summaries.
- Added synchronized Daily, Weekly, and Cumulative 52-week activity views with token breakdown tooltips, milestone highlights, responsive scrolling, and accessible labels.
- Embedded a compact Profile activity summary in Overview with shared aggregation controls and a direct path to the full Profile view.

### Changed

- Updated the Paper theme heatmap palette and added theme-aware milestone colors while preserving the existing Month, Year, and 3D Stats views.

### Fixed

- Refresh Profile activity after a manual dashboard update while retaining the one-time background warm cache during normal updates.

## 1.4.5 - 2026-07-26

### Added

- Added MiMo Code and Grok Build icons to the supported-client strips in the README, 中文 README, and supported-clients reference, and added the missing Antigravity icon to the latter two.

### Changed

- Show MiniMax's general quota labels as `5-hour` and `Weekly`, and render status-3 weekly allowances as `Unlimited` without treating them as numeric history data.
- Follow MiniMax's explicit remaining-percentage and status fields; ambiguous 0/0 count fields are not used as a fallback.
- Label Kimi Code's plan-wide quota as `Weekly` without changing its stored bucket identity.
- Preserve distinct Kimi explicit-weekly and plan-weekly windows while deduplicating exact echoes.

## 1.4.4 - 2026-07-25

### Added

- Added pricing for `moonshotai/k3-256k` (Kimi K3 256k context variant), aliased as `kimi-code/k3-256k` and `kimi-k3-256k`, using the same pricing as Kimi K3.

## 1.4.3 - 2026-07-25

### Added

- Added pricing for Claude Opus 5 and Opus 5 Fast, plus Gemini 3 Pro Image, Gemini 3.1 Flash Image, and Gemini 3.1 Flash Lite Image.

### Fixed

- Price Grok inference entries during ingestion so persistent usage totals no longer show known Grok models at zero cost.
- Keep persistent Overview and Stats costs aligned with live parsing when a model group mixes priced rows with historical zero-cost placeholders.

## 1.4.2 - 2026-07-24

### Changed

- Display MiniMax's mainland-China region in the quota-card title instead of repeating it on every bucket, and map Kimi's internal paid `LEVEL_*` API enums to its advertised membership names.

## 1.4.1 - 2026-07-24

### Fixed

- Fixed the Grok log-signature test on Windows by using platform-native filename extraction.
- Fixed MiniMax and Grok quota cards showing “not detected” after successful detection when their APIs omit a plan name. Kimi's “Intermediate” suffix remains because it is the membership level returned by Kimi.

## 1.4.0 - 2026-07-24

### Added

- Added opt-in quota polling for MiniMax Token Plans (global and mainland China), Kimi Code, and SuperGrok/Grok Build. Static subscription keys work for MiniMax and Kimi; Grok consumer billing requires the CLI's xAI OAuth sign-in and rejects normal xAI API keys.
- Added separate local-credential-read consent plus allowlisted OpenCode, active Claude-settings, and read-only CC Switch provider discovery for MiniMax and Kimi. Provider logs and arbitrary file references are never scanned.
- Added local Grok Build token and cost tracking from `$GROK_HOME/logs/unified.jsonl`, using per-inference prompt, cache, completion, and reasoning counters with per-process model attribution.
- Added pricing for Gemini 3.5 Flash Lite and Gemini 3.6 Flash.

### Security

- Restricted MiniMax and Kimi quota requests to their official HTTPS hosts before attaching credentials.
- Gated Claude credential-file and macOS Keychain reads behind the separate local-credential consent.

## 1.3.1 - 2026-07-17

### Fixed

- Persistent parse-cache signatures are now content-based instead of path/mtime-based. After the one-time `v1.3.1` signature migration, a reinstall or upgrade that leaves parser code byte-identical (e.g. `pipx upgrade` restamping every installed file) no longer invalidates the entire usage/session store, so later updates avoid a full-corpus reparse caused only by installer timestamps.
- Startup cache warming now also precomputes each Sessions tool panel's default view (today), so the first Sessions tab visit after a server restart no longer pays the codex/claude session-store sync serially per tool.
- Quota polling now samples fixed windows before and after resets without creating provider-wide request bursts: boundary targets are provider-scoped, nearby targets are coalesced, daemon cycles remain at least five minutes apart, HTTP 429 responses are not retried immediately, and post-reset samples stay anchored to the observed reset epoch when a poll crosses the rollover.

## 1.3.0 - 2026-07-16

### Added

- Kimi Code update support: usage tracking for the new `~/.kimi-code` data root and `usage.record` wire schema introduced in Kimi Code 0.26, with automatic fallback to legacy `~/.kimi` installs.
- Kimi K3 pricing update: added `kimi-k3` launch pricing and the `kimi-code/k3` alias.

## 1.2.2 - 2026-07-14

### Fixed

- Unified Codex live-quota window classification across singular, plural, and metered-feature API schemas. Exact 5-hour and 7-day duration metadata is authoritative; without recognized durations, two returned windows retain primary/secondary semantics while a single pair-shaped live window is treated as weekly during Codex temporary 5-hour disablement. Flat legacy API payloads and local session-log behavior remain unchanged.
- Kept quota ingestion and stored raw-history re-derivation on the same shared classifier, with round-trip regression coverage for normal, swapped, partial-duration, weekly-only, legacy, and metered response shapes.

## 1.2.1 - 2026-07-12

### Fixed

- Updated Codex live-quota handling for the temporary disablement of 5-hour windows. Tokdash now classifies returned windows by their reported duration, displays weekly-only limits when Codex omits the 5-hour window, suppresses stale 5-hour cards while retaining their history, and resumes showing both windows automatically when Codex returns both again.

## 1.2.0 - 2026-07-12

### Added

- Interactive `tokdash setup` now offers an explicit update-notice consent step. The prompt defaults to Yes, remains opt-in, and is skipped for automated, non-interactive, and non-TTY setup.
- Added a documentation index at `docs/README.md`, with guides, reference material, and maintainer documentation organized into `docs/guides/`, `docs/reference/`, and `docs/development/`.
- Added a detailed Codex usage-counting design note covering subagent replay detection, safety properties, verification, persistent-store behavior, and the accepted nested-subagent limitation.

### Changed

- Changed `/api/quota/refresh` from `POST` to `GET`. The endpoint polls providers' read-only usage APIs, remains subject to its cooldown and consent settings, and now works through Tailscale Serve, WSL forwarding, and other read-only remote paths.
- Changed `/api/update-check` from `POST` to `GET`. The endpoint performs only the consented, cached PyPI version check and remains separate from the write-gated consent endpoint.
- Updated the dashboard to use the new GET endpoints without CSRF tokens.
- Reorganized documentation into task-oriented guides, API and client references, and development material. Updated both READMEs, CI paths, package metadata, internal references, and public agent guides for the new layout.

### Fixed

- Fixed severe Codex usage inflation caused by MultiAgent V2 `thread_spawn` rollout files replaying their parent thread's `token_count` history. Tokdash now skips direct-parent replay events while preserving genuine subagent usage, primary sessions, and guardian review sessions.
- Fixed Codex Sessions entries being overwritten by partial parent-history replays from subagent rollout files.
- Fixed idle Codex quota windows displaying phantom reset times. A window with 0% usage now reports no reset until its rolling timer actually starts.
- Prevented an idle-to-active Codex quota transition from being counted as false new consumption.
- Made the update-check kill switch consistently recognize `0`, `false`, `no`, and `off`, including mixed-case and whitespace-padded values.

### Upgrade notes

- API integrations calling `POST /api/quota/refresh` or `POST /api/update-check` must switch to `GET`; the old methods are no longer registered.
- The persistent usage store automatically reparses Codex rollout files after the parser upgrade, removing previously stored replay entries without requiring `tokdash db resync`.
- Deduplication currently matches only the declared direct parent. With non-default `agents.max_depth > 1`, grandparent replay events may still be over-counted. This accepted limitation favors visible over-counting over silent deletion of legitimate usage.

## 1.1.5 - 2026-07-11

### Added
- Added pricing for OpenAI `gpt-5.6-luna-pro`, `gpt-5.6-sol-pro`, and `gpt-5.6-terra-pro`; xAI `grok-4.5`; and Tencent `hy3`.
- Documented secure remote access options, including Tailscale Serve and Funnel, Cloudflare Tunnel, and reverse-proxy deployments.

## 1.1.4 - 2026-07-10

### Fixed
- Fixed Codex quota parsing for live API `used_percent` values between 0 and 1: Codex reports percentages on a 0-100 scale, so `1` now means 1% used instead of being normalized to 100% used. A one-time quota DB repair corrects already-stored mis-scaled Codex API rows when their raw payload proves the original value.

### Added
- Added official OpenAI GPT-5.6 standard short-context pricing for `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna` to the bundled pricing DB.

## 1.1.3 - 2026-07-07

### Fixed
- Codex quota consumption now treats live API polling as authoritative when enabled: stale `codex_session` snapshots are excluded from both quota history and current quota cards, and session-log-only Codex quota data is clearly marked as estimated.
- Added a guard for reset-boundary torn reads so impossible same-window spikes or carry-over samples do not distort quota history points or consumption bars.

## 1.1.2 - 2026-07-06

### Added
- Added Mimo / Mimocode usage and session support from `~/.local/share/mimocode/mimocode.db`, including the Sessions tab, API/docs entries, and EN/CN frontend labels.

### Fixed
- Mimo now follows the OpenCode-style native SQLite design with SQL date windows, WAL/SHM-aware freshness checks, project worktree joins, and native session summaries.
- Excluded Claude Code history imported into Mimo from Mimo usage and session totals, preventing double-counting with Tokdash's Claude parser.

## 1.1.1 - 2026-07-05

### Fixed
- **Model name normalization for date-snapshot suffixes.** Providers append release-date snapshots to model IDs (e.g. `volcengine-coding-plan/glm-5-2-260617` = 2026-06-17). The normalizer and pricing resolver previously stripped only `YYYY-MM-DD` / `YYYYMMDD`, so `YYMMDD` snapshots split into separate dashboard rows and priced as $0. The backend normalizer, pricing resolver, and the client-side JS normalizer now strip `YYMMDD` (with month/day bounds so arbitrary numeric identifiers are preserved), and the client-side normalizer was synced with the backend to stop frontend/backend label drift. 4-digit `YYMM` is stripped only in the DB-aware pricing resolver — where exact-match-first protects canonical version stamps like `mistral-large-2512` — never in the grouping normalizer, so distinct priced models stay distinct in the combined view. Adds a node-based frontend/backend normalizer-sync guard.
- **Codex rolling-window quota consumption.** Codex's 7-day quota buckets are rolling windows where older usage can age out while the reset timestamp stays stable, so a later climb below the prior high is genuine new consumption but was treated as noise, undercounting daily usage. Quota history now uses adjacent-delta semantics for Codex `7d` / `*_7d` buckets and any bucket with a missing reset timestamp, with a recovery band that suppresses a transient low reading recovering to the prior high as fake usage. Fixed-window buckets (Claude weekly, distinct reset epochs) keep the existing running-high behavior.

## 1.1.0 - 2026-07-03

### Added
- Added Antigravity CLI (`agy`) token-usage parsing from `~/.gemini/antigravity-cli/conversations/*.db`, including WAL-aware change detection, protobuf wire decoding without a new runtime dependency, and pricing aliases for raw Gemini 3 Antigravity model IDs.
- Dashboard update notice: when the opt-in update check is enabled, the header shows a dismissible "Update available: vX.Y.Z" badge with a copyable `tokdash update` command; when disabled, a muted one-click "Enable update notices" link performs the consent. The web UI only reports availability; it never runs upgrades, and no network check happens without consent.
- `tokdash doctor` now reports the Tokdash version (first line of the human output; `version` field in `--json`).

### Changed
- Reworded the quota data-source terminology everywhere it surfaces (setup wizard, Quota-tab consent cards, per-bar source chip, README): "session" vs "API" is now "local logs" vs "live polling", each stated with its consequences — local logs are Codex-only, update only when Codex runs, and never contain reset credits or metered-feature windows; live polling is fresher, adds those, and is the only quota source for Claude Code and Antigravity.

### Fixed
- Fixed Quota-tab consumption history for reset-window rollovers, transient dips, interleaved account windows, and small reset-time jitter; consumption now counts only increases above each reset window's running high.

## 1.0.7 - 2026-07-02

### Added
- Added a **Quota tab** tracking subscription quota for Codex, Claude Code, and Antigravity: per-window remaining bars with reset countdowns, Codex reset-credit inventory, remaining/consumption history charts with range and provider-visibility controls, and per-provider consent cards. Provider API polling is opt-in per provider and **off by default**; without consent the tab uses local data only (see "Quota tracking (optional)" in the README and `docs/SECURITY.md`).
- Added quota API routes: `GET /api/quota` and `GET /api/quota/history`, plus write-gated `POST /api/quota/consent`, `POST /api/quota/settings`, and `POST /api/quota/refresh` (60-second cooldown). Documented in `docs/reference/API.md`.
- Added `tokdash quota poll|show|consent` CLI verbs, an optional quota step in interactive `tokdash setup`, quota state in `tokdash doctor`, and `tokdash export --include-quota` (exports exclude quota data by default).
- Added a background quota poller to `tokdash serve` (default every 30 minutes; `quota.poll_interval_minutes` in `config.json`, `TOKDASH_QUOTA_POLL_INTERVAL` env override, `TOKDASH_QUOTA_POLL=0` kill switch, `quota.enabled` master switch) with incremental watermark-based Codex session ingestion and a one-time history backfill.
- Added `TOKDASH_QUOTA_RETENTION_DAYS` opt-in retention pruning for stored quota snapshots (default: off — snapshots are kept indefinitely).
- Added macOS Keychain support for Claude quota credentials: Tokdash reads `CLAUDE_CODE_OAUTH_TOKEN` first, then `.credentials.json`, then the `Claude Code-credentials` Keychain item (read-only, via `security find-generic-password`). The env var is the locked/headless-Keychain override and short-circuits the Keychain subprocess. Verified by a macOS CI job (new `macos-latest` matrix entry) — the first macOS CI coverage for the platform's experimental support.

### Changed
- Usage database schema v4 → v5 (additive): new `quota_snapshots` and `quota_file_state` tables.
- Codex path resolution (`sessions/`, `state_5.sqlite`) now honors `$CODEX_HOME` across the whole usage pipeline, and a new `$CLAUDE_CONFIG_DIR`-aware resolver locates Claude Code credentials for quota tracking.

## 1.0.6 - 2026-07-01

### Added
- Added Claude Sonnet 5 introductory API pricing (`claude-sonnet-5`, plus `sonnet-5`, `sonnet5`, and dated aliases) to the bundled pricing database.

### Fixed
- Manual dashboard Refresh now forces `/api/usage` recomputation while preserving cache backpressure protections, and the button surfaces refreshing, cached, busy, and failure states instead of appearing to do nothing on cache hits.
- Successful automatic refreshes now clear stale transient manual-refresh labels, and the failure button copy now prompts a retry.

## 1.0.5 - 2026-07-01

### Added
- Added native Windows support seams and CI coverage, including Windows-aware client path resolution, `msvcrt` file locking, Task Scheduler onboarding support, Windows venv path handling, and PowerShell statusline documentation.
- Added Windows-focused tests for file locking, client path discovery, service selection, and Task Scheduler rendering.

## 1.0.4 - 2026-06-22

### Added
- Added ready-made Claude Code statusline templates under `docs/guides/statusline/`: a minimal one-line script and a fuller multi-row dashboard script that read local Tokdash usage totals without calling mutating endpoints.

### Fixed
- Hardened interactive Tailscale Serve setup so the targeted teardown command is recorded before exposure, failed Serve attempts reconcile the manifest back to the unset state, and post-success URL write failures warn without crashing setup.
- Tailscale Serve status parsing now always reports the path-scoped `/tokdash` URL instead of accidentally advertising the tailnet host root.
- Manifest writes and dashboard pricing override writes now clean up `.tmp` sidecars after failed atomic writes.
- The write-protection loopback check now parses real IP literals, rejecting spoofed host strings such as `127.0.0.1.evil.com`.
- The full statusline template now supports macOS/BSD `date` for rate-limit countdowns and clamps only the visual context bar when usage exceeds 100%.

## 1.0.3 - 2026-06-21

### Changed
- `tokdash update` now reports the managed runtime's Tokdash version before and after the upgrade command. If the version is unchanged, human output says Tokdash is already at that version instead of implying a new package was installed; `--json` includes `version_before`, `version_after`, and `updated`.
- README and onboarding docs now include the explicit migration command for switching an existing conda/system/user-pip service to Tokdash's managed venv runtime: `tokdash setup --runtime venv --force`.

## 1.0.2 - 2026-06-21

### Fixed
- Interactive `tokdash setup` now opens the dashboard with a detached platform opener whose stdout/stderr are redirected away from the terminal, preventing Chromium/Chrome GPU, voice, TensorFlow Lite, and GCM logs from appearing after setup completes.

## 1.0.1 - 2026-06-21

### Fixed
- `tokdash setup` no longer fails before writing the setup manifest when a slow `systemctl restart` times out client-side but the Tokdash service becomes healthy. The setup flow now records the restart diagnostic, verifies the `/health` fingerprint, and succeeds only when the configured port is actually serving Tokdash.
- macOS launchd setup now follows the same readiness-driven behavior for slow `launchctl bootout` / `bootstrap` calls, with longer lifecycle command timeouts and regression coverage for timeout and fail-closed cases.

## 1.0.0 - 2026-06-21

### Added
- Python-native lifecycle commands: `tokdash setup`, `doctor`, `update`, and `uninstall`. `setup` configures a reversible user-level background service (systemd user service on Linux/WSL2, launchd LaunchAgent on macOS) with no shell scripts and no `sudo`; `doctor` diagnoses runtime/service/port health; `update` upgrades a setup-owned runtime (pipx or managed venv) in place and restarts the service; `uninstall` reverses exactly what setup created, driven by a `<data_dir>/install.json` manifest and ownership markers, keeping usage history unless `--purge`. All commands support `--auto`/`--json` for bundlers and `--dry-run`. See `docs/guides/ONBOARDING.md`.
- Optional, default-off update check (`TOKDASH_UPDATE_CHECK=1` or persisted consent via `POST /api/update-check/consent`): `tokdash doctor` and `POST /api/update-check` report whether a newer version is on PyPI (PEP 440 comparison). No automatic background checks; it only reports, never upgrades.
- Dashboard pricing edits now persist to a user override at `<data_dir>/pricing_db.json` instead of the packaged baseline, so they survive `tokdash update` / a pip reinstall and work on a read-only install. The override fully replaces the baseline (WYSIWYG: deletions stick); a missing/corrupt override falls back to the shipped baseline.

### Changed
- README Quick start now uses the onboarding lifecycle (`tokdash setup` / `doctor` / `update` / `uninstall`) as the default path and removes the old manual systemd/update walkthrough from the main flow.
- Human onboarding output now uses terminal colors when stdout is a real TTY, while `--json` and captured/scripted output remain plain.

### Fixed
- Dashboard pricing edits now correctly invalidate pricing-dependent API responses, coding-tools and OpenClaw cost caches, session pricing, and the persistent usage store, so edited rates take effect immediately across Overview/Usage/Tools; previously those layers could keep serving stale costs after an edit or out-of-band override change.
- `tokdash setup` now verifies that systemd loaded the unit file setup wrote and that the configured port answers with Tokdash's `/health` fingerprint before reporting success; `doctor` flags service/port mismatches, and `uninstall` will not stop a same-named foreign systemd service while cleaning up a setup-written unit.
- `tokdash setup --force` can now migrate pre-1.0 manual `tokdash.service` installs that already occupy the target port but lack the new `/health` fingerprint; setup rewrites and restarts the unit before readiness probing.
- Interactive `tokdash setup` now handles Tailscale's "serve config denied" failure by offering the one-time `sudo tailscale set --operator=$USER` operator grant and retrying `tailscale serve`.
- After a successful interactive Tailscale Serve setup, `tokdash setup` now prints and records the actual `https://...ts.net/tokdash` URL from `tailscale serve status`, uses a path-scoped Serve rule so the tailnet host root remains available for other services, and hides the generic remote-access hint from the final success output.
- `tokdash uninstall` no longer reports success (and deletes the manifest) when a systemd/launchd stop fails: a failed stop is recorded as an error, leaving the unit and manifest in place for retry.
- `tokdash update` reports a failed service restart with the platform-correct remediation command instead of crashing with a traceback when `systemctl`/`launchctl` hangs.
- The write-protection gate returns `403` (not `500`) on a malformed `Referer` header.

## 0.6.2 - 2026-06-19

### Added
- Added Pi session drill-down support, Codex review-session (auto-permission approval) visibility controls, native session display names, and `scripts/benchmark_api_latency.py` for comparing stable/dev HTTP endpoint latency. The live benchmark can also be run from pytest with `TOKDASH_RUN_API_BENCHMARK=1`.

### Changed
- Codex session names now come from Codex's local `state_5.sqlite` thread titles when available, with a read-only/query-only SQLite lookup and a 50 ms busy timeout. Pi sessions use `session_info.name` when present and otherwise fall back to the first user message instead of only the project directory.
- The Sessions frontend now fetches tools independently with short 503 retries, keeps review sessions (auto-permission) hidden by default, supports showing them from a persisted toggle, and includes Pi in the per-tool and combined session views.


## 0.6.1 - 2026-06-17

### Changed
- Made Claude session reads much faster by merging stored session records in one pass instead of repeatedly re-sorting and de-duplicating resumed sessions.
- Made OpenCode session reads much faster by pushing date windows into OpenCode's native SQLite query and extracting token/model fields with SQLite JSON functions, with raw JSON fallback when needed.

### Fixed
- Added regression coverage for Claude same-timestamp session merge ordering and OpenCode session window boundaries, malformed JSON handling, multi-session fallback, and API window propagation.

## 0.6.0 - 2026-06-16

### Added
- Added a default-on persistent SQLite usage index at `~/.tokdash/usage.sqlite3`. It stores normalized usage rows and Codex/Claude session summaries so repeated dashboard/API reads can use indexed SQL instead of reparsing every source log. Disable it with `TOKDASH_USAGE_DB=0`, move it with `TOKDASH_USAGE_DB_PATH` or `TOKDASH_DATA_DIR`, and control missing-source retention with `TOKDASH_USAGE_DB_DURABLE`.
- Added `tokdash db status`, `sync`, `resync`, `verify`, `repair`, and `watch` for inspecting, rebuilding, validating, repairing, and periodically syncing the local usage DB. `TOKDASH_USAGE_DB_WATCH=1` enables the same polling sync loop inside `tokdash serve`; `TOKDASH_USAGE_DB_WATCH_INTERVAL` controls the interval.
- Added Cloudflare GLM-5.2 pricing (`glm-5.2`, input $1.40/M, output $4.40/M, cached read $0.26/M).

### Changed
- Dashboard usage aggregation now uses the persistent DB for the file-backed coding-tool and OpenClaw paths where possible, with live-parser fallback if the DB is disabled or unavailable. OpenCode continues to use its native SQLite source for windowed reads.
- Local cold-parser benchmarks on a real multi-agent log corpus show about 30x faster usage scans than pre-0.6.0 Tokdash and 15x faster Overview today latency than `ccusage daily --json --offline`.

### Fixed
- OpenClaw token counting excludes snapshot/checkpoint/backup/sidecar transcripts, deduplicates message ids, and ignores all-zero assistant usage rows, correcting inflated totals from duplicated transcript copies.
- Added per-test usage DB isolation so the default-on persistent DB cannot leak cached rows between fixtures.

## 0.5.7 - 2026-06-12

### Fixed
- Claude Code session parsing now reads the role-less `type:"assistant"` streaming-snapshot format emitted by newer CLI builds (observed on 2.1.173+ via OpenAI-compatible endpoints). These assistant turns were previously skipped entirely, under-counting tokens and cost for affected sessions. Duplicate streaming snapshots are deduplicated by message id, keeping the latest (most complete) usage.

## 0.5.6 - 2026-06-09

### Added
- Added Claude Fable 5 pricing and shorthand aliases (`fable-5`, `fable5`, and `fable`) to the bundled pricing database.

## 0.5.5 - 2026-06-05

### Fixed
- Pricing lookup now strips common quantization and precision suffixes such as `-FP8`, `-FP16`, `-INT8`, and `-AWQ`, so provider IDs like `vllm-hpc/qwen3.6-27B-FP8` resolve to the base model price instead of showing as zero-cost.

## 0.5.4 - 2026-06-03

### Added
- `scripts/bench_openclaw.py` — a local benchmark helper for validating OpenClaw parser totals and cold/warm parse latency across common windows.
- `docs/guides/agents/systemd/health-probe/` — an optional systemd user timer + oneshot that restarts Tokdash if `/health` stops answering after several short attempts, turning an "alive but wedged" hang into automatic recovery.

### Changed
- **OpenClaw cold-start performance.** OpenClaw session parsing now caches parsed entries by file signature and filters by date from memory, so repeated Overview/Stats calls no longer re-read the full OpenClaw log set. Startup warming also precomputes the dashboard's initial Overview and Stats cache keys, the Overview tab defers `/api/sessions` calls until the Sessions tab opens, and the frontend prefetches Stats in the background.
- **Overload resilience.** Under a heavy request burst the server could become unresponsive while the process stayed alive (so `systemctl` still reported it healthy). The response cache now does **single-flight with stale-while-revalidate** — concurrent refreshes for the same stale key collapse into one compute and readers get the last value instead of stampeding the parser — and a **global heavy-compute cap** (`TOKDASH_COMPUTE_CONCURRENCY`, default 2) keeps a burst of cold requests from saturating the worker pool. Cold misses over the cap now return `503` quickly instead of queuing inside worker threads. The `/health`, dashboard, manifest, and service-worker handlers are now async so liveness/health probes keep responding even while every worker is busy. `serve` also passes uvicorn backpressure limits (`TOKDASH_LIMIT_CONCURRENCY` default 64, `TOKDASH_KEEPALIVE` default 5).
- README (English + 中文): documented the new overload/backpressure environment knobs and the optional `/health` watchdog.

### Fixed
- **OpenClaw duplicate token accounting.** Snapshot/checkpoint/backup/sidecar files such as `*.checkpoint.*.jsonl`, `*.jsonl.bak-*`, `*.trajectory.jsonl`, and `*.acp-stream.jsonl` are excluded from usage parsing, entries are deduplicated by message id, and all-zero assistant usage rows are ignored. This corrects inflated OpenClaw totals caused by duplicated transcript copies.
- **Pricing DB cache invalidation race.** Pricing updates now reload session pricing before clearing the API response cache, and in-flight computations that started before a cache clear can no longer repopulate stale results.
- **Frontend `503` handling.** Overview and Sessions now treat fail-fast backpressure responses as errors, keep the last good data on screen, and show a temporary busy status instead of rendering the error body as zero/NaN metrics.

## 0.5.3 - 2026-06-03

### Changed
- Updated `src/tokdash/pricing_db.json` from pricing DB `2.0.5` to `2.0.7` (`lastUpdated: 2026-06-02T22:39:42Z`). This adds 59 model pricing entries from the pricing-updater proposal, including new Anthropic fast variants, MiniMax M3, GLM vision/exacto entries, OpenAI `gpt-5.5-pro` / `gpt-chat-latest`, additional Gemini/Gemma, Mistral, Qwen, Perplexity Sonar, xAI Grok, Cohere, Baidu, Reka, StepFun, and Tencent models.

## 0.5.2 - 2026-06-02

### Added
- **Install button for the PWA.** When the dashboard is installable (Chromium browsers, served with the manifest + service worker), an **Install** button appears in the header toolbar so you can pin Tokdash as a desktop/mobile app in one click. It hides itself automatically when the app is already installed or when the browser exposes no install prompt (e.g. iOS Safari).
- **History-retention guidance.** The README now warns that Claude Code and Gemini CLI delete local sessions older than about 30 days by default, `tokdash serve` prints a one-time reminder with `TOKDASH_NO_RETENTION_NOTICE=1` as an escape hatch, and `docs/reference/HISTORY_RETENTION.md` records the per-client retention survey plus the config-based fix.

### Changed
- Renamed the **pi-agent** client to **Pi** across the dashboard and docs. The detection path (`~/.pi/agent/sessions/`) and the `PI_AGENT_DIR` override are unchanged — this is a display-name change only.
- README (English + 中文): added an agent logo strip under the tagline and moved the detailed client list + log paths to [`docs/reference/SUPPORTED_CLIENTS.md`](../reference/SUPPORTED_CLIENTS.md). Demo links now point at `tokdash.github.io/demo/` (the root `tokdash.github.io` is the project home page).
- Deferred the in-app snapshot-store design in favor of keeping each client's own logs, with the full design retained in `docs/SNAPSHOTS_PLAN.md` for future revisit if client retention policies change.

### Fixed
- **Stats tab first-load blank state.** A slow or interrupted first `/api/stats` request could leave the Stats tab showing all-zero summary values and empty calendars, and clicking Month/Year during the failed load made the blank state look permanent. The calendar now shows a loading/error banner with Retry, ignores stale overlapping stats responses, and avoids rendering empty grids until the first successful stats load.

## 0.5.1 - 2026-06-01

### Added
- **`tokdash serve` now opens the dashboard in your browser on startup**, with a new `--no-open` flag to disable it. Auto-open is skipped automatically in headless contexts — CI (`CI` env var), SSH sessions (`SSH_CONNECTION`/`SSH_TTY`), and Linux without an X11/Wayland display — and the bundled systemd/launchd service templates now pass `--no-open`. The browser launch fires from a short-delay daemon timer so the server is listening before the page loads, and any failure to open is swallowed so it can never take down the server. (Thanks @KurokawaShiorei for the original contribution in #5.)

## 0.5.0 - 2026-05-30

### Added
- **Sortable columns in the Overview breakdown tables.** Tools Breakdown, Apps & Models Breakdown, and Combined Models now support click-to-sort on every column, mirroring the Sessions ranking: click a header to sort, click again to flip direction (numeric columns rank high→low first, the name column A→Z), with a ▲/▼ indicator on the active column. In Apps & Models all per-app sub-tables sort in lockstep, and the active-sort indicator survives a language toggle. The Combined Models list sorts before its top-N cap, so the cap reflects the chosen sort.

### Fixed
- **Apps & Models Breakdown column alignment.** Each app renders as its own table, which under auto layout sized columns independently so they did not line up across apps. The sub-tables now use a fixed layout with a shared column template, so Input/Output/Cache/… align vertically across every app (long model names ellipsize with a hover tooltip and the table scrolls horizontally on very narrow screens).
- **Overview "Total Tokens" overflow under wide date ranges.** Large totals (e.g. hundred-million+ under "This Year") overflowed the narrow KPI card. The Total Tokens / Total Cost / Total Messages values now shrink to fit their card only when the number would actually overflow — measured against the real card width, and re-fit on resize. Normal/small values are unchanged.

## 0.4.1 - 2026-05-30

### Fixed
- **`period=all` / `period=year` silently returned today only.** `period_to_days()` mapped every unrecognised named period — including `all` and `year` — to 1 day, so `/api/usage?period=all` and `?period=year` returned just the current day's data and looked like a large undercount. Named periods now resolve correctly (`year` → 365 days, `all` → all-time), and any unknown period defaults to all-time (which visibly over-reports) rather than collapsing to today. The dashboard UI was unaffected — it sends explicit `date_from`/`date_to` ranges — so this only bit direct API callers.
- **`/api/sessions` and `/api/usage` disagreed on named periods.** `sessions.py` carried its own copy of the period→days mapping that still collapsed `year`/`all`/unknown to today, so `/api/sessions?period=all` behaved like today while `/api/usage?period=all` spanned all-time. `sessions` now delegates to the single canonical mapping in `compute`, keeping both endpoints consistent (with a regression test locking the alignment).

### Changed
- Polished the README header (English + 中文): the logo, tagline, badges, and demo callout are now centered, and the wordmark logo serves as the title (the redundant text heading was removed).

## 0.4.0 - 2026-05-29

### Added
- **Cache Hit Rate** across the dashboard. The metric is the token-weighted share of *prompt input* served from cache — `cacheRead / (input + cacheWrite + cacheRead)` — matching the published definitions of DeepSeek (`prompt_cache_hit_tokens / prompt_tokens`), Anthropic, OpenAI, and Gemini. Cache *writes* (cache creation) count as misses (they are prompt input not served from cache), and output/reasoning tokens are excluded. It appears as: an `Avg Cache Hit Rate` KPI card on the Overview header; a `Hit %` column in the Tools Breakdown, Apps & Models, and Combined Models tables (and per-app in the breakdown headers); a `Hit %` column plus a per-session figure in the Sessions tab (Codex/Claude/OpenCode/Combined); and a `Cache Hit Rate` figure in the Stats Month panel and the Day Details panel (Month and Year tabs). Sources that do not report cache data show `n/a`. Backend exposes `cache_hit_rate` on `/api/usage` (header + `by_tool` + `combined_models`), `/api/tools`, `/api/openclaw`, and `/api/sessions` / `/api/session` (per-session and per-turn).

### Fixed
- **Gemini CLI token & cost double-count.** Gemini CLI logs `tokens.input` *inclusive* of the cached prompt tokens (`tokens.cached`), but the parser previously also added `cached` separately as cache-read, counting those tokens twice in totals and cost on every cache-hit turn. The parser now subtracts (`input = tokens.input − tokens.cached`) to recover the fresh/uncached portion — matching how the Codex and Copilot parsers already handle cache-inclusive input. Effect: Gemini CLI total tokens and cost now match Gemini's own reported `total` (they decrease for sessions with cache hits); all other tools are unaffected. The session-level `cache_ratio` (cacheRead ÷ all tokens, incl. output) is retained for back-compat but is no longer surfaced as a hit rate; the Sessions panel now shows the faithful `cache_hit_rate`.

## 0.3.3 - 2026-05-29

### Added
- Added Claude Opus 4.8 pricing entry (`claude-opus-4.8`) with the same rates as Opus 4.7 (input $5 / output $25 per 1M; cache read $0.50 / cache write $6.25), plus an `opus-4.8` shorthand alias. Names such as `claude-opus-4-8` and `opus-4.8` normalize to the canonical entry.

## 0.3.2 - 2026-05-26

### Fixed
- Claude Code sessions from third-party builds that write a zero-token placeholder entry before the real assistant entry (sharing the same `message.id`) are no longer silently dropped. The deduplication step now ignores placeholders so the real usage gets counted. In practice this restores token, cost, and session-count data from `~/.claude-mi` (mimo-v2.5) and `~/.claude-infini` (glm-5.1) installs; the official `~/.claude` build is unaffected.

## 0.3.1 - 2026-05-25

### Added
- Added Xiaomi MiMo V2.5 pricing entries: `mimo-v2.5` (input $0.40 / output $2 per 1M) and `mimo-v2.5-pro` (input $1 / output $3 per 1M), matching OpenRouter's published rates.
- Added a `Monthly Totals` table below the Year heatmap on the `Stats` tab, showing per-month total tokens, total cost, and total energy for the selected year (Jan through the current month for the current year; full year otherwise).
- Added `Total Tokens` and `Energy` columns to the `Models Used` table in the Day Details panel.
- Added click-to-navigate from the Year view to the Month view: clicking a month label above the year heatmap, or a row in the Monthly Totals table, jumps to that month.

### Changed
- Reorganized the Month Stats sidebar into a 2-column grid so the panel takes roughly half the previous vertical space.

### Fixed
- Year heatmap previous/next arrow buttons now update the title and grid immediately on click instead of waiting for the async year-stats fetch to complete, so rapid back-to-back clicks register correctly.

## 0.3.0 - 2026-05-21

### Added
- Added support for pi-agent token usage parsing from ~/.pi/agent/sessions/. Override the location via the `PI_AGENT_DIR` env var (comma-separated list of dirs). Captures input/output/cache tokens and per-message cost when present.
- Added support for Hermes agent token usage parsing from ~/.hermes/state.db. Override the location via the `HERMES_HOME` env var. Reads session-level aggregates including per-session message counts, reasoning tokens, and recorded cost (with pricing-table fallback for subscription-included sessions where Hermes records a zero cost).
- Added support for GitHub Copilot CLI token usage. Full input/cache/reasoning/cost data is read from OpenTelemetry exporter JSONL at ~/.copilot/otel/ or the file pointed at by `COPILOT_OTEL_FILE_EXPORTER_PATH`. For sessions without OTel enabled, output-only token counts are recovered from ~/.copilot/session-state/*/events.jsonl as a fallback.
- Added [`docs/reference/API.md`](../reference/API.md) — full HTTP API reference for the Tokdash server, intended for building external integrations (e.g. Claude Code statusline items, IDE plugins, custom dashboards).

### Notes
- To capture full GitHub Copilot CLI usage (input + cache + cost), set `COPILOT_OTEL_FILE_EXPORTER_PATH` in your shell profile before launching the Copilot CLI; e.g. `export COPILOT_OTEL_FILE_EXPORTER_PATH="$HOME/.copilot/otel/usage.jsonl"`. Without this, Tokdash will still surface output-token counts from the local events log.
- The Sessions tab does not yet support pi-agent, GitHub Copilot CLI, or Hermes — these agents currently appear only in Overview/Stats aggregates. Per-session drill-down is planned for a follow-up.
- **Statusline integration**: Tokdash's local HTTP API can power a Claude Code (or any other agent) statusline item showing live token/cost stats. Hand your coding agent the prompt below, plus [`docs/reference/API.md`](../reference/API.md) for endpoint details:
  > *"I would like to add a statusline item from the tokdash endpoint's API; it should show the total tokens used today."*

## 0.2.7 - 2026-05-20

### Added
- Added local benchmark scripts for parser-cache and API endpoint latency checks.

### Fixed
- Included all local `.claude*` project directories when parsing Claude Code usage and session drill-down data, so alternate Claude installs are counted with the default `~/.claude/projects` logs.

## 0.2.6 - 2026-05-11

### Changed
- Updated the Tokdash logo across the dashboard header, PWA icons, and README assets.

## 0.2.5 - 2026-05-10

### Added
- Added an opt-in **Energy** metric on the `Stats` tab: a `Total Energy (kWh)` row in the Month Stats sidebar, an `Energy` field in the Day Details modal, and a fourth `Energy` button in the Daily Activity metric switcher that recolors the heatmap, 3D cubes, and Peak Day / Peak Week / Peak Weekday / Avg-Active-Day insight cards. `Overview`, `Sessions`, `Pricing`, and `/api/*` responses are unchanged.
- Energy is estimated entirely in the frontend from the existing token breakdown using model-family `(prefill, cached, decode)` Joule-per-token coefficients derived from TokenPowerBench (AAAI 2026) and "How Hungry is AI?" (Jegham et al., 2025). Order-of-magnitude accuracy; intended for relative trends rather than absolute reporting. Month totals are shown in kWh; day details and metric values auto-format as Wh or kWh.

## 0.2.4 - 2026-04-24

### Added
- Added a dashboard `Pricing` tab, contributed by StormTian, for viewing, formatting, validating, reloading, and saving the packaged `pricing_db.json` from the local Tokdash UI.
- Added `/api/pricing-db` read and write endpoints with JSON parsing, schema-shape validation, atomic file replacement, and test coverage for valid saves, invalid JSON, and missing `models` data.
- Added `gpt-5.5` pricing support to the local pricing database and release-safe contract tests.
- Added `deepseek-v4-pro` pricing from OpenRouter at `$1.74` input / `$3.48` output per million tokens.
- Added `deepseek-v4-flash` pricing from OpenRouter at `$0.14` input / `$0.28` output per million tokens.
- Added `kimi-k2.6` Moonshot AI pricing at `$0.95` input / `$4.00` output / `$0.16` cache-read per million tokens, including `k2p6`, `k2-6`, `kimi-2.6`, `kimi2.6`, and `moonshot-ai/kimi-k2.6` aliases.

### Changed
- Normalized saved pricing JSON through the editor API so dashboard edits produce stable, readable formatting before replacing the on-disk database.
- Expanded Kimi model normalization so K2.6 variants group under `kimi-k2.6` without collapsing into the existing `kimi-k2.5` dashboard bucket.
- Extended pricing contract coverage so newly added DeepSeek V4 and Kimi K2.6 entries are verified through the same `PricingDatabase` lookup path used at runtime.

### Fixed
- Cleared cached API responses after pricing database saves so refreshed dashboard views use the updated pricing file.
- Reloaded the session-level pricing database and cleared parsed session caches after pricing edits, preventing already-parsed Codex, Claude Code, and OpenCode session detail costs from staying stale until process restart.

## 0.2.3 - 2026-04-16

### Added
- Added `claude-opus-4.7` pricing to the local pricing database with the same rates as `claude-opus-4.6`, plus `opus-4.7` shorthand alias coverage.

## 0.2.2 - 2026-04-15

### Added
- Added regression coverage for OpenClaw's inner message timestamps and archived/checkpoint transcript discovery.

### Changed
- Reworked coding-tool parsing caches so repeated API requests can reuse short-lived file signatures, shared parser results, and bounded OpenCode query caches instead of rescanning logs for each date switch.

### Fixed
- Updated OpenClaw date filtering to prefer each assistant message's inner `message.timestamp`, with fallback to the outer entry timestamp and file mtime, matching current OpenClaw transcript semantics more closely.
- Restored OpenClaw scanning for archived `.jsonl.deleted.*`, `.jsonl.reset.*`, and checkpoint `.jsonl` transcripts while still excluding `.lock` files.

## 0.2.1 - 2026-04-09

### Added
- Added `Paper`, `Liquid`, `Vibrant`, `Midnight`, `Terminal`, `Brutalist`, `Arcade`, and `Studio` dashboard style themes, with localized labels in English and Chinese.
- Added a dedicated `docs/development/RELEASING.md` checklist and linked it from `docs/CONTRIBUTING.md` so the manual tag, push, GitHub Release, and verification steps stay documented.

### Changed
- Moved theme-specific palettes and overrides out of `src/tokdash/static/index.html` into standalone static assets, reducing dashboard-shell sprawl and making future theme work easier to maintain.
- Expanded the style selector into a broader theme gallery while keeping light/dark mode compatibility across the dashboard.

### Fixed
- Fixed charts, heatmaps, and browser `theme-color` metadata to stay synchronized with the selected style theme in both light and dark mode.

## 0.2.0 - 2026-04-09

### Added
- Added calendar-based custom date range selection with quick presets spanning `Yesterday`, rolling day/week windows, month presets, and year presets.
- Added a `Style` selector in the dashboard header with `Classic` and `Elevated` presentation modes, alongside the existing light/dark theme toggle.
- Added `GLM-5.1` pricing and alias resolution (`glm5.1`, `glm-5-1`, `z-ai/glm-5.1`, `zhipu/glm-5.1`) to the local pricing database.

### Changed
- Reworked the dashboard header controls so the date picker, quick-range actions, refresh button, language toggle, theme toggle, and style selector align more cleanly across desktop widths.
- Expanded packaged static assets to include the full `static/` tree, ensuring icons, manifest assets, and service-worker resources ship with the installed package.
- Switched service-worker cache versioning to a content-derived cache name so upgraded installs pick up new static assets more reliably.

### Fixed
- Fixed custom date-range requests to serialize local calendar dates correctly instead of drifting backward in UTC-positive timezones.
- Fixed API validation for incomplete, malformed, and reversed `date_from` / `date_to` query pairs.
- Applied no-cache headers consistently to the dashboard shell, service worker, manifest, and static assets to reduce stale-client behavior after upgrades.
- Hardened release metadata validation so packaging checks continue to work with the current static-version layout and remain compatible with future dynamic-version setups.

## 0.1.0 - 2026-03-31

### Changed
- Promoted tokdash to its first minor release after stabilizing the new multi-tool Sessions workflow introduced in `0.0.13`.
- Refined the Sessions tables with aligned grouped summary rows so headers, project summaries, and nested session rows line up consistently across Codex, Claude Code, OpenCode, and combined views.
- Added click-to-sort ranking on the session tables for numeric and time columns: input, cache, output, total tokens, cost, and last updated.

### Fixed
- Fixed grouped project ordering so project rows now follow the active selected sort mode instead of staying token-sorted underneath a different header state.
- Fixed `Last updated` sorting to compare real timestamps instead of plain strings.
- Fixed GitHub CI to install dev requirements before running tests, ensuring `httpx` is available for the API smoke test path.

## 0.0.13 - 2026-03-31

### Added
- Added a dedicated `Sessions` page with Codex, Claude Code, OpenCode, and combined cross-tool session views.
- Added per-session drill-down charts, including cumulative token trends over turn order and over time.
- Added `Total Messages` to the Overview KPI bar, alongside period-over-period comparisons for tokens, cost, and messages.

### Changed
- Moved session analysis out of the Overview page so the top-level dashboard stays focused on aggregate usage.
- Changed comparison semantics to use prior full calendar blocks: `today` now compares to the full previous day, fixed `N`-day ranges compare to the previous full `N` days, and `month` compares to the full previous calendar month.

### Fixed
- Fixed Claude Code session undercounting by merging subagent transcript files that share the same session ID.
- Removed the OpenCode session display cap so long-range views no longer hide many sessions.
- Replaced the old Codex-only session backend path with the shared multi-tool session API used by the new dashboard.
- Added the explicit `httpx` dev dependency required by the API smoke tests and removed stale dead code from the previous Codex-only implementation.

## 0.0.11 - 2026-03-20

### Fixed
- Restored the multilingual README setup with cross-links between the English and Chinese docs.
- Added `README_CN.md` as the Chinese project README.
- Restored dashboard language switching between English and Chinese, with browser-language detection used as the default.
- Restored automatic night mode plus a manual light/dark toggle in the dashboard.
- Preserved the current Stats calendar view when switching language or theme.

## 0.0.10 - 2026-03-20

### Reverted
- Removed the unmerged multilingual README additions and deleted the Chinese README variant.
- Reverted the dashboard language toggle, browser-language auto-selection, automatic night mode, and manual light/dark theme toggle to restore the previous light-only UI.

## 0.0.9 - 2026-03-16

- Renamed the Kimi tool label to `Kimi CLI` in the dashboard.
- Sorted Tools Breakdown views by token count in descending order.
- Bumped the package version to `0.0.9`.

## 0.0.8 - 2026-03-16

### Pricing DB
- Major pricing database overhaul: 61 models -> 137 models across 8 providers.
- Added DeepSeek (11 models), Xiaomi/MiMo (1 model) as tracked providers.
- Updated all existing model prices from OpenRouter + official provider USD pricing pages (docs.z.ai, platform.minimax.io, platform.moonshot.ai, api-docs.deepseek.com).
- Applied conservative `max(openrouter, official)` pricing policy: GLM-5 $0.72->$1.00, Kimi K2.5 $0.45->$0.60, etc.
- Corrected cache pricing for OpenAI (50% read), Anthropic (10% read / 125% write), Kimi (flat $0.15 read) using official rates instead of generic heuristics.
- Added many new OpenAI models (o3, o4-mini, gpt-5-pro, gpt-5.4-pro, gpt-4.1-nano, gpt-3.5-turbo, gpt-4-turbo, etc.), Anthropic models (claude-opus-4.1, claude-sonnet-4, claude-haiku-4.5, claude-3.5-haiku, etc.), Google Gemini models (gemini-2.5-pro, gemini-2.5-flash, gemini-3.1-pro, etc.), and Z.ai models (glm-5-turbo).

### Testing
- Added `tests/test_pricing_db_contract.py`: consumer contract test verifying manual models, aliases, derived models, and per-provider resolution survive pricing DB updates.

## 0.0.7 - 2026-03-06

- Added Kimi CLI accounting support by parsing `~/.kimi/sessions/*/*/wire.jsonl` StatusUpdate events.
- Registered Kimi as a default coding-tools source and documented the supported Kimi session path in the README.
- Added a regression test for the Kimi parser and support for overriding the Kimi data directory with `KIMI_SHARE_DIR`.
- Documented the current Kimi billing-model assumption (`kimi-for-coding` -> `kimi-k2.5`) in code for future timestamp-based model rollovers.

## 0.0.6 - 2026-03-05

- Added GPT-5.4 pricing support to the local pricing database.
- Bumped the package version to `0.0.6`.

## 0.0.1 - 2026-02-25

- Initial PyPI packaging (`pyproject.toml`) + `tokdash` CLI (`tokdash serve`, `tokdash export`).
- FastAPI server serving a local dashboard and `/api/*` endpoints.
- Local parsers for OpenCode, Codex, Claude Code, Gemini CLI, and OpenClaw sessions.
