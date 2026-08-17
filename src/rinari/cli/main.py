"""Rinari CLI: root app, global flags, and root command dispatch."""

from __future__ import annotations

import typer

from rinari import __version__
from rinari.cli.commands import config as config_cmd
from rinari.cli.commands import model as model_cmd
from rinari.cli.commands import models as models_cmd
from rinari.cli.commands import provider as provider_cmd
from rinari.cli.commands import providers as providers_cmd
from rinari.cli.commands import sessions as sessions_cmd
from rinari.cli.commands import system as system_cmd
from rinari.cli.deps import CliParams, fail, set_params
from rinari.cli.session_flow import start_flow
from rinari.shared.errors import RinariError

app = typer.Typer(
    name="rinari",
    help="Rinari - production agent harness.",
    add_completion=False,
)

app.add_typer(config_cmd.app, name="config")
app.add_typer(providers_cmd.app, name="providers")
app.add_typer(provider_cmd.app, name="provider")
app.add_typer(models_cmd.app, name="models")
app.add_typer(model_cmd.app, name="model")
app.add_typer(sessions_cmd.session_app, name="session")
app.add_typer(system_cmd.system_app, name=None)

app.command("chat")(sessions_cmd.chat_cmd)
app.command("resume")(sessions_cmd.resume_cmd)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
    config: str = typer.Option(
        None,
        "--config",
        help="Use an explicit config file instead of ~/.rinari/config.toml.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the machine output envelope (docs/commands.md section 67).",
        is_eager=True,
    ),
) -> None:
    """Rinari CLI. With no subcommand, starts the session for the current context."""
    set_params(ctx, CliParams(config=config, json_output=json_output))
    if ctx.invoked_subcommand is None:
        try:
            start_flow(ctx, None, forced_chat=False, command="session.start")
        except RinariError as err:
            fail(ctx, "session.start", err)


@app.command("_session", hidden=True)
def root_session(
    ctx: typer.Context,
    prompt: list[str] = typer.Argument(
        None, help="Prompt for the session (runs one agent turn, then the REPL if attached)."
    ),
) -> None:
    """Hidden: root default-command target (see main())."""
    text = " ".join(prompt or ()).strip() if prompt else None
    try:
        start_flow(ctx, text, forced_chat=False, command="session.start")
    except RinariError as err:
        fail(ctx, "session.start", err)


ROOT_FLAG_TOKENS = {"-h", "--help", "-V", "--version", "--json"}
ROOT_VALUE_TOKENS = {"--config"}


def dispatch_root_command(args: list[str]) -> list[str]:
    """Route `rinari <prompt>` to the hidden `_session` command.

    Tokens before the subcommand position are preserved (e.g. `--json fix`),
    value-taking root flags consume their value, and any known subcommand
    stops the scan.
    """
    import typer.main as _typer_main

    known = set(_typer_main.get_command(app).commands)
    i = 0
    while i < len(args):
        token = args[i]
        if token in ROOT_FLAG_TOKENS:
            i += 1
            continue
        if token in ROOT_VALUE_TOKENS:
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        if token in known:
            break
        return [*args[:i], "_session", *args[i:]]
    return args


def main() -> int:
    # Register typer's shell-completion classes so `_RINARI_COMPLETE=...` works.
    from typer._completion_classes import completion_init

    completion_init()
    import sys as _sys

    from rinari.cli import deps as _deps

    raw = list(_sys.argv[1:])
    argv = [a for a in raw if a != "--json"]
    if len(argv) != len(raw):
        _deps.set_global_json(True)
    try:
        app(dispatch_root_command(argv))
    except RinariError as err:
        fail(None, "session.start", err)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
