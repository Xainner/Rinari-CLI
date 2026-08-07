"""Vista de inicio de Rinari: banner con logo, estado del sistema y sugerencias.

- render_logo(): el arte ASCII de Rinari (asset, hecho a mano)
- git_info(): estado del repo (branch, clean/dirty, commit corto)
- check_endpoint_health(): verifica que el endpoint responde (con timeout corto)
- build_welcome(): arma el texto del dashboard
- render_welcome(): lo pinta con rich
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from rinari import __version__

LOGO_FONT = "slant"
_ASSET = Path(__file__).parent / "assets" / "rinari_ascii.txt"


def render_logo(max_width: int | None = None, target_height: int | None = None) -> str:
    """El arte ASCII de Rinari (silueta con alas), o el figlet como fallback.

    Si max_width se da y el arte lo excede, se escala por densidad (downscale)
    preservando la silueta. target_height comprime también verticalmente
    (logo compacto para la columna lateral).
    """
    art = _ASSET.read_text(encoding="utf-8").rstrip("\n") if _ASSET.exists() else None
    if art is None:
        from pyfiglet import Figlet

        art = Figlet(font=LOGO_FONT).renderText("RINARI").rstrip("\n")
    if max_width is None and target_height is None:
        return art
    return _scale_art(art, max_width or 200, target_height)


def render_logo_compact() -> str:
    """Logo de Rinari compacto (~40×26) para la columna derecha del dashboard."""
    return render_logo(max_width=40, target_height=26)


def render_logo_side() -> str:
    """Logo aún más compacto (~34×22) para caber al lado de la info en 80 cols."""
    return render_logo(max_width=34, target_height=22)


def _scale_art(art: str, max_width: int, target_height: int | None = None) -> str:
    """Escala arte ASCII por densidad para que quepa en max_width.

    Agrupa celdas en bloques proporcionales; un bloque con tinta (>=1 char)
    se convierte en un solo carácter de tinta — la silueta se conserva.
    target_height comprime también verticalmente (logo compacto).
    """
    lines = [l for l in art.split("\n") if l]
    if not lines:
        return art
    width = max(len(l) for l in lines)
    height = len(lines)
    if width <= max_width and target_height is None:
        return art

    sx = width / max_width
    if target_height is not None:
        sy = height / max(target_height, 1)
    else:
        sy = max(1.0, sx * 0.62)  # proporción vertical: ~1.6:1 horizontal
    out_h = max(1, int(height / sy))
    out: list[str] = []
    for oy in range(out_h):
        y0 = int(oy * sy)
        y1 = max(y0 + 1, int((oy + 1) * sy))
        row: list[str] = []
        for ox in range(max_width):
            x0 = int(ox * sx)
            x1 = max(x0 + 1, int((ox + 1) * sx))
            # tinta = cualquier carácter no espacio en el bloque
            ink = False
            for yy in range(y0, min(y1, height)):
                line = lines[yy]
                if len(line) > x0 and any(c != " " for c in line[x0:x1]):
                    ink = True
                    break
            row.append("#" if ink else " ")
        out.append("".join(row).rstrip())
    return "\n".join(out)


def git_info(repo_path: Path | str) -> dict:
    """Estado git del repo: {branch, clean, commit}. Vacío si no es repo git."""
    repo_path = Path(repo_path)
    git_dir = repo_path / ".git"
    if not git_dir.exists():
        return {}
    try:
        branch = subprocess.run(
            ["git", "-C", str(repo_path), "branch", "--show-current"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).stdout.strip()
        commit = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--short=7", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(repo_path), "status", "--porcelain"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).stdout.strip()
        return {
            "branch": branch or "detached",
            "clean": status == "",
            "commit": commit,
        }
    except (subprocess.SubprocessError, OSError):
        return {}


def check_endpoint_health(client, timeout: float = 3.0) -> bool:
    """True si el endpoint responde /v1/models dentro del timeout."""
    from rinari.client import LLMError

    old_timeout = getattr(client, "timeout", None)
    try:
        client.timeout = timeout
        client.list_models()
        return True
    except (LLMError, Exception):  # noqa: BLE001 - cualquier error = caído
        return False
    finally:
        if old_timeout is not None:
            client.timeout = old_timeout


def measure_endpoint_latency(client, timeout: float = 3.0) -> float | None:
    """Latencia del endpoint en ms, o None si está caído."""
    import time

    from rinari.client import LLMError

    old_timeout = getattr(client, "timeout", None)
    try:
        client.timeout = timeout
        start = time.perf_counter()
        client.list_models()
        return round((time.perf_counter() - start) * 1000)
    except (LLMError, Exception):  # noqa: BLE001
        return None
    finally:
        if old_timeout is not None:
            client.timeout = old_timeout


def count_tools() -> int:
    """Cantidad de tools disponibles para el agente (nativas + MCP)."""
    from rinari.agent.tools import ToolRegistry

    n = len(ToolRegistry()._TOOLS)
    try:
        from rinari.mcp import load_mcp_servers

        n += len(load_mcp_servers())
    except Exception:  # noqa: BLE001 - MCP opcional
        pass
    return n


def build_welcome(
    profile: str,
    model: str,
    base_url: str,
    repo_name: str,
    git: dict,
    endpoint_ok: bool,
    version: str = __version__,
    sessions_count: int = 0,
    latency_ms: int | None = None,
    tools_count: int = 0,
    max_width: int | None = None,
) -> str:
    """Construye el texto del dashboard de bienvenida (sin el logo; el logo
    se renderiza a la derecha en render_welcome como columna compacta)."""
    lines = [
        f"[bold magenta]Rinari CLI[/bold magenta] [dim]v{version}[/dim] (✿◠‿◠)",
        "",
        f"  Perfil : [yellow]{profile}[/yellow]",
        f"  Modelo : [bold]{model}[/bold]",
        f"  Endpoint: {base_url}",
    ]

    if git:
        branch = git.get("branch", "?")
        commit = git.get("commit", "")
        clean = git.get("clean", True)
        state = "[green]limpio[/green]" if clean else "[yellow]con cambios[/yellow]"
        lines.append(f"  Repo   : [cyan]{repo_name}[/cyan] ([bold]{branch}[/bold] {state})")
        if commit:
            lines.append(f"  Commit : [dim]{commit}[/dim]")
    else:
        lines.append(f"  Repo   : [cyan]{repo_name}[/cyan] [dim](no es repo git)[/dim]")

    if endpoint_ok:
        lat = f" [dim]({latency_ms} ms)[/dim]" if latency_ms is not None else ""
        lines.append(f"  Estado : [green]✓ endpoint conectado[/green]{lat}")
    else:
        lines.append("  Estado : [red]✗ endpoint sin conexión[/red]")

    if sessions_count:
        lines.append(f"  Historial: {sessions_count} sesión(es) guardada(s)")
    if tools_count:
        lines.append(f"  Tools  : [bold]{tools_count}[/bold] disponibles")

    lines += [
        "",
        "[bold]Sugerencias:[/bold]",
        "  • Escribe una tarea y Rinari la ejecuta con tools",
        "  • /model <perfil>  /new  /approve (toggle)  /exit  /help",
        "  • Ctrl+C para detener la generación",
    ]
    return "\n".join(lines)


def render_welcome(
    profile: str,
    model: str,
    base_url: str,
    repo_name: str,
    git: dict,
    endpoint_ok: bool,
    sessions_count: int = 0,
    latency_ms: int | None = None,
    tools_count: int = 0,
) -> None:
    """Pinta el dashboard de bienvenida: logo centrado, info y sugerencias debajo."""
    from rich.align import Align
    from rich.console import Group
    from rich.text import Text

    console = Console()
    info = build_welcome(
        profile=profile,
        model=model,
        base_url=base_url,
        repo_name=repo_name,
        git=git,
        endpoint_ok=endpoint_ok,
        sessions_count=sessions_count,
        latency_ms=latency_ms,
        tools_count=tools_count,
    )
    # separar info de sugerencias: las sugerencias van al final
    header, _, suggestions = info.partition("[bold]Sugerencias:[/bold]")
    header = header.rstrip()
    suggestions = suggestions.strip()

    content: list = [Align(Text(render_logo_compact()), align="center")]
    content.append(Align(Text.from_markup(header), align="center"))
    if suggestions:
        content.append(Align(Text.from_markup(f"[bold]Sugerencias:[/bold] {suggestions}"), align="center"))
    console.print(Panel(Group(*content), border_style="magenta", padding=(1, 2)))
