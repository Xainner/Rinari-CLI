"""Identidad de Rinari: SOUL.md canónico.

El SOUL.md en assets/ es la ÚNICA fuente de personalidad del CLI.
Chat y agente componen su prompt desde aquí — sin duplicados.

UNA voz: maid moderna con humor seco, atenta y eficiente. Sin modos.
El nombre del usuario se inyecta desde el config ([user] name) con
default "Xainner" si no está configurado.
"""

from __future__ import annotations

from pathlib import Path

SOUL_PATH = Path(__file__).parent / "assets" / "soul.md"

DEFAULT_USER_NAME = "Xainner"


def load_soul() -> str:
    """Devuelve el contenido completo del SOUL.md canónico."""
    return SOUL_PATH.read_text(encoding="utf-8")


def user_name_from_config() -> str:
    """Nombre del usuario desde el config ([user] name), con default."""
    from rinari.config import load_config

    try:
        cfg = load_config()
        return cfg.user_name or DEFAULT_USER_NAME
    except Exception:  # noqa: BLE001 — sin config válido, default
        return DEFAULT_USER_NAME


def _render(soul: str) -> str:
    return soul.replace("{{USER}}", user_name_from_config())


def build_chat_prompt() -> str:
    """Prompt de sistema para el chat interactivo."""
    return _render(load_soul()).strip()


def build_agent_prompt() -> str:
    """Prompt de sistema para el modo agente de código."""
    return _render(load_soul()).strip()
