<#
.SYNOPSIS
    Build the Tokdash Companion MSIX package for Microsoft Store submission.

.DESCRIPTION
    Two modes, chosen by whether the Partner Center identity is supplied:

    Store mode  (-IdentityName / -Publisher / -PublisherDisplayName, or the matching
                TOKDASH_MSIX_* environment variables)
                Produces an UNSIGNED .msix for upload to Partner Center. The Store
                re-signs it during certification - do not sign it yourself, and do not
                expect it to install locally.

    Test mode   (no identity supplied)
                Substitutes placeholder identity values and, with -SelfSignForTesting,
                signs with a throwaway self-signed certificate so the package can be
                installed on this machine to verify packaged behavior. The result is
                for local verification ONLY and must never be published.

    Unlike the portable builder this publishes UNPACKED (PublishSingleFile=false). MSIX
    is already a container; single-file would add a decompress-to-temp step on every
    launch and defeat the Store's incremental update blocks.

.EXAMPLE
    powershell -NoProfile -File companion/scripts/build_windows_msix.ps1 -SelfSignForTesting

.EXAMPLE
    powershell -NoProfile -File companion/scripts/build_windows_msix.ps1 `
        -IdentityName "12345Tokdash.TokdashCompanion" `
        -Publisher "CN=01234567-89ab-cdef-0123-456789abcdef" `
        -PublisherDisplayName "Tokdash"
#>
[CmdletBinding()]
param(
    [string]$OutputDirectory = "",
    [string]$IdentityName = $env:TOKDASH_MSIX_IDENTITY_NAME,
    [string]$Publisher = $env:TOKDASH_MSIX_PUBLISHER,
    [string]$PublisherDisplayName = $env:TOKDASH_MSIX_PUBLISHER_DISPLAY_NAME,
    [switch]$SkipTests,
    [switch]$SelfSignForTesting
)

$ErrorActionPreference = "Stop"
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$companionRoot = Join-Path $repoRoot "companion"
$version = (Get-Content (Join-Path $companionRoot "VERSION") -Raw).Trim()

if ($version -notmatch '^\d+\.\d+\.\d+$') {
    throw "companion/VERSION must be MAJOR.MINOR.PATCH, got '$version'"
}

# The Store reserves the revision field and rejects any package that sets it.
$packageVersion = "$version.0"

# Microsoft: "The other sections must be set to an integer between 0 and 65535 (except
# for the first section, which cannot be 0)." A 0.x.y version is rejected at upload, so
# fail here rather than after a full publish-and-pack cycle.
# https://learn.microsoft.com/en-us/windows/apps/publish/publish-your-app/msix/app-package-requirements
if ([int]($version.Split('.')[0]) -eq 0) {
    throw "the Store rejects a zero major version: '$version'. companion/VERSION must start at 1.0.0 or later."
}

# Test-mode placeholders. The publisher must match the self-signed certificate subject
# exactly or Windows refuses to install the package.
$testIdentityName = "Tokdash.Companion.Test"
$testPublisher = "CN=Tokdash Companion Test Builds"
$testPublisherDisplayName = "Tokdash (test build)"

$storeMode = -not [string]::IsNullOrWhiteSpace($IdentityName)

if ($storeMode) {
    foreach ($pair in @(
        @{ Name = "Publisher"; Value = $Publisher },
        @{ Name = "PublisherDisplayName"; Value = $PublisherDisplayName }
    )) {
        if ([string]::IsNullOrWhiteSpace($pair.Value)) {
            throw "Store mode needs -$($pair.Name). Supply the whole Partner Center identity triple or none of it."
        }
    }
    if ($Publisher -notmatch '^CN=') {
        throw "-Publisher must be the Partner Center Package/Identity/Publisher value, which starts with 'CN=', got '$Publisher'"
    }
    if ($SelfSignForTesting) {
        throw "-SelfSignForTesting cannot be combined with a Store identity. The Store signs the package it ingests; a package signed with any other certificate is rejected at upload."
    }
    $flavor = "store"
} else {
    $IdentityName = $testIdentityName
    $Publisher = $testPublisher
    $PublisherDisplayName = $testPublisherDisplayName
    $flavor = "test"
}

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $repoRoot "dist\companion\$version\windows"
}
$outputRoot = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null

$project = Join-Path $companionRoot "windows\TokdashCompanion\TokdashCompanion.csproj"
$solution = Join-Path $companionRoot "windows\TokdashCompanion.slnx"
$packagingRoot = Join-Path $companionRoot "windows\packaging"
$workRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("tokdash-companion-msix-" + [guid]::NewGuid())
$packageStage = Join-Path $workRoot "package"

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

<#
    Resolve a Windows SDK tool. Prefers the newest SDK that has it, and prefers the x64
    build so this works from an Arm64 host too.
#>
function Resolve-SdkTool {
    param([Parameter(Mandatory = $true)][string]$Name)

    $roots = @(
        "${env:ProgramFiles(x86)}\Windows Kits\10\bin",
        "$env:ProgramFiles\Windows Kits\10\bin"
    ) | Where-Object { $_ -and (Test-Path $_) }

    $candidates = foreach ($root in $roots) {
        Get-ChildItem -Path $root -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '^10\.' } |
            Sort-Object { [version]($_.Name) } -Descending |
            ForEach-Object {
                foreach ($arch in @("x64", "x86")) {
                    $candidate = Join-Path $_.FullName "$arch\$Name"
                    if (Test-Path $candidate) { $candidate }
                }
            }
    }

    $resolved = $candidates | Select-Object -First 1
    if (-not $resolved) {
        throw "$Name was not found in any Windows 10/11 SDK. Install the Windows SDK (it ships with the 'MSIX Packaging Tools' component)."
    }
    return $resolved
}

try {
    New-Item -ItemType Directory -Path $packageStage -Force | Out-Null

    if (-not $SkipTests) {
        Invoke-Checked -FilePath dotnet -ArgumentList @(
            "test", $solution, "--configuration", "Release"
        )
    }

    # Unpacked publish: MSIX supplies the container, so single-file only costs startup time.
    Invoke-Checked -FilePath dotnet -ArgumentList @(
        "publish", $project,
        "--configuration", "Release",
        "--runtime", "win-x64",
        "--self-contained", "true",
        "--output", $packageStage,
        "-p:PublishSingleFile=false",
        "-p:PublishTrimmed=false",
        "-p:ContinuousIntegrationBuild=true",
        "-p:Deterministic=true",
        "-p:DebugType=None",
        "-p:DebugSymbols=false",
        "-p:Version=$version"
    )

    $exePath = Join-Path $packageStage "TokdashCompanion.exe"
    if (-not (Test-Path $exePath)) {
        throw "Publish did not produce $exePath"
    }

    # Symbols are not shipped: they bloat the package and every byte counts against the
    # Store's incremental update blocks.
    Get-ChildItem -Path $packageStage -Filter *.pdb -Recurse -ErrorAction SilentlyContinue |
        Remove-Item -Force

    # Tile, Store and taskbar assets referenced by the manifest live alongside the app's
    # own runtime Assets\tray.ico, which the publish step already placed here.
    $stageAssets = Join-Path $packageStage "Assets"
    New-Item -ItemType Directory -Path $stageAssets -Force | Out-Null
    $manifestAssets = @(
        "StoreLogo.png",
        "Square150x150Logo.png",
        "Square44x44Logo.png",
        "Square44x44Logo.targetsize-44_altform-unplated.png"
    )
    foreach ($asset in $manifestAssets) {
        $source = Join-Path $packagingRoot "Assets\$asset"
        if (-not (Test-Path $source)) {
            throw "Manifest asset is missing: $source"
        }
        Copy-Item $source (Join-Path $stageAssets $asset) -Force
    }

    Copy-Item (Join-Path $repoRoot "LICENSE") (Join-Path $packageStage "LICENSE.txt") -Force
    $dotnetNotices = Join-Path (Split-Path (Get-Command dotnet).Source) "ThirdPartyNotices.txt"
    if (-not (Test-Path $dotnetNotices)) {
        throw ".NET ThirdPartyNotices.txt was not found at $dotnetNotices"
    }
    Copy-Item $dotnetNotices (Join-Path $packageStage "THIRD-PARTY-NOTICES.txt") -Force

    # Substitute the manifest template. Every @@TOKEN@@ must be consumed - an unreplaced
    # token would otherwise reach Partner Center as a literal and fail at upload.
    $manifestTemplate = Get-Content (Join-Path $packagingRoot "AppxManifest.xml.in") -Raw
    $manifest = $manifestTemplate.
        Replace("@@IDENTITY_NAME@@", $IdentityName).
        Replace("@@PUBLISHER@@", $Publisher).
        Replace("@@PUBLISHER_DISPLAY_NAME@@", $PublisherDisplayName).
        Replace("@@VERSION@@", $packageVersion)
    if ($manifest -match '@@[A-Z_]+@@') {
        throw "AppxManifest template still contains unreplaced tokens: $($Matches[0])"
    }
    $manifestPath = Join-Path $packageStage "AppxManifest.xml"
    # MakeAppx wants UTF-8 without a BOM.
    [System.IO.File]::WriteAllText($manifestPath, $manifest, (New-Object System.Text.UTF8Encoding($false)))

    $makeappx = Resolve-SdkTool -Name "makeappx.exe"
    $msixName = "Tokdash-Companion-$version-windows-x64-$flavor.msix"
    $msixPath = Join-Path $outputRoot $msixName
    if (Test-Path $msixPath) { Remove-Item $msixPath -Force }

    Invoke-Checked -FilePath $makeappx -ArgumentList @(
        "pack", "/d", $packageStage, "/p", $msixPath, "/overwrite"
    )

    if ($SelfSignForTesting) {
        $signtool = Resolve-SdkTool -Name "signtool.exe"
        $pfxPath = Join-Path $workRoot "test-signing.pfx"
        $cerPath = Join-Path $outputRoot "Tokdash-Companion-test-signing.cer"
        # Password is irrelevant: the key exists for the length of this build and the
        # certificate is only ever trusted manually on a developer machine.
        $password = ConvertTo-SecureString -String "tokdash-test" -Force -AsPlainText

        $cert = New-SelfSignedCertificate `
            -Type Custom `
            -Subject $testPublisher `
            -KeyUsage DigitalSignature `
            -FriendlyName "Tokdash Companion test signing" `
            -CertStoreLocation "Cert:\CurrentUser\My" `
            -TextExtension @("2.5.29.37={text}1.3.6.1.5.5.7.3.3", "2.5.29.19={text}")

        try {
            Export-PfxCertificate -Cert $cert -FilePath $pfxPath -Password $password | Out-Null
            Export-Certificate -Cert $cert -FilePath $cerPath | Out-Null
            Invoke-Checked -FilePath $signtool -ArgumentList @(
                "sign", "/fd", "SHA256", "/a", "/f", $pfxPath,
                "/p", "tokdash-test", $msixPath
            )
        }
        finally {
            Remove-Item "Cert:\CurrentUser\My\$($cert.Thumbprint)" -Force -ErrorAction SilentlyContinue
        }
    }

    # Flavor-specific: the store and test packages share an output directory, and a
    # shared filename let whichever built second silently clobber the other's record.
    $checksumPath = Join-Path $outputRoot "SHA256SUMS-windows-msix-$flavor.txt"
    $hash = Get-FileHash -Algorithm SHA256 $msixPath
    "$($hash.Hash.ToLowerInvariant()) *$msixName" | Set-Content -Path $checksumPath -Encoding ascii

    Write-Host ""
    Write-Host "MSIX package:"
    Write-Host "  $msixPath"
    Write-Host "  $checksumPath"
    Write-Host "  identity : $IdentityName"
    Write-Host "  publisher: $Publisher"
    Write-Host "  version  : $packageVersion"

    if ($storeMode) {
        Write-Host ""
        Write-Host "Store mode: upload this package UNSIGNED to Partner Center."
        Write-Host "Microsoft re-signs it during certification; signing it yourself will fail the upload."
    } else {
        Write-Host ""
        Write-Warning "TEST BUILD - placeholder identity, not publishable."
        if ($SelfSignForTesting) {
            Write-Host "To install locally (elevated, once per machine for the certificate):"
            Write-Host "  Import-Certificate -FilePath '$cerPath' -CertStoreLocation Cert:\LocalMachine\TrustedPeople"
            Write-Host "  Add-AppxPackage -Path '$msixPath'"
            Write-Host "To remove:"
            Write-Host "  Get-AppxPackage *TokdashCompanion* | Remove-AppxPackage"
        } else {
            Write-Host "Pass -SelfSignForTesting to produce a package that can be installed locally."
        }
    }
}
finally {
    if (Test-Path $workRoot) {
        Remove-Item $workRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
