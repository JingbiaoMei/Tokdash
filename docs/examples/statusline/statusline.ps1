# Tokdash -> Claude Code statusline (PowerShell / native Windows starter)
# -------------------------------------------------------------------------------
# Renders one line, e.g.:   [Claude Sonnet 4.6] 📁 myproject | 📊 12.3M ($4.56) today
#
# Requires: PowerShell 5.1+ (Invoke-RestMethod / ConvertFrom-Json are built in --
# no curl or jq needed). Fails silently (drops the 📊 segment) if tokdash isn't
# running. This script only ever issues GET requests, so the write-protection
# gate never blocks it.
#
# Install:
#   1. copy statusline.ps1 to %USERPROFILE%\.claude\scripts\statusline.ps1
#   2. add the Windows "statusLine" block from this folder's README.md to
#      %USERPROFILE%\.claude\settings.json
#
# If you changed TOKDASH_HOST / TOKDASH_PORT, point the script at your endpoint,
# e.g. in your PowerShell profile:
#   $env:TOKDASH_URL = "http://127.0.0.1:55423"
#
# This is the PowerShell counterpart of statusline-minimal.sh -- keep the two in
# sync if you change one.

$TokdashUrl = if ($env:TOKDASH_URL) { $env:TOKDASH_URL } else { "http://127.0.0.1:55423" }
$Period = if ($env:TOKDASH_STATUSLINE_PERIOD) { $env:TOKDASH_STATUSLINE_PERIOD } else { "today" }   # today | 3days | week | 14days | month | year | all

# Claude Code feeds the statusline a JSON blob on stdin.
$stdinText = [Console]::In.ReadToEnd()
$Model = "?"
$Dir = ""
try {
    $inputJson = $stdinText | ConvertFrom-Json -ErrorAction Stop
    if ($inputJson.model.display_name) { $Model = $inputJson.model.display_name }
    if ($inputJson.workspace.current_dir) { $Dir = [string]$inputJson.workspace.current_dir }
} catch {
    # Missing/malformed stdin (e.g. a manual test run) -- keep the "?" fallback,
    # same as the bash template's `jq -r '.model.display_name // "?"'`.
}
$DirName = if ($Dir) { Split-Path -Leaf $Dir } else { "" }

# Compact a raw token count to B / M / k (mirrors fmt_tok() in statusline-minimal.sh).
# Formats with InvariantCulture so the separator is always "." regardless of the
# machine's regional settings (some locales default to "," for decimals).
function Format-TokenCount {
    param([double]$Count)
    $inv = [System.Globalization.CultureInfo]::InvariantCulture
    if ($Count -ge 1000000000) {
        return ($Count / 1000000000).ToString("F1", $inv) + "B"
    } elseif ($Count -ge 1000000) {
        return ($Count / 1000000).ToString("F1", $inv) + "M"
    } elseif ($Count -ge 1000) {
        $rounded = [Math]::Round($Count / 1000, 0, [MidpointRounding]::AwayFromZero)
        return "$([int64]$rounded)k"
    } else {
        return "$([int64]$Count)"
    }
}

# Fetch the period totals. -TimeoutSec 1 keeps the bar responsive if tokdash is
# mid-restart -- the same purpose as the bash template's `curl -m 1`.
$TokdashSegment = ""
try {
    $usage = Invoke-RestMethod -Uri "$TokdashUrl/api/usage?period=$Period" -TimeoutSec 1 -ErrorAction Stop
    $tokens = if ($null -ne $usage.total_tokens) { [double]$usage.total_tokens } else { 0 }
    $cost = if ($null -ne $usage.total_cost) { [double]$usage.total_cost } else { 0 }
    if ($tokens -ne 0) {
        $costText = $cost.ToString("F2", [System.Globalization.CultureInfo]::InvariantCulture)
        $TokdashSegment = " | 📊 $(Format-TokenCount $tokens) (`$$costText) $Period"
    }
} catch {
    # Tokdash isn't running / unreachable -- fail silently, same as the bash
    # template's empty-JSON check.
}

Write-Output "[$Model] 📁 $DirName$TokdashSegment"
