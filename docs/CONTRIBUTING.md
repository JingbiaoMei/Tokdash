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
source .venv/bin/activate
pip install -U pip
pip install -e .
pip install pytest
```

Run from source:
```bash
python3 main.py
```

Run tests:
```bash
pytest -q
```

## Releases

For the manual release checklist, see [RELEASING.md](RELEASING.md).
Important: pushing a tag is not enough to populate GitHub's Releases page. After tagging and pushing, also create the GitHub Release object for that tag.

## Security / secrets

- Do **not** commit API keys, cookies, or tokens.
- Use environment variables or local key files under `.api_keys/` (gitignored).
- If you suspect a security issue, see `docs/SECURITY.md`.
