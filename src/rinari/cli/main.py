import typer

from rinari import __version__
from rinari.cli.commands.config import app as config_app
from rinari.cli.deps import CliParams, set_params
from rinari.shared.errors import RinariError

app = typer.Typer(
    name="rinari",
    help="Rinari — production agent harness.",
    no_args_is_help=True,
    add_completion=False,
)

app.add_typer(config_app, name="config")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
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
) -> None:
    """Rinari CLI."""
    set_params(ctx, CliParams(config=config))


def main() -> int:
    try:
        app()
    except RinariError as err:
        typer.echo(f"error: {err.message}", err=True)
        if err.hint:
            typer.echo(f"hint: {err.hint}", err=True)
        return int(err.code)
    return 0
