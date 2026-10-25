# Contributing

Thanks for considering a contribution!

## What we want

- Fixes for parsers (as long as token fields are **explicit** and not inferred)
- New client parsers with real fixtures (redacted) + documented file locations
- UI/UX improvements that keep the dashboard fast
- Docs improvements (especially platform-specific notes)

## What we don’t want (by default)

- Anything that requires copying session cookies/tokens from a browser (security risk)
- Uploading usage or prompts to external services (“phone home”) without an explicit, opt-in design
- Heavy dependencies unless clearly justified

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate.bat (cmd) or .venv\Scripts\Activate.ps1 (PowerShell)
pip install -U pip
pip install -e .
pip install -r requirements-dev.txt
```

`requirements-dev.txt` adds what the tests need beyond the runtime: `pytest`, `httpx` for
FastAPI's test client, `build` for the packaging tests, and `pytest-timeout` and
`pytest-xdist` for the per-test cap and the parallel run. Some frontend tests also run
`node` and are skipped when it is not installed.

Run from source with its own data directory. A source checkout can be ahead of the Tokdash
you have installed, and opening the usage database migrates it to the newer schema, which
the installed version then refuses to read. A separate `TOKDASH_DATA_DIR` keeps the two
apart (`output/` is gitignored):
```bash
TOKDASH_DATA_DIR=output/dev-data python3 main.py
# PowerShell: $env:TOKDASH_DATA_DIR = "output/dev-data"; python main.py
```

For UI work, start the real dashboard against a dense synthetic dataset:
```bash
python3 main.py --dev-fixture dense --dev-seed 17
```

Omit `--dev-seed` for a new dataset on each server start, then copy the printed seed
to reproduce it. Fixture mode skips the usage and quota background workers, does not
read local history, credentials, pricing overrides, or quota snapshots, and rejects
mutating HTTP requests. Overview, `/api/tools` and `/api/openclaw` -- header and day
grid alike -- scale with the window they were asked for and agree about it, and
`/api/active-time` answers the review-sessions toggle. The session lists are the
exception: they serve a fixed set of rows whatever window you ask for.

Both flags are only accepted by `serve` — `tokdash export --dev-fixture dense` is a
usage error rather than a silent export of your real usage. `/api/insights` answers
from a seeded fixture while one is active, inventing the rows and folding them with
the production fold functions, so the Report tab can be developed and screenshotted
without ever reading your history.

Run tests:
```bash
pytest -q -n auto --dist loadgroup                     # every core, as CI runs it
pytest tests/test_pricing_normalization.py -k lookup   # one file, filtered by test name
```

`-n` comes from `pytest-xdist`. `--dist loadgroup` keeps the tests that share a resource on
one worker; mark a new test that builds from the repo root or holds a fixed port with the same
`xdist_group` as its peers, or two workers will collide on it.

The frontend tests run the shipped JavaScript instead of searching it for strings: they slice
the functions they need out of `index.html` with `extract_js_function()`, run them in Node over
a stub DOM, and assert on what the code did. That helper, `run_node()` and the `needs_node`
marker live in `tests/support/frontend.py`; import them rather than copying them into a new
file. A test that only checks a string is present in the page passes as long as the string
does, whatever the code around it now does. Keep text checks for what really is text: the
translation tables, the vendored assets, the theme registry.

The TUI tests drive the real app through Textual's pilot, one file per pane or concern
(`test_tui_speed_pane.py`, `test_tui_range_strip.py`, `test_tui_resize_refit.py`, ...). The
contract-shaped payloads, the fake service behind the `api` fixture and the pilot helpers live
in `tests/support/tui.py`; a test file imports them after its `pytest.importorskip("textual")`,
never from another test file. Wait on a condition, never on a fixed sleep: `settle()` returns
as soon as the app is idle, and `wait_for()` polls a predicate, both from `tests/support/tui.py`.
A fixed `pilot.pause(0.25)` costs every leg of CI that long on every call, and is still too
short on a slow Windows runner.

The suite points `TOKDASH_DATA_DIR` at a temporary directory itself, so it never touches
your real data.

## Chinese, Japanese and Korean text

Fira Sans and Fira Code carry no Han glyph, so both font stacks end in the CJK face each
operating system already ships: PingFang SC on macOS, Microsoft YaHei on Windows, Noto
Sans SC on Linux. A machine with no CJK font of its own, or one that cannot reach
`fonts.googleapis.com`, has neither, and Chinese then falls to whatever the browser
substitutes, usually a serif face nothing else on the page uses. For those:

```bash
scripts/vendor_cjk_fonts.sh
```

It fetches Noto Sans SC into `static/vendor/fonts/`, which is gitignored, and the
dashboard serves it from the next restart. Downloads are staged with their license;
a failed download preserves the installed copy. `TOKDASH_CJK_WEIGHTS` selects the
weights (default `400;500;700`). Keep the same weights when repairing an installed
upstream version, because its stylesheet URL is cached as immutable. This script
affects a source-tree run; it does not modify an installed pipx service.
The optional font directory is also excluded from wheels and source archives.

## Releases

For the manual release checklist, see [RELEASING.md](development/RELEASING.md).
Important: pushing a tag is not enough to populate GitHub's Releases page. After tagging and pushing, also create the GitHub Release object for that tag.

## Changelog and credit

Merged PRs are credited in [the changelog](development/CHANGELOG.md) by number and handle, e.g.
`(#48, thanks @yourhandle)`. You do not need to write your own entry — entries are written at
release time — but a PR description that says plainly what changed and why makes the entry accurate.
The convention is in [RELEASING.md](development/RELEASING.md#changelog-entries).

## Security / secrets

- Do **not** commit API keys, cookies, or tokens.
- Use environment variables or local key files under `.api_keys/` (gitignored).
- If you suspect a security issue, see `docs/SECURITY.md`.
