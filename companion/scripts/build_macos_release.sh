#!/usr/bin/env bash
set -euo pipefail

repo_root="${COMPANION_REPO_ROOT:-$PWD}"
if [[ ! -f "$repo_root/companion/VERSION" ]]; then
  echo "Run this script from the Tokdash repository root." >&2
  exit 1
fi
version="$(tr -d '[:space:]' < "$repo_root/companion/VERSION")"
build_number="2"
output_dir="${COMPANION_OUTPUT_DIR:-$repo_root/dist/companion/$version/macos}"
signing_identity="${MACOS_SIGNING_IDENTITY:-}"
notary_profile="${MACOS_NOTARY_KEYCHAIN_PROFILE:-}"

if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "companion/VERSION must be MAJOR.MINOR.PATCH, got '$version'" >&2
  exit 1
fi
work_dir="$(mktemp -d "${TMPDIR:-/tmp}/tokdash-companion.XXXXXX")"
trap 'rm -rf "$work_dir"' EXIT

derived_data="$work_dir/DerivedData"
dmg_staging="$work_dir/dmg"
project="$repo_root/companion/macos/TokdashCompanion.xcodeproj"

xcodebuild \
  -project "$project" \
  -scheme TokdashCompanion \
  -configuration Release \
  -destination "generic/platform=macOS" \
  -derivedDataPath "$derived_data" \
  ARCHS="arm64 x86_64" \
  ONLY_ACTIVE_ARCH=NO \
  CODE_SIGNING_ALLOWED=NO \
  SWIFT_TREAT_WARNINGS_AS_ERRORS=YES \
  GCC_TREAT_WARNINGS_AS_ERRORS=YES \
  MARKETING_VERSION="$version" \
  CURRENT_PROJECT_VERSION="$build_number" \
  build

app_path="$derived_data/Build/Products/Release/TokdashCompanion.app"
binary_path="$app_path/Contents/MacOS/TokdashCompanion"
if [[ ! -x "$binary_path" ]]; then
  echo "Release app was not produced at $app_path" >&2
  exit 1
fi

architectures="$(lipo -archs "$binary_path")"
if [[ "$architectures" != *"arm64"* || "$architectures" != *"x86_64"* ]]; then
  echo "Expected universal arm64+x86_64 app, got: $architectures" >&2
  exit 1
fi

release_suffix="-unsigned"
if [[ -n "$signing_identity" ]]; then
  # The app currently has no nested helpers or embedded frameworks. Sign the bundle
  # itself; do not use --deep as a substitute for explicit inside-out signing.
  codesign \
    --force \
    --options runtime \
    --timestamp \
    --sign "$signing_identity" \
    "$app_path"
  codesign --verify --deep --strict --verbose=2 "$app_path"
  release_suffix="-signed-not-notarized"
fi

mkdir -p "$dmg_staging" "$output_dir"
ditto "$app_path" "$dmg_staging/TokdashCompanion.app"
ln -s /Applications "$dmg_staging/Applications"

dmg_path="$output_dir/Tokdash-Companion-$version-macos-universal$release_suffix.dmg"
rm -f "$dmg_path"
hdiutil create \
  -volname "Tokdash Companion" \
  -srcfolder "$dmg_staging" \
  -ov \
  -format UDZO \
  "$dmg_path"

if [[ -n "$signing_identity" ]]; then
  codesign --force --timestamp --sign "$signing_identity" "$dmg_path"
  codesign --verify --verbose=2 "$dmg_path"
fi

if [[ -n "$notary_profile" ]]; then
  if [[ -z "$signing_identity" ]]; then
    echo "MACOS_NOTARY_KEYCHAIN_PROFILE requires MACOS_SIGNING_IDENTITY" >&2
    exit 1
  fi
  xcrun notarytool submit "$dmg_path" --keychain-profile "$notary_profile" --wait
  xcrun stapler staple "$dmg_path"
  xcrun stapler validate "$dmg_path"
  spctl --assess --type open --context context:primary-signature --verbose=4 "$dmg_path"

  final_dmg="$output_dir/Tokdash-Companion-$version-macos-universal.dmg"
  mv "$dmg_path" "$final_dmg"
  dmg_path="$final_dmg"
fi

hdiutil verify "$dmg_path"

checksum_path="$output_dir/SHA256SUMS-macos.txt"
checksum="$(shasum -a 256 "$dmg_path" | awk '{print $1}')"
printf '%s *%s\n' "$checksum" "$(basename "$dmg_path")" > "$checksum_path"

echo "macOS companion artifact: $dmg_path"
echo "Architectures: $architectures"
if [[ -z "$signing_identity" ]]; then
  echo "WARNING: artifact is unsigned. Gatekeeper will warn users; publish only as an explicitly labelled unsigned preview." >&2
fi
