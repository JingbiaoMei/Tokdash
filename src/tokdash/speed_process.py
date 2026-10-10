"""Non-destructive worker health checks with process creation identity."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess


def _windows_snapshot(pid):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x1000 | 0x00100000, False, pid)  # query limited + synchronize
    if not handle:
        return False, None
    try:
        if kernel.WaitForSingleObject(handle, 0) != 258:  # WAIT_TIMEOUT means still running
            return False, None
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, created, exited, system, user):
            return False, None
        return True, f'windows:{created.dwHighDateTime}:{created.dwLowDateTime}'
    finally:
        kernel.CloseHandle(handle)


def snapshot(pid):
    try:
        pid = int(pid or 0)
        if pid <= 0:
            return False, None
        if os.name == 'nt':
            return _windows_snapshot(pid)
        stat = Path(f'/proc/{pid}/stat')
        if stat.exists():
            fields = stat.read_text().rsplit(') ', 1)[1].split()
            if fields[0] == 'Z':
                return False, None
            boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            return True, f'linux:{boot}:{fields[19]}'
        os.kill(pid, 0)  # POSIX only
        # macOS/BSD have no /proc. lstart is a process-instance identifier, not a lease timeout.
        created = subprocess.check_output(['ps', '-p', str(pid), '-o', 'lstart='], text=True).strip()
        return bool(created), 'posix:' + created if created else None
    except (OSError, ValueError, IndexError, ImportError, AttributeError, subprocess.SubprocessError):
        return False, None


def alive(pid, token=None):
    running, observed = snapshot(pid)
    return running and (not token or observed == token)
