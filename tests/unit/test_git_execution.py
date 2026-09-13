"""Real-process regressions for Git timeout, cancellation and inherited handles."""

import subprocess
import sys
import threading
import time

import pytest

from rinari.projects import _git_process
from rinari.runtime.cancellation import CancellationToken
from rinari.shared.errors import CancelledError


@pytest.mark.parametrize("cancel", [False, True])
def test_slow_git_is_bounded_and_cancellable(tmp_path, monkeypatch, cancel):
    popen = subprocess.Popen
    started = threading.Event()
    processes = []

    def slow_git(args, **kwargs):
        proc = popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        processes.append(proc)
        started.set()
        return proc

    monkeypatch.setattr(_git_process.subprocess, "Popen", slow_git)

    # Windows tree termination uses subprocess.run internally, so restore Popen
    # as soon as the single simulated Git command has been launched.
    def spawn(args, **kwargs):
        monkeypatch.setattr(_git_process.subprocess, "Popen", popen)
        return slow_git(args, **kwargs)

    monkeypatch.setattr(_git_process.subprocess, "Popen", spawn)
    token = CancellationToken()
    timer = threading.Timer(0.2, token.cancel)
    if cancel:
        timer.start()
    before = time.monotonic()
    try:
        if cancel:
            with pytest.raises(CancelledError):
                _git_process.run_git(tmp_path, ["status"], timeout_s=30, cancellation=token)
        else:
            result = _git_process.run_git(tmp_path, ["status"], timeout_s=0.2)
            assert result.timed_out
        assert time.monotonic() - before < 4
        assert processes[0].poll() is not None
    finally:
        timer.cancel()
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)


def test_exited_git_does_not_wait_for_descendant_output_handles(tmp_path, monkeypatch):
    popen = subprocess.Popen
    child_pid = tmp_path / "child.pid"
    script = (
        "import subprocess,sys,pathlib; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(1)']); "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(p.pid)); "
        "print('## main')"
    )

    def spawn(args, **kwargs):
        monkeypatch.setattr(_git_process.subprocess, "Popen", popen)
        return popen([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr(_git_process.subprocess, "Popen", spawn)
    before = time.monotonic()
    result = _git_process.run_git(tmp_path, ["status"], timeout_s=0.5)
    assert result.returncode == 0
    assert "## main" in result.stdout
    assert time.monotonic() - before < 1
