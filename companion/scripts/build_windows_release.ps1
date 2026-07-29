[CmdletBinding()]
param(
    [string]$OutputDirectory = "",
    [string]$PackagePublisher = "CN=Tokdash Development",
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
$msixStage = Join-Path $workRoot "msix"
$unpackStage = Join-Path $workRoot "msix-unpacked"

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

function Invoke-CheckedQuiet {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList
    )
    $output = & $FilePath @ArgumentList 2>&1
    if ($LASTEXITCODE -ne 0) {
        $output | Select-Object -Last 80 | ForEach-Object { Write-Host $_ }
        throw "$FilePath failed with exit code $LASTEXITCODE"
    }
}

function Find-WindowsSdkTool {
    param([Parameter(Mandatory = $true)][string]$Name)
    $kitsRoot = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    $tool = Get-ChildItem -Path $kitsRoot -Filter $Name -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match '\\x64\\' } |
        Sort-Object FullName -Descending |
        Select-Object -First 1
    if (-not $tool) {
        throw "$Name was not found under $kitsRoot. Install the Windows SDK."
    }
    return $tool.FullName
}

try {
    New-Item -ItemType Directory -Path $portableStage, $msixStage, $unpackStage -Force | Out-Null

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

    $zipPath = Join-Path $outputRoot "TokdashCompanion-$version-windows-x64-portable-unsigned.zip"
    if (Test-Path $zipPath) {
        Remove-Item $zipPath -Force
    }
    Compress-Archive -Path (Join-Path $portableStage "*") -DestinationPath $zipPath -CompressionLevel Optimal

    Invoke-Checked -FilePath dotnet -ArgumentList @(
        "publish", $project,
        "--configuration", "Release",
        "--runtime", "win-x64",
        "--self-contained", "true",
        "--output", $msixStage,
        "-p:PublishSingleFile=false",
        "-p:DebugType=None",
        "-p:DebugSymbols=false",
        "-p:Version=$version"
    )

    $packagingRoot = Join-Path $companionRoot "windows\packaging"
    $msixAssets = Join-Path $msixStage "Assets"
    New-Item -ItemType Directory -Path $msixAssets -Force | Out-Null
    Copy-Item `
        (Join-Path $companionRoot "windows\TokdashCompanion\Assets\tray.ico") `
        (Join-Path $msixAssets "tray.ico") `
        -Force
    Copy-Item (Join-Path $packagingRoot "Assets\*") $msixAssets -Recurse -Force

    $packageVersion = "$version.0"
    $escapedPublisher = [System.Security.SecurityElement]::Escape($PackagePublisher)
    $manifest = (Get-Content (Join-Path $packagingRoot "AppxManifest.xml.in") -Raw).
        Replace("@@VERSION@@", $packageVersion).
        Replace("@@PUBLISHER@@", $escapedPublisher)
    Set-Content -Path (Join-Path $msixStage "AppxManifest.xml") -Value $manifest -Encoding utf8

    $makeAppx = Find-WindowsSdkTool "makeappx.exe"
    $msixPath = Join-Path $outputRoot "TokdashCompanion-$version-windows-x64-unsigned.msix"
    Invoke-CheckedQuiet -FilePath $makeAppx -ArgumentList @(
        "pack", "/d", $msixStage, "/p", $msixPath, "/o"
    )
    Invoke-CheckedQuiet -FilePath $makeAppx -ArgumentList @(
        "unpack", "/p", $msixPath, "/d", $unpackStage, "/o"
    )

    [xml]$packedManifest = Get-Content (Join-Path $unpackStage "AppxManifest.xml") -Raw
    if ($packedManifest.Package.Identity.Version -ne $packageVersion) {
        throw "Packed MSIX version does not match $packageVersion"
    }

    $checksumPath = Join-Path $outputRoot "SHA256SUMS-windows.txt"
    @($zipPath, $msixPath) | ForEach-Object {
        $hash = Get-FileHash -Algorithm SHA256 $_
        "$($hash.Hash.ToLowerInvariant()) *$([System.IO.Path]::GetFileName($_))"
    } | Set-Content -Path $checksumPath -Encoding ascii

    Write-Host "Windows companion artifacts:"
    Write-Host "  $zipPath"
    Write-Host "  $msixPath"
    Write-Host "  $checksumPath"
    Write-Warning "Artifacts are unsigned. The portable ZIP may trigger SmartScreen; the MSIX cannot be installed normally until signed."
}
finally {
    if (Test-Path $workRoot) {
        Remove-Item $workRoot -Recurse -Force
    }
}
