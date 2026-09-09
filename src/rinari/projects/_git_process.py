"""Bounded Git probes that cannot strand Python on inherited Windows pipes."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class GitCommandResult:
    stdout: str
    returncode: int | None
    timed_out: bool = False
    error: str | None = None


def _terminate_tree(proc: subprocess.Popen[bytes]) -> None:
    """Terminate the command and every descendant without inheriting pipes."""
    poll = getattr(proc, "poll", None)
    if poll is not None and poll() is not None:
        return
    pid = getattr(proc, "pid", None)
    if os.name == "nt" and pid is not None:
        # Git frequently launches helpers on Windows. Killing only git.exe can
        # leave a helper alive, which in turn can strand a caller indefinitely.
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1.0,
                check=False,
            )
    elif os.name != "nt" and pid is not None:
        with suppress(OSError):
            os.killpg(pid, signal.SIGKILL)
    with suppress(OSError):
        proc.kill()
    with suppress(OSError, subprocess.TimeoutExpired):
        proc.wait(timeout=0.5)


def run_git(cwd: Path, args: list[str], *, timeout_s: float) -> GitCommandResult:
    """Run one bounded Git probe and preserve a structured failure reason."""
    started = time.monotonic()
    try:
        with tempfile.TemporaryFile(mode="w+b") as output:
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            proc = subprocess.Popen(
                ["git", *args],
                cwd=cwd,
                stdout=output,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
                start_new_session=os.name != "nt",
            )
            try:
                return_code = proc.wait(timeout=max(0.01, timeout_s))
            except subprocess.TimeoutExpired:
                _terminate_tree(proc)
                return GitCommandResult(
                    stdout="",
                    returncode=None,
                    timed_out=True,
                    error=f"git {' '.join(args)} exceeded {timeout_s:.2f}s",
                )
            output.seek(0)
            stdout = output.read().decode("utf-8", errors="replace")
            if return_code != 0:
                return GitCommandResult(
                    stdout=stdout,
                    returncode=return_code,
                    error=f"git {' '.join(args)} exited with {return_code}",
                )
            return GitCommandResult(stdout=stdout, returncode=return_code)
    except (OSError, subprocess.SubprocessError) as exc:
        return GitCommandResult(
            stdout="",
            returncode=None,
            error=f"git {' '.join(args)} could not start: {exc}",
        )
    finally:
        # Keep the monotonic read here so clock instrumentation can wrap this
        # helper without changing its process-lifetime guarantee.
        _ = time.monotonic() - started


def capture_git(cwd: Path, args: list[str], *, timeout_s: float) -> str | None:
    """Return Git stdout, or ``None`` on failure/timeout.

    A PIPE-based ``subprocess.run`` can exceed its timeout indefinitely on
    Windows when a Git descendant inherits the pipe. A temporary file avoids
    reader threads and lets the caller return immediately after killing the
    direct child.
    """
    result = run_git(cwd, args, timeout_s=timeout_s)
    return result.stdout if result.returncode == 0 and not result.timed_out else None


__all__ = ["GitCommandResult", "capture_git", "run_git"]
