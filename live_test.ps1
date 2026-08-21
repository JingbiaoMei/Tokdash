#requires -Version 5.1
<#
Tokdash Windows setup fix - live end-to-end test (fix/windows-setup worktree).

Run from a NORMAL (non-elevated) PowerShell. It:
  0. checks it is not elevated and no Tokdash task exists
  1. builds a throwaway test venv from the worktree (H: drive copy of the fix)
  2. proves `tokdash serve` survives a cp1252-redirected stdout (bug 3: the
     UnicodeEncodeError on the first emoji print that killed the task process)
  3. runs a full non-elevated `tokdash setup --auto` (bug 1: per-user logon
     trigger registers without UAC; bug 2: port 55423 is held by wslrelay/WSL,
     so setup must auto-pick a free port and the service must really come up)
  4. re-runs setup (re-setup path: own service keeps the port, old task stopped)
  5. doctor
  6. uninstall -y and verifies the machine is back to a clean state
  7. removes the test venv if everything passed

Nothing outside the throwaway venv and the Tokdash task/manifest is touched.
#>
$ErrorActionPreference = "Continue"
$script:failures = @()
function Check($name, $cond, $detail) {
  if ($cond) { Write-Host "  [PASS] $name" -ForegroundColor Green }
  else { Write-Host "  [FAIL] $name - $detail" -ForegroundColor Red; $script:failures += $name }
}

$worktree = "H:\Developing\Agent\Tokdash_Project\tokdash\.claude\worktrees\windows-setup-fix"
$testRoot = Join-Path $env:LOCALAPPDATA "tokdash-winfix-test"
$testVenv = Join-Path $testRoot "venv"
$py = Join-Path $testVenv "Scripts\python.exe"
$wrapper = Join-Path $worktree "tokdash"
$base = Join-Path $env:TEMP "tokdash-winfix"
New-Item -ItemType Directory -Force -Path $base | Out-Null

Write-Host "=== 0. Preconditions ==="
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Check "not elevated" (-not $isAdmin) "run this from a normal (non-elevated) PowerShell"
if ($isAdmin) { Write-Host "Aborting: elevated would mask the bug-1 failure." -ForegroundColor Yellow; exit 1 }
$existing = Get-ScheduledTask -TaskName "Tokdash" -ErrorAction SilentlyContinue
Check "no pre-existing Tokdash task" ($null -eq $existing) "a Tokdash task already exists; run `tokdash uninstall` first"
if ($null -ne $existing) { exit 1 }

Write-Host "=== 1. Test venv from the worktree ==="
if (Test-Path $py) {
  # A leftover venv from a previous round still contains the OLD worktree code
  # (pip copies the package). The task runs `pythonw -m tokdash` from the venv,
  # so force-reinstall the current worktree code before testing.
  Write-Host "  (venv already present, force-reinstalling worktree code)"
  & $py -m pip install -q --disable-pip-version-check --force-reinstall --no-deps $worktree
  Check "worktree code refreshed in venv" ($LASTEXITCODE -eq 0) "pip --force-reinstall failed"
} else {
  $sysPy = "C:\Users\H1937\AppData\Local\Programs\Python\Python313\python.exe"
  & $sysPy -m venv $testVenv
  Check "venv created" (Test-Path $py) "python -m venv failed"
  & $py -m pip install -q --disable-pip-version-check "fastapi>=0.115.0" "uvicorn[standard]>=0.32.0" "packaging>=21.0" "zstandard>=0.23"
  Check "deps installed" ($LASTEXITCODE -eq 0) "pip install of fastapi/uvicorn failed"
  & $py -m pip install -q --disable-pip-version-check $worktree
  Check "worktree code installed" ($LASTEXITCODE -eq 0) "pip install of the worktree failed"
}

Write-Host "=== 2. Bug 3: serve under cp1252-redirected stdout ==="
# Redirected stdio => Python uses the locale code page (cp1252). Pre-fix, the first
# emoji print raised UnicodeEncodeError and killed the process before uvicorn started.
$serveOut = Join-Path $base "serve-stdout.txt"
$serveErr = Join-Path $base "serve-stderr.txt"
$job = Start-Process -FilePath $py -ArgumentList "`"$wrapper`"","serve","--bind","127.0.0.1","--port","55424","--no-open" `
       -PassThru -WindowStyle Hidden -RedirectStandardOutput $serveOut -RedirectStandardError $serveErr
Start-Sleep -Seconds 4
$alive = -not $job.HasExited
$health = ""
try { $health = (Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:55424/health").Content } catch { $health = "unreachable" }
Stop-Process -Id $job.Id -Force -ErrorAction SilentlyContinue
$serveErrPreview = Get-Content $serveErr -TotalCount 3 -ErrorAction SilentlyContinue | Out-String
Check "serve process survived the banner print" $alive ("process exited early (pre-fix behavior); stderr: " + $serveErrPreview)
Check "serve answers /health on 55424" ($health -match '"service":"tokdash"') "health: $health"

Write-Host "=== 3. Bug 1 + 2: full non-elevated setup (default port 55423 is held by WSL relay) ==="
& $py $wrapper setup --auto --json > (Join-Path $base "setup1.json") 2>&1
$rc1 = $LASTEXITCODE
$j1 = $null
try { $j1 = Get-Content (Join-Path $base "setup1.json") -Raw | ConvertFrom-Json } catch {}
$setup1Output = Get-Content (Join-Path $base "setup1.json") -Raw
Check "setup exit code 0" ($rc1 -eq 0) ("rc=$rc1 ; output: " + $setup1Output)
Check "task registered" ($j1.service.enabled -eq $true) "service block: $($j1.service | ConvertTo-Json -Compress)"
Check "readiness ok (port really serves the NEW service)" ($j1.readiness.ok -eq $true) "readiness: $($j1.readiness | ConvertTo-Json -Compress)"
$picked = $j1.port
Check "auto-picked a free port (not the WSL-held 55423)" ($picked -ne 55423) "port=$picked"
$xml = Get-Content "$env:LOCALAPPDATA\Tokdash\Tokdash.xml" -Raw
Check "task XML has per-user LogonTrigger UserId" ($xml -match '<UserId>') "the trigger is still any-user (would need elevation)"
Start-Sleep -Seconds 2
$taskInfo = Get-ScheduledTask -TaskName "Tokdash" -ErrorAction SilentlyContinue
$listener = Get-NetTCPConnection -LocalPort $picked -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
Check "task state is Running" ($taskInfo.State -eq "Running") "state: $($taskInfo.State)"
$listenerProc = if ($listener) { (Get-Process -Id $listener.OwningProcess).ProcessName } else { "none" }
Check "port $picked held by pythonw (our service, not wslrelay)" ($listenerProc -match "^pythonw") "holder: $listenerProc"

Write-Host "=== 4. Re-setup: own service keeps the port ==="
& $py $wrapper setup --auto --json > (Join-Path $base "setup2.json") 2>&1
$rc2 = $LASTEXITCODE
$j2 = $null
try { $j2 = Get-Content (Join-Path $base "setup2.json") -Raw | ConvertFrom-Json } catch {}
Check "re-setup exit code 0" ($rc2 -eq 0) "rc=$rc2"
Check "re-setup kept port $picked" ($j2.port -eq $picked) "port=$($j2.port)"
Check "re-setup readiness ok" ($j2.readiness.ok -eq $true) "readiness: $($j2.readiness | ConvertTo-Json -Compress)"
$notes2 = [string]::Join('|', @($j2.notes))
$notesDetail = [string]::Join(' | ', @($j2.notes))
Check 'note says port already serves Tokdash' ($notes2 -match 'already serves Tokdash') ("notes: " + $notesDetail)
# The re-setup readiness above can pass on a STALE instance: schtasks /End is only a
# request, and the old pythonw can keep the port while the replacement dies on
# "address in use" (uvicorn startup-failure exit 3). Verify the task itself is
# really serving now, before doctor.
Start-Sleep -Seconds 2
$taskInfo2 = Get-ScheduledTask -TaskName "Tokdash" -ErrorAction SilentlyContinue
$listener2 = Get-NetTCPConnection -LocalPort $picked -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
Check "re-setup: task state is Running" ($taskInfo2.State -eq "Running") "state: $($taskInfo2.State)"
$listenerProc2 = if ($listener2) { (Get-Process -Id $listener2.OwningProcess).ProcessName } else { "none" }
Check "re-setup: port $picked held by pythonw" ($listenerProc2 -match "^pythonw") "holder: $listenerProc2"

Write-Host "=== 5. doctor ==="
& $py $wrapper doctor > (Join-Path $base "doctor.txt") 2>&1
$rcD = $LASTEXITCODE
$doctorOutput = Get-Content (Join-Path $base "doctor.txt") -Raw
Check "doctor exit code 0" ($rcD -eq 0) $doctorOutput

Write-Host "=== 6. Uninstall and verify clean state ==="
& $py $wrapper uninstall -y --json > (Join-Path $base "uninstall.json") 2>&1
$rcU = $LASTEXITCODE
$taskAfter = Get-ScheduledTask -TaskName "Tokdash" -ErrorAction SilentlyContinue
$manifestAfter = Test-Path "$env:USERPROFILE\.tokdash\install.json"
$uninstallOutput = Get-Content (Join-Path $base "uninstall.json") -Raw
Check "uninstall exit code 0" ($rcU -eq 0) ("rc=$rcU ; " + $uninstallOutput)
Check "task removed" ($null -eq $taskAfter) "Tokdash task still registered"
Check "manifest removed" (-not $manifestAfter) "install.json still present"
Check "usage data kept" (Test-Path "$env:USERPROFILE\.tokdash\usage.sqlite3") "usage.sqlite3 missing"

Write-Host "=== 7. Cleanup ==="
if ($script:failures.Count -eq 0) {
  Remove-Item $testRoot -Recurse -Force -ErrorAction SilentlyContinue
  Write-Host "  test venv removed"
} else {
  Write-Host "  keeping $testRoot for debugging" -ForegroundColor Yellow
}

Write-Host ""
if ($script:failures.Count -eq 0) {
  Write-Host "ALL CHECKS PASSED" -ForegroundColor Green
  exit 0
} else {
  $failureSummary = $script:failures -join ", "
  Write-Host "FAILURES: $failureSummary" -ForegroundColor Red
  $taskXmlPath = Join-Path $env:LOCALAPPDATA 'Tokdash\Tokdash.xml'
  Write-Host "Debug artifacts: $base ; task XML: $taskXmlPath"
  exit 1
}
