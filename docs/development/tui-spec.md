# Tokdash TUI + `tokdash report` — implementation spec

Status: final, ready for parallel implementation. Every symbol, cache key, flag,
binding, and Textual API in this document was verified against the source tree and
against `textual` 8.2.8 live on PyPI (installed and exercised in a scratch venv).
Where this spec disagrees with intuition about an old Textual version, this spec is
right about 8.2.8 — see §6 "verified Textual 8.x facts".

## 0. Architecture in one paragraph

Two new commands on the existing flat argparse parser: `tokdash tui` (Textual,
Overview + Report tabs) and `tokdash report` (one-shot, ccusage-style text). Both
consume data through ONE new thin module (`src/tokdash/tui/data.py`) that imports
`get_cached_or_fetch` and the private key helpers from `tokdash.api` — which
`cli.py` already imports at module top (`from .api import app`, cli.py:18), so
importing `tokdash.api` costs the CLI nothing new. The TUI reproduces the exact
route-level call shapes (same keys → same single-flight, stale-while-revalidate,
pricing-aware, day-pinned cache semantics **within its own process**; the shared
SQLite usage store remains the cross-process cache). No HTTP, no uvicorn, no daemon
threads started by the TUI, no new global flags, no writes. `tokdash report` never
imports Textual (headless-safe); only `src/tokdash/tui/app.py` imports `textual`.

Locked decisions (user-approved, not re-litigated):
1. Rendering stack = Textual (new direct pyproject dependency, pulls rich).
2. Commands: `tokdash tui` (interactive, Overview + Report tabs) and
   `tokdash report` (one-shot, honors `--json/--pretty/--output` via `_emit_json`).
3. Data access = in-process imports of the same functions the FastAPI routes
   delegate to — no HTTP client, no uvicorn.
4. v1 scope = Overview + Report only. No Sessions tab, no quota, no writes, no
   multi-server merge, no `--date-from/--date-to` custom windows.

## 1. Module layout (every file justified)

```
src/tokdash/tui/__init__.py      # empty package marker; auto-discovered under [tool.setuptools] package-dir {"": "src"}
src/tokdash/tui/data.py          # the ONLY data-access module (TUI + report share it); wraps get_cached_or_fetch with the SAME keys the routes use
src/tokdash/tui/formatting.py    # pure string rules (Run type, tokens, cost, duration, deltas, glyphs, null-vs-zero law); NO textual/rich import
src/tokdash/tui/report.py        # shared segment renderer (build_report_segments + emit_plain/emit_ansi/emit_markup) + run_report(args); NO textual/rich import
src/tokdash/tui/app.py           # Textual App + run_tui(args); the ONLY tokdash module importing textual (and the only one allowed to import rich)
src/tokdash/cli.py               # EDIT: 2 choices + 2 lazy dispatch branches + tui flag guard + parse-time period guard
pyproject.toml                   # EDIT: add "textual>=8.0.0"
docs/development/CHANGELOG.md    # EDIT: Unreleased → Added entries
README.md + README_CN/ES/JA/KO/PT.md  # EDIT in the SAME PR: document both commands (RELEASING.md "README sync": six READMEs are one document)
tests/test_tui_data.py           # cache-key parity + window mapping + retry + preflight no-op
tests/test_tui_formatting.py     # pure formatting rules, null-vs-zero law, delta-color split, emitters
tests/test_tui_report.py         # one-shot render + --json contract + parser guards + non-mutation
tests/test_tui_import_discipline.py  # textual blocked via sys.meta_path; report path must still work
tests/test_tui_app.py            # Textual pilot tests (pytest.importorskip("textual"))
```

Import boundaries (hard rules, each has a test):
- All user-derived strings (model/tool names, the db path) reaching a
  markup-parsing renderer (rich Table cells, DataTable str cells, Static
  markup) pass through `rich.markup.escape` in app.py — a model name like
  `claude [1m]` must render literally, never raise MarkupError mid-paint.
- `formatting.py` and `data.py` import no textual and no rich.
- `report.py` imports `data` + `formatting` + stdlib + `..cli._emit_json`; it must
  not import textual or rich (the emit_markup escape is hand-rolled, §5).
- `app.py` may import textual and rich (textual pulls rich transitively).
- `cli.py` imports `tokdash.tui.report` / `tokdash.tui.app` lazily inside their
  dispatch branches (precedent: `from .onboard.engine import run_lifecycle`,
  cli.py dispatch).

Rejected file-level alternatives: a `tokdash/term.py` extraction of ANSI helpers
(instead re-export the verified `onboard/engine.py:1611-1646` family — zero
churn to shipped lifecycle code); one mega `tui.py` (would force the report path
to import textual); an HTTP `clients/` module (locked decision 3); a separate
`tui/cache.py` (the api module's cache is reused verbatim); a separate viewmodels
layer (the segment builder in report.py is the view model).

## 2. CLI integration (`src/tokdash/cli.py`) — exact edits

1. **choices (cli.py:85)** — add two words, keep everything else byte-identical:
   ```python
   choices=["serve", "export", "db", "quota", "tui", "report", "version", "setup", "doctor", "update", "uninstall"],
   ```
2. **Parse-time guards** — inserted in `cli()` (cli.py:996) immediately after the
   existing dev-fixture/dev-seed guard block (which already rejects
   `--dev-fixture`/`--dev-seed` for every non-serve command, including the two new
   ones; leave that block untouched):
   ```python
   if args.command == "tui" and (args.json or args.output or args.pretty):
       parser.error("--json/--pretty/--output belong to `report`/`export`; `tui` is interactive only")

   # Kill the silent all-time fallback at the parse edge for the two new verbs.
   # `export` keeps its historical permissive --period behavior untouched.
   if args.command in {"tui", "report"}:
       from .compute import period_is_recognized
       if not period_is_recognized(args.period):
           parser.error(
               f"Unknown period {args.period!r}; use today, week, month, year, all, "
               "an integer number of days, or Nd/Nw/Nm/Ny shorthand"
           )
   ```
   `period_is_recognized` (compute.py:601) lower-cases the token and accepts
   NAMED_PERIODS (`today/3days/week/14days/month/year/all`), integer day counts,
   and `Nd/Nw/Nm/Ny` shorthand. Unknown tokens otherwise fall back to
   `ALL_TIME_DAYS = 36500` (compute.py:548) — unacceptable for a terminal default,
   hence the guard (argparse exit 2, consistent with `parser.error` convention).
3. **Dispatch** — insert before the trailing `parser.error(f"Unknown command: ...")`:
   ```python
   if args.command == "report":
       from .tui.report import run_report
       return run_report(args)

   if args.command == "tui":
       from .tui.app import run_tui
       return run_tui(args)
   ```
4. **No new flags.** Reused globals verbatim: `--period` (window selector for BOTH
   commands; default `today`), `--json` (report machine output; export compat
   no-op), `--pretty`, `--output` (report). Rejected new flags: `--window`
   (`--period` covers it), `--date-from/--date-to` (custom windows are not v1),
   `--no-project-names` (insights always runs with `include_project_names=True`,
   matching the warmed server keys), `--width` (computed, §5), `--ascii`
   (encoding probe is automatic, §4). Flat-parser contract §7.1
   (cli.py:192-194 comment) holds because no flag names were added.
5. **`db_action` positional (cli.py:88-94) is untouched and meaningless for both
   commands** — but it is NOT inert the way it looks: it has its own `choices`
   list, so `tokdash report week` fails at argparse with "invalid choice: 'week'"
   (exit 2), while `tokdash report status` is accepted and silently ignored (same
   class as `tokdash db`'s argument). **Period is flag-only: `tokdash report
   --period week`.** The README must say so.
6. `_harden_windows_stdio()` already runs first in `cli()` (cli.py:997) — both
   commands inherit `errors="replace"` stdio on Windows.

Exit-code conventions: handlers return `int`; user-facing failures
`raise SystemExit("message")` (stderr, exit 1); argparse misuse exit 2 via
`parser.error`. The two new guards both use `parser.error` → exit 2.

## 3. `src/tokdash/tui/data.py` — public signatures (exact)

Imports (all verified to exist):
```python
from ..api import (
    REPORT_FACETS,            # api.py:449 — "daily,streaks,firsts,hourly,weekday,tools,models,projects"
    CacheBackpressureError,   # api.py:866
    _active_time_cache_key,   # api.py:312
    _active_time_payload,     # api.py:325 (route-only "range" augmentation lives here — see gotcha below)
    _insights_cache_key,      # api.py:350
    _local_today,             # api.py:244
    _report_windows,          # api.py:452 (Monday-week / month-1st / jan-1st, all ending today)
    _usage_cache_key,         # api.py:346
    _window_cache_key,        # api.py:279 (pricing-aware; pins OPEN windows to capture day)
    get_cached_or_fetch,      # api.py:1430 (single-flight, TTL 600s, stale-while-revalidate,
                              #   heavy-compute semaphore; return_metadata=True → CacheFetchResult)
)
from ..compute import (
    compute_usage_with_comparison,  # compute.py:1081
    compute_stats,                  # compute.py:1156
    period_is_recognized,           # compute.py:601
    resolve_period,                 # compute.py:630
)
from ..dateutil import parse_date_range   # dateutil.py
from ..insights import compute_insights   # insights.py:279
from ..usage_store import (
    UsageDatabaseSchemaTooNewError,        # usage_store.py:24
    UsageEntryStore,                       # usage_store.py:935 (__init__ mkdirs the DB parent — see ensure_usage_db_compatible)
    persistent_usage_db_enabled,           # usage_store.py:537 (TOKDASH_USAGE_DB not in {0,false,no,off})
    raise_if_usage_db_incompatible,        # usage_store.py:68 (free function; read-only open, NO mkdir, silent unless too-new)
)
```

```python
OVERVIEW_PERIODS: tuple[str, ...] = ("today", "7d", "month", "year", "all")  # TUI 'p' cycle order
REPORT_PERIODS: tuple[str, ...] = ("week", "month", "year")                   # index-aligned with _report_windows()

@dataclass(frozen=True)
class FetchOutcome:
    value: dict[str, Any]
    status: str                 # "hit" | "stale" | "recomputed" (CacheFetchResult.status, api.py:871)
    age_seconds: float | None

def ensure_usage_db_compatible() -> None:
    """Call once before UI/compute starts. NO-OP unless persistent_usage_db_enabled()
    (under TOKDASH_USAGE_DB=0 the store is not consulted, and UsageEntryStore() would
    mkdir the DB parent — forbidden filesystem mutation). Raises
    UsageDatabaseSchemaTooNewError. Implementation:
        if persistent_usage_db_enabled(): raise_if_usage_db_incompatible()
    """

def resolve_report_period(period: str, *, today: datetime.date | None = None
                          ) -> tuple[str, str | None, str | None]:
    """Map a user period token to the (period, date_from, date_to) triple for the
    report / TUI-Report surfaces (the exact shape _report_warm_targets builds,
    api.py:490-510).
    - token.lower() in ("week","month","year") -> ("today", *_report_windows(today or _local_today())[idx])
      (calendar-aligned Monday week / month-to-date / year-to-date; the web Report tab's
      windows, so keys collide with the server's warmed report key families)
    - else period_is_recognized(token)          -> (token, None, None)   (rolling pass-through)
    - else: raise ValueError(f"Unknown period {period!r}; use today, week, month, year, all, "
                             f"N days as an integer, or Nd/Nw/Nm/Ny shorthand")
    NOTE: "month"/"year" here mean the CALENDAR report windows (via _report_windows),
    NOT compute's rolling month/year tokens. "today"/ints/Nd.../all pass through.
    (The cli.py parse-time period_is_recognized guard, §2.2, normally fires before
    this ValueError can.)"""

def fetch_usage(period: str, date_from: str | None = None, date_to: str | None = None,
                *, refresh: bool = False) -> FetchOutcome:
    """_with_backpressure_retry around
    get_cached_or_fetch(_usage_cache_key(period, date_from, date_to),
        lambda: compute_usage_with_comparison(period, date_from, date_to),
        force_refresh=refresh, return_metadata=True) → FetchOutcome."""

def fetch_active_time(period: str = "today", date_from: str | None = None,
                      date_to: str | None = None,
                      include_review_sessions: bool | None = None,
                      *, refresh: bool = False) -> FetchOutcome:
    """key: _active_time_cache_key(period, date_from, date_to, include_review_sessions)
    fetch: _active_time_payload(period, date_from, date_to, include_review_sessions).
    MUST go through _active_time_payload, NOT get_active_time_data directly — the
    payload carries the route-only 'range' augmentation (the warmer divergence the
    codebase already hit once, api.py:325 docstring)."""

def fetch_insights(period: str = "year", date_from: str | None = None,
                   date_to: str | None = None, facets: str = REPORT_FACETS,
                   include_project_names: bool = True,
                   *, refresh: bool = False) -> FetchOutcome:
    """key: _insights_cache_key(period, date_from, date_to, facets, include_project_names).
    Callers pass REPORT_FACETS verbatim — facet-string ORDER is part of the key
    (api.py:445-449 comment); NEVER reorder or interpolate a custom facet list.
    Report windows pass period="year" (the route default the warmer primes,
    api.py:498-503), never the user's period token."""

def fetch_stats(year: int | None = None, *, refresh: bool = False) -> FetchOutcome:
    """key: _window_cache_key(f"stats_{year}", None, f"{year}-12-31" if year else None)
    — byte-for-byte the route expression at api.py:2330-2332 (note `if year`, not
    `is not None`; year=0 behaves as None there). fetch: compute_stats(year).
    None = TRAILING 365 days (label it "trailing 365 days", never "this year")."""

def report_windows(today: datetime.date | None = None) -> list[tuple[str, str]]:
    """return _report_windows(today or _local_today())."""

def db_summary() -> str:
    """One-line db status for the TUI status bar / report footer.
    if not persistent_usage_db_enabled(): return "usage db disabled (TOKDASH_USAGE_DB=0) — live parsing"
    store = UsageEntryStore(); st = store.status()
    return f"{st['path']} · {st['usage_entries']} usage rows"
    (status() keys verified: path, meta, usage_entries, quota_snapshots, sources,
    files, sessions, durable. Constructing the store here is acceptable — it only
    runs when the persistent DB is enabled, and mkdir of the parent is then the
    normal runtime behavior.)"""
```

Private `_with_backpressure_retry(fn: Callable[[], FetchOutcome]) -> FetchOutcome`:
catches ONLY `CacheBackpressureError`, retries up to `attempts = 3` total tries
with `time.sleep(0.45 * (attempt + 1) + random.uniform(0.0, 0.25))` — the
jittered 503 policy the web uses (`fetchJsonWithRetry`, index.html:8766:
`attempts=3`, `baseDelayMs=450`, `jitter<250ms`, delay = base*(attempt+1)+jitter).
All five `fetch_*` go through it. NEVER retry `UsageDatabaseSchemaTooNewError`
(terminal by design, usage_store.py:24) or `ValueError`.

Why key parity still matters even though `get_cached_or_fetch` is per-process
memory (a TUI beside `tokdash serve` shares only the on-disk store): identical
keys give the TUI correct within-process semantics — single-flight, TTL,
stale-while-revalidate, pricing-identity folding, and the day-pin on open windows
(`_window_cache_key` stamps the capture day into OPEN-window keys, api.py:279-295,
so a "today" panel seen before and after midnight can never answer from the other
day's partial snapshot).

Explicit non-goals: no `fetch_sessions`, no `fetch_quota`, no
`get_codex_activity_insights` (the web Overview profile band's activity strip is
optional; the band here is fed by `fetch_stats`), no `_warm_caches()` call (a
serial TUI never fans out 20 tools; the 25s+ startup warm would be spent on keys
the user may never view).

## 4. `src/tokdash/tui/formatting.py` — pure rules, no textual/rich imports

Re-export the ANSI gate:
```python
from ..onboard.engine import _color_enabled, _bold, _ok, _warn, _bad, _accent
```
(verified engine.py:1611-1646: `_color_enabled` honors `NO_COLOR`, `TERM=dumb`,
`sys.stdout.isatty()`; `_style` emits `\033[{code}m{text}\033[0m`; codes 1/32/33/31/36).
These are the ONLY existing terminal-styling helpers in the repo. `emit_ansi`
applies them (double-gated); `emit_plain`/`emit_markup` never touch them.

Shared segment type (lives here, used by report.py and app.py):
```python
@dataclass(frozen=True)
class Run:
    text: str
    style: str | None = None   # None | "ok" | "bad" | "warn" | "bold" | "accent" | "muted"
```

```python
EM_DASH = "—"          # cp1252-safe; every missing figure per the web convention
NA = "n/a"

def glyph_style() -> str:
    """'blocks' if (sys.stdout.encoding or "").lower().replace("-", "").startswith("utf")
    else 'ascii'   (legacy cp1252 conhost: blocks degrade to '?').
    Callers override for files: a report written with encoding="utf-8" is always 'blocks'."""

def fmt_int(n: int | float | None) -> str
    # f"{int(n):,}" — fixed ',' grouping (no locale module). None → EM_DASH.

def fmt_tokens(n: int | float | None) -> str
    # None → EM_DASH; < 1000 → fmt_int; else one-decimal K/M/B/T, rolling UP at 1000+
    # (1_000 → "1.0K", 12_400 → "12.4K", 1e12 → "1.0T"). Mirrors the web's compact form.

def fmt_cost_headline(x: float | None) -> str
    # f"${x:,.2f}"; None → EM_DASH. $0.00 is a MEASURED zero and prints as "$0.00".

def fmt_cost_row(x: float | None) -> str
    # f"${x:,.2f}"; None OR <= 0 → EM_DASH — an unpriced row (PricingDatabase.get_cost
    # returns 0.0 for unknown models, pricing.py:320) is not free.

def fmt_hit(x: float | None) -> str
    # f"{x*100:.1f}%"; None → "n/a" (no prompt input). Use the backend value; NEVER recompute.

def fmt_duration(ms: int | float | None) -> str
    # None → EM_DASH. Locked table (tested):
    #   < 1 min   → "42s"
    #   < 1 h     → "7m"
    #   < 1 day   → "5h 03m"            (zero-padded minutes)
    #   < 7 days  → "2 days 4 hours"    (singular "1 day 1 hour")
    #   < 30 days → "1 week 2 days"     ("3 weeks 4 days"; singular "1 week 1 day")
    #   else      → "3 months 12 days"  (months = days // 30; singular both sides)

def fmt_delta(pct: float | None, *, surface: str = "report") -> Run
    # pct is in PERCENTAGE POINTS (compute.pct_change, compute.py:1075-1079, returns
    # round(((cur-prev)/prev)*100, 1)) — fmt_delta must NEVER multiply by 100.
    # None  → Run(EM_DASH, None)
    # >= 0  → text "↑ 8.2%"  (ascii glyph mode: "+8.2%")
    # <  0  → text "↓ 3.1%"  (ascii glyph mode: "-3.1%")
    # == 0 on overview → text "→ 0.0%", style "muted"
    # style: surface == "report"   → "ok" if >= 0 else "bad"   (web Report tab:
    #        index.html:14908 + 1752-1755, .usage-report-delta-up = GREEN #16A34A)
    #        surface == "overview" → "bad" if > 0 else "ok"    (web Overview KPI
    #        chips: index.html:8323 renderDelta — up = RED #DC2626, down = GREEN #16A34A)
    # The web has TWO OPPOSITE conventions; each surface follows its own web
    # counterpart. Do not "unify" them.

def day_spark(values: list[float], *, glyphs: str = "blocks") -> str
    # one glyph per value, 5 levels: blocks ramp ("▁","▂","▃","▅","▇") or
    # ascii ramp (".", ":", "-", "=", "#"); thresholds = 20/40/60/80 percentiles of
    # the NONZERO values (value 0 → level 0; all-zero list → level 0 everywhere).
    # Empty list → EM_DASH.

def text_bar(value: float, maxv: float, *, width: int = 20, glyphs: str = "blocks") -> str
    # "█" * round(width * value / maxv); "#" in ascii mode; maxv <= 0 → empty bar.

def window_line(rng: dict) -> str
    # f"{rng['from']} → {rng['to']} · {rng['days']} days" — from a resolve_period
    # "range" block (keys from/to/days/recognized). When rng.get("recognized") is
    # falsy, append " (unrecognized period — showing all time)" — never trust the
    # caller's token for the label (defense-in-depth; v1 parse guard makes this
    # unreachable on the new commands).
```

**Null-vs-zero law (single source of truth, one test class, all renderers follow it):**
- `None` from the backend = unknown/not-applicable → EM_DASH (or "n/a" for hit rates).
- measured `0` → renders as `0` / `$0.00` / `0m` (headline totals, counters).
- unpriced row cost `<= 0` in table/row context → EM_DASH via `fmt_cost_row`
  (unpriced ≠ free), while a headline `total_cost == 0.0` prints `$0.00`.
- missing join key (agent row without an active_time entry, weekday never reached
  in a partial window) → EM_DASH in that cell, never `0`.
- Renderers must COPY any nested payload row they annotate — compute payloads share
  objects (`"top_models": combined_models[:5]`, compute.py:989; a TUI
  mutating them corrupts later consumers in the same process). The non-mutation
  test (§10) locks this.

## 5. `src/tokdash/tui/report.py` — shared renderer + one-shot command

**Shared-renderer seam (structural anti-drift):** the pipeable `tokdash report`
bytes and the TUI Report tab are built by ONE function from the same three
payloads, so they cannot disagree:

```python
def build_report_segments(usage: dict, insights: dict | None, active_time: dict | None,
                          *, window: dict, width: int = 80, glyphs: str = "blocks",
                          surface: str = "report") -> list[list[Run]]
    # One entry per output line; each line is a list[Run]. insights or active_time
    # may be None (soft-failed fetch) → those sections render EM_DASH placeholders
    # and a warning line, never an exception. window = {"period": <label>,
    # "from": str, "to": str, "days": int, "timezone": str | None} built from the
    # usage payload's "range" block. width sizes name truncation (22-char names)
    # and text bars (bar width = 20 if width >= 70 else 12).
    # Optional window keys (report-lane additions): "recognized" (default True,
    # echoed from the range block — never trust the request token) and
    # "warnings" (one-shot soft-fail reasons with exception text).
    # TUI-only keys: "pending" (True during progressive paints — a None source
    # mid-load means "running…", a DIM note, NOT the one-shot's "scan failed"
    # warn; the TUI hard-fails the whole pane on a real error, so None there
    # can only mean in-flight) and "db_line" (the app's cached db_summary line;
    # the builder must NOT call db_summary on the TUI path — repaints run on
    # the UI event loop and a fresh machine's db_summary creates ~/.tokdash).

def emit_plain(segments: list[list[Run]]) -> str    # strips styles, no ANSI. Piped stdout, --output, tests.
def emit_ansi(segments: list[list[Run]]) -> str     # applies _ok/_bad/_warn/_bold/_accent ("muted"/None → plain).
                                                    # Engine helpers already no-op when _color_enabled() is false.
def emit_markup(segments: list[list[Run]]) -> str   # Textual/rich markup for the TUI Report tab:
    # style map: ok→green, bad→red, warn→yellow, bold→bold, accent→cyan, muted→dim.
    # Escapes literal '[' as '\\[' by hand — report.py must NOT import rich/textual.
```

```python
def run_report(args: argparse.Namespace) -> int
```
Flow (serial — a one-shot process never contends with its own semaphore; the
retry helper covers external contention):
1. `period_used, date_from, date_to = resolve_report_period(args.period)`;
   `ValueError` → `raise SystemExit(str(exc))` (normally the §2.2 parse guard
   already exited 2).
2. `ensure_usage_db_compatible()`; `UsageDatabaseSchemaTooNewError` →
   `raise SystemExit(f"{e} — run 'tokdash update'")`.
3. Fetch, sequentially, usage-first (mirrors `loadUsageReport()` ordering,
   index.html:14363+):
   - `usage = fetch_usage(period_used, date_from, date_to).value` — hard-fail:
     `CacheBackpressureError` after 3 tries →
     `SystemExit("Tokdash is busy computing — try again shortly")`; any other
     exception → `SystemExit(str(e))`.
   - insights (SOFT): calendar case
     `fetch_insights("year", date_from, date_to, REPORT_FACETS, True)`; rolling
     case `fetch_insights(period_used, None, None, REPORT_FACETS, True)`; on any
     error EXCEPT `UsageDatabaseSchemaTooNewError` (re-raise → SystemExit) set
     `insights = None` and remember `warnings.append(f"analytics scan failed: {e}")`.
   - active_time (SOFT): `fetch_active_time(period_used, date_from, date_to, True)`
     (Report pins include_review_sessions=True, per the web and the warmer key
     api.py:504-508); soft-fail like insights.
4. `args.json` → `payload = {"generated_at": <iso8601 local, timespec="seconds">,
   "window": {"period": args.period, "mode": "calendar"|"rolling",
   "from": ..., "to": ..., "days": ..., "recognized": true,
   "timezone": insights["timezone"] if insights else None},
   "usage": <full compute_usage_with_comparison dict>,
   "insights": <full compute_insights dict or null>,
   "active_time": <full _active_time_payload dict or null>}`;
   `from ..cli import _emit_json; _emit_json(payload, args.pretty, args.output)`;
   return 0. (`_emit_json`, cli.py:365: `json.dumps(indent=2 if pretty,
   sort_keys=bool(pretty))`; `--output` writes the file with a trailing newline;
   otherwise prints.)
5. Text mode: build segments (§5 layout), then
   - width: `80` when `args.output` or not `sys.stdout.isatty()`, else
     `max(60, min(200, shutil.get_terminal_size((80, 24)).columns))` —
     deterministic 80 when piped (CI-stable bytes);
   - glyphs: `"blocks"` when writing a file (utf-8) else `glyph_style()`;
   - `text = emit_ansi(segments)` when `not args.output and _color_enabled()`
     else `emit_plain(segments)` — piped bytes and file bytes are ALWAYS plain;
   - `args.output` → `Path(args.output).write_text(text + "\n", encoding="utf-8")`,
     else `print(text)`. Soft failures print their warning lines inside the
     report body; exit code stays 0 (dashes + warnings, not a dead report).

**Fixed section layout** (field-sourced; `—` = EM_DASH; null rules per §4 law):

```
Tokdash report · {period label} · {window_line(usage.range)}[ · {insights.timezone}]
Tokens {fmt_tokens(total_tokens)}  {delta comparison.tokens_pct}
Cost   {fmt_cost_headline(total_cost)}  {delta comparison.cost_pct}
Msgs   {fmt_int(total_messages)}  {delta comparison.messages_pct}
Agent  {fmt_duration(active_ms)} clock · {fmt_duration(active_ms_sum)} sum  {delta comparison.active_ms_sum_pct}
Streak {streaks.current_streak} days · longest {longest_streak} · active {active_days} of {days} · busiest {firsts.busiest_day} ({fmt_tokens(firsts.busiest_day_tokens)})
Daily  {day_spark([d.tokens for d in insights.daily])}  {daily[0].date} → {daily[-1].date}
When   peak hour {peak_hour:02d}:00 · night share {fmt_hit(night_share)}
       {text_bar rows: top 3 hourly buckets "HH:00"}
       busiest weekday {name} ({fmt_tokens(bucket tokens)})
Podium Top agent    {tools.ranked[0].tool} · {fmt_tokens} · {fmt_cost_row} · {active_time.by_tool[tool].session_count} sessions · {fmt_duration(..active_ms_sum)}
       Top model    {models.ranked[0].model} · {fmt_tokens} · {fmt_cost_row}
       Top project  {projects.projects[0].project} · {fmt_tokens} · {fmt_cost_row}   # or warn "projects: {projects.unavailable_reason}"
Agents (tools.ranked rows with tokens>0, filtered to those with a session_count
        when active_time answered — same rule as usageReportBuildModel — top 10)
       Agent        Sessions  Tokens   Cost      Active
       {tool}        {..}      {..}     {..}      {..}     # missing join key → —; cost<=0 → —
Note   Headline totals are the usage scan; facet/agent figures are the analytics
       scan (tools scope only) and may differ.
[warn] analytics scan failed: …            # only on soft failure
[warn] {N} source(s) failed this window: {names}   # usage.source_errors (entries are
                                                  # strings or dicts with "source") — ALWAYS surfaced
db     {db_summary()}
```

Field sources (verified): usage rows `by_tool{tokens,cost,tokens_in,tokens_cache,
cache_hit_rate}`; `apps{name:{...tokens_out,...}}`; `combined_models[:15]`
`{name,tokens,tokens_in,tokens_out,tokens_cache,cost,messages,cache_hit_rate,source}`;
`comparison{tokens_pct,cost_pct,messages_pct}`; active-time
`{active_ms, active_ms_sum, comparison{active_ms_sum_pct}, by_tool{label:
{tool_label,session_count,active_ms,active_ms_sum}}, unavailable_tools}`
(active_ms = cross-tool clock union; active_ms_sum = summed per-tool agent time —
always label both explicitly, they are different facts); insights
`{range,timezone,totels?,daily[{date,tokens,cost,messages,entries,intensity}],
streaks{current_streak,longest_streak,active_days,total_days},firsts{first_active_day,
last_active_day,busiest_day,busiest_day_tokens,peak_hour},hourly{buckets[{hour,
tokens,cost,messages,entries}],peak_hour,night_share,night_hours},weekday{buckets
[{weekday,name,tokens,...}],peak_weekday},tools{ranked[{tool,tokens,cost,messages,
entries}]},models{ranked,most_used,highest_cost},projects{projects[{project,
tokens,cost,...}],unattributed,attributed_project_count,names_included,
unavailable_reason?}}`. Per-figure source labels are the note line — insights
totals legitimately disagree with usage totals (insights are tools-scope; openclaw
session usage is not in insight rows).

Rejected report alternatives: three-window combined print (web prints one window;
`--period` picks it — and 3 windows triples cold compute); `--back N` chips;
`--from/--to` flags.

## 6. `src/tokdash/tui/app.py` — Textual structure

**Verified Textual 8.x facts (live-tested against textual 8.2.8 — do not "fix"
these back to old-Textual patterns):**
- `from textual import work` accepts `(name=, group=, exit_on_error=, exclusive=,
  description=, thread=)`. A non-async function REQUIRES `thread=True`
  (`WorkerDeclarationError` otherwise).
- **There are no `on_<worker>_success` result messages in 8.x.** A decorated
  `@work` method returns a `Worker` synchronously and **is not awaitable**
  (`await self.load()` raises `TypeError`). The mandated pattern:
  ```python
  @work(thread=True, exit_on_error=False)
  def _ov_usage_job(self, period: str, refresh: bool) -> FetchOutcome:
      return fetch_usage(period)                      # blocking, off the event loop

  @work(group="ov", exclusive=True, exit_on_error=False)
  async def _load_overview(self, period: str, refresh: bool) -> None:
      gen = self._gen
      outcome = await self._ov_usage_job(period, refresh).wait()   # Worker.wait()
      if gen != self._gen: return                     # superseded → drop paint
      self._paint_ov_usage(outcome)                   # safe: we are on the event loop
      ...next job, same guard between every await...
  ```
  Started as plain calls (`self._load_overview(...)`, no `await`) from `on_mount`
  and actions. `Worker.wait()` (async) re-raises a worker crash as
  `textual.worker.WorkerFailed` wrapping the original — unwrap with
  `getattr(exc, "error", exc)` in the supervisor's `except Exception` and classify
  there (there is NO `on_worker_failed` message to rely on; `exit_on_error=True`
  (the default!) would crash the app, so every decorator sets `exit_on_error=False`).
- `exclusive=True` cancels running workers in the same group (new supervisor wins;
  an abandoned thread job finishes harmlessly into the cache; its result is
  dropped by the generation guard).
- The `tab` key does NOT switch TabbedContent panes (it moves focus — verified).
  Pane switching is via the `1`/`2` bindings below (round 3 added `3` — Quota);
  when the TabStrip itself has focus its Left/Right keys change tabs. Do NOT add
  `tab`/`shift+tab` bindings.
- `Binding("P", ...)` is shift+p, distinct from `"p"` (verified). REMOVED in
  round 3 (§14): the backward axis became the date shift `[` / `]`, not a
  reverse period cycle. `?` is the key name `"question_mark"`. `ctrl+p` is
  taken by the command palette (`COMMAND_PALETTE_BINDING`) — avoid it.
- `App.action_show_help_panel` + the built-in `HelpPanel` widget exist — the `?`
  help overlay is just `Binding("question_mark", "show_help_panel", "Keys")`
  (verified: pressing `?` shows the panel; every binding description surfaces).
- `App.run_test(headless=True, size=(80,24))` + `Pilot.press`/`Pilot.pause` exist;
  `TabbedContent(initial="pane-overview")` composes via context managers;
  `TabbedContent.active` is a reactive str; `TabbedContent.TabActivated` is the
  lazy-load trigger; `Static.update(content)` accepts str/rich renderables;
  `Sparkline(data, *, id=...)`; `DataTable.add_columns(*labels)` /
  `add_row(cells)` / `clear()`; `App.exit(return_code=1)` exists.
- `MessagePump.set_timer(delay, callback)` and `set_interval` exist.

```python
def run_tui(args: argparse.Namespace) -> int:
    if not sys.stdout.isatty() or os.environ.get("TERM", "") == "dumb":
        raise SystemExit("`tokdash tui` needs an interactive terminal. "
                         "One-shot view: `tokdash report`; JSON: `tokdash export --pretty`.")
    try: ensure_usage_db_compatible()
    except UsageDatabaseSchemaTooNewError as e:
        raise SystemExit(f"{e} — run 'tokdash update'")
    if not period_is_recognized(args.period):        # belt-and-suspenders (§2.2 guard normally fires)
        raise SystemExit(f"Unknown period {args.period!r}.")
    app = TokdashApp(initial_period=args.period)     # textual import stays inside this module
    app.run()
    return app.return_code or 0                      # App.run() returns None; textual prescribes
                                                     # sys.exit(app.return_code) — without this the
                                                     # mid-run fatal exit never reaches the shell

class TokdashApp(App):
    TITLE = "tokdash"
    CSS = """
    #status { height: 1; }
    #pane-overview, #pane-report { padding: 0 1; }
    DataTable { height: auto; max-height: 16; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("1", "show_pane('pane-overview')", "Overview"),
        Binding("2", "show_pane('pane-report')", "Report"),
        Binding("p", "cycle_window(1)", "Next window"),
        Binding("P", "cycle_window(-1)", "Prev window"),
        Binding("question_mark", "show_help_panel", "Keys"),
    ]
```

**Widget tree (all built-in widgets; ZERO custom widget classes):**
```
Header
Static(id="status")
TabbedContent(id="tabs", initial="pane-overview")
├─ TabPane("Overview", id="pane-overview") -> VerticalScroll:
│   Static(id="ov-range")      # window_line(usage.range) (deltas use surface="overview" colors)
│   Static(id="ov-kpis")       # rich Table via Static.update(): Tokens | Cost | Msgs | Agent sum | Cache hit | Top model
│   Static(id="ov-band")       # stats: "trailing 365 days · streak N · longest M · active D/T days · favorite model {…}"
│   Sparkline(id="ov-daily")   # last 30 stats contributions' totals.tokens
│   DataTable(id="ov-tools")   # Tool | Tokens | Hit % | Cost   ← usage.by_tool, tokens desc
│   DataTable(id="ov-models")  # Model | Tokens | Cost | Msgs   ← usage.combined_models[:15], cost desc (never 300 rows)
└─ TabPane("Report", id="pane-report") -> VerticalScroll:
    Static(id="rp-body")       # emit_markup(build_report_segments(usage, insights, active_time, window=…, width=72, glyphs=glyph_style()))
Footer
```
The Report pane is ONE Static fed by the §5 shared segment builder — the same
function that produces the piped report bytes, so the TUI Report tab and
`tokdash report` structurally cannot drift (hero/streak/when/podium/agents all
live inside the segments; progressive paint = re-emit with more sources filled).
(Replaces any per-figure rp-* widget set.)

KPI field mapping (Overview): Tokens `total_tokens` + `comparison.tokens_pct`;
Cost `total_cost` (fmt_cost_headline) + `cost_pct`; Msgs `total_messages` +
`messages_pct`; Agent `active_time.active_ms_sum` labeled "sum" +
`comparison.active_ms_sum_pct`; Cache hit `cache_hit_rate` (fmt_hit); Top model
`top_models[0].name` + `top_models[0].cost` (fmt_cost_row). Overview deltas are
surface="overview" (up=red, down=green — matches web renderDelta, §4).

**Workers:** per-pane async supervisors as above, groups `"ov"` and `"rp"`, plus
thread jobs:
- `_ov_usage_job(period, refresh) -> FetchOutcome` → `fetch_usage(period)`
- `_ov_active_job(period, refresh)` → `fetch_active_time(period, None, None, None)`
  (Overview passes include_review_sessions=None = route default, matching the
  warm key api.py:439; Report pins True)
- `_ov_stats_job(refresh)` → `fetch_stats(None)`
- `_rp_usage_job(idx, refresh)`, `_rp_insights_job(idx, refresh)`,
  `_rp_active_job(idx, refresh)`: window = `report_windows(self._today)[idx]`
  → the exact warm triple `usage("today", f, t)` / `insights("year", f, t,
  REPORT_FACETS, True)` / `active("today", f, t, True)`.
Supervisor sequence per pane = the measured web order (usage → paint →
insights → active-time → paint; serial, not gathered — index.html:14399-14408
comment: parallel computes contend on the GIL and the serial order won at 1.1s
first-paint vs 16.4s).
- Report lazy-load: `on_tabbed_content_tab_activated` starts
  `self._load_report(0, False)` on first activation only (flag `self._rp_started`).
  Report default = idx 0 (week to date).
- Generation guards: `self._gen: int` incremented by every action that (re)starts
  a load (window cycle, refresh, day rollover). Supervisors snapshot `gen` and
  check after every `await` before painting; stale results are dropped silently.
  A bump issued from pane A also drops pane B's in-flight load, and B gets no
  successor — `_end_load` therefore marks such a pane in `self._stranded`
  (set cleared by the next `_begin_load`), and `on_tabbed_content_tab_activated`
  re-issues the stranded pane's load on reactivation, so a superseded pane can
  never sit on the skeleton with no fetch pending.
- `action_refresh`: restart the ACTIVE pane's supervisor with `refresh=True`
  (force_refresh → the same join/stale semantics as the web Refresh button).
  Never blanks existing content: the skeleton copy
  `"computing… first run scans session logs and can take tens of seconds"` is
  written ONLY when a pane is still empty or error-stated.
- `action_cycle_window(delta)`: Overview cycles `OVERVIEW_PERIODS`; Report cycles
  idx 0..2 over `REPORT_PERIODS` labels; re-launches that pane's supervisor
  (refresh=False — new keys recompute naturally).
- `action_show_pane(id)`: `self.query_one("#tabs", TabbedContent).active = id`.
- NO auto-refresh timer anywhere (deliberate: polling contends with the server's
  compute semaphore; `r` is the refresh).

**Stale visibility + 12 s silent re-read (web-parity):** all fetches go through
`FetchOutcome`. After a pane's final paint, if ANY surface in that load had
`status == "stale"` and `(pane, gen)` is not already in `self._reread_done`:
`self.set_timer(STALE_REREAD_SECONDS, ...)` which, if `gen` is unchanged, starts
that pane's supervisor again with `refresh=False` — exactly one silent, non-forced
re-read per window-load (mirrors `USAGE_REPORT_STALE_REREAD_MS = 12000`,
index.html:14469; a forced refresh would start a second full recompute the
background daemon is already doing). Module constant:
`STALE_REREAD_SECONDS = 12.0` (test locks the value).
`Static(id="status")` line: `"{pane} · {window label} · cache {status} age {s}s · {db_summary()}"`
+ while any supervisor is in flight, an elapsed suffix `" · computing… {n}s"`
driven by `self.set_interval(1.0, self._tick_status)` (no-op when idle).

**Day-rollover repaint (the day-pinned-key answer for a long-lived TUI):** open
windows (`today`, week/month/year-to-date, period-only queries) are day-pinned
into their cache keys (`_usage_window_is_open`, api.py) — an app left open past
midnight must not keep serving yesterday's pinned keys. `self._today =
_local_today()`; a check `_check_day_rollover()` runs at the top of
`on_key` and inside every action: if `_local_today() != self._today`, set the new
date, bump `self._gen`, and restart the active pane's supervisor with
`refresh=False` (the new day's keys miss the cache on their own; nothing is
forced). Keypress-triggered — no midnight timer, no polling.

**Failure delivery:** worker bodies raise; supervisors `except Exception` and
classify the unwrapped error (WorkerFailed → `.error`) per the §7 matrix:
`UsageDatabaseSchemaTooNewError` → `sys.stderr.write(f"{e} — run 'tokdash update'\n")`
then `self.exit(return_code=1)`; `CacheBackpressureError` (already 3 failed
attempts inside data.py) → red `_bad`-styled line in that pane's body Static
("busy computing — press r"); any other Exception → pane-local red line
`f"error: {e}"`, app stays alive.

Rejected TUI alternatives: custom KpiCard/Heatmap/ChipsLine widget classes
(built-in Static+Sparkline+DataTable + the segment seam suffice); the legacy
`on_<name>_success` handler convention (does not exist in 8.x); `push_screen`
ModalScreen help overlay (built-in HelpPanel); tab/shift+tab bindings (tab is
focus-nav); Sessions fan-out or quota in Overview; `activity-insights` strip
(optional on the web; stats-fed band covers the profile line); running
`_warm_caches()` at startup; auto 5-min refresh; pytest-textual-snapshot.

## 7. Error-handling matrix

| Error | Raised by | `tokdash tui` | `tokdash report` | Retry? |
|---|---|---|---|---|
| `UsageDatabaseSchemaTooNewError` | preflight / store / compute (terminal by design; never swallow) | startup: `SystemExit(f"{e} — run 'tokdash update'")` exit 1; mid-run: stderr print + `self.exit(return_code=1)`, propagated to the process via `run_tui`'s `return app.return_code or 0` | `SystemExit(f"{e} — run 'tokdash update'")` exit 1 | Never |
| `CacheBackpressureError` | get_cached_or_fetch cold-miss contention | data.py retries 3× (450 ms·(n+1) + ≤250 ms jitter); still failing → pane-local "busy computing — press r" | same retry; usage → `SystemExit("Tokdash is busy computing — try again shortly")`; insights/active → soft-fail to dashes + warn line, exit 0 | 3× then stop |
| Unrecognized period token | §2.2 parse guard (tui/report) | exit 2, accepted-token list | exit 2, same | No |
| `ValueError` from `resolve_report_period` | data.py | n/a (cycle lists are closed; parse guard fires) | `SystemExit(str(exc))` exit 1 | No |
| `UnknownFacetError` (insights.py:59) | compute_insights | impossible — REPORT_FACETS verbatim; never interpolate facet strings | same | No |
| Any other Exception in a worker | compute layer (fail-open paths already tried live parsers) | pane-local red error line, app alive | usage → `SystemExit(str(e))`; insights/active → warn + dashes, exit 0 | No |
| Not a tty / `TERM=dumb` | `run_tui` preflight | `SystemExit` pointing at `tokdash report` / `tokdash export` | n/a (report never needs a tty) | No |
| argparse (unknown cmd, `report week` positional, flag misuse) | flat parser | exit 2 (existing behavior) | exit 2 | No |

Invariant rows: never write `usage.sqlite3` outside store methods; never hold
`usage_db_process_lock` across UI work; reads may themselves run a reprice repair
(`_read_priced`) — fine, that's inside store code; respect `TOKDASH_USAGE_DB=0`
(no store construction → `ensure_usage_db_compatible` no-ops, `db_summary` prints
the disabled line without touching the filesystem); `stats["sessions"]` is a
legacy MESSAGE count (compute.py:1254-1266) — never label it "sessions";
`compute_usage_with_comparison` computes the prior window itself — never call the
previous-usage helpers directly.

## 8. Packaging & deps

- `pyproject.toml` `dependencies += ["textual>=8.0.0"]`. Verified live against
  PyPI: current release **8.2.8**, `requires-python >=3.9` (satisfies the repo's
  `>=3.10` floor); rich comes transitively. Floor-only pin per repo convention —
  **no upper bound** (both historical proposals' `<4.0` / `<3.0` ceilings exclude
  every current release and must not be resurrected).
- `requirements-dev.txt` unchanged; CI installs `pip install -e .`
  (ci.yml:37), so textual lands for the test suite on every matrix leg — the
  3.10 leg is the effective floor arbiter.
- `app.py` may import rich; `data/formatting/report` must not (rich is not yet a
  direct dependency and only arrives via textual).
- No new console script. `include-package-data` unaffected (`src/tokdash/tui/` is
  pure Python under the existing src-layout auto-discovery).
- Dev/test runs ONLY in a dedicated venv per AGENTS.md — never the pipx runtime.
  A pipx-installed tokdash on a dev box simply won't see textual until the
  release ships; `tokdash tui` will then fail at the textual import inside
  `run_tui`'s module (acceptable pre-release; `report` stays usable only from the
  dev env for now).

## 9. Docs / changelog

- `docs/development/CHANGELOG.md` → `## Unreleased` → `### Added` (Keep-a-Changelog
  prose style with PR ref, matching existing entries):
  - `tokdash tui` — interactive terminal dashboard (Overview + Report) reading the
    same in-process compute and caches as the web dashboard, no server needed.
  - `tokdash report` — ccusage-style one-shot activity report; `--json/--pretty/
    --output` follow the export conventions.
- `README.md` new "Terminal dashboard" subsection (both commands, `--period`
  semantics incl. flag-only-period note, interactive keys table) → same-PR sync of
  `README_CN/ES/JA/KO/PT.md` (RELEASING.md:51-55: a PR that changes README.md
  updates all five siblings in the same PR; headings/flags/code fences identical).
- `src/tokdash/static/release-notes.json` edited at release bump only (not in the
  feature PR).

## 10. Test plan

`tests/test_tui_data.py` (no textual; runs in the plain suite):
- Key parity: monkeypatch `tokdash.tui.data.get_cached_or_fetch` recorder;
  `fetch_usage("today", d, d)` yields the key `== _usage_cache_key("today", d, d)`
  computed via the api helpers; same for insights (facets verbatim identity
  assert: `facets is REPORT_FACETS` reaches the key), active-time (include_review
  True and None produce different keys), stats (route expression byte-for-byte,
  year=None and year=2026).
- `resolve_report_period`: with `today=` a fixed Tuesday, `"week"` →
  `("today", Monday, Tuesday)` etc. for month/year via `_report_windows`;
  `"7d"`/`"14d"`/`"today"`/`"30"`/`"all"` pass through as `(token, None, None)`;
  `"banana"` → ValueError; case-insensitive `"WEEK"`.
- `FetchOutcome` pass-through (status/age from a faked CacheFetchResult).
- `ensure_usage_db_compatible`: no-op under `TOKDASH_USAGE_DB=0` (spy that
  `raise_if_usage_db_incompatible` is NOT called and `UsageEntryStore` is NOT
  constructed); calls it when enabled; re-raises schema-too-new.
- `db_summary` both modes (no store construction when disabled).
- Retry helper: faked fn raising `CacheBackpressureError` twice → succeeds, 4× →
  raises; `time.sleep` monkeypatched and delay formula asserted (450 ms·(n+1)+jitter,
  jitter < 250 ms).

`tests/test_tui_formatting.py`:
- Number/cost/duration table (999 / 1_000 / 12_400 / 1e12; None cost headline →
  em-dash, $0.00 prints; row cost 0.0 → em-dash; durations "42s","7m","5h 03m",
  "1 day 1 hour","1 week 2 days","3 months 12 days").
- **Percentage-points lock:** `compute.pct_change(110, 100) == 10.0` and
  `fmt_delta(10.0)` renders `"↑ 10.0%"` — the points convention can never be
  ×100'd twice.
- **Delta-color split lock:** `fmt_delta(5.0, surface="report").style == "ok"`,
  `fmt_delta(5.0, surface="overview").style == "bad"`, `fmt_delta(-5.0,
  surface="overview").style == "ok"`, `fmt_delta(0.0, surface="overview")` →
  "→ 0.0%" muted; comment cites index.html:14908/1752 vs 8323.
- Glyph probe under monkeypatched `sys.stdout.encoding` (cp1252 → ascii ramp).
- Emitters: `emit_plain` has no `\x1b` and no brackets; `emit_ansi` gated by
  NO_COLOR (engine behavior); `emit_markup` maps styles and escapes `[` → `\[`.
- day_spark thresholds/empty; text_bar width/glyphs; window_line unrecognized
  warning branch.

`tests/test_tui_report.py`:
- Canned fixtures (deepcopy'd) for usage/insights/active_time (importorskip
  nothing — pure python). Monkeypatch `tokdash.tui.report`'s imported fetch
  names + `_local_today`. Human path assertions: window line, "Top agent",
  `Sessions 42`, top-10 truncation, em-dash for a missing active_time join and a
  `cost=0.0` row, `projects.unavailable_reason` warn branch, source_errors footer
  (string entry + dict-with-source), db footer both TOKDASH_USAGE_DB modes,
  insights soft-fail → dash + `analytics scan failed` line + **exit 0**.
- `--json`: `cli(["report","--period","week","--json","--pretty"])` stdout parses;
  keys == §5.4 contract; `--output` writes utf-8 with trailing newline and NO ANSI
  even with `tokdash.tui.report._color_enabled` monkeypatched `lambda: True`;
  piped text (stdout not tty) width == 80 (assert a deterministic fixture line
  count / `shutil.get_terminal_size` monkeypatch).
- Parser guards: `cli(["tui","--json"])` → exit 2; `cli(["report","--period","banana"])`
  → exit 2; `cli(["report","week"])` → exit 2 (db_action choices — period is
  flag-only); `cli(["report","--dev-fixture","dense"])` → exit 2 (existing guard);
  `cli(["tui","status"])` parses (db_action "status" is a legal choice) and would
  be ignored — assert via monkeypatched run_tui capturing args.
- **Non-mutation:** fixtures deep-copied before `build_report_segments` + report
  run; deep-compare after (compute payloads share nested rows —
  `top_models` rows are `combined_models` slices, compute.py:989).

`tests/test_tui_import_discipline.py`:
- Install a `sys.meta_path` finder that raises `ImportError("textual blocked")`
  for `textual*`; inside it: `import tokdash.tui.data, tokdash.tui.formatting,
  tokdash.tui.report` succeed, `run_report` (fetchers monkeypatched) returns 0;
  `"textual" not in sys.modules` after. Removing the finder, `import
  tokdash.tui.app` works.

`tests/test_tui_app.py` (textual installed via `-e .`; keep the whole module on
`pytest.importorskip("textual", reason=...)` so a stripped env stays green):
- Canned FetchOutcomes monkeypatched at `tokdash.tui.app` fetch names;
  `async with TokdashApp("today").run_test() as pilot:` — `ov-kpis` text contains
  `"12.4M"` and `"$142.31"` and Overview-red up-delta (surface="overview");
  `ov-tools` row count == len(by_tool); skeleton copy present before completion;
  status bar contains `cache hit` + db line.
- press `"2"` → report pane active, `rp-body` contains `"Top agent"` (report tab
  lazy-load fired); press `"p"` in overview → next `fetch_usage` called with
  `"7d"`; press `"P"` → back to `"today"`.
- Worker failure: `fetch_usage` raising `RuntimeError("boom")` → pane shows
  `"error: boom"`, app alive; raising `UsageDatabaseSchemaTooNewError` → app
  exits with return_code 1.
- Stale re-read: canned `status="stale"` outcomes; monkeypatched `set_timer`
  captures the scheduled callback + delay == `STALE_REREAD_SECONDS == 12.0`;
  invoking the callback re-calls the fetch with `refresh=False` exactly once
  (second stale outcome does NOT reschedule for the same gen).
- Day rollover: monkeypatch `tokdash.tui.app._local_today` to a mutable holder;
  flip the date, `pilot.press("r")` (or any key) → pane reload started (fetch call
  count grows) and `self._today` advanced.
- `run_tui` tty preflight: monkeypatch `sys.stdout.isatty → False` → SystemExit
  (this also protects the rest of the suite from ever entering the alt screen).
- Full-suite regression: `PYTHONPATH=src python3 -m pytest` green with the two
  new command choices (parser-surface tests `test_cli_serve.py`,
  `test_version_surfaces.py` unaffected; verified no test pins the choices list).

Manual gates (spec'd, not CI'd) — Windows first-class per AGENTS.md:
Windows Terminal + legacy conhost `tokdash report` (blocks→`#`, colors→plain when
`_color_enabled` false); `tokdash tui` under WT; `NO_COLOR=1` and `TERM=dumb`
refusals; `tokdash tui` running while `tokdash serve` is warm (stat-only sync, at
most one duplicate tail parse); cp1252 console with an emoji in a project path
(errors='replace' survives — `_harden_windows_stdio`).

## 11. Open questions — resolved

1. **textual pin** → `textual>=8.0.0` floor-only; PyPI live-verified 8.2.8,
   requires-python >=3.9. No upper bound. (Textual 8.x worker API is materially
   different from ≤5.x; the §6 supervisor/`wait()` pattern is 8.x-only and locked.)
2. **Worker result delivery in 8.x** → `Worker.wait()` inside async supervisors;
   generation guards; no `on_<name>_success`, no `on_worker_failed`.
3. **Pane switching keys** → `1`/`2` bindings; `tab` is focus-nav (verified does
   not switch panes); TabStrip Left/Right when focused.
4. **Delta colors** → per-surface split (§4), each matching its own web CSS.
5. **New flags** → none; `--period` is the report window selector
   (week|month|year → calendar `_report_windows`; rolling tokens pass through;
   unrecognized → exit 2 at parse).
6. **One-shot report windows** → single window (ccusage-style); TUI Report default
   week (idx 0). No 3-window default print.
7. **Stale visibility** → `FetchOutcome` + status bar + exactly-one silent 12 s
   non-forced re-read per load.
8. **Midnight on a long-lived TUI** → keypress-triggered day-rollover restart
   (day-pinned keys regenerate naturally; nothing forced).
9. **Partial failures in the report** → usage hard-fail; insights/active soft-fail
   to dashes + warning + exit 0.
10. **include_review_sessions** → Overview `None` (route default key), Report
    `True` (warm key) — pinned per surface, never mixed.
11. **Headless safety** → report/formatting/data never import textual/rich;
    enforced by the meta_path discipline test.
12. **Project names** → always included (matches warmed keys); no toggle flag in v1.
13. **Help** → built-in HelpPanel on `question_mark` (zero custom classes).
14. **Sessions / quota / multi-server / auto-refresh / _warm_caches** → out of v1
    (locked).

## 12. Rejected alternatives (kept for the record)

- `tokdash/term.py` refactor of `onboard/engine.py` (blast radius on shipped
  setup/doctor/update code; re-export instead).
- README-lag policy ("other locales may lag one release") — violates
  RELEASING.md's same-PR six-README rule.
- textual upper-bound pins (`<3.0`, `<<4.0`) — stale vs the verified 8.2.8 line.
- `KpiCard`/`ChipsLine`/`Heatmap` custom widgets; per-figure `rp-*` widget tree
  (replaced by the single segment-fed `rp-body`).
- Three-window one-shot report default; `--back N`; `--from/--to`; `--window`;
  `--no-project-names`; `--ascii`; `--width`.
- `tab`/`shift+tab` bindings as pane switchers (they move focus).
- `call_from_thread`-style cross-thread painting (the supervisor pattern paints
  on the event loop; threads never touch widgets).
- `pytest-textual-snapshot` screenshots; Overview insights facet scan (the web
  Overview band uses the warmed stats key); embedding uvicorn in the TUI.

## 13. Round 2 — delegation, period visuals, Detail, Quota tab (as shipped)

1. **Remote delegation** → `data.py` answers through the same fetch seam the
   one-shot report uses: when a local server answers `/api/*`, the TUI fetches
   over HTTP (shares its caches/warm keys); no server → in-process compute.
   Tests never open sockets (the seam is monkeypatched).
2. **Adaptive chart slot** → one `#ov-chart` Static, dispatched by TOKEN
   (week day bars / month grid / year heatmap), fed by the existing
   trailing-365 stats fetch plus ONE calendar-year fetch that fires for the
   year token only (mutation-locked).
3. **Detail section** → `charts.tool_model_detail` (pure): section row per
   tool + indented model rows; openclaw synthesizes from `openclaw_models`.
4. **Quota tab** → third TabPane, lazy (first activation loads, never
   re-fetches), companion-shaped cards (`charts.quota_table`), snapshot-only.
   `u` is the ONLY network entry point (mirrors `tokdash quota poll`);
   no timer may ever poll. Reentrancy-guarded; stranded-load recovery
   mirrors the other panes.
5. **Credits / status split** → codex reset credits are a bottom section
   (with expiry timestamps), never a table row; the status column is a
   `Text` cell — ok GREEN, anything else RED.
6. **Claude windows** → `weekly_scoped_fable` labels as "Fable", separate
   from plain "Weekly" (prefix-strip + exact match, never substring).

## 14. Round 3 — scroll, keys, date shift, full lists, names, stats rework (as shipped)

1. **Scroll** → TabbedContent/TabPane ship NO overflow in textual 8.2.8:
   `TabbedContent { height: 1fr }` + panes `height: 1fr; overflow-y: auto` —
   the PANE is the scroll container (wheel scrolls it, `screen.scroll_y`
   stays 0). `height: auto` panes never scroll (virtual == region). Locked by
   a wheel test at 80×14 (80×24 would pass vacuously) posting
   `events.MouseScrollDown/Up` (Pilot has no mouse-scroll).
2. **DataTable `max-height` dropped** → the wheel bubbles past tables to the
   pane (verified), so a capped table would leave its tail rows
   wheel-unreachable, defeating the never-collapse rule.
3. **Keys** → `P` REMOVED (p cycles forward only). `t/w/m/y/a` select the
   period DIRECTLY on both period panes (`t`/`a` inert on Report — its
   windows are week/month/year; the whole set inert on Quota).
4. **Date-shift axis** → `[` / `]` pull the window END DATE one day back /
   forward (clamped ≥ 0, never future), `0` resets. The anchor is
   `today − shift`, snapshotted ONCE per load and threaded through every
   date-reading job: `resolve_overview_period(today=anchor)`,
   `report_windows(anchor)`, AND the year heatmap's CY (`anchor.year` — the
   New Year shift would render the wrong calendar year otherwise). Shift
   actions reload Overview + Report-if-started; day rollover does NOT reset
   the shift (standing user intent; the anchor moves with the day). `[`/`]`
   are inert on Overview `all`/Nd (no window to translate); always live on
   Report. Shift > 0 is cold compute but correct — warm-key parity is
   preserved byte-exactly at shift 0.
   **SUPERSEDED (round 6, §17):** the step UNIT is the pane's whole calendar
   period, not a day; the anchor is the resolved window END, not `today−shift`.
5. **Never collapse** → `tool_model_detail` lists EVERY model by default
   (the `…+k more` row exists only when a caller passes an explicit `top_n`).
6. **Normalized tool names** → `sessions.TOOL_LABELS` (canonical) +
   `formatting.tool_label` web-`formatToolName` fallback; the map is passed
   INTO charts as data (purity), report.py imports it (already backend-side).
7. **Overview de-noise** → the trailing-365 band and daily sparkline are
   DELETED (the `stats(None)` fetch stays — week/month charts read its
   contributions); the date line is the section header (semantic `"header"`
   Run → `reverse bold` markup / bold ANSI, translated ONLY in report.py's
   emitters); today and all show NO chart.
8. **Stats table rework** → tools table: Tool | Input | Output | Cache |
   Total | Hit | Cost | Msgs | Time; Input/Output/Cache/Msgs from
   `usage.apps[tool]` (openclaw has no entry → dash cells), Time joined from
   the active payload's per-tool `active_ms_sum` (exact-then-case-folded
   key). The standalone active-times bar section is deleted.
9. **ROUND-3 OVERRIDE of the null-vs-zero law — Time column ONLY**: a
   MEASURED 0 prints EM_DASH in the Overview tools table ("0s should not be
   displayed"), while the report agents table keeps `0s`. Two tests lock the
   split; do not unify.

## 15. Round 4 — margins, full field sets, alignment, pills, legends, loading law, credits, bare verb (as shipped)

1. **Section margins** → `#ov-hints/#ov-chart/#ov-tools/#ov-models/
   #ov-detail/#rp-hints/#quota-hints/#quota-table/#quota-credits {
   margin-top: 1 }` — a margin on a DataTable renders a real 1-row gap in
   8.2.8; tables stay `height: auto`, panes stay the scroll containers.
2. **Full field sets** → the models table (8 cols: Model | Input | Output |
   Cache | Total | Hit | Cost | Msgs) and the detail table (9 cols, +Time)
   carry the tools table's breakdown ("the model tool/model do not have all
   the fields i asked, only tool have"). `tool_model_detail` returns
   KIND-PREFIXED 10-tuples `("tool"|"model"|"more", name, in, out, cache,
   total, hit, cost, msgs, time)`: section rows take the SAME by_tool+apps
   join and the active-time join as the tools table (one shared helper,
   `charts.active_time_ms`, exact-then-case-folded), model rows read their
   OWN dict fields (compute.py ships them per model).
3. **Model Time is ALWAYS EM_DASH** → no per-model active time exists
   anywhere in the payloads (active intervals are per-tool only), and the
   round-3 override already prints measured-zero as a dash — "no source"
   and "measured zero" land on the same cell by law.
4. **Alignment** → textual 8.2.8 DataTable has NO column-level justify
   (`add_column(label, *, width, key, default)` — verified against the
   installed source). Numeric cells enter as rich `Text(value,
   justify="right")` (app `_num`), which right-aligns inside the column
   (auto-sized in round 4; FIXED widths from round 5 — see §16.2).
   THE TWO CELL PATHS ARE DELIBERATELY DIFFERENT: str cells are
   markup-parsed (user data MUST enter as `escape(str(...)`), Text cells are
   literal (raw strings, no escape) — do not "unify" them.
5. **Section pills** → detail section names render as
   `Text(name.ljust(...) + "  ", style="bold on #334155")` — the
   background covers the text span INCLUDING the trailing pad, so every
   pill is a full-width band (round 4 padded to the widest section name;
   round 5 pads to the shared name-column width — see §16.2). On
   2-color conhost the fill degrades to plain (cosmetic; conhost gates
   remain deferred per §10).
6. **Key legends** → user-confirmed spelled-out wording, a Static painted
   once at the TOP of each period pane (" t today · w week · m month · y
   year · a all" / " [ earlier · ] later · 0 back to today"; Report line 1
   " w week · m month · y year"; Quota " u poll now"), echoed compactly in
   the status bar ("t/w/m/y/a period · [ ] shift day · 0 today"). The pane
   text rides `emit_markup` (hand-escapes "["); the status hint keeps
   `escape()`.
7. **ROUND-4 LOADING LAW (reverses the round-1 "never blank painted
   data")** → a reload NEVER leaves the previous window's figures standing:
   `_begin_load` → `_paint_loading` clears the pane (tables `.clear()`,
   kpi/chart emptied and hidden, credits hidden) and shows a dim
   "computing…"; the long first-run skeleton stays only while NOTHING was
   ever painted (or after a failure). Clearing belongs to the NEWEST load;
   superseded results still die at the gen guards, so a cleared pane is
   refilled exactly once by its owner. Flicker on rapid key-repeat is
   accepted (explicitly requested).
8. **Credits, provider-labeled, ALL providers** → the Quota credits section
   is no longer Codex's: `_credit_sources` collects from every card — a
   multi-account card answers PER INSTALL via `accounts[].reset_credits`
   (absent-not-null; the provider-level block IS the primary install's same
   block, so taking accounts[] never double-counts it) and rows carry the
   install ("Claude Code@academic" beside "Claude Code@default"); single-
   account cards use the provider-level block. Rows sort by expiry ASC
   across the EPOCH-INT (claude) / ISO-STRING (codex) duality (null last),
   cap 5 TOTAL, header sums `available_count` across sources; absent every-
   where → NO section at all (the "0 available" header belongs to a card
   that granted none, never to a universe without credits). `labels`
   (TOOL_LABELS) arrive as data — charts purity holds.
9. **Bare `tokdash` prints help** → `command` default is None and `cli()`
   answers None with `parser.print_help(); return 0` (global flags alone are
   still no verb). The old implicit `serve` + browser-open is gone; every
   autostart writer embeds "serve" explicitly, so nothing unattended rides
   on the default. `serve` itself is unchanged.

## 16. Round 5 — no cursor band, shared column grid, grouped quota (as shipped)

1. **No row cursor anywhere** → "the first item got background highlighted"
   was the DataTable cursor: 8.2.8's default `cursor_type` is `"cell"`, and
   on a fresh table the cursor cell is row 0 col 0 — painted with a
   background band. Every table (three Overview + quota) is constructed
   `cursor_type="none"`; the wheel is the only scroll axis, so the
   round-3 note "a focused table still pages with the cursor keys" is gone.
   (8.2.8 accepts an ARBITRARY string for cursor_type — validation would
   silently swallow a typo; the test reads the reactive back.)
2. **ONE shared column grid** → "align the input output ... rows": each
   Overview table is its own auto-sizing DataTable, so identical FIXED
   `width=` per column is the only cross-table alignment lever. `_add_ov_grid`
   is the single source of truth (name `_COL_NAME`, Input..Total `_COL_TOK`,
   Hit `_COL_PCT`, Cost `_COL_COST`, Msgs `_COL_MSGS`, +Time `_COL_TIME`);
   the width pins the column edge, `_num`'s right-justify pins the digit
   edge. Models = the same grid minus Time. Consequence: names longer than
   `_COL_NAME` crop (rare, display-only) and the detail pill pads to
   `_COL_NAME` (the round-4 "widest section name" rule is dead).
   Geometry tests read `table.ordered_columns` — `.columns` is a key→Column
   MAPPING (iterating it yields ColumnKeys, which have no width).
3. **Quota grouped by provider** → a provider writes its name on its
   group's FIRST row only. Continuations are "" (same account) or the bare
   "@account" when the card folds accounts — compared against the
   PRECEDING row's account, not the group opener, so interleaved accounts
   each re-assert their label instead of silently joining the group above.
   An ALL-EMPTY spacer row separates groups (`["", ..., ""]` — the only
   "margin" a DataTable can render between rows), never before the first
   group or after the last. The app paints the empty status cell PLAIN (no
   red-Text override on a spacer). `rows` is display data: the test-side
   group walk skips all-empty rows and opens a group at every non-empty r[0]
   that does not start with "@".

## 17. Round 6 — period stepping, stranded-pane re-issue, stale-slot drop (as shipped)

1. **`[` / `]` step WHOLE CALENDAR PERIODS** (user bug: month view 9.1→9.24
   stepped to 9.1→9.23; it must go to Aug 1→31). The counter (`_period_shift`)
   counts PERIODS in each pane's own unit: `_stepped_calendar_window`
   (data.py) returns the FULL period N steps before `today`'s — whole month
   (`divmod` on `y*12+month`, end = day 1 after +32d `replace(day=1)` − 1d,
   so Feb gets its true 28/29 and no Feb-31 clamp exists), whole Mon–Sun
   week, whole calendar year (endpoints from the year NUMBER — a Feb-29
   today stepping into a common year cannot explode). PAST windows are
   never clamped to today; only shift 0 is, and it delegates to
   `_report_windows(today)` EXACTLY — warm-key parity preserved byte-for-
   byte (mutation-locked). The "today" token's period IS a day, so its
   steps stay single days. `_anchor()` is DEAD: each resolver call takes
   `today=` + `shift=`; `_ov_anchor` is now the RESOLVED WINDOW END, so the
   year heatmap's CY and today-marker follow the shifted period.
2. **Marker units follow the pane** → status `· -Nd` became `· -{n}{unit}`
   with unit from `_shift_unit(token)` (d/w/m/y); Overview "all"/rolling and
   Quota have no unit → no marker (it can never lie about a window that did
   not move). The "(viewing Nd back · 0 = today)" range suffix got the same
   unit treatment. Hint copy: "shift day" → "shift period", bindings read
   "Prev/Next period".
3. **A shift is FULLY INERT when nothing is date-pinned** (windowless
   Overview AND Report never started): no counter store (a lazy Report start
   must not silently begin already-shifted), no gen bump (a bump without a
   successor strands the visible pane's in-flight load — the reviewer's
   finding), no reload. `0` is never inert while shifted: the counter
   clears even when there is nothing to reload ("0" always means now).
4. **Stranded VISIBLE panes re-issue immediately** — `_end_load` knew only
   the TabActivated hook (hidden panes); `p` while a quota fetch is in
   flight stranded the quota pane the user is looking at. When the finished
   load's gen moved AND no successor took the pane AND the pane is the
   ACTIVE one, its load is re-issued at the current gen (lands; cannot
   loop). Hidden panes keep the TabActivated rule.
5. **Reload drops the late-payload SLOTS** (round-4 loading law, completed):
   usage repaints MID-load and reads `_ov_active` / `_rp_insights` /
   `_rp_active` / `_quota_history_val` — they kept the PREVIOUS window's
   payloads, so KPIs/Time showed stale figures beside fresh tables and the
   report's "running…" note vanished too early. `_begin_load` now clears
   them per pane; every paint site already treats None as dash/pending.
   (Test law: `_ov_state == "ok"` is STICKY across reloads — completion
   waits must watch slots or counters, never the state flag.)
6. **Textual pinned `>=8.0,<9`** → app.py is written against VERIFIED Textual-8
   facts (no `on_*_success` delivery, non-async `@work` needs `thread=True`,
   no DataTable column justify, arbitrary `cursor_type` strings); an unpinned
   major could silently break the dashboard on an upgrade. Adopting 9.x is a
   deliberate change, not a resolver accident.
7. **The disabled-poll note rides emit_markup** → "warn" is a SEMANTIC run
   name (report.py maps it to yellow); written as raw markup the literal
   "[warn]" tag PAINTED in the pane. Every Static update in the app now goes
   through `emit_markup`/`Run` or plain strings — raw semantic-tag markup is
   a bug class, and the test asserts the TRANSLATED style in the content.
8. **The db footer read runs off the event loop** → `_update_status` used to
   call `db_summary()` INLINE on the first paint (opening/counting SQLite on
   the UI thread — a visible freeze on a big store). `_db_summary_job`
   (thread) + the exclusive `_refresh_db_line` watcher now own it;
   `_ensure_db_line` is a PURE READ returning "db status pending…" until the
   first answer lands, and the watcher repaints the report footer ONLY while
   the pane is idle — repainting mid-reload would re-emit the previous
   window's body on top of the loading clear (sticky-state again). Tests
   record EVERY `db_summary` call's thread (a last-writer-wins flag is
   masked by the legit worker call) and interleave a landing against a
   gated report reload.
