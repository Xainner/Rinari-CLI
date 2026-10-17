"""project.instructions.propose: Rinari proposes the project's RINARI.md (plan 0.4.0 #81).

`/init` starts a read-only turn that explores the project and ends by calling
this tool with the whole file. Nothing is written here: the desktop shows a
card with the proposal (or the diff against the current RINARI.md) and «Guardar»
/ «Editar» / «Descartar»; only «Guardar» writes, through
`project.instructions.resolve`, which reads the proposal back from this event.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from rinari.tools.definition import (
    RISK_LOW,
    SIDE_EFFECT_NONE,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)

INSTRUCTIONS_FILE = "RINARI.md"
#: The resolver reads at most 32 KB of a RINARI.md; a longer proposal would be cut.
MAX_CONTENT_CHARS = 32_000
#: Other agents' instruction files that /init can reuse instead of duplicating.
REUSABLE = ("AGENTS.md", "CLAUDE.md")

DESCRIPTION = (
    "Propose the project's RINARI.md to the user (the /init flow). Give the whole file in "
    "content: what the project is and its stack; how to install, test, build and run it with "
    "the real commands you found, under a «## Cómo probar» / «## How to test» section with "
    "one command per line in backticks; conventions, important folders and what not to touch. "
    "Nothing is written: the user saves, edits or discards it. reuses: the AGENTS.md or "
    "CLAUDE.md you summarized or linked, if any."
)

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["content"],
    "properties": {
        "content": {"type": "string", "minLength": 1, "maxLength": MAX_CONTENT_CHARS},
        "summary": {"type": "string", "maxLength": 300},
        "reuses": {
            "type": "array",
            "maxItems": len(REUSABLE),
            "items": {"type": "string", "enum": list(REUSABLE)},
        },
    },
}


def _fail(code: ToolErrorCode, message: str) -> ToolResult:
    return ToolResult(ok=False, error=ToolErrorInfo(code=code, message=message))


def current_instructions(root: Path) -> str | None:
    path = root / INSTRUCTIONS_FILE
    if not path.is_file():
        return None
    with contextlib.suppress(OSError, UnicodeDecodeError):
        return path.read_text(encoding="utf-8")
    return None


def _propose(arguments: dict, ctx: ToolContext) -> ToolResult:
    root = ctx.project_root
    if root is None or not Path(root).is_dir():
        return _fail(
            ToolErrorCode.INVALID_ARGUMENT,
            "This conversation has no project folder; RINARI.md belongs to a project.",
        )
    content = (arguments or {}).get("content")
    if not isinstance(content, str) or not content.strip():
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "content must be the whole RINARI.md")
    content = content.strip() + "\n"
    summary = " ".join(str((arguments or {}).get("summary") or "").split())[:300]
    reuses = [name for name in (arguments or {}).get("reuses") or [] if name in REUSABLE]
    root = Path(root)
    previous = current_instructions(root)
    proposal_id = getattr(ctx, "tool_call_id", "") or "instructions"
    sink = getattr(ctx, "activity_sink", None)
    if callable(sink):
        with contextlib.suppress(Exception):
            sink(
                "instructions.proposed",
                {
                    "proposal_id": proposal_id,
                    "path": str(root / INSTRUCTIONS_FILE),
                    "content": content,
                    "previous": previous,
                    "summary": summary,
                    "reuses": reuses,
                },
            )
    return ToolResult(
        ok=True,
        data={
            "proposal_id": proposal_id,
            "status": "proposed",
            "updates": previous is not None,
            "message": "Shown to the user to save, edit or discard. Do not write the file.",
        },
    )


def instructions_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="project.instructions.propose",
            description=DESCRIPTION,
            input_schema=INPUT_SCHEMA,
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            idempotent=True,
            handler=_propose,
            namespace="project",
        )
    ]
