"""Windows Job Objects for the processes Rinari starts (plan 0.4.0 #90).

A process started by the model is created suspended, put in its own job with
KILL_ON_JOB_CLOSE (no breakaway) and then resumed, so everything it launches
belongs to the job too, including what it starts with `start`, `&` or
`Start-Process`, which escaped `taskkill /T`. Stopping it terminates the whole
job, and if the Engine dies Windows closes the job handle and kills the tree.

Elsewhere this module is a no-op: POSIX process groups already give that.
Any failure (an older Windows, a process already in a job that forbids
nesting) falls back to the plain process, never to not starting it.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from typing import Any

_CREATE_SUSPENDED = 0x00000004
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


class ProcessJob:
    """One job object; `terminate` kills every process in it."""

    def __init__(self, handle: int) -> None:
        self._handle = handle

    def terminate(self, exit_code: int = 1) -> bool:
        if not self._handle:
            return False
        import ctypes

        return bool(ctypes.windll.kernel32.TerminateJobObject(self._handle, exit_code))

    def close(self) -> None:
        if not self._handle:
            return
        import ctypes

        with contextlib.suppress(Exception):
            ctypes.windll.kernel32.CloseHandle(self._handle)
        self._handle = 0


def _create_job(kill_on_close: bool) -> int:
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        return 0
    if kill_on_close:
        info = ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
        )
        if not ok:
            kernel32.CloseHandle(handle)
            return 0
    return int(handle)


def _resume(process: subprocess.Popen) -> None:
    import ctypes

    # Popen keeps the process handle, not the primary thread's: resume the
    # whole process (it has exactly one suspended thread at this point).
    ctypes.windll.ntdll.NtResumeProcess(int(process._handle))  # type: ignore[attr-defined]


def popen_in_job(
    argv: Any, *, kill_on_close: bool = True, **kwargs: Any
) -> tuple[subprocess.Popen, ProcessJob | None]:
    """`subprocess.Popen` whose whole tree lives in a job (Windows); the job, or None."""
    if sys.platform != "win32":
        return subprocess.Popen(argv, **kwargs), None
    kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | _CREATE_SUSPENDED
    process = subprocess.Popen(argv, **kwargs)
    job: ProcessJob | None = None
    try:
        import ctypes

        handle = _create_job(kill_on_close)
        if handle and ctypes.windll.kernel32.AssignProcessToJobObject(handle, int(process._handle)):  # type: ignore[attr-defined]
            job = ProcessJob(handle)
        elif handle:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        job = None
    finally:
        # Never leave a process suspended, whatever happened with the job.
        with contextlib.suppress(Exception):
            _resume(process)
    return process, job
