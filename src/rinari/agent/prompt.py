"""System prompt para el modo agente de código.

Compuesto desde el SOUL.md canónico (rinari.identity) — la personalidad
vive en un solo lugar.
"""

from __future__ import annotations

from rinari.identity import build_agent_prompt

AGENT_SYSTEM_PROMPT = build_agent_prompt()


def build_agent_messages(task: str) -> list[dict]:
    return [
        {"role": "system", "content": AGENT_SYSTEM_PROMPT},
        {"role": "user", "content": f"Tarea: {task}"},
    ]
