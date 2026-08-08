"""Identidad de Rinari: SOUL.md canónico.

El SOUL.md en assets/ es la ÚNICA fuente de personalidad del CLI.
Chat y agente componen su prompt desde aquí — sin duplicados.

UNA voz: maid moderna con humor seco, atenta y eficiente. Sin modos.
"""

from __future__ import annotations

from pathlib import Path

SOUL_PATH = Path(__file__).parent / "assets" / "soul.md"


def load_soul() -> str:
    """Devuelve el contenido completo del SOUL.md canónico."""
    return SOUL_PATH.read_text(encoding="utf-8")


def build_chat_prompt() -> str:
    """Prompt de sistema para el chat interactivo."""
    return load_soul().strip()


def build_agent_prompt() -> str:
    """Prompt de sistema para el modo agente de código."""
    return load_soul().strip()
