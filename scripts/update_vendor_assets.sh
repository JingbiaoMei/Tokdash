#!/usr/bin/env bash
# Run from the repository root. Keep these versions and filenames in sync with
# index.html, sw.js, and static/vendor/NOTICE.txt when updating dependencies.
# The server tells browsers to cache /static/vendor/ for a year, so a new
# library version needs a new filename; never change a file under an old name.
set -euo pipefail

vendor_dir=src/tokdash/static/vendor
mkdir -p "$vendor_dir"

download() {
  local url=$1 filename=$2
  # static/**/* is packaged, so a failed download must not leave a .tmp behind.
  if ! curl --fail --location --retry 3 --output "$vendor_dir/$filename.tmp" "$url"; then
    rm -f "$vendor_dir/$filename.tmp"
    return 1
  fi
  mv "$vendor_dir/$filename.tmp" "$vendor_dir/$filename"
}

download 'https://cdn.tailwindcss.com/3.4.17' 'tailwindcss-3.4.17.js'
download 'https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js' 'chart-4.4.0.umd.min.js'
download 'https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.min.js' 'three-0.160.0.min.js'
download 'https://cdn.jsdelivr.net/npm/flatpickr@4.6.13/dist/flatpickr.min.css' 'flatpickr-4.6.13.min.css'
download 'https://cdn.jsdelivr.net/npm/flatpickr@4.6.13/dist/flatpickr.min.js' 'flatpickr-4.6.13.min.js'
