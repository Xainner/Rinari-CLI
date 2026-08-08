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
from rinari.history import History, HistoryError
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
    """Renderiza un paso del agente en vivo, con spinner mientras la tool corre."""
    from rinari.render import ToolSpinner, render_status, render_tool_result

    t = step["type"]
    if t == "tool_call":
        name = step.get("name", "")
        args = step.get("arguments", {})
        cmd = args.get("command") or args.get("path") or ""
        label = f"{name} {cmd}".strip()
        render_status(f"🔧 {label}", style="cyan")
        # spinner animado mientras la tool se ejecuta
        spinner = ToolSpinner()
        spinner.start(f"⏳ {name} ejecutando…")
        _agent_on_step._spinner = spinner
    elif t == "tool_result":
        # detener el spinner antes de pintar el resultado
        spinner = getattr(_agent_on_step, "_spinner", None)
        if spinner is not None:
            spinner.stop()
            _agent_on_step._spinner = None
        result = step.get("result", {})
        render_tool_result(result)
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

    from rinari.history import History
    from rinari.ui import (
        check_endpoint_health,
        count_tools,
        git_info,
        measure_endpoint_latency,
        render_welcome,
    )

    # Health check rápido del endpoint (no bloquea si tarda)
    latency_ms = None
    endpoint_ok = False
    try:
        probe_client = _make_client(current)
        endpoint_ok = check_endpoint_health(probe_client)
        if endpoint_ok:
            latency_ms = measure_endpoint_latency(probe_client)
    except Exception:  # noqa: BLE001
        endpoint_ok = False

    sessions_count = 0
    try:
        sessions_count = len(History().list_sessions(limit=100))
    except Exception:  # noqa: BLE001
        pass

    render_welcome(
        profile=current_profile,
        model=current.model,
        base_url=current.base_url,
        repo_name=repo_name,
        git=git_info(workdir),
        endpoint_ok=endpoint_ok,
        sessions_count=sessions_count,
        latency_ms=latency_ms,
        tools_count=count_tools(),
    )

    def build_client(profile_name: str):
        nonlocal current
        current = cfg.get_profile(profile_name)
        return _make_client(current)

    while True:
        try:
            line = console.input(f"[bold magenta]rinari@{repo_name}[/bold magenta] > ")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Adiós[/dim]")
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
            "[bold magenta]Rinari — tu maid de la terminal[/bold magenta]\n\n"
            "Atenta y cariñosa | Super productiva | Humor seco | Siempre dispuesta a ayudar\n\n"
            "Tu asistente personal: te ayuda con código, terminal y lo que sea, "
            "y lo hace bien.\n\n"
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


def _make_client(profile) -> LLMClient:
    """Crea un LLMClient desde un Profile (respeta el provider)."""
    return LLMClient(
        base_url=profile.base_url,
        api_key=profile.api_key,
        model=profile.model,
        provider=getattr(profile, "provider", "openai") or "openai",
    )


def _build_client(profile_name: str) -> tuple[LLMClient, object]:
    cfg = load_config()
    try:
        profile = cfg.get_profile(profile_name)
    except ConfigError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    return _make_client(profile), profile


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

    from rinari.ui import render_logo_compact

    console.print(
        Panel(
            f"[bold magenta]{render_logo_compact()}[/bold magenta]\n\n"
            f"[bold magenta]Rinari CLI {__version__}[/bold magenta]\n"
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
                msg = run_command(cmd, args, session, config_dir=cfg.path.parent)
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
            client = _make_client(current)
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
    from rinari.identity import build_chat_prompt

    client, profile_obj = _build_client(profile)
    temp = temperature if temperature is not None else profile_obj.temperature
    messages = [
        {"role": "system", "content": build_chat_prompt()},
        {"role": "user", "content": prompt},
    ]
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


def pick_model_index(models: list[dict], choice: str) -> str:
    """Devuelve el id del modelo según el índice elegido por el usuario."""
    try:
        idx = int(choice.strip())
    except ValueError as e:
        raise ConfigError(f"'{choice}' no es un número válido.") from e
    if not (0 <= idx < len(models)):
        raise ConfigError(f"Índice fuera de rango: 0–{len(models) - 1}.")
    return models[idx]["id"]


def pick_provider(choice: str) -> str:
    """Devuelve el nombre del provider por el índice elegido."""
    from rinari.config import PROVIDERS

    try:
        idx = int(choice.strip())
    except ValueError as e:
        raise ConfigError(f"'{choice}' no es un número válido.") from e
    names = list(PROVIDERS)
    if not (0 <= idx < len(names)):
        raise ConfigError(f"Índice fuera de rango: 0–{len(names) - 1}.")
    return names[idx]


def format_providers() -> str:
    """Lista los providers numerados con su descripción (para el wizard)."""
    from rinari.config import PROVIDERS

    lines = []
    for i, (name, spec) in enumerate(PROVIDERS.items()):
        lines.append(f"  [bold]{i}[/bold] → {name} — {spec['description']}")
    return "\n".join(lines)


def format_model_list(models: list[dict]) -> str:
    """Numera los modelos para mostrarlos en el wizard."""
    lines = []
    for i, m in enumerate(models):
        mid = m.get("id", "?")
        owner = m.get("owned_by")
        extra = f" [dim]({owner})[/dim]" if owner else ""
        lines.append(f"  [bold]{i}[/bold] → {mid}{extra}")
    return "\n".join(lines)


@app.command()
def models(profile: str = typer.Option("default", "--profile", "-p", help="Perfil de configuración")):
    """Lista los modelos disponibles en el endpoint del perfil (activo marcado)."""
    client, prof = _build_client(profile)
    try:
        detailed = client.list_models_detailed()
    except LLMError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)
    if not detailed:
        console.print("[yellow]El endpoint no devolvió modelos.[/yellow]")
        raise typer.Exit(0)
    console.print(
        Panel(
            f"[bold magenta]Modelos en '{profile}'[/bold magenta]\n"
            f"  Activo: [bold]{prof.model}[/bold]\n\n"
            + format_model_list(detailed),
            border_style="magenta",
        )
    )


model_app = typer.Typer(help="Gestiona el modelo del perfil: sin args abre el selector")


def _model_list_models(base_url: str, api_key: str | None, provider: str = "openai") -> list[dict]:
    """Lista modelos del endpoint para el picker (inyectable en tests)."""
    from rinari.client import LLMClient

    client = LLMClient(base_url=base_url, api_key=api_key, model="", provider=provider)
    try:
        return client.list_models_detailed()
    except Exception:  # noqa: BLE001 — el picker cae a nombre manual
        return []


@model_app.callback(invoke_without_command=True)
def _model_picker(
    ctx: typer.Context,
    profile: str = typer.Option("default", "--profile", "-p", help="Perfil de configuración"),
):
    """Sin subcomando: selector interactivo de modelos del endpoint."""
    from rinari.config import load_config, set_profile_model

    # si viene un subcomando (set), el subcomando lo maneja
    if ctx.invoked_subcommand:
        return

    cfg = load_config()
    try:
        current = cfg.get_profile(profile)
    except ConfigError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    console.print(f"[bold magenta]Modelos en '{profile}'[/bold magenta]\n")
    console.print(f"  Activo: [bold]{current.model}[/bold]\n")

    models = _model_list_models(current.base_url, current.api_key, current.provider)
    if models:
        console.print(format_model_list(models))
        try:
            choice = input("\nElige el número del modelo (o escribe uno a mano): ").strip()
        except EOFError:
            choice = ""
        try:
            model_id = pick_model_index(models, choice) if choice.isdigit() else (choice or current.model)
        except ConfigError:
            model_id = choice or current.model
    else:
        console.print("[yellow]El endpoint no devolvió modelos. Escribe el nombre a mano.[/yellow]")
        try:
            model_id = input("Modelo: ").strip()
        except EOFError:
            model_id = ""
        if not model_id:
            console.print("[red]Sin modelo, no hay nada que hacer. Abortando.[/red]")
            raise typer.Exit(1)

    set_profile_model(cfg.path.parent, profile, model_id)
    console.print(
        f"[green]✓ Modelo de '{profile}' actualizado:[/green] "
        f"[bold]{current.model}[/bold] → [bold]{model_id}[/bold]"
    )


@model_app.command("set")
def _model_set_cmd(
    model_name: str = typer.Argument(..., help="Nombre del modelo"),
    profile: str = typer.Option("default", "--profile", "-p", help="Perfil de configuración"),
):
    """Cambia el modelo del perfil y guarda el config."""
    from rinari.config import load_config, set_profile_model

    cfg = load_config()
    try:
        current = cfg.get_profile(profile)
    except ConfigError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    set_profile_model(cfg.path.parent, profile, model_name)
    console.print(
        f"[green]✓ Modelo de '{profile}' actualizado:[/green] "
        f"[bold]{current.model}[/bold] → [bold]{model_name}[/bold]"
    )


app.add_typer(model_app, name="model")


def _open_history() -> History:
    """Abre el historial de sesiones (helper inyectable en tests)."""
    return History()


def _session_preview(hist: History, session_id: int, limit: int = 3) -> str:
    """Primeros mensajes de la sesión como preview de una línea."""
    try:
        messages = hist.get_messages(session_id)
    except HistoryError:
        return ""
    preview = ""
    for m in messages:
        content = (m.get("content") or "").strip().replace("\n", " ")
        if content and m.get("role") != "system":
            preview = content[:60]
            break
    return preview


history_app = typer.Typer(help="Historial de conversaciones: listar, ver, borrar, exportar")


@history_app.callback(invoke_without_command=True)
def _history_list(
    ctx: typer.Context,
    limit: int = typer.Option(10, "--limit", "-l", help="Máximo de sesiones a listar"),
):
    """Sin subcomando: lista las sesiones guardadas."""
    if ctx.invoked_subcommand:
        return
    hist = _open_history()
    try:
        sessions = hist.list_sessions(limit=limit)
        if not sessions:
            console.print("[yellow]Sin sesiones guardadas todavía.[/yellow]")
            console.print("  Chatea con `rinari chat` y se guardarán solas. (o usa /save)")
            return
        lines = []
        for s in sessions:
            preview = _session_preview(hist, s["id"])
            extra = f" — {preview}" if preview else ""
            lines.append(
                f"  [bold]{s['id']}[/bold] · [cyan]{s['profile']}[/cyan] · "
                f"[dim]{s['created_at'][:19].replace('T', ' ')}[/dim] · "
                f"{s['message_count']} msgs{extra}"
            )
    finally:
        hist.close()
    console.print("[bold magenta]Sesiones guardadas:[/bold magenta]")
    console.print("\n".join(lines))
    console.print("\n[dim]Usa `rinari history show <id>`, `history rm <id>` o "
                  "`history export <id>`.[/dim]")


@history_app.command("show")
def _history_show(session_id: int = typer.Argument(..., help="ID de la sesión")):
    """Muestra la conversación completa de una sesión."""
    hist = _open_history()
    try:
        md = hist.export_session(session_id)
    except HistoryError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    finally:
        hist.close()
    console.print(Markdown(md))


@history_app.command("rm")
def _history_rm(
    session_id: int = typer.Argument(..., help="ID de la sesión"),
    yes: bool = typer.Option(False, "--yes", "-y", help="No pedir confirmación"),
):
    """Borra una sesión (pide confirmación salvo -y)."""
    hist = _open_history()
    try:
        hist.get_messages(session_id)  # valida que exista
    except HistoryError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    if not yes:
        try:
            confirm = input(f"¿Borrar la sesión {session_id}? [s/N]: ").strip().lower()
        except EOFError:
            confirm = ""
        if confirm not in ("s", "si", "y", "yes"):
            console.print("[dim]Cancelado.[/dim]")
            return
    hist.delete_session(session_id)
    hist.close()
    console.print(f"[green]✓ Sesión {session_id} borrada.[/green]")


@history_app.command("export")
def _history_export(
    session_id: int = typer.Argument(..., help="ID de la sesión"),
    output: Path = typer.Option(None, "--output", "-o", help="Archivo de salida (default: conversacion-<id>.md)"),
):
    """Exporta una sesión a markdown."""
    hist = _open_history()
    try:
        md = hist.export_session(session_id)
    except HistoryError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    finally:
        hist.close()
    out = output or Path(f"conversacion-{session_id}.md")
    out.write_text(md, encoding="utf-8")
    console.print(f"[green]✓ Exportada a [bold]{out}[/bold][/green]")


app.add_typer(history_app, name="history")


def diagnose_profile(name: str, prof: dict, make_client=None) -> tuple[bool, str]:
    """Diagnostica un perfil: expansión de env, conexión al endpoint, modelos.

    Devuelve (ok, mensaje). make_client se inyecta en tests.
    """
    from rinari.config import ConfigError, _expand_env

    try:
        api_key = _expand_env(prof.get("api_key") or "") or None
    except ConfigError as e:
        return False, f"env rota: {e}"

    client = make_client or LLMClient(
        base_url=prof["base_url"], api_key=api_key, model=prof.get("model", ""),
    )
    # listar modelos directamente: lanza LLMError con el detalle real si cae
    try:
        models = client.list_models_detailed()
    except Exception as e:  # noqa: BLE001
        return False, f"endpoint caído: {e}"
    active = prof.get("model")
    in_list = any(m.get("id") == active for m in models) if models else False
    if active and models and not in_list:
        # alias probable: llama.cpp acepta cualquier nombre aunque liste otro
        # (1 modelo listado, activo distinto = alias del servidor)
        if len(models) == 1:
            return True, (f"⚠ {len(models)} modelo(s) listado: '{models[0].get('id')}' "
                          f"— el activo '{active}' es un alias (funciona igual)")
        return False, f"modelo activo '{active}' no está en el endpoint ({len(models)} modelos)"
    return True, f"{len(models)} modelo(s), activo: {active or '—'}"


@app.command()
def doctor():
    """Diagnostica la configuración: revisa todos los perfiles y endpoints."""
    from rinari.config import load_config

    cfg = load_config()
    all_ok = True
    console.print("[bold magenta]rinari doctor[/bold magenta]\n")

    # perfiles a revisar: default + los nombrados
    checks = [("default", cfg.default)]
    checks += [(name, prof) for name, prof in sorted(cfg.profiles.items())]

    for name, prof in checks:
        prof_dict = {
            "base_url": prof.base_url,
            "model": prof.model,
            "api_key": prof.api_key,
        }
        ok, msg = diagnose_profile(name, prof_dict)
        if not ok:
            all_ok = False
        icon = "[green]✓[/green]" if ok else "[red]✗[/red]"
        label = f"[bold]{name}[/bold]" if name == "default" else f"[cyan]{name}[/cyan]"
        console.print(f"  {icon} {label}: {msg}")

    if all_ok:
        console.print("\n[green]✓ Todo en orden. Rinari está lista.[/green]")
    else:
        console.print("\n[red]✗ Hay perfiles con problemas.[/red] "
                      "[yellow]Revisa arriba o usa `rinari setup` para corregir.[/yellow]")
        raise typer.Exit(1)


def _setup_list_models(base_url: str, api_key: str | None, provider: str = "openai") -> list[dict]:
    """Lista modelos del endpoint para el wizard (inyectable en tests)."""
    from rinari.client import LLMClient

    client = LLMClient(base_url=base_url, api_key=api_key, model="", provider=provider)
    return client.list_models_detailed()


@app.command()
def setup(
    profile: str = typer.Option(None, "--name", "-n", help="Nombre del perfil (default: 'default')"),
    base_url: str = typer.Option(None, "--base-url", help="Endpoint del proveedor"),
    api_key: str = typer.Option(None, "--api-key", help="API key (o deja vacío)"),
    provider: str = typer.Option(None, "--provider", help="Proveedor (openai, anthropic, local…)"),
):
    """Wizard interactivo: nombre, proveedor, endpoint, modelo — crea el perfil."""
    import os

    from rinari.config import PROVIDERS, set_profile_model, set_user_name
    from rinari.ui import render_logo_compact

    console.print(render_logo_compact())
    console.print("\n[bold magenta]Setup de Rinari[/bold magenta]\n")

    cfg = load_config()

    # 0. nombre del usuario (se guarda en [user] y Rinari lo usa)
    existing_name = cfg.user_name
    try:
        name_input = input(
            f"¿Cómo te llamas? [default: {existing_name}]: " if existing_name else "¿Cómo te llamas?: "
        ).strip()
    except EOFError:
        name_input = ""
    user_name = name_input or existing_name or "Xainner"
    if user_name != existing_name:
        set_user_name(cfg.path.parent, user_name)
        console.print(f"[dim]Guardado: te llamarás {user_name} para Rinari.[/dim]\n")

    # 0b. nombre del perfil (pregunta salvo que venga --name)
    if profile is None:
        try:
            name = input("Nombre del perfil [default: default]: ").strip() or "default"
        except EOFError:
            name = "default"
    else:
        name = profile

    # 1. elegir provider (salta si viene --provider)
    if provider is None:
        console.print("[bold]¿Qué proveedor usas?[/bold]")
        console.print(format_providers() + "\n")
        try:
            choice = input("Elige el número del provider: ").strip()
        except EOFError:
            choice = ""
        if not choice:
            # default: el provider del perfil actual, o local
            try:
                provider = cfg.get_profile(name).provider or "openai"
            except ConfigError:
                provider = "local"
        else:
            try:
                provider = pick_provider(choice)
            except ConfigError as e:
                console.print(f"[red]{e}[/red]")
                raise typer.Exit(1)
    provider = provider or "openai"
    if provider not in PROVIDERS:
        console.print(f"[red]Provider '{provider}' desconocido. "
                      f"Válidos: {', '.join(PROVIDERS)}[/red]")
        raise typer.Exit(1)
    spec = PROVIDERS[provider]

    # 1. base_url (default: el del provider, o el perfil actual)
    if base_url is None:
        default_url = spec["base_url"] or ""
        try:
            current = cfg.get_profile(name)
            if current.base_url and current.provider == provider:
                default_url = current.base_url
        except ConfigError:
            pass
        try:
            prompt = f"Endpoint [default: {default_url}]: " if default_url else "Endpoint: "
            base_url = input(prompt).strip()
        except EOFError:
            base_url = ""
        if not base_url:
            if not default_url:
                console.print("[red]Necesito un endpoint. Usa --base-url o elige un provider "
                              "con endpoint por defecto.[/red]")
                raise typer.Exit(1)
            base_url = default_url

    # 2. api_key: env var del provider si existe, si no pregunta
    if api_key is None:
        api_key = os.environ.get(spec["env_var"]) if spec["env_var"] else None
        if not api_key:
            try:
                api_key = input("API key (vacío si no requiere): ").strip() or None
            except EOFError:
                api_key = None

    # 3. conectar y listar modelos reales
    from rinari.client import LLMError

    console.print(f"\n[cyan]Conectando a {base_url}…[/cyan]")
    try:
        models = _setup_list_models(base_url, api_key or None, provider=provider)
    except LLMError as e:
        console.print(f"[red]✗ No se pudo listar modelos: {e}[/red]")
        raise typer.Exit(1)
    if not models:
        console.print("[red]✗ El endpoint no devolvió modelos.[/red]")
        raise typer.Exit(1)

    console.print(f"[green]✓ {len(models)} modelo(s) encontrado(s):[/green]\n")
    console.print(format_model_list(models))

    # 4. elegir modelo
    try:
        choice = input("\nElige el número del modelo: ").strip()
    except EOFError:
        choice = ""
    try:
        model_id = pick_model_index(models, choice or "0")
    except ConfigError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    # 5. guardar
    set_profile_model(
        cfg.path.parent, name, model_id, base_url=base_url, api_key=api_key,
        provider=provider,
    )
    console.print(
        f"\n[green]✓ Perfil '{name}' listo:[/green] {provider} → {base_url} → [bold]{model_id}[/bold]\n"
        f"  Pruébalo con: [bold]rinari run \"hola\" --profile {name}[/bold]"
    )


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
            f"[bold magenta]Rinari agente[/bold magenta]\n"
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
