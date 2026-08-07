"""Servidor MCP de prueba: expone tools 'echo' y 'add' vía stdio.

Para tests de integración del cliente MCP de Rinari.
"""

import sys
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("test-server")


@mcp.tool()
def echo(text: str) -> str:
    """Devuelve el texto recibido."""
    return f"echo: {text}"


@mcp.tool()
def add(a: int, b: int) -> int:
    """Suma dos números."""
    return a + b


if __name__ == "__main__":
    mcp.run(transport="stdio")
