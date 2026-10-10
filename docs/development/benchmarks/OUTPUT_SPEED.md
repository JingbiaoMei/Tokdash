# Output speed benchmarks

The lazy timing cache removes timing extraction from ordinary usage ingestion and
Overview requests. Scoped publication and aggregate caching substantially reduce
work for short sessions and repeated model comparisons. Large-history builds and
CPU/storage contention remain material costs: the measurements below do not
establish unchanged Overview performance or fast ordinary reopening on real
history.

These measurements were collected on 9–10 October 2026 from isolated source
checkouts and disposable databases. They precede integration with `main` at
`627bfc1` (v2.6.13); that integration receives separate correctness validation.
The subsequent UI change removes duplicate stale notices without changing cache
or measurement semantics. This is a benchmark report for the reviewed feature,
not a claim that CI measures performance.

## Method

Paired fixtures use three alternating before/after pairs, fresh processes,
prepared SQLite backups and the same filesystem device. Operating-system page
caches are not forcibly cleared. Tables show medians and, where relevant, the
minimum and maximum. Differences between paired runs are computed per pair;
they need not equal the difference between the two independent medians.

The release comparison uses the pre-feature v2.6.11 source. Operational-fix
comparisons use earlier versions of the lazy-cache implementation, rather than
the released application. Their improvements cannot establish equivalence to
the pre-feature release. Real-history observations use an isolated development
cache; they are unpaired and do not provide regression deltas.

Browser measurements include rendering and the requests triggered by an action.
Range-control paint, complete table paint and build completion are separate
measurements. CPU and peak RSS belong to the indicated process; RSS is a process
high-water mark, not a per-request allocation or unique-memory measurement.
Windows worker behavior was tested through simulation, rather than a native
Windows benchmark.

## Overview and ordinary session loads

The initial lazy-cache comparison against v2.6.11 measured unchanged full Overview
refresh at **2,493 → 2,690 ms**. The paired median overhead was **118 ms**, with
a range of **37–584 ms** across three pairs. Main-server CPU was **300 → 310 ms**
and peak RSS **107.8 → 107.4 MiB**. Overview performed no timing extraction and
issued zero speed requests, including after visiting the speed page.

The same fixture's cached session-modal paint was **258 → 353 ms**, with a
paired difference of **78 ms [17, 107]**. A 20,000-call fixture's ordinary detail
payload was **4,249,475 → 7,009,623 bytes**; the separate timeline payload was
552,712 bytes. Request isolation does not mean that every existing surface has
identical latency or payload size.

In the first publication/cache-fix comparison, Overview refresh during an
overlapping scoped worker increased by a paired **668 ms [447, 1,477]**. The
latest scope-fix comparison measured paired changes of **+255, −337 and +278 ms**
at 1, 1,000 and 10,000 unrelated native scopes. These latter comparisons are
against the previous lazy implementation and do not cancel the earlier
contention result or establish equivalence to v2.6.11.

## Scoped timing builds

The latest comparison holds 10,000 unrelated native calls constant while
distributing them across different numbers of indexed sessions. It builds one
new Codex response. Before means the reviewed lazy implementation before the
three scope fixes; after includes those fixes.

| Native session scopes | Build wall ms before → after | Worker CPU ms before → after | Peak RSS MiB before → after |
| --- | --- | --- | --- |
| 1 | 361.11 → 351.36 | 52.04 → 50.51 | 29.72 → 29.86 |
| 1,000 | 3,405.15 → 407.72 | 459.68 → 55.92 | 30.88 → 29.90 |
| 10,000 | 29,175.41 → 659.74 | 3,892.00 → 76.05 | 43.62 → 33.24 |

At 10,000 scopes, wall-time ranges were **28,099–31,860 ms before** and
**617–771 ms after**. Signature observations dropped from 10,003 to one.
Every build staged one response, made 20 SQLite row changes and preserved
unrelated additive totals and a sampled response's physical row identity.
Metadata SQL work still grows with scope count; this is not a constant-time
claim for every part of the build.

The preceding 100,000-call fixture tested a different dimension: the
publication fix reduced SQLite row changes from **600,651 to 21** and a short
build from **41.1 s to 260 ms**. Repeated model-table paint improved from
**945 to 261 ms** after aggregate memoization. These figures describe that
earlier comparison, not an additional measurement of the latest scope fixture.

## Model page loads

The latest 10,000-scope browser comparison uses ordinary ensure requests for
opening and reopening the page. It does not rewrite those requests to
`cache_only=true`. An append-reopen verifies exactly one added Codex response.

| Operation | Before median ms | After median ms | After range ms |
| --- | --- | --- | --- |
| First complete build and paint | 46,478.86 | 8,632.71 | 7,746–8,826 |
| Cached reopen | 768.80 | 859.13 | 751–958 |
| Reopen after a small append | 3,079.87 | 3,396.23 | 3,298–5,071 |

The first complete build improves, while cached and append reopening do not
improve in this comparison. The earlier release comparison measured range-control
paint at **92 ms [56, 126]**, complete range-table paint at **240 ms [189, 276]**,
and first hourly-chart paint at **226 ms [224, 242]**. Those are separate fixture
measurements, not latency guarantees for a large real history.

## Storage and real history

On the initial 20,000-call paired fixture, the primary database changed from
**39,718,912 to 39,342,080 bytes (−0.95%)**. Opening speed created a separate
derived file of **98,263,040 bytes**, containing **55,373,824 bytes** of logical
tables/indexes and 10,471 free pages. Existing usage, insights, active-time and
session-list payload sizes were identical in that comparison. Separate timing
storage adds to total disk use even when the primary database is smaller.

The reviewed real snapshot contains **309,823 indexed responses, 309,714
eligible responses and 42,861 measured calls** across Kimi, OMP, Codex, dsh,
Qwen Code, OpenCode, KiloCode and mimo. The derived file was **638,578,688 bytes**.
This published snapshot is incomplete: 19 inputs remained pending, so it must
not be presented as a complete current reindex.

A real full-window build took **18.3 minutes** as observed by the browser, and a
subsequent ordinary reopen requested another full build. The latest recorded
worker timing phase reported **953.8 s wall time and 80.4 s CPU**, excluding
earlier accounting work.
A normal real Overview refresh during an explicitly requested build took
**109.9 s** and made zero speed requests. This unpaired observation cannot assign
that latency to the feature or prove a regression, but it prevents a claim that
the real-history performance requirement has been verified.

Snapshot-only real browser checks verified 62 model rows from all eight
supported harnesses, with no page errors and rates matching the additive token
and duration sums. Their single **6.52 s** paint measurement explicitly used
`cache_only=true`; it is not evidence of ordinary reopen or build latency.

## Reproduction and merge criteria

Run from the project root on Linux with runtime dependencies installed. The
browser comparison also requires Playwright and Chromium. Keep all databases
and generated results under `output/`.

```bash
python3 scripts/benchmark_lazy_speed_scopes.py \
  --baseline path/to/earlier-lazy/src --fixed src \
  --out output/output-speed-scopes --calls 10000 --pairs 3

TOKDASH_BENCH_PYTHON=python3 python3 scripts/benchmark_lazy_speed_scope_browser.py \
  --baseline path/to/earlier-lazy/src --fixed src \
  --common output/output-speed-scopes \
  --out output/output-speed-browser --pairs 3 --reps 1

PYTHONPATH=src python3 -m pytest -q tests
python3 scripts/check_dashboard_js.py
git diff --check
```

Both scope-comparison source trees must contain the lazy-cache modules; the
pre-feature release cannot serve as that harness's baseline. Its comparisons
above come from the separate release benchmark. Each scope/browser invocation
writes `benchmark.json`, individual measurements and browser screenshots.

Correctness checks cover additive aggregation, canonical ownership,
session/report parity, failed-input isolation, snapshot publication, worker
health, bounded result caching and preservation of usage/cost totals. The
reviewed snapshot passed **3,721 tests with six skips**, plus 150 Chromium
assertions. Integration and CI results belong to the PR's tested commit.

The performance merge criteria remain open: unchanged Overview performance
under shared worker load and acceptable ordinary reopening on real history.
Green correctness checks alone do not close those criteria. Merging requires
either meeting them with paired measurements or explicitly accepting the
documented costs.
