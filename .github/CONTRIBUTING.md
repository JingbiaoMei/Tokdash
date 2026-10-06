# Contributing to Tokdash

## Welcome

Thanks for considering a contribution to Tokdash! Tokdash is a local token and cost dashboard for AI coding tools. We welcome contributions ranging from bug fixes and parser enhancements to documentation and performance improvements.

### What we want

- Fixes for parsers (as long as token fields are **explicit** and not inferred)
- New client parsers with real fixtures (redacted) + documented file locations
- UI/UX improvements that keep the dashboard fast
- Docs improvements (especially platform-specific notes)

### What we don’t want (by default)

- Anything that requires copying session cookies/tokens from a browser (security risk)
- Uploading usage or prompts to external services (“phone home”) without an explicit, opt-in design
- Heavy dependencies unless clearly justified

### Security and secrets

- Do **not** commit API keys, cookies, or tokens.
- Use environment variables or local key files under `.api_keys/` (gitignored).
- If you suspect a security issue, please follow our security policy at [`docs/SECURITY.md`](../docs/SECURITY.md) and report vulnerabilities privately.

---

## Local development setup

Set up a virtual environment and install the development dependencies:

```bash
# 1. Create a virtual environment
python -m venv .venv

# 2. Activate the virtual environment
# On Linux/macOS:
source .venv/bin/activate
# On Windows (cmd):
.venv\Scripts\activate.bat
# On Windows (PowerShell):
.venv\Scripts\Activate.ps1

# 3. Upgrade pip and install editable package with development extras
pip install -U pip
pip install -e .[dev]

# 4. Verify installation by running tests
pytest -q
```

Run Tokdash from source:

```bash
# Start server
python main.py

# Or via the CLI entrypoint
tokdash
```

---

## Coding conventions

We adhere to standard Python coding and documentation standards to keep the codebase maintainable and readable:

- **PEP 8**: Follow standard Python style guide conventions across all code.
- **Type hints**: Provide explicit static type annotations for all functions, methods, parameters, and return values.
- **Google docstrings**: Document modules, classes, and public functions using Google-style docstrings (`Args:`, `Returns:`, `Raises:`).
- **Ruff linter**: Use [Ruff](https://github.com/astral-sh/ruff) for linting and code formatting (`ruff check .`, `ruff format .`). Ensure no lint warnings or style errors are introduced.

---

## Test commands

We use `pytest` for running automated tests.

### Running all tests

```bash
pytest -q
```

### Running single test files and specific tests

To run a single test module:
```bash
pytest tests/test_pricing.py
```

To run a specific test by name expression:
```bash
pytest tests/test_pricing.py -k "test_pricing_lookup"
```

To run with verbose output and timing details:
```bash
pytest -vv --durations=20
```

### Visual regression testing and UI development

For UI and front-end work, start the dashboard against a dense synthetic dataset:

```bash
python main.py --dev-fixture dense --dev-seed 17
```

Key points about fixture mode:
- **`--dev-fixture dense`**: Skips the usage and quota background workers, does not read local history, credentials, pricing overrides, or quota snapshots, and rejects mutating HTTP requests. Overview, `/api/tools`, and `/api/openclaw` — header and day grid alike — scale with the requested time window and agree about it, while `/api/active-time` answers the review-sessions toggle. Session lists serve a fixed set of rows. `/api/insights` generates reproducible fixture rows and folds them using production fold functions, allowing the Report tab to be developed and screenshotted without accessing local history.
- **`--dev-seed 17`**: Pins a deterministic seed for reproducible visual state. Omit `--dev-seed` to generate a new dataset on each server start, and copy the printed seed to reproduce that layout.
- Both flags are only accepted by `serve` (`tokdash serve` or `python main.py`) — running `tokdash export --dev-fixture dense` will fail as a usage error rather than silently exporting real usage.

---

## PR submission checklist

Before submitting a pull request, please review and verify the following checklist:

1. **Description**:
   - Provide a clear, concise summary of what was changed and why.
   - Explain the motivation and the approach taken.
2. **Linked issues**:
   - Link any related issues or discussions using keywords like `Fixes #123` or `Closes #456`.
3. **Category**:
   - Clearly state the category/type of the change in your PR title and description (e.g., `feat`, `fix`, `docs`, `chore`, `refactor`, `perf`, `test`).
4. **Testing evidence**:
   - Run `pytest -q` and confirm all tests pass.
   - Provide command output, test results, or screenshots (for UI changes tested via fixture mode) demonstrating that the changes work as expected.
5. **Localized READMEs updated**:
   - If any user-facing text, command-line flags, configuration options, feature bullet points, or documentation links in `README.md` were modified, update all five localized siblings in the same PR:
     - `README_CN.md` (Simplified Chinese)
     - `README_ES.md` (Spanish)
     - `README_JA.md` (Japanese)
     - `README_KO.md` (Korean)
     - `README_PT.md` (Portuguese)
   - Keep section skeletons, client support matrices, identifiers, code fences, and documentation links identical across all six README files. Only translate prose.

### Changelog and credit

Merged pull requests are credited in the changelog ([`docs/development/CHANGELOG.md`](../docs/development/CHANGELOG.md)) by PR number and GitHub handle at release time (e.g. `(#48, thanks @yourhandle)`). A clear PR description helps ensure your change is accurately described and credited.

---

## Release process

Tokdash follows a manual release checklist to keep PyPI packages, Git tags, GitHub Releases, and localized release notes in sync.

For complete release instructions, see the release guide:
[`docs/development/RELEASING.md`](../docs/development/RELEASING.md)

**Key reminders:**
- Pushing a git tag (`vX.Y.Z`) triggers the PyPI publishing workflow, but does **not** automatically populate the GitHub Releases page.
- Always create the corresponding GitHub Release object after pushing the tag (e.g. via `gh release create` using `scripts/release_body.py`).
