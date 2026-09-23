# Releasing Tokdash

Use this checklist for manual releases so the PyPI publish, Git tag, and GitHub Releases page stay in sync.

## Pre-release checklist

Before tagging:

1. Ensure `pyproject.toml` and `src/tokdash/__init__.py` have the same version.
2. Update `docs/development/CHANGELOG.md` with a new `## X.Y.Z - YYYY-MM-DD` section, following
   [Changelog entries](#changelog-entries) below.
3. Update `src/tokdash/static/release-notes.json`: set `current` to the package version and add that version as the first release so the in-app **What's new** view stays in sync.
   Then mirror that release into Chinese, or the Chinese view leads with an English entry (see
   [Changelog languages](#changelog-languages)):
   - add the version to `src/tokdash/static/release-notes.zh.json` with the same sections, in the
     same order, with one translated string per English item;
   - regenerate the page a GitHub Release links to: `python3 scripts/changelog_cn.py`.
4. If `README.md` changed this release, mirror the changes into **every** translated README —
   `README_CN.md`, `README_ES.md`, `README_JA.md`, `README_KO.md`, `README_PT.md` — so all six stay
   in sync. Sections, flags, examples, and links to other docs should match; only prose is
   translated. See [README sync](#readme-sync) below.
5. Ensure the worktree is clean except for intended release changes.
6. Run the test suite:
   ```bash
   PYTHONPATH=src python3 -m pytest
   ```
7. Build the package locally:
   ```bash
   python3 -m build
   ```
8. Confirm the release tag does not already exist locally or on `origin`.
9. Tag the current `HEAD` only, never an older commit.

## Changelog entries

Every entry ends with the PR that made the change, after the final period:

```
- Cline now falls back to the session record's working directory. (#40)
- Added opt-in Z.ai Coding Plan quota tracking. (#48, thanks @Werkaninchen)
```

- Add `thanks @handle` when the PR came from someone other than the maintainer. Self-thanks is noise.
- Cite the PR that did the work, not the release PR that writes the entry down. Entries are usually
  written at release time, so `git log -S` on the changelog finds the wrong commit. List the PRs in
  the range instead:
  ```bash
  git log --format='%s' vPREV..HEAD
  gh pr list --state merged --json number,title,author
  ```
- When the entry closes a reported issue, cite that too: `(#42, closes #41, thanks @handle)`.
- If the PR cannot be identified, leave the ref off. A wrong number is worse than none.
- `src/tokdash/static/release-notes.json` carries no refs or credits. It is rendered as plain text
  in the in-app **What's new** view, so `(#48)` would show up literally and `@handle` would not link.

## Changelog languages

Two languages ship today: English, which is canonical, and Simplified Chinese, which is the only
translation. Every other dashboard locale reads the English record. That is a deliberate limit --
this project releases several times a week, and a changelog translated into six languages is a
debt that gets paid on every one of them.

| Artifact | Audience | Language |
|---|---|---|
| `docs/development/CHANGELOG.md` | repository, GitHub Release body | English, with `(#NNN)` refs |
| `src/tokdash/static/release-notes.json` | in-app **What's new** | English, no refs (rendered as plain text) |
| `src/tokdash/static/release-notes.zh.json` | in-app **What's new**, Chinese locale | 中文, no refs |
| `docs/development/CHANGELOG_CN.md` | GitHub Release link, generated | 中文 + the section's PR refs |

The dashboard overlays the Chinese strings on the English payload by index, so the sidecar must
carry one string per English item. Anything it does not carry falls back to English per release --
that is how a locale without a sidecar and an untranslated old release both behave, and it is why
an incomplete translation is a soft edge rather than a broken view.

Rules worth keeping:

- The newest release is always translated. The in-app view opens on it, and an English entry there
  is the Chinese view failing exactly when someone looks.
- Keep at least ten releases translated (`MIN_TRANSLATED_RELEASES` in
  `tests/test_changelog_localization.py`); backfill older ones when there is time.
- Never hand-edit `docs/development/CHANGELOG_CN.md`. It is generated from the sidecar plus the PR
  refs pulled out of the English section, so the app and the page cannot drift.
- `zh` means Simplified for every Chinese reader. `detectBrowserLang()` matches on prefix, so
  `zh-CN`, `zh-TW` and `zh-HK` all resolve to `zh` and read Simplified today. A Traditional
  translation needs a dashboard locale of its own first -- a sidecar file alone cannot reach
  those readers, because nothing would select it.
- Adding a third language is a standing obligation, not a filename: add the locale's sidecar to
  `RELEASE_NOTES_SIDECARS` in `src/tokdash/static/index.html`, widen `SUPPORTED_SIDECAR_LANGS` in
  the test, and extend this section.

`pytest` guards all of it -- item counts, the current version being translated, the generated page
being current -- and CI runs the suite, so a forgotten translation fails the release PR instead of
the reader.

## README sync

There are six READMEs and they are one document in six languages, not an English original with
optional translations. A PR that changes `README.md` updates all five siblings in the same PR:
`README_CN.md`, `README_ES.md`, `README_JA.md`, `README_KO.md`, `README_PT.md`.

What must match across all six:

- **Section skeleton** — the same headings in the same order at the same levels.
- **Client support matrix** — the same client rows.
- **Identifiers** — flags, env vars, file paths, config keys, code fences and the commands inside
  them. Only placeholders inside them are translated (`--port <port>` may become `--port <puerto>`).
- **Links to other docs** — a guide linked from `README.md` is linked from all six. Pointing a
  translation at CLI help instead is a dead end for that language.
- **Feature bullets** — the same list, no language missing an entry.

Only prose is translated. Screenshot URLs, badge label text and store-badge locales are
per-language by design and are expected to differ.

A quick way to spot drift before opening the PR:

```bash
# every translation should report the same counts as README.md
for f in README*.md; do
  printf '%-14s headings=%s fences=%s\n' "$f" \
    "$(grep -cE '^#{1,6} ' "$f")" "$(grep -c '^```' "$f")"
done
```

Then diff the backticked identifiers per file; a translation missing one usually means a paragraph
was never carried over.

## Release sequence

Push `main` first, then push the tag in sequence:

```bash
VERSION=X.Y.Z

git add pyproject.toml src/tokdash/__init__.py \
  src/tokdash/static/release-notes.json src/tokdash/static/release-notes.zh.json \
  docs/development/CHANGELOG.md docs/development/CHANGELOG_CN.md
git commit -m "Release v$VERSION"
git tag -a "v$VERSION" -m "Release v$VERSION"
git push origin main
git push origin "refs/tags/v$VERSION"
```

The `publish-pypi.yml` workflow will publish to PyPI from the pushed tag.

## GitHub Release step

Git tags and GitHub Releases are separate objects.

Pushing `vX.Y.Z` is enough to trigger the PyPI workflow, but the version will not appear on the repository Releases page until a GitHub Release object is created for that tag.

After the tag push succeeds, build the release body from the matching changelog section:

```bash
VERSION=X.Y.Z

python3 scripts/release_body.py --version "$VERSION"

gh release create "v$VERSION" \
  --title "v$VERSION" \
  --latest \
  -F /tmp/tokdash-release-notes.md
```

The script writes `/tmp/tokdash-release-notes.md`: the English section, with a
`**简体中文：**` line on top linking that version's section on
`docs/development/CHANGELOG_CN.md`. GitHub renders one body per release and has no notion of a
locale, so a link to the translated page is as close to a bilingual release as the platform
allows. If the version is missing from `release-notes.zh.json` the script says so on stderr and
omits the line, because linking a heading that was never written is worse than no link.

If the tag already exists but the release page does not show it, check:

```bash
gh release view "v$VERSION"
```

If that fails with `release not found`, the tag exists but the GitHub Release object has not been created yet.

## Post-release verification

Verify all three release surfaces:

```bash
git ls-remote --tags origin "refs/tags/v$VERSION"
gh release view "v$VERSION"
pip install "tokdash==$VERSION"
```

Also confirm the GitHub Actions `Publish to PyPI` workflow succeeded for the pushed tag.
