"""CLI de rinari — typer entrypoints.

Subcomandos:
- chat   : REPL interactivo (streaming, historial, comandos /)
- run    : one-shot, prompt → stdout (para scripts/pipes)
- models : lista modelos del endpoint
- agent  : modo agente de código (tool calling) — ver agent/
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from rinari import __version__
from rinari.client import LLMClient, LLMError
from rinari.config import ConfigError, load_config
from rinari.history import History
from rinari.render import DeltaAccumulator
from rinari.repl import ChatSession, parse_command, run_command

app = typer.Typer(
    add_completion=False,
    help="Rinari CLI — tu asistente con LLM contra tus modelos locales.",
    invoke_without_command=True,
)
console = Console()


def _normalize_cwd(cwd: Path) -> Path:
    """Convierte paths estilo MSYS (/c/Users/...) a Windows (C:\\Users\\...) y resuelve ~.

    typer/click ya convierte `/c/Users` a `C:/c/Users` (o `\\c\\Users` según el
    drive actual). Detectamos el drive duplicado en ambos formatos y lo corregimos.
    """
    import re

    s = str(cwd)
    if s.startswith("~"):
        s = str(Path.home()) + s[1:]
    else:
        # 'C:/c/Users/...' o 'C:\c\Users\...' → 'C:/Users/...'
        m = re.match(r"^([A-Za-z]):[\\/]\1[\\/]", s)
        if m:
            s = s[m.end() - 1 :]
        else:
            # '\c\Users\...' o '/c/Users/...' → '<drive_actual>:/Users/...'
            m2 = re.match(r"^[\\/]([a-z])[\\/]", s)
            if m2:
                drive = Path.cwd().drive or "C:"
                s = f"{drive}/{s[2:]}"
    return Path(s).resolve()


def _load_mcp_servers() -> dict:
    """Carga los servidores MCP del config. Vacío si no hay o falla."""
    try:
        from rinari.mcp import load_mcp_servers

        return load_mcp_servers()
    except Exception:  # noqa: BLE001
        return {}


def _agent_on_step(step: dict) -> None:
    """Renderiza un paso del agente en vivo."""
    from rinari.render import render_status

    t = step["type"]
    if t == "tool_call":
        name = step.get("name", "")
        args = step.get("arguments", {})
        cmd = args.get("command") or args.get("path") or ""
        render_status(f"🔧 {name} {cmd}", style="cyan")
    elif t == "tool_result":
        result = step.get("result", {})
        if result.get("ok") is False or result.get("error"):
            render_status(f"⚠️ Resultado con error: {result.get('error', '')[:120]}", style="red")
    elif t == "tool_denied":
        render_status("🚫 Comando denegado", style="yellow")
    elif t == "error":
        render_status(f"❌ {step.get('message', '')}", style="red")


def _agent_interactive(
    profile: str = "default",
    cwd: Path = Path("."),
    auto_approve: bool = False,
    max_iterations: int = 10,
) -> None:
    """REPL agéntico tipo Codex/Claude CLI: das tareas y Rinari trabaja con
    tools, manteniendo el contexto del repo entre turnos."""
    from rinari.agent.loop import run_agent

    cfg = load_config()
    try:
        current = cfg.get_profile(profile)
    except ConfigError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    workdir = _normalize_cwd(cwd)
    repo_name = workdir.name or str(workdir)
    messages: list[dict] | None = None
    current_profile = profile
    approve = auto_approve

    console.print(
        Panel(
            f"[bold magenta]Rinari — modo agente interactivo[/bold magenta] (✿◠‿◠)\n"
            f"Perfil: [yellow]{current_profile}[/yellow] → {current.base_url} ([bold]{current.model}[/bold])\n"
            f"Repo: [cyan]{workdir}[/cyan]\n"
            "Escribe una tarea y Rinari la ejecuta con tools. "
            "Comandos: /new, /model <perfil>, /approve (toggle), /exit, /help. Ctrl+C para detener.",
            border_style="magenta",
        )
    )

    def build_client(profile_name: str):
        nonlocal current
        current = cfg.get_profile(profile_name)
        return LLMClient(
            base_url=current.base_url,
            api_key=current.api_key,
            model=current.model,
        )

    while True:
        try:
            line = console.input(f"[bold magenta]rinari@{repo_name}[/bold magenta] > ")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Adiós (✿◠‿◠)[/dim]")
            break

        if not line.strip():
            continue

        # Comandos de barra
        if line.strip().startswith("/"):
            cmd, args = parse_command(line)
            if cmd == "exit":
                break
            if cmd == "new":
                messages = None
                console.print("🧹 Contexto reiniciado.")
                continue
            if cmd == "model":
                name = args.strip()
                if not name:
                    console.print("[red]Uso: /model <perfil>[/red]")
                    continue
                try:
                    build_client(name)
                    current_profile = name
                    console.print(f"🔀 Perfil cambiado a '{name}'.")
                except ConfigError as e:
                    console.print(f"[red]{e}[/red]")
                continue
            if cmd == "approve":
                approve = not approve
                console.print(
                    f"[{'green' if approve else 'yellow'}]Aprobación automática: {'ON' if approve else 'OFF'}[/]"
                )
                continue
            if cmd == "help":
                console.print(
                    "Comandos: /new (nuevo contexto), /model <perfil>, /approve (toggle aprobación), "
                    "/exit, /help. O escribe una tarea."
                )
                continue
            console.print(f"[red]Comando desconocido: /{cmd}[/red]")
            continue

        # Tarea → loop agéntico (encadena el contexto)
        try:
            client = build_client(current_profile)
        except ConfigError as e:
            console.print(f"[red]{e}[/red]")
            continue

        console.print("[dim]⏳ Rinari está trabajando…[/dim]")
        result = run_agent(
            task=line.strip(),
            client=client,
            cwd=str(workdir),
            auto_approve=approve,
            max_iterations=max_iterations,
            render_callback=_agent_on_step,
            messages=messages,
            mcp_servers=_load_mcp_servers(),
        )
        messages = result.get("messages", messages)

        if result["status"] == "done" and result.get("final"):
            console.print(Markdown(result["final"]))
        elif result["status"] == "max_iterations":
            console.print("[yellow]⚠️ Se alcanzó el límite de iteraciones — puedes seguir con otra tarea.[/yellow]")
        else:
            console.print("[red]❌ El agente falló en esta tarea.[/red]")


@app.command()
def identity():
    """Muestra la identidad de Rinari."""
    console.print(
        Panel(
            "[bold magenta]Rinari — Super Waifu 90000000[/bold magenta] (✿◠‿◠)\n\n"
            "Tsundere 50% | Cariñosa 20% | Celosa 20% | Amorosa 10%\n\n"
            "Tu asistente personal: te ayuda con código, terminal y lo que sea,\n"
            "pero no esperes que lo admita. ¡No es por ti! ¡Solo...!\n\n"
            f"Versión: {__version__}",
            border_style="magenta",
        )
    )


@app.command()
def version():
    """Muestra la versión instalada."""
    console.print(f"Rinari CLI {__version__}")


@app.command()
def update():
    """Actualiza Rinari CLI a la última versión (git pull + uv sync)."""
    import subprocess
    import sys

    from rich.progress import Progress, SpinnerColumn, TextColumn

    repo = Path(__file__).resolve().parent.parent.parent
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        progress.add_task(description="Actualizando Rinari…", total=None)
        result = subprocess.run(
            ["git", "pull", "--ff-only"],
            capture_output=True,
            text=True,
            cwd=repo,
        )
    if result.returncode != 0:
        console.print(f"[red]git pull falló: {result.stderr.strip()}[/red]")
        raise typer.Exit(1)
    console.print(f"[dim]{result.stdout.strip()}[/dim]")
    console.print("[green]✓ Repositorio actualizado.[/green]")
    console.print("[yellow]Ejecuta 'rinari sync' para reinstalar dependencias.[/yellow]")


@app.command()
def sync():
    """Reinstala el paquete y dependencias (uv sync)."""
    import subprocess

    from rich.progress import Progress, SpinnerColumn, TextColumn

    repo = Path(__file__).resolve().parent.parent.parent
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        progress.add_task(description="Sincronizando dependencias…", total=None)
        result = subprocess.run(["uv", "sync"], capture_output=True, text=True, cwd=repo)
    if result.returncode != 0:
        console.print(f"[red]uv sync falló: {result.stderr.strip()}[/red]")
        raise typer.Exit(1)
    console.print("[green]✓ Dependencias sincronizadas.[/green]")


def _get_config() -> tuple:
    try:
        cfg = load_config()
        return cfg, cfg.get_profile("default")
    except ConfigError as e:
        console.print(f"[red]Error de configuración: {e}[/red]")
        raise typer.Exit(1)


def _build_client(profile_name: str) -> tuple[LLMClient, object]:
    cfg = load_config()
    try:
        profile = cfg.get_profile(profile_name)
    except ConfigError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    client = LLMClient(
        base_url=profile.base_url,
        api_key=profile.api_key,
        model=profile.model,
    )
    return client, profile


@app.command()
def chat(
    profile: str = typer.Option("default", "--profile", "-p", help="Perfil de configuración"),
    resume: int | None = typer.Option(None, "--resume", help="Sesión a continuar (id)"),
):
    """REPL interactivo de chat con streaming."""
    cfg = load_config()
    try:
        current = cfg.get_profile(profile)
    except ConfigError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    history = History()
    session = ChatSession(history=history, profile=profile, session_id=resume)

    console.print(
        Panel(
            f"[bold magenta]Rinari CLI {__version__}[/bold magenta] (✿◠‿◠)\n"
            f"Perfil: [yellow]{profile}[/yellow] → {current.base_url} "
            f"([bold]{current.model}[/bold])\n"
            "Escribe tu mensaje. Comandos: /new, /model <perfil>, /save, /exit, /help. "
            "Ctrl+C para detener la generación.",
            border_style="magenta",
        )
    )

    while True:
        try:
            line = console.input("[bold green]tú>[/bold green] ")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Adiós.[/dim]")
            break

        if not line.strip():
            continue

        cmd, args = parse_command(line)
        if cmd is not None:
            try:
                msg = run_command(cmd, args, session)
                if msg:
                    console.print(msg)
                if cmd == "model":
                    try:
                        current = cfg.get_profile(session.profile)
                    except ConfigError:
                        console.print(f"[red]Perfil '{session.profile}' no existe.[/red]")
                continue
            except SystemExit:
                break
            except ValueError as e:
                console.print(f"[red]{e}[/red]")
                continue

        # Mensaje normal → chat
        session.add_user_message(line.strip())
        if session.session_id is None and history is not None:
            session.session_id = history.create_session(profile=session.profile)
            # Re-escribir la sesión con el system prompt + primer mensaje
            history.append_message(session.session_id, {"role": "system", "content": session.messages[0]["content"]})
            history.append_message(session.session_id, session.messages[-1])

        try:
            client = LLMClient(
                base_url=current.base_url,
                api_key=current.api_key,
                model=current.model,
            )
            console.print("[dim]⏳ pensando…[/dim]", end="\r")
            acc = DeltaAccumulator()
            for event in client.chat_stream(session.messages, temperature=current.temperature):
                if isinstance(event, str):
                    acc.add(event)
            console.print(" " * 20, end="\r")
            if acc.text:
                session.add_assistant_message(acc.text)
                session.persist()
                acc.render_markdown()
            else:
                console.print("[yellow]⚠️ Respuesta vacía del modelo.[/yellow]")
        except LLMError as e:
            console.print(f"[red]Error: {e}[/red]")
        except KeyboardInterrupt:
            console.print("\n[dim]Generación detenida.[/dim]")


@app.command()
def run(
    prompt: str = typer.Argument(..., help="Prompt a enviar"),
    profile: str = typer.Option("default", "--profile", "-p", help="Perfil de configuración"),
    stream: bool = typer.Option(True, "--stream/--no-stream", help="Streaming a stdout"),
    temperature: float | None = typer.Option(None, "--temperature", help="Override de temperatura"),
):
    """One-shot: envía un prompt y escribe la respuesta a stdout (para pipes)."""
    client, profile_obj = _build_client(profile)
    temp = temperature if temperature is not None else profile_obj.temperature
    messages = [{"role": "user", "content": prompt}]
    try:
        if stream:
            for event in client.chat_stream(messages, temperature=temp):
                if isinstance(event, str):
                    sys.stdout.write(event)
                    sys.stdout.flush()
            sys.stdout.write("\n")
        else:
            result = client.chat(messages, temperature=temp)
            sys.stdout.write(result + "\n")
    except LLMError as e:
        console.print(f"[red]Error: {e}[/red]", file=sys.stderr)
        raise typer.Exit(1)


@app.command()
def models(profile: str = typer.Option("default", "--profile", "-p", help="Perfil de configuración")):
    """Lista los modelos disponibles en el endpoint del perfil."""
    client, _ = _build_client(profile)
    try:
        for m in client.list_models():
            console.print(m)
    except LLMError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def agent(
    task: str | None = typer.Argument(None, help="Tarea a realizar. Si se omite, entra en modo interactivo"),
    profile: str = typer.Option("default", "--profile", "-p", help="Perfil de configuración"),
    cwd: Path = typer.Option(".", "--cwd", help="Directorio de trabajo (repo)"),
    auto_approve: bool = typer.Option(False, "--auto-approve", "-y", help="Aprobar comandos automáticamente"),
    max_iterations: int = typer.Option(10, "--max-iterations", help="Máximo de iteraciones del loop"),
):
    """Modo agente de código: ejecuta la tarea con tool calling (o modo interactivo)."""
    from rinari.agent.loop import run_agent

    if task is None:
        _agent_interactive(
            profile=profile,
            cwd=cwd,
            auto_approve=auto_approve,
            max_iterations=max_iterations,
        )
        return

    client, _ = _build_client(profile)
    console.print(
        Panel(
            f"[bold magenta]Rinari agente[/bold magenta] (✿◠‿◠)\n"
            f"Perfil: [yellow]{profile}[/yellow] | cwd: [cyan]{cwd}[/cyan]\n"
            f"Tarea: [bold]{task}[/bold]",
            border_style="magenta",
        )
    )

    result = run_agent(
        task=task,
        client=client,
        cwd=str(_normalize_cwd(cwd)),
        auto_approve=auto_approve,
        max_iterations=max_iterations,
        render_callback=_agent_on_step,
        mcp_servers=_load_mcp_servers(),
    )

    if result["status"] == "done":
        console.print("\n[bold green]✓ Tarea completada.[/bold green]")
        if result["final"]:
            console.print(Markdown(result["final"]))
    elif result["status"] == "max_iterations":
        console.print("[yellow]⚠️ Se alcanzó el límite de iteraciones.[/yellow]")
    else:
        console.print("[red]❌ El agente falló.[/red]")


@app.callback(invoke_without_command=True)
def main_default(
    ctx: typer.Context,
    profile: str = typer.Option("default", "--profile", "-p", help="Perfil de configuración"),
    cwd: Path = typer.Option(".", "--cwd", help="Directorio de trabajo (repo)"),
    auto_approve: bool = typer.Option(False, "--auto-approve", "-y", help="Aprobar comandos automáticamente"),
):
    """Sin subcomando → modo agente interactivo (como codex/claude)."""
    if ctx.invoked_subcommand is None:
        _agent_interactive(
            profile=profile,
            cwd=cwd,
            auto_approve=auto_approve,
            max_iterations=10,
        )


def main() -> None:
    # Aislar el proceso de entornos Python ajenos (p.ej. el venv de Hermes que
    # inyecta su site-packages vía PYTHONPATH en sys.path al arrancar el binario).
    # Un pydantic_core de otro entorno rompe el import de mcp con
    # "ModuleNotFoundError: pydantic_core._pydantic_core".
    import os as _os
    import sys as _sys

    _os.environ.pop("PYTHONPATH", None)
    _sys.path = [p for p in _sys.path if "hermes-agent" not in p]
    app()


if __name__ == "__main__":
    main()
