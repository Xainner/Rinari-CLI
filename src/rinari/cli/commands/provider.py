"""`rinari provider` - current provider selection (docs/commands.md 15).

Singular noun = one item. `rinari provider use` changes the active
selection without deleting anything (section 15 invariant).
"""

from __future__ import annotations

import typer

from rinari.application.provider_service import ProviderSelection
from rinari.cli.deps import fail, is_json, services, with_error_handling
from rinari.cli.output import emit_json, success_envelope
from rinari.cli.serializers import provider_dict
from rinari.shared.errors import RinariError

app = typer.Typer(help="Current provider selection. With no subcommand, shows the current one.")


@app.callback(invoke_without_command=True)
def provider_callback(ctx: typer.Context) -> None:
    """Provider selection for the active provider and model."""
    if ctx.invoked_subcommand is None:
        try:
            show_current(ctx, "provider")
        except RinariError as err:
            fail(ctx, "provider", err)


def selection_payload(s, selection: ProviderSelection) -> dict:
    model = None
    if selection.model is not None:
        model = {
            "id": selection.model.id,
            "alias": selection.model.alias,
            "provider_model_id": selection.model.provider_model_id,
        }
    return {
        "provider": provider_dict(
            selection.provider,
            active=True,
            credential_ref=s.providers.credential_ref(selection.provider),
        ),
        "model": model,
    }


def show_current(ctx: typer.Context, command: str) -> None:
    with services(ctx) as s:
        selection = s.providers.current()
        if selection is None:
            if is_json(ctx):
                emit_json(success_envelope(command, None, warnings=["no active provider"]))
                return
            typer.echo(
                "No active provider. Add one: rinari providers add <openai|anthropic|custom>"
            )
            return
        data = selection_payload(s, selection)
        if is_json(ctx):
            emit_json(success_envelope(command, data))
            return
        model = selection.model
        typer.echo(f"Provider  {selection.provider.alias} ({selection.provider.type})")
        if model is not None:
            typer.echo(f"Model     {model.alias} ({model.provider_model_id})")
        else:
            typer.echo("Model     - (none saved for this provider)")


@app.command("current")
@with_error_handling("provider.current")
def provider_current(ctx: typer.Context) -> None:
    """Show the active provider and its active model."""
    show_current(ctx, "provider.current")


@app.command("show")
@with_error_handling("provider.show")
def provider_show(ctx: typer.Context) -> None:
    """Show the active provider's full record."""
    with services(ctx) as s:
        selection = s.providers.current()
        if selection is None:
            from rinari.shared.errors import NotFoundError

            raise NotFoundError(
                "No active provider", hint="Run `rinari provider use <alias>` first."
            )
        data = provider_dict(
            selection.provider,
            active=True,
            credential_ref=s.providers.credential_ref(selection.provider),
        )
        if is_json(ctx):
            emit_json(success_envelope("provider.show", data))
            return
        import json

        typer.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))


@app.command("use")
@with_error_handling("provider.use")
def provider_use(ctx: typer.Context, alias: str = typer.Argument(...)) -> None:
    """Select the active provider. Nothing else is deleted or changed."""
    with services(ctx) as s:
        selection = s.providers.use(alias)
        data = selection_payload(s, selection)
        if is_json(ctx):
            emit_json(success_envelope("provider.use", data))
            return
        typer.echo(f"Active provider: {selection.provider.alias}")
        if selection.model is not None:
            typer.echo(f"Active model:    {selection.model.alias}")
        else:
            typer.echo("Active model:    - (add one with `rinari models add`)")
