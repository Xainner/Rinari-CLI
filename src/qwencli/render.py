"""Rendering de streaming con rich.

DeltaAccumulator junta deltas de streaming y renderiza markdown al final.
strip_ansi limpia códigos ANSI (útil para salida de comandos).
"""

from __future__ import annotations

import re

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


class DeltaAccumulator:
    """Acumula deltas de streaming y renderiza markdown al finalizar."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self.console = Console()

    @property
    def text(self) -> str:
        return "".join(self._parts)

    def add(self, delta: str) -> None:
        self._parts.append(delta)

    def reset(self) -> None:
        self._parts = []

    def render_markdown(self) -> None:
        """Renderiza el texto acumulado como markdown (no streaming)."""
        text = self.text
        if not text.strip():
            return
        try:
            self.console.print(Markdown(text))
        except Exception:
            self.console.print(text)


def render_code_block(code: str, language: str = "python", title: str = "Código") -> None:
    """Renderiza un bloque de código con syntax highlighting."""
    console = Console()
    syntax = Syntax(code, language, theme="monokai", line_numbers=True)
    console.print(Panel(syntax, title=title, border_style="cyan"))


def render_status(message: str, style: str = "yellow") -> None:
    """Renderiza un mensaje de estado (pasos del agente, etc.)."""
    console = Console()
    console.print(Text(f"• {message}", style=style))
