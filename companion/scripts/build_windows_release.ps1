[CmdletBinding()]
param(
    [string]$OutputDirectory = "",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$companionRoot = Join-Path $repoRoot "companion"
$version = (Get-Content (Join-Path $companionRoot "VERSION") -Raw).Trim()

if ($version -notmatch '^\d+\.\d+\.\d+$') {
    throw "companion/VERSION must be MAJOR.MINOR.PATCH, got '$version'"
}

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $repoRoot "dist\companion\$version\windows"
}
$outputRoot = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null

$project = Join-Path $companionRoot "windows\TokdashCompanion\TokdashCompanion.csproj"
$solution = Join-Path $companionRoot "windows\TokdashCompanion.slnx"
$workRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("tokdash-companion-" + [guid]::NewGuid())
$portableStage = Join-Path $workRoot "portable"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList
    )
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath failed with exit code $LASTEXITCODE"
    }
}

try {
    New-Item -ItemType Directory -Path $portableStage -Force | Out-Null

    if (-not $SkipTests) {
        Invoke-Checked -FilePath dotnet -ArgumentList @(
            "test", $solution, "--configuration", "Release"
        )
    }

    Invoke-Checked -FilePath dotnet -ArgumentList @(
        "publish", $project,
        "--configuration", "Release",
        "--runtime", "win-x64",
        "--self-contained", "true",
        "--output", $portableStage,
        "-p:PublishSingleFile=true",
        "-p:IncludeNativeLibrariesForSelfExtract=true",
        "-p:PublishTrimmed=false",
        "-p:ContinuousIntegrationBuild=true",
        "-p:Deterministic=true",
        "-p:DebugType=None",
        "-p:DebugSymbols=false",
        "-p:Version=$version"
    )

    $runtimeAssets = Join-Path $portableStage "Assets"
    New-Item -ItemType Directory -Path $runtimeAssets -Force | Out-Null
    Copy-Item `
        (Join-Path $companionRoot "windows\TokdashCompanion\Assets\tray.ico") `
        (Join-Path $runtimeAssets "tray.ico") `
        -Force
    Copy-Item (Join-Path $repoRoot "LICENSE") (Join-Path $portableStage "LICENSE.txt")
    Copy-Item (Join-Path $companionRoot "docs\RELEASE.md") (Join-Path $portableStage "README.txt")
    $dotnetNotices = Join-Path (Split-Path (Get-Command dotnet).Source) "ThirdPartyNotices.txt"
    if (-not (Test-Path $dotnetNotices)) {
        throw ".NET ThirdPartyNotices.txt was not found at $dotnetNotices"
    }
    Copy-Item $dotnetNotices (Join-Path $portableStage "THIRD-PARTY-NOTICES.txt")

    $exePath = Join-Path $portableStage "TokdashCompanion.exe"
    if (-not (Test-Path $exePath)) {
        throw "Self-contained executable was not produced at $exePath"
    }

    $zipPath = Join-Path $outputRoot "Tokdash-Companion-$version-windows-x64-unsigned.zip"
    if (Test-Path $zipPath) {
        Remove-Item $zipPath -Force
    }
    Compress-Archive -Path (Join-Path $portableStage "*") -DestinationPath $zipPath -CompressionLevel Optimal

    $checksumPath = Join-Path $outputRoot "SHA256SUMS-windows.txt"
    @($zipPath) | ForEach-Object {
        $hash = Get-FileHash -Algorithm SHA256 $_
        "$($hash.Hash.ToLowerInvariant()) *$([System.IO.Path]::GetFileName($_))"
    } | Set-Content -Path $checksumPath -Encoding ascii

    Write-Host "Windows companion unsigned release artifact:"
    Write-Host "  $zipPath"
    Write-Host "  $checksumPath"
    Write-Warning "Artifact is unsigned. Windows will report an unknown publisher; publish only as an explicitly labelled unsigned preview."
}
finally {
    if (Test-Path $workRoot) {
        Remove-Item $workRoot -Recurse -Force
    }
}
