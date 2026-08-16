"""`rinari model` - current model selection (docs/commands.md 19)."""

from __future__ import annotations

import typer

from rinari.application.model_service import ResolvedModel
from rinari.cli.deps import fail, is_json, services, with_error_handling
from rinari.cli.output import emit_json, success_envelope
from rinari.shared.errors import NotFoundError, RinariError

app = typer.Typer(help="Current model selection. With no subcommand, shows the current one.")


def current_model_payload(s, model=None) -> dict:
    if model is None:
        return None
    data = {
        "id": model.id,
        "alias": model.alias,
        "provider_model_id": model.provider_model_id,
        "availability": model.availability,
    }
    provider = s.providers.get(model.provider_id)
    data["provider"] = provider.alias
    return data


def resolved_payload(s, resolved: ResolvedModel) -> dict:
    return {
        "model": current_model_payload(s, resolved.model),
        "switched_provider": resolved.switched_provider,
    }


def show_model(ctx: typer.Context, command: str) -> None:
    with services(ctx) as s:
        selection = s.providers.current()
        if selection is None or selection.model is None:
            if is_json(ctx):
                emit_json(success_envelope(command, None, warnings=["no active model"]))
                return
            typer.echo("No active model. Select one: rinari model use <alias-or-id>")
            return
        data = current_model_payload(s, selection.model)
        if is_json(ctx):
            emit_json(success_envelope(command, data))
            return
        typer.echo(f"Provider  {selection.provider.alias}")
        typer.echo(f"Model     {selection.model.alias} ({selection.model.provider_model_id})")


@app.callback(invoke_without_command=True)
def model_callback(ctx: typer.Context) -> None:
    """Model selection for the active provider."""
    if ctx.invoked_subcommand is None:
        try:
            show_model(ctx, "model")
        except RinariError as err:
            fail(ctx, "model", err)


@app.command("current")
@with_error_handling("model.current")
def model_current(ctx: typer.Context) -> None:
    """Show the active model and its provider."""
    show_model(ctx, "model.current")


@app.command("show")
@with_error_handling("model.show")
def model_show(ctx: typer.Context) -> None:
    """Show the active model record."""
    with services(ctx) as s:
        selection = s.providers.current()
        if selection is None or selection.model is None:
            raise NotFoundError(
                "No active model", hint="Run `rinari model use <alias-or-id>` first."
            )
        from rinari.cli.serializers import model_dict

        data = model_dict(selection.model, provider_alias=selection.provider.alias, active=True)
        if is_json(ctx):
            emit_json(success_envelope("model.show", data))
            return
        import json

        typer.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))


@app.command("use")
@with_error_handling("model.use")
def model_use(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Model alias, provider model ID, or internal ID."),
    provider: str = typer.Option(None, "--provider", help="Disambiguate across providers."),
) -> None:
    """Select the active model. Switches provider explicitly when needed."""
    with services(ctx) as s:
        resolved = s.models.use(ref, provider)
        data = resolved_payload(s, resolved)
        if is_json(ctx):
            emit_json(success_envelope("model.use", data))
            return
        if resolved.switched_provider:
            typer.echo(f"Provider switched: {resolved.provider.alias}")
        typer.echo(f"Active provider: {resolved.provider.alias}")
        typer.echo(f"Active model:    {resolved.model.alias}")


@app.command("reset")
@with_error_handling("model.reset")
def model_reset(
    ctx: typer.Context,
    provider: str = typer.Option(None, "--provider", help="Provider whose default to restore."),
) -> None:
    """Reset to the provider default/last-used model."""
    with services(ctx) as s:
        resolved = s.models.reset(provider)
        if resolved is None:
            if is_json(ctx):
                emit_json(success_envelope("model.reset", None, warnings=["no fallback model"]))
                return
            typer.echo("Reset to provider default (no saved model found).")
            return
        data = resolved_payload(s, resolved)
        if is_json(ctx):
            emit_json(success_envelope("model.reset", data))
            return
        typer.echo(f"Active provider: {resolved.provider.alias}")
        typer.echo(f"Active model:    {resolved.model.alias} (provider default)")
