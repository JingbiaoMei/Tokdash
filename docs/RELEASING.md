# Releasing Tokdash

Use this checklist for manual releases so the PyPI publish, Git tag, and GitHub Releases page stay in sync.

## Pre-release checklist

Before tagging:

1. Ensure `pyproject.toml` and `src/tokdash/__init__.py` have the same version.
2. Update `docs/CHANGELOG.md` with a new `## X.Y.Z - YYYY-MM-DD` section.
3. Ensure the worktree is clean except for intended release changes.
4. Run the test suite:
   ```bash
   PYTHONPATH=src python3 -m pytest
   ```
5. Build the package locally:
   ```bash
   python3 -m build
   ```
6. Confirm the release tag does not already exist locally or on `origin`.
7. Tag the current `HEAD` only, never an older commit.

## Release sequence

Push `main` first, then push the tag in sequence:

```bash
VERSION=X.Y.Z

git add pyproject.toml src/tokdash/__init__.py docs/CHANGELOG.md
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
' docs/CHANGELOG.md > /tmp/tokdash-release-notes.md

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
