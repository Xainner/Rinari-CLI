"""CLI output: `--json` envelope (docs/commands.md section 67)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import typer


def timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def success_envelope(command: str, data: Any, warnings: Sequence[str] = ()) -> dict:
    return {
        "ok": True,
        "command": command,
        "data": data,
        "warnings": list(warnings),
        "metadata": {"timestamp": timestamp()},
    }


def failure_envelope(command: str, message: str, code: str, retryable: bool) -> dict:
    return {
        "ok": False,
        "command": command,
        "error": {"code": code, "message": message, "retryable": retryable},
    }


def emit_json(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
