#!/usr/bin/env bash
# Run from the project root. Uses an isolated Mac checkout, never the installed app.
set -euo pipefail
MAC_HOST=${MAC_HOST:?Set MAC_HOST to your SSH-enabled Mac}
MAC_WORK=${MAC_WORK:-/tmp/tokdash-companion-review}
OUT=${OUT:-output/companion-native}
mkdir -p "$OUT"
[[ "$MAC_WORK" =~ ^/[a-zA-Z0-9_/-]+$ ]] || exit 2
ssh -o BatchMode=yes -o ConnectTimeout=10 "$MAC_HOST" "mkdir -p '$MAC_WORK'"
tar -cz --exclude=bin --exclude=obj --exclude=build --exclude=DerivedData \
    companion/macos companion/contract | \
    ssh "$MAC_HOST" "tar -xz -C '$MAC_WORK'"
ssh "$MAC_HOST" "cd '$MAC_WORK'; mkdir -p render; TEST_RUNNER_TOKDASH_RENDER_DIR='$MAC_WORK/render' caffeinate -is xcodebuild test \
    -project companion/macos/TokdashCompanion.xcodeproj \
    -scheme TokdashCompanion -configuration Release \
    CODE_SIGNING_ALLOWED=NO ENABLE_TESTABILITY=YES SWIFT_TREAT_WARNINGS_AS_ERRORS=YES GCC_TREAT_WARNINGS_AS_ERRORS=YES -destination 'platform=macOS' \
    -derivedDataPath dd-ci > test.log 2>&1; result=\$?; tail -n 35 test.log; exit \$result" \
    | tee "$OUT/macos-test.log"

scp -q "$MAC_HOST:$MAC_WORK/render/mac-settings.png" "$OUT/mac-settings.png"
