"""Two Engines on one home: the second refuses instead of sharing it."""

from __future__ import annotations

import json
import os
import subprocess
import sys


def _engine(home):
    env = {**os.environ, "RINARI_HOME": str(home), "PYTHONUTF8": "1"}
    return subprocess.Popen(
        [sys.executable, "-m", "rinari", "engine", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=home,
        text=True,
        encoding="utf-8",
    )


def test_a_second_engine_on_the_same_home_refuses(tmp_path):
    first = _engine(tmp_path)
    try:
        hello = json.loads(first.stdout.readline())
        assert hello.get("type") == "hello"
        second = _engine(tmp_path)
        _out, err = second.communicate(timeout=60)
        assert second.returncode != 0
        assert "Another Rinari engine is already using" in err
    finally:
        first.stdin.close()
        try:
            first.wait(timeout=20)
        except subprocess.TimeoutExpired:
            first.kill()
    # Once the first one is gone, the home is free again.
    third = _engine(tmp_path)
    try:
        assert json.loads(third.stdout.readline()).get("type") == "hello"
    finally:
        third.stdin.close()
        try:
            third.wait(timeout=20)
        except subprocess.TimeoutExpired:
            third.kill()
