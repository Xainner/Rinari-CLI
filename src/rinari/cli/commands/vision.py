"""Configure the same image route used by desktop and runtime tools."""

import typer

from rinari.application.vision import configure, settings
from rinari.cli.deps import is_json, services, with_error_handling
from rinari.cli.output import emit_json, success_envelope
from rinari.shared.errors import InvalidUsageError

app = typer.Typer(help="Visual model routing.", no_args_is_help=True)


def output(ctx, command, value):
    if is_json(ctx):
        emit_json(success_envelope(command, value))
    else:
        typer.echo(f"Vision: {value['mode']} · model: {value['model_id'] or 'conversation'}")


@app.command("show")
@with_error_handling("vision.show")
def show(ctx: typer.Context):
    with services(ctx) as service:
        output(ctx, "vision.show", settings(service))


@app.command("set")
@with_error_handling("vision.set")
def set_vision(
    ctx: typer.Context,
    mode: str,
    model: str | None = typer.Option(None, "--model"),
    confirm_unknown: bool = typer.Option(False, "--confirm-unknown", hidden=True),
):
    with services(ctx) as service:
        try:
            value = configure(
                service,
                {
                    **settings(service),
                    "mode": mode,
                    "model_id": model,
                    "confirm_unknown": confirm_unknown,
                },
            )
        except ValueError as exc:
            raise InvalidUsageError(str(exc)) from exc
        output(ctx, "vision.set", value)


@app.command("capability")
@with_error_handling("vision.capability")
def capability(ctx: typer.Context, model: str, value: str):
    """Declare a model's vision capability: auto, supported, unsupported."""
    if value not in {"auto", "supported", "unsupported"}:
        raise InvalidUsageError("Use auto, supported or unsupported")
    with services(ctx) as service:
        config = settings(service)
        ref = service.models.resolve(model).id
        overrides = dict(config.get("model_overrides", {}))
        if value == "auto":
            overrides.pop(ref, None)
        else:
            overrides[ref] = value == "supported"
        output(
            ctx, "vision.capability", configure(service, {**config, "model_overrides": overrides})
        )


@app.command("execution")
@with_error_handling("vision.execution")
def execution(
    ctx: typer.Context,
    concurrency: int | None = typer.Option(None),
    provider: str | None = typer.Option(None),
    model: str | None = typer.Option(None),
    output_tokens: int | None = typer.Option(None),
    inherit: bool = typer.Option(False),
):
    """Configure shared model execution; not a vision-only limit."""
    with services(ctx) as service:
        config = settings(service)
        value = config["execution"]
        if provider:
            ref = service.providers.get(provider).id
            if inherit:
                value["providers"].pop(ref, None)
            elif concurrency is not None:
                value["providers"][ref] = concurrency
        elif concurrency is not None:
            value["max_concurrency"] = concurrency
        if model:
            ref = service.models.resolve(model).id
            if inherit:
                value["models"].pop(ref, None)
            elif output_tokens is not None:
                value["models"][ref] = output_tokens
        elif output_tokens is not None:
            raise InvalidUsageError("--output-tokens requires --model")
        try:
            result = configure(service, {**config, "execution": value})
        except ValueError as exc:
            raise InvalidUsageError(str(exc)) from exc
        if is_json(ctx):
            emit_json(success_envelope("vision.execution", result["execution"]))
        else:
            import json

            typer.echo(json.dumps(result["execution"], ensure_ascii=False, indent=2))
