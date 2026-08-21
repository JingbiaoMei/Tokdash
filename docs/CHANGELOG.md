# Changelog

## v2.0.1 - 2026-08-21

### Fixed
- `tokdash setup` on native Windows: the logon trigger now names the invoking
  user, so a standard user registers the task without elevation (no more
  `schtasks /Create` "Access is denied").
- `tokdash serve` no longer dies on Windows consoles without UTF-8 (or with no
  console at all under `pythonw.exe`).
- Setup no longer mis-identifies a WSL-relayed or foreign Tokdash on the
  default port as its own service; it reports the holder and auto-picks a free
  port with `--auto`. Re-setup keeps the previously recorded port.
- Windows re-setup/upgrade: the service port is verified to actually release
  after `schtasks /End`, and a lingering instance of our own runtime is stopped
  by PID before the replacement starts. A stale `pythonw.exe` used to keep the
  port, make the new instance exit with code 3, and let setup report success
  anyway; `uninstall` now stops and waits on the service the same way.
- Readiness no longer depends on the locale of `schtasks`/`netstat` output
  (works on non-English Windows).
- `tokdash doctor` names the stale process still serving the port when the
  task is inactive.

### Changed
- A manually-run `tokdash serve` on the setup port is no longer adopted by
  setup; the port is reported as busy serving another Tokdash install (use
  `--port`, or stop the manual instance, then re-run setup).
