# Windows native agent: verify the fix/windows-setup changes

Run on the Windows host as user `HOWARD\H1937` in a **normal (non-elevated)**
PowerShell. Everything needed is in this worktree; nothing else on the machine
is in scope.

## Context (already diagnosed — do not re-debug)

v2.0.0 `tokdash setup` fails on this machine for three independent reasons:

1. `winsched.py` rendered a bare `<LogonTrigger>` ("any user's logon") — a
   system-wide event that Task Scheduler refuses to register for a standard
   user. Symptom: `schtasks /Create ... /XML` -> `ERROR: Access is denied`
   non-elevated (proven by element bisection; TimeTrigger works,
   LogonTrigger/BootTrigger denied).
2. Port 55423 is bound by `wslrelay.exe` — WSL2 mirrored networking mirrors a
   WSL distro's localhost to the Windows host, and that distro runs its own
   Tokdash v1.9.0 service there. A Windows service can never bind 55423, and
   the old readiness check (port answers with tokdash fingerprint) was
   satisfiable by the relayed WSL service -> false success or "nothing
   answered".
3. `tokdash serve` printed an emoji banner on its first line; under
   `pythonw.exe` (no console) or a cp1252 pipe this raised
   `UnicodeEncodeError`/`AttributeError` and killed the process before uvicorn
   started.

Fixes (uncommitted in this worktree's working tree, branch
`fix/windows-setup`, based on v2.0.0 `58f9a36`):

- `src/tokdash/onboard/winsched.py`: LogonTrigger now carries
  `<UserId>DOMAIN\USER</UserId>` (per-user trigger; registers non-elevated).
- `src/tokdash/cli.py`: `_harden_windows_stdio()` at CLI entry (devnull for
  missing streams, `errors="replace"` for present ones, Windows only).
- `src/tokdash/onboard/plan.py` + `detect.py`: busy port serving a *foreign*
  Tokdash (no marked service of ours) blocks or auto-picks with `--auto`,
  naming the holder (`wslrelay.exe` called out explicitly); our own marked
  service still keeps its port.
- `src/tokdash/onboard/engine.py`: stops a running previous task before
  re-registration; readiness additionally requires the task itself to be
  Running; Access-denied gets an elevated-shell hint.

Unit tests are green on WSL: `PYTHONPATH=src python3 -m pytest
tests/test_winsched.py tests/test_onboard.py tests/test_cli_serve.py`
(216 passed). If you want to re-run them here, install the deps in a venv
first; they are OS-agnostic (schtasks is monkeypatched).

## Environment facts

- Python: `C:\Users\H1937\AppData\Local\Programs\Python\Python313\python.exe`
- No pipx on this machine. Windows user: `HOWARD\H1937`. Win11 24H2.
- Do NOT stop or "fix" the WSL relay on 55423 — it belongs to the WSL
  install and is expected to stay there.
- The task registered by setup is named `Tokdash` and lives in
  `%LOCALAPPDATA%\Tokdash\Tokdash.xml` (UTF-16).

## Test procedure

Fast path — run the staged end-to-end script:

    powershell -ExecutionPolicy Bypass -File H:\Developing\Agent\Tokdash_Project\tokdash\.claude\worktrees\windows-setup-fix\scripts\live_test.ps1

It builds a throwaway venv (`%LOCALAPPDATA%\tokdash-winfix-test\venv`),
installs the worktree code, then checks (each printed as [PASS]/[FAIL]):

1. `serve --port 55424` under cp1252-redirected stdout survives and answers
   `/health` (bug 3).
2. Non-elevated `setup --auto --json` exits 0: task registers WITHOUT UAC,
   readiness ok, port auto-picked away from 55423 (bugs 1+2).
3. `%LOCALAPPDATA%\Tokdash\Tokdash.xml` contains `<UserId>` (bug 1 evidence).
4. `Get-ScheduledTaskInfo Tokdash` state is Running and the picked port is
   held by `pythonw`, not `wslrelay`.
5. A second `setup --auto` keeps the same port (re-setup path) and stays green.
6. `doctor` exits 0.
7. `uninstall -y --json` removes the task + `%USERPROFILE%\.tokdash\install.json`
   and KEEPS `%USERPROFILE%\.tokdash\usage.sqlite3`.
8. Test venv is removed only if every check passed.

If anything fails: artifacts are in `%TEMP%\tokdash-winfix` (setup1.json,
setup2.json, doctor.txt, uninstall.json, serve-stdout/stderr.txt); the test
venv is kept at `%LOCALAPPDATA%\tokdash-winfix-test`.

Manual fallback (same assertions, step by step):

    $wt = "H:\Developing\Agent\Tokdash_Project\tokdash\.claude\worktrees\windows-setup-fix"
    $py313 = "C:\Users\H1937\AppData\Local\Programs\Python\Python313\python.exe"
    $venv = "$env:LOCALAPPDATA\tokdash-winfix-test\venv"
    & $py313 -m venv $venv
    & "$venv\Scripts\python.exe" -m pip install -q "fastapi>=0.115.0" "uvicorn[standard]>=0.32.0" "packaging>=21.0" "zstandard>=0.23" $wt
    $vpy = "$venv\Scripts\python.exe"

    # bug 3: redirected stdio (cp1252) must not kill serve
    Start-Process $vpy -ArgumentList "`"$wt\tokdash`"","serve","--bind","127.0.0.1","--port","55424","--no-open" -WindowStyle Hidden -RedirectStandardError $env:TEMP\serve-err.txt
    Start-Sleep 4
    (Invoke-WebRequest -UseBasicParsing http://127.0.0.1:55424/health).Content   # expect tokdash fingerprint
    # kill the serve process (Get-Process python | Where Path like *tokdash-winfix-test*), then:

    # bugs 1+2: full setup, non-elevated, default port (WSL-held)
    & $vpy "$wt\tokdash" setup --auto --json    # expect exit 0, port != 55423, readiness.ok true
    Get-Content "$env:LOCALAPPDATA\Tokdash\Tokdash.xml" | Select-String "UserId"
    Get-ScheduledTaskInfo -TaskName Tokdash      # expect State: Running
    Get-NetTCPConnection -LocalPort <picked> -State Listen   # owner should be pythonw.exe
    & $vpy "$wt\tokdash" doctor                  # expect exit 0
    & $vpy "$wt\tokdash" uninstall -y --json     # expect task + manifest gone, usage.sqlite3 kept
    Remove-Item "$env:LOCALAPPDATA\tokdash-winfix-test" -Recurse -Force

## Expected final state after a green run

- No `Tokdash` scheduled task, no `%LOCALAPPDATA%\Tokdash`, no
  `%USERPROFILE%\.tokdash\install.json`, no test venv.
- `%USERPROFILE%\.tokdash\usage.sqlite3` still present.
- WSL relay on 55423 untouched.

## Round 2 (after first live run)

First live run: everything green except re-setup (it adopted the WSL-relayed
Tokdash on 55423 as "its own" endpoint, killed the healthy 55424 instance, and
the new task on 55423 died on the relayed port; doctor then correctly reported
the service inactive).

Fixed (already in this worktree's working tree):

- `plan.py`: a re-setup now defaults to the **manifest-recorded port** of the
  previous install (explicit `--port` still wins), and "port is ours" now
  requires the manifest to have recorded *exactly this* port for *this* service
  type **and** a marked service to exist — a marked task alone can no longer
  make a foreign/relayed Tokdash on another port count as our endpoint.
- `engine.py`: readiness additionally verifies the port's *holder process* is
  the service runtime (`pythonw.exe`/`python.exe`) via `netstat`+`tasklist`;
  a relayed/foreign holder fails readiness fast with an explicit error instead
  of the 8s timeout.

Re-run the same script (`live_test.ps1`, unchanged): step 4 (re-setup keeps the
picked port) and step 5 (doctor green) are the two checks that were red.

## Round 3 (after second live run)

Second live run: all green except re-setup, which picked 55425 instead of
keeping 55424. Cause: `cli.py` eagerly resolved an implicit setup port to
55423 (`args.port = _default_port()` when `args.port is None`), so the planner
never saw `port=None` and could not adopt the manifest-recorded 55424.

Fixed (already in this worktree's working tree): the resolution now runs only
when `TOKDASH_PORT` is actually present, so an implicit port reaches the
planner as None (explicit `--port` and `TOKDASH_PORT` still win over the
manifest; a malformed `TOKDASH_PORT` still rejects setup).

Re-run the same script: step 4 (re-setup keeps the picked port + the
"already serves Tokdash" note) is the only red check to verify.

## Round 4 (after third live run)

Third live run: all green except `doctor`. Focused query: the task briefly
reports Running, then Ready; port 55424 becomes free; **Last Result = 3**.

Root cause (now proven, not a guess):

1. **Last Result 3 is uvicorn's `STARTUP_FAILURE`, not a Task Scheduler path
   error.** `uvicorn/config.py` defines `STARTUP_FAILURE = 3`; `server.py`
   calls `sys.exit(STARTUP_FAILURE)` when `create_server` raises `OSError`
   (i.e. the port is already bound). The *new* task's pythonw started, failed
   to bind 55424, and exited 3.
2. **Why the port was still bound: `schtasks /End` is a request, not a kill.**
   A `pythonw.exe` instance (GUI subsystem, no console for the stop to reach)
   outlived the `/End` + re-registration + `/Run` sequence (~1-2 s). The stale
   setup1 instance kept serving 55424.
3. **Why setup2 still reported green:** readiness passed on the stale
   responder — the port answered with Tokdash's fingerprint, the task state
   transiently read "Running", and the holder check matched *any* pythonw.
   Doctor then correctly reported the port held while the task was inactive.

Fix (in this worktree's working tree):

- `engine.py`: new `_wait_for_port_release()` — after `end_task()`, setup
  blocks until the port actually stops answering (precondition of
  re-registration + `/Run`). If the manifest (previous install) recorded this
  exact port for our winsched task, a lingering holder running our own runtime
  image (pythonw/python) is hard-killed via `taskkill /F` by PID. A foreign
  occupant is never touched; a port that stays busy fails setup with a named
  holder instead of a silent exit-3 task.
- `engine.py`: `_runtime_holder_names()` helper shared by readiness and the
  release gate (uses `PureWindowsPath` so it works on the POSIX test host).
- `engine.py` (doctor): when the task is inactive but the port still answers
  with Tokdash's fingerprint, the issue names the stale holder.
- `detect.py`: `port_holder(port)` returns `(pid, image name)`;
  `port_holder_process` is now a thin wrapper.
- `live_test.ps1`: after re-setup, asserts the task state is Running and the
  port is held by pythonw (would have caught the stale false-positive).
- `tests/test_winsched.py`: 6 new tests — release-gate kill/no-kill/timeout
  paths, holder-name helper, and a full re-setup regression test asserting
  `end < taskkill < create < run` ordering with a stale pythonw holding the
  port.

Unit tests: `PYTHONPATH=src python3 -m pytest tests/test_winsched.py
tests/test_onboard.py tests/test_cli_serve.py` (228 passed); full suite
1244 passed, 2 pre-existing companion-packaging failures (also fail on main).

Re-run the same script: the two new "re-setup: ..." checks and `doctor` are
the checks to watch.

## Round 4 result: ALL CHECKS PASSED (green)

Live re-run on the host (non-elevated PowerShell): every check passed, including
the two new re-setup checks (task Running, port 55424 held by pythonw) and
`doctor`. WSL relay held 55423, setup auto-picked 55424, re-setup retained it,
uninstall cleaned up and kept usage data, and the debug venv was removed by the
harness. The fix is live-verified; the branch is ready to merge.

## Git

Changes are NOT committed (the WSL-side approval channel was down). After a
green run:

    git -C H:\Developing\Agent\Tokdash_Project\tokdash\.claude\worktrees\windows-setup-fix add -A
    git -C <same> commit -m "Fix native Windows setup: per-user logon trigger, stdio safety, foreign-port handling"

## Round 5 (post-review hardening)

Inline review of the green branch found five issues, all fixed:

1. **Blocking — non-English Windows:** readiness required the English
   "Running" string from `schtasks /Query /V`. The gate now accepts EITHER the
   task state OR the locale-independent holder check (PID -> image name), and
   `detect.port_holder` falls back to PowerShell `Get-NetTCPConnection`
   (invariant output) when netstat's localized state words yield nothing.
2. Kill authority in `_wait_for_port_release` now also requires the port to
   answer with Tokdash's fingerprint (image name alone no longer authorizes
   `taskkill`).
3. `tasklist` "no tasks match" INFO lines (localized, no leading quote) are
   no longer parsed as image names.
4. `update` and `uninstall` reuse the release gate: update does /End -> wait
   (kill own lingering holder) -> /Run; uninstall does /End -> wait -> /Delete
   and reports a lingering holder it must not touch.
5. `plan.py` docstrings now state that planning is mutation-free but
   read-only probing (incl. subprocesses on Windows) happens; busy messages
   get a colon.

Also: `live_test.ps1` + this file moved to `scripts/`; `docs/CHANGELOG.md`
added (v2.0.1 entry). New tests cover the locale readiness path, kill
authority, tasklist parsing, the PowerShell fallback, and the update/uninstall
release-gate ordering.

## Round 6 (second review pass)

- Uninstall no longer fails (or waits 15s) when a FOREIGN process holds the
  recorded port: the release wait now runs only when the holder is provably
  our own runtime (fingerprint + image name + exact interpreter path). The
  WSL-relay-on-55423 host configuration unblocks cleanly.
- Kill authority tightened to the exact interpreter: the holder's executable
  path (PowerShell Get-Process) is compared against the manifest runtime's
  pythonw twin, so a manually-started `tokdash serve` from another venv is
  never force-killed by setup/update/uninstall.
- Holder lookup cached for the life of one wait (plus a 2s TTL in the
  readiness loop): one PowerShell spawn per wait instead of per tick on
  hosts with localized netstat.
- update dry-run text updated to match the stop -> wait -> run flow.
- Changelog entry moved to the canonical docs/development/CHANGELOG.md
  (the docs/CHANGELOG.md path was deleted in the docs reorg).

## Round 7 (third review pass)

- Holder cache is now real: an unresolvable holder and a not-ours verdict are
  terminal for the wait (no re-lookup per tick); only a killed PID triggers a
  refresh. One PowerShell spawn per distinct holder per wait.
- Ownership classification (`_classify_port_holder`) is shared by setup, update
  and uninstall: 'free' | 'ours' | 'foreign'. 'ours' = our image name + exact
  interpreter path when resolvable (stronger than a single /health probe, so a
  WEDGED own instance that stopped answering is still stopped by uninstall),
  else the fingerprint. 'foreign' = everything else, never killed.
- The resolved holder is passed into `_wait_for_port_release` (initial_holder),
  so the probe/netstat/Get-Process triple is not repeated.
- Update now fails the restart immediately (with the holder named) when a
  foreign occupant holds the recorded port, instead of paying a 15s wait; setup
  likewise fails fast on a foreign occupant.
- `detect.process_image_path` got the os.name guard its sibling has.
- Known precision gap (accepted): non-python.exe runtimes (e.g. a pipx
  tokdash.exe) fall back to the name+fingerprint gate.
