"""Vista de inicio de Rinari: banner con logo, estado del sistema y sugerencias.

- render_logo(): genera el ASCII art de RINARI con pyfiglet
- git_info(): estado del repo (branch, clean/dirty, commit corto)
- check_endpoint_health(): verifica que el endpoint responde (con timeout corto)
- build_welcome(): arma el texto del dashboard
- render_welcome(): lo pinta con rich
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from pyfiglet import Figlet
from rich.console import Console
from rich.panel import Panel

from rinari import __version__

LOGO_FONT = "slant"


def render_logo() -> str:
    """Genera el ASCII art de RINARI."""
    fig = Figlet(font=LOGO_FONT)
    return fig.renderText("RINARI").rstrip("\n")


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


def build_welcome(
    profile: str,
    model: str,
    base_url: str,
    repo_name: str,
    git: dict,
    endpoint_ok: bool,
    version: str = __version__,
    sessions_count: int = 0,
) -> str:
    """Construye el texto del dashboard de bienvenida."""
    lines = [
        render_logo(),
        "",
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
        lines.append("  Estado : [green]✓ endpoint conectado[/green]")
    else:
        lines.append("  Estado : [red]✗ endpoint sin conexión[/red]")

    if sessions_count:
        lines.append(f"  Historial: {sessions_count} sesión(es) guardada(s)")

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
) -> None:
    """Pinta el dashboard de bienvenida en la terminal."""
    console = Console()
    welcome = build_welcome(
        profile=profile,
        model=model,
        base_url=base_url,
        repo_name=repo_name,
        git=git,
        endpoint_ok=endpoint_ok,
        sessions_count=sessions_count,
    )
    console.print(Panel(welcome, border_style="magenta", padding=(1, 2)))
