"""Who a PID is: its start time, so a reused PID is never taken for ours (#90).

A process left running when the Engine went away is found again by PID, but
PIDs are reused: the start time recorded with it tells the same process from a
new one with the same number. Windows asks the kernel; Linux reads /proc; other
systems have no cheap way and only check that the PID is alive.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_TERMINATE = 0x0001
_STILL_ACTIVE = 259


def started_at(pid: int) -> float | None:
    """The process's start time in seconds (an opaque, comparable number), or None."""
    if pid <= 0:
        return None
    if sys.platform == "win32":
        return _windows_started_at(pid)
    with contextlib.suppress(OSError, ValueError, IndexError):
        with open(f"/proc/{pid}/stat", encoding="utf-8") as stat:
            fields = stat.read().rsplit(")", 1)[1].split()
        return float(fields[19])  # starttime, in clock ticks since boot
    return None


def _windows_started_at(pid: int) -> float | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != _STILL_ACTIVE:
            return None
        creation, exit_time, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return ticks / 10_000_000
    finally:
        kernel32.CloseHandle(handle)


def is_same(pid: int, recorded: float | None) -> bool:
    """The PID is alive and is the process recorded (same start time when known)."""
    current = started_at(pid)
    if current is None:
        if sys.platform == "win32" or recorded is not None:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    return recorded is None or abs(current - recorded) < 0.01


def kill_tree(pid: int, timeout_s: float = 5.0) -> None:
    """Stop a process and its children by PID (no handle of ours left)."""
    if sys.platform == "win32":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout_s,
                check=False,
            )
        return
    import signal

    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(pid), signal.SIGKILL)
