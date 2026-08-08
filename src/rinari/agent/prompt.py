"""System prompt para el modo agente de código.

Compuesto desde el SOUL.md canónico (rinari.identity) — la personalidad
vive en un solo lugar. Memoria por repo (RINARI.md, estilo CLAUDE.md/AGENTS.md)
se inyecta en el system prompt cuando existe.
"""

from __future__ import annotations

from pathlib import Path

from rinari.identity import build_agent_prompt

AGENT_SYSTEM_PROMPT = build_agent_prompt()

_MEMORY_FILENAMES = ("RINARI.md", ".rinari.md")


def load_repo_memory(cwd: str | Path | None) -> str | None:
    """Busca RINARI.md en cwd o en los ancestros (repo raíz).

    Devuelve el contenido si existe, None si no. Igual que CLAUDE.md /
    AGENTS.md: convenciones del repo que el agente debe respetar.
    """
    if not cwd:
        return None
    path = Path(cwd)
    if path.is_file():
        path = path.parent
    for directory in [path, *path.parents]:
        for name in _MEMORY_FILENAMES:
            candidate = directory / name
            if candidate.is_file():
                try:
                    return candidate.read_text(encoding="utf-8").strip() or None
                except OSError:
                    return None
    return None


def build_agent_messages(task: str, cwd: str | Path | None = None) -> list[dict]:
    system = AGENT_SYSTEM_PROMPT
    memory = load_repo_memory(cwd)
    if memory:
        system = (
            f"{system}\n\n"
            f"## Memoria del repositorio (RINARI.md)\n"
            f"Sigue estas convenciones del proyecto. Son reglas del repo, no sugerencias:\n\n"
            f"{memory}"
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Tarea: {task}"},
    ]

