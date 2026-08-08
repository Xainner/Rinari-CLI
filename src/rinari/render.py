"""Rendering de streaming con rich.

DeltaAccumulator junta deltas de streaming y renderiza markdown al final.
strip_ansi limpia códigos ANSI (útil para salida de comandos).
"""

from __future__ import annotations

import re
import sys

from rich.panel import Panel

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

    def stream_live(self, events, console: Console | None = None) -> None:
        """Renderiza eventos de streaming EN VIVO (token a token).

        Escribe cada delta directamente a stdout con flush — funciona en
        cualquier terminal (incluidas consolas Windows/PowerShell donde
        rich Live no refresca de forma confiable). Al final, nueva línea.
        events: iterable de str o dicts (los dicts con tool_calls se ignoran).
        """
        console = console or self.console
        for event in events:
            if isinstance(event, str):
                self.add(event)
                sys.stdout.write(event)
                sys.stdout.flush()
        sys.stdout.write("\n")
        sys.stdout.flush()


def render_code_block(code: str, language: str = "python", title: str = "Código") -> None:
    """Renderiza un bloque de código con syntax highlighting."""
    console = Console()
    syntax = Syntax(code, language, theme="monokai", line_numbers=True)
    console.print(Panel(syntax, title=title, border_style="cyan"))


def render_status(message: str, style: str = "yellow") -> None:
    """Renderiza un mensaje de estado (pasos del agente, etc.)."""
    console = Console()
    console.print(Text(f"• {message}", style=style))


class ToolSpinner:
    """Spinner de rich que gira mientras una tool se ejecuta.

    rich arranca un thread de refresh interno en start(); el hilo principal
    puede bloquearse ejecutando la tool mientras el spinner sigue animando.
    """

    def __init__(self) -> None:
        self._status = None
        self._console = None

    def start(self, message: str) -> None:
        self._console = Console()
        self._status = self._console.status(message, spinner="dots")
        self._status.start()

    def stop(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None

    def is_active(self) -> bool:
        return self._status is not None


def render_tool_result(result: dict, max_output: int = 800) -> None:
    """Resalta el resultado de una tool: output en panel, errores en rojo."""
    console = Console()
    if result.get("ok") is False or result.get("error"):
        err = str(result.get("error") or "")[:max_output]
        console.print(Text(f"⚠️  {err}", style="red"))
        return
    stdout = str(result.get("stdout") or "").strip()
    if stdout:
        preview = stdout[:max_output]
        if len(stdout) > max_output:
            preview += f"\n… ({len(stdout) - max_output} caracteres más)"
        console.print(Panel(preview, border_style="cyan", title="output", padding=(0, 1)))
