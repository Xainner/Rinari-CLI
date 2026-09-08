"""Rinari CLI: root app, global flags, and root command dispatch."""

from __future__ import annotations

import typer

from rinari import __version__
from rinari.cli.commands import agents as agents_cmd
from rinari.cli.commands import api as api_cmd
from rinari.cli.commands import approvals as approvals_cmd
from rinari.cli.commands import artifacts as artifacts_cmd
from rinari.cli.commands import cache as cache_cmd
from rinari.cli.commands import checkpoint as checkpoint_cmd
from rinari.cli.commands import code as code_cmd
from rinari.cli.commands import config as config_cmd
from rinari.cli.commands import context as context_cmd
from rinari.cli.commands import engine as engine_cmd
from rinari.cli.commands import evals_cmd
from rinari.cli.commands import export_import as export_import_cmd
from rinari.cli.commands import hooks as hooks_cmd
from rinari.cli.commands import index as index_cmd
from rinari.cli.commands import logs as logs_cmd
from rinari.cli.commands import mcp as mcp_cmd
from rinari.cli.commands import memory as memory_cmd
from rinari.cli.commands import metrics as metrics_cmd
from rinari.cli.commands import model as model_cmd
from rinari.cli.commands import models as models_cmd
from rinari.cli.commands import network as network_cmd
from rinari.cli.commands import permissions as permissions_cmd
from rinari.cli.commands import plugins as plugins_cmd
from rinari.cli.commands import profiles as profiles_cmd
from rinari.cli.commands import project as project_cmd
from rinari.cli.commands import provider as provider_cmd
from rinari.cli.commands import providers as providers_cmd
from rinari.cli.commands import sandbox as sandbox_cmd
from rinari.cli.commands import secrets as secrets_cmd
from rinari.cli.commands import sessions as sessions_cmd
from rinari.cli.commands import skills as skills_cmd
from rinari.cli.commands import system as system_cmd
from rinari.cli.commands import tasks as tasks_cmd
from rinari.cli.commands import tools as tools_cmd
from rinari.cli.commands import trace as trace_cmd
from rinari.cli.commands import trust as trust_cmd
from rinari.cli.commands import undo as undo_cmd
from rinari.cli.commands import update_cmd as update_cmd
from rinari.cli.commands import work as work_cmd
from rinari.cli.deps import CliParams, fail, set_params
from rinari.cli.session_flow import start_flow
from rinari.shared.errors import RinariError

app = typer.Typer(
    name="rinari",
    help="Rinari - production agent harness.",
    add_completion=False,
)

app.add_typer(artifacts_cmd.app, name="artifacts")
app.add_typer(config_cmd.app, name="config")
app.add_typer(context_cmd.app, name="context")
app.add_typer(providers_cmd.app, name="providers")
app.add_typer(provider_cmd.app, name="provider")
app.add_typer(models_cmd.app, name="models")
app.add_typer(model_cmd.app, name="model")
app.add_typer(sessions_cmd.session_app, name="session")
app.add_typer(trust_cmd.app, name="trust")
app.add_typer(index_cmd.app, name="index")
app.add_typer(tasks_cmd.app, name="tasks")
app.add_typer(memory_cmd.app, name="memory")
app.add_typer(network_cmd.app, name="network")
app.add_typer(undo_cmd.app, name="undo")
app.add_typer(plugins_cmd.app, name="plugins")
app.add_typer(mcp_cmd.app, name="mcp")
app.add_typer(api_cmd.app, name="api")
app.add_typer(hooks_cmd.app, name="hooks")
app.add_typer(skills_cmd.app, name="skills")
app.add_typer(agents_cmd.app, name="agents")
app.add_typer(profiles_cmd.app, name="profiles")
app.add_typer(project_cmd.app, name="project")
app.add_typer(checkpoint_cmd.app, name="checkpoint")
app.add_typer(permissions_cmd.app, name="permissions")
app.add_typer(approvals_cmd.app, name="approvals")
app.add_typer(sandbox_cmd.app, name="sandbox")
app.add_typer(secrets_cmd.app, name="secrets")
app.add_typer(tools_cmd.app, name="tools")
app.command("engine", help="Machine transport for desktop clients.")(engine_cmd.engine)
app.command("code", help="Open this project in Rinari Code (desktop).")(code_cmd.code)
app.command("trace", help="Inspect a session's event trace.")(trace_cmd.trace)
app.add_typer(logs_cmd.app, name="logs")
app.add_typer(metrics_cmd.app, name="metrics")
app.add_typer(cache_cmd.app, name="cache")
app.add_typer(system_cmd.system_app, name=None)

app.command("chat")(sessions_cmd.chat_cmd)
app.command("resume")(sessions_cmd.resume_cmd)
app.add_typer(export_import_cmd.export_app, name="export")
app.add_typer(export_import_cmd.import_app, name="import")
app.command("update")(update_cmd.update)
app.command("ask")(work_cmd.ask)
app.command("plan")(work_cmd.plan)
app.command("agent")(work_cmd.agent)
app.command("review")(work_cmd.review)
app.command("run")(work_cmd.run)
app.command("stop")(work_cmd.stop)
app.command("verify")(work_cmd.verify)
app.add_typer(evals_cmd.eval_app, name="eval")


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
    no_banner: bool = typer.Option(
        False,
        "--no-banner",
        help="Skip the startup banner and session header.",
        is_eager=True,
    ),
    no_progress: bool = typer.Option(
        False,
        "--no-progress",
        help="Hide live tool progress lines (final status rail stays).",
        is_eager=True,
    ),
) -> None:
    """Rinari CLI. With no subcommand, starts the session for the current context."""
    set_params(
        ctx,
        CliParams(
            config=config, json_output=json_output, no_banner=no_banner, no_progress=no_progress
        ),
    )
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


ROOT_FLAG_TOKENS = {"-h", "--help", "-V", "--version", "--json", "--no-banner", "--no-progress"}
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
    from rinari.cli.text import configure_utf8_stdio

    configure_utf8_stdio()
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
