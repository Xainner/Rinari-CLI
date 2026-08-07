"""CLI de qwencli — typer entrypoints.

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
from rich.panel import Panel
from rich.text import Text

from qwencli import __version__
from qwencli.client import LLMClient, LLMError
from qwencli.config import ConfigError, load_config
from qwencli.history import History
from qwencli.render import DeltaAccumulator
from qwencli.repl import ChatSession, parse_command, run_command

app = typer.Typer(add_completion=False, help="qwencli — tu CLI con LLM contra tus modelos locales.")
console = Console()


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
            f"[bold cyan]qwencli {__version__}[/bold cyan]\n"
            f"Perfil: [yellow]{profile}[/yellow] → {current.base_url} "
            f"([bold]{current.model}[/bold])\n"
            "Escribe tu mensaje. Comandos: /new, /model <perfil>, /save, /exit, /help. "
            "Ctrl+C para detener la generación.",
            border_style="cyan",
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
    task: str = typer.Argument(..., help="Tarea a realizar"),
    profile: str = typer.Option("default", "--profile", "-p", help="Perfil de configuración"),
    cwd: Path = typer.Option(".", "--cwd", help="Directorio de trabajo (repo)"),
):
    """Modo agente de código: ejecuta la tarea con tool calling."""
    from qwencli.agent.loop import run_agent

    client, profile_obj = _build_client(profile)
    run_agent(
        task=task,
        client=client,
        profile=profile_obj,
        cwd=str(cwd),
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
