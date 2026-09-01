# Releasing Tokdash

Use this checklist for manual releases so the PyPI publish, Git tag, and GitHub Releases page stay in sync.

## Pre-release checklist

Before tagging:

1. Ensure `pyproject.toml` and `src/tokdash/__init__.py` have the same version.
2. Update `docs/development/CHANGELOG.md` with a new `## X.Y.Z - YYYY-MM-DD` section, following
   [Changelog entries](#changelog-entries) below.
3. Update `src/tokdash/static/release-notes.json`: set `current` to the package version and add that version as the first release so the in-app **What's new** view stays in sync.
4. If `README.md` changed this release, mirror the changes into `README_CN.md` so the English and 中文 READMEs stay in sync (sections, flags, and examples should match).
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

## Release sequence

Push `main` first, then push the tag in sequence:

```bash
VERSION=X.Y.Z

git add pyproject.toml src/tokdash/__init__.py src/tokdash/static/release-notes.json docs/development/CHANGELOG.md
git commit -m "Release v$VERSION"
git tag -a "v$VERSION" -m "Release v$VERSION"
git push origin main
git push origin "refs/tags/v$VERSION"
```

The `publish-pypi.yml` workflow will publish to PyPI from the pushed tag.

## GitHub Release step

Git tags and GitHub Releases are separate objects.

Pushing `vX.Y.Z` is enough to trigger the PyPI workflow, but the version will not appear on the repository Releases page until a GitHub Release object is created for that tag.

After the tag push succeeds, create the GitHub Release from the matching changelog section:

```bash
VERSION=X.Y.Z

awk -v v="$VERSION" '
  $0 ~ "^## " v " - " { flag = 1 }
  flag && $0 ~ /^## / && $0 !~ "^## " v " - " { exit }
  flag { print }
' docs/development/CHANGELOG.md > /tmp/tokdash-release-notes.md

gh release create "v$VERSION" \
  --title "v$VERSION" \
  --latest \
  -F /tmp/tokdash-release-notes.md
```

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
