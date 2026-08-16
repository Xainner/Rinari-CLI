import typer

from rinari import __version__

app = typer.Typer(
    name="rinari",
    help="Rinari — production agent harness.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def root(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """Rinari CLI."""


def main() -> int:
    app()
    return 0
