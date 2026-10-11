#!/usr/bin/env bash
# Vendor the Simplified-Chinese sans face the dashboard's font stacks name.
#
# Run from the repository root:
#   scripts/vendor_cjk_fonts.sh
#
# The dashboard needs a CJK family because Fira ships no Han glyph. The stacks in
# index.html already name the ones every operating system ships, which covers most
# machines; this is for the ones that ship none (a slim Linux box, a container) or
# that cannot reach fonts.googleapis.com at all. The dashboard picks the vendored
# face up on its own -- see tokdash/assets.py local_cjk_font_css() -- and needs a
# restart afterwards, because the answer is cached for the process lifetime.
#
# Noto Sans SC is under the SIL Open Font License 1.1, which permits embedding and
# redistribution and requires the license travel with the files. That is why this
# script downloads OFL.txt into the same directory, and why static/vendor/NOTICE.txt
# carries the same notice. The files land unmodified.
set -euo pipefail

weights=${TOKDASH_CJK_WEIGHTS:-400;500;700}
# Google serves woff2 only to user agents that ask for it, so the CSS has to be
# fetched as a browser would or the URLs come back as ttf.
ua='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
css_url="https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@${weights}&display=swap"
license_url=https://raw.githubusercontent.com/google/fonts/main/ofl/notosanssc/OFL.txt

work=$(mktemp -d)
stage=''
trap 'rm -rf "$work"; if [ -n "$stage" ]; then rm -rf "$stage"; fi' EXIT

curl --fail --location --retry 3 -A "$ua" --output "$work/upstream.css" "$css_url"

# Google serves the family as ~100 unicode-range slices so a page downloads only
# the slices it needs. The version in the file URLs (.../s/notosanssc/v41/...) names
# the local directory: /static/vendor/ is cached by the browser for a year, so a
# later version must not be able to answer under a name already cached.
version=$(sed -n 's|.*https://fonts.gstatic.com/s/notosanssc/\(v[0-9][0-9]*\)/.*|\1|p' "$work/upstream.css" | sort -u)
if [[ ! "$version" =~ ^v[0-9]+$ ]]; then
  echo "cannot tell which Noto Sans SC version upstream serves" >&2
  exit 1
fi

grep -o 'https://fonts.gstatic.com/[^)]*\.woff2' "$work/upstream.css" | sort -u > "$work/urls"
if [ ! -s "$work/urls" ]; then
  echo "upstream CSS named no woff2 slice" >&2
  exit 1
fi

font_root=src/tokdash/static/vendor/fonts
font_dir=$font_root/noto-sans-sc-$version
mkdir -p "$font_root"
# Stage on the destination filesystem so the first install is one rename. The
# hidden directory cannot match the server's published-stylesheet discovery.
stage=$(mktemp -d "$font_root/.noto-sans-sc-${version}.XXXXXX")

while IFS= read -r url; do
  # Each slice keeps the name Google gives it, which is what the rewritten CSS points at.
  curl --fail --location --retry 3 --output "$stage/$(basename "$url")" "$url"
done < "$work/urls"

sed "s|https://fonts.gstatic.com/s/notosanssc/${version}/||g" "$work/upstream.css" > "$stage/noto-sans-sc.css"
curl --fail --location --retry 3 --output "$stage/OFL.txt" "$license_url"

if grep -q 'https://fonts.gstatic.com' "$stage/noto-sans-sc.css"; then
  echo "the rewritten stylesheet still points at the CDN" >&2
  exit 1
fi

if [ -d "$font_dir" ]; then
  # Published vendor URLs are immutable. Refuse a different weight set under
  # the same URL, and repair matching installs with atomic file replacements.
  if [ -f "$font_dir/noto-sans-sc.css" ] && ! cmp -s "$font_dir/noto-sans-sc.css" "$stage/noto-sans-sc.css"; then
    echo "Noto Sans SC $version is already installed with different CSS; keep its original TOKDASH_CJK_WEIGHTS until the upstream version changes" >&2
    exit 1
  fi
  mv "$stage/"*.woff2 "$font_dir/"
  mv "$stage/OFL.txt" "$font_dir/OFL.txt"
  mv "$stage/noto-sans-sc.css" "$font_dir/noto-sans-sc.css"
else
  mv "$stage" "$font_dir"
fi

printf 'vendored Noto Sans SC %s (%s weights, %s slices, %s) into %s\n' \
  "$version" "$weights" "$(wc -l < "$work/urls" | tr -d ' ')" \
  "$(du -sh "$font_dir" | cut -f1)" "$font_dir"
echo 'restart Tokdash to serve it; the directory it lands in is gitignored on purpose.'
