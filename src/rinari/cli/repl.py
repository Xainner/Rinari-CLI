"""Interactive REPL for the agent session (phase 2).

Render + input layer only: no business logic. Streaming tokens go to stdout
unbuffered; tool activity is shown as dim one-liners; approvals come from the
approval prompt inside ToolRuntime (typer.prompt on a new line).

Slash commands:
    /exit  /quit      leave the session (work stays in the working tree)
    /help             show commands
    /provider [alias] list providers, or switch the session's provider
    /model [alias]    list the session provider's models, or switch model
    /session          show session id and kind

Cancellation (harness.md phase-2 cancellation spec):
    Ctrl+C during a turn  -> cancels the turn (model stream / tool / subprocess)
    Ctrl+C again          -> exits the REPL
"""

from __future__ import annotations

import sys

import typer
from rich.console import Console
from rich.text import Text

from rinari.cli import agent_runtime
from rinari.cli.agent_runtime import AgentSession
from rinari.shared.errors import InvalidUsageError, RinariError


def _banner(session: AgentSession) -> Text:
    record = session.record
    provider = session.services.providers.get(record.provider_id)
    model = session.services.ctx.model_repo.get(record.model_id) if record.model_id else None
    root = record.project_root_snapshot or f"cwd {record.current_cwd}"
    text = Text()
    text.append("Rinari ", style="bold")
    text.append(f"session {record.id[:12]}  ", style="dim")
    text.append(f"{record.kind.lower()}", style="bold cyan")
    text.append(f"  {root}", style="dim")
    text.append("\n")
    alias = provider.alias if provider is not None else "-"
    model_alias = model.alias if model is not None else "-"
    text.append(f"  {alias} / {model_alias}   ", style="dim")
    text.append("/help for commands, /exit to leave", style="dim")
    return text


def _print_turn_header() -> None:
    typer.echo()


def _on_delta(delta: str) -> None:
    sys.stdout.write(delta)
    sys.stdout.flush()


def _on_tool(phase: str, name: str, detail: object) -> None:
    if phase == "start":
        args = ""
        if isinstance(detail, dict):
            for key in ("path", "pattern", "command"):
                value = detail.get(key)
                if value:
                    args = f" {str(value)[:80]}"
                    break
        typer.echo(f"  [tool] {name}{args} ...", err=False)
    else:
        from rinari.tools.definition import ToolResult

        if isinstance(detail, ToolResult):
            state = "ok" if detail.ok else f"error({detail.error.code.value})"
            typer.echo(f"  [tool] {name} {state} ({detail.duration_ms:.0f}ms)")


def _list_providers(session: AgentSession) -> None:
    for record in session.services.providers.list():
        marker = "*" if record.id == session.record.provider_id else " "
        typer.echo(f" {marker} {record.alias} ({record.type})")


def _list_models(session: AgentSession) -> None:
    for record in session.services.ctx.model_repo.list(session.record.provider_id):
        marker = "*" if record.id == session.record.model_id else " "
        typer.echo(f" {marker} {record.alias}  ({record.provider_model_id})")


def run_repl(session: AgentSession, initial_prompt: str | None = None) -> None:
    console = Console()
    console.print(_banner(session))

    interrupted_this_turn = False
    prompt = initial_prompt

    while True:
        session.token.reset()
        interrupted_this_turn = False
        if prompt is not None:
            message = prompt
            prompt = None
        else:
            message = typer.prompt("rinari>", no_default=True)
            message = message.strip()
            if not message:
                continue

        if message.startswith("/"):
            try:
                if _handle_command(session, message, console):
                    return
            except InvalidUsageError as err:
                typer.echo(f"error: {err.message}", err=True)
            continue

        _print_turn_header()
        try:
            result = agent_runtime.run_turn(session, message, on_delta=_on_delta, on_tool=_on_tool)
        except KeyboardInterrupt:
            # First Ctrl+C: cancel the in-flight turn. Second one: hard exit.
            if interrupted_this_turn:
                console.print(Text("bye.", style="dim"))
                return
            interrupted_this_turn = True
            session.token.cancel()
            sys.stdout.write("\n")
            sys.stdout.flush()
            typer.echo("turn cancelled (Ctrl+C again to exit)", err=True)
            continue
        except RinariError as err:
            typer.echo(f"error: {err.message}", err=True)
            if err.hint:
                typer.echo(f"hint: {err.hint}", err=True)
            continue

        if result.kind == "cancelled":
            typer.echo("turn cancelled", err=True)
        elif result.kind not in ("answer", "truncated"):
            console.print(Text(result.content, style="yellow"))
        else:
            typer.echo()
            if result.kind == "truncated":
                typer.echo("(output stopped at the model's max tokens)", err=True)
        typer.echo()

        if session.promoted_root is not None:
            console.print(
                Text(
                    f"*** session promoted to PROJECT — root: {session.promoted_root} ***",
                    style="bold green",
                )
            )
            session.promoted_root = None
            console.print(_banner(session))


def _handle_command(session: AgentSession, message: str, console) -> bool:
    _, *rest = message.split(maxsplit=1)
    arg = rest[0].strip() if rest and rest[0].strip() else None
    command = message.split()[0].lower()

    if command in ("/exit", "/quit"):
        return True
    if command == "/help":
        typer.echo(
            "\n".join(
                [
                    "/exit | /quit          leave the session",
                    "/help                  this help",
                    "/provider [alias]      list providers / switch (session only)",
                    "/model [alias]         list models / switch (session only)",
                    "/session               show this session",
                    "Ctrl+C                 cancel turn (again: exit)",
                ]
            )
        )
        return False
    if command == "/provider":
        if arg is None:
            _list_providers(session)
            return False
        result = agent_runtime.switch_provider(session, arg)
        typer.echo(f"session now uses {result.provider_alias} / {result.model_alias or '-'}")
        return False
    if command == "/model":
        if arg is None:
            _list_models(session)
            return False
        result = agent_runtime.switch_model(session, arg)
        typer.echo(f"session now uses {result.provider_alias} / {result.model_alias}")
        return False
    if command == "/session":
        typer.echo(f"session {session.record.id} ({session.record.kind})")
        return False
    raise InvalidUsageError(f"Unknown command: {command}", hint="Try /help")
