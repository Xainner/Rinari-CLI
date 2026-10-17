"""The desktop's fast entry: ``python -m rinari.engine_protocol`` (plan 0.4.0 #91)."""

from __future__ import annotations

import json
import os
import subprocess
import sys


def test_fast_entry_says_hello_without_the_cli(tmp_path) -> None:
    env = {**os.environ, "RINARI_HOME": str(tmp_path / "home"), "RINARI_KEYRING": "0"}
    probe = (
        "import runpy, sys\n"
        "sys.argv = ['rinari.engine_protocol']\n"
        "try:\n"
        "    runpy.run_module('rinari.engine_protocol', run_name='__main__')\n"
        "except SystemExit:\n"
        "    pass\n"
        "print('TYPER' if 'typer' in sys.modules else 'NO-TYPER', file=sys.stderr)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        input="",
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    hello = json.loads(done.stdout.splitlines()[0])
    assert hello["type"] == "hello" and hello["protocol"] == "rinari-engine"
    assert "NO-TYPER" in done.stderr
