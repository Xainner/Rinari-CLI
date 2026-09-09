"""Bounded Git probes that cannot strand Python on inherited Windows pipes."""

from __future__ import annotations

import subprocess
import tempfile
from contextlib import suppress
from pathlib import Path


def capture_git(cwd: Path, args: list[str], *, timeout_s: float) -> str | None:
    """Return Git stdout, or ``None`` on failure/timeout.

    A PIPE-based ``subprocess.run`` can exceed its timeout indefinitely on
    Windows when a Git descendant inherits the pipe. A temporary file avoids
    reader threads and lets the caller return immediately after killing the
    direct child.
    """
    try:
        with tempfile.TemporaryFile(mode="w+b") as output:
            proc = subprocess.Popen(
                ["git", *args],
                cwd=cwd,
                stdout=output,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
            try:
                return_code = proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                with suppress(OSError):
                    proc.kill()
                with suppress(OSError, subprocess.TimeoutExpired):
                    proc.wait(timeout=0.5)
                return None
            if return_code != 0:
                return None
            output.seek(0)
            return output.read().decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
