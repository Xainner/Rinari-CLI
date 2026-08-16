"""`rinari models` - model registry management (docs/commands.md 18)."""

from __future__ import annotations

import typer

from rinari.cli.deps import fail, is_json, services, with_error_handling
from rinari.cli.output import emit_json, success_envelope
from rinari.cli.serializers import discovered_model_dict, model_dict
from rinari.shared.errors import ProviderModelError

app = typer.Typer(help="Manage the model registry.", no_args_is_help=True)


def _provider_alias(s, provider_id: str) -> str | None:
    try:
        return s.providers.get(provider_id).alias
    except Exception:
        return None


@app.command("list")
@with_error_handling("models.list")
def models_list(
    ctx: typer.Context,
    provider: str = typer.Option(None, "--provider", help="Only models of one provider."),
) -> None:
    """List saved models."""
    with services(ctx) as s:
        records = s.models.list(provider)
        current = s.providers.current()
        active_model_id = (
            current.model.id if current is not None and current.model is not None else None
        )
        data = [
            model_dict(
                m,
                provider_alias=_provider_alias(s, m.provider_id),
                active=(m.id == active_model_id),
            )
            for m in records
        ]
        if is_json(ctx):
            emit_json(success_envelope("models.list", data))
            return
        if not records:
            typer.echo("No models saved. Add one with:")
            typer.echo("  rinari models add --provider <alias> --model <id> --name <alias>")
            return
        typer.echo(f"{'ALIAS':<16} {'PROVIDER':<18} {'MODEL ID':<28} {'STATUS':<12} ACTIVE")
        for m in records:
            marker = "*" if m.id == active_model_id else ""
            typer.echo(
                f"{m.alias:<16} {_provider_alias(s, m.provider_id) or '?':<18} "
                f"{m.provider_model_id:<28} {m.availability:<12} {marker}"
            )


@app.command("available")
@with_error_handling("models.available")
def models_available(
    ctx: typer.Context,
    provider: str = typer.Option(
        None, "--provider", help="Provider to query (defaults to the active one)."
    ),
) -> None:
    """Query provider model discovery without saving models automatically."""
    with services(ctx) as s:
        results = s.models.available(provider)
        data = {
            alias: [discovered_model_dict(m) for m in models] for alias, models in results.items()
        }
        if is_json(ctx):
            emit_json(success_envelope("models.available", data))
            return
        for alias, models in results.items():
            typer.echo(f"{alias}: {len(models)} models discovered")
            for m in models:
                caps = ", ".join(k for k in (m.capabilities or {}) if (m.capabilities or {}).get(k))
                typer.echo(
                    f"  {m.provider_model_id}  [{m.availability}]" + (f"  ({caps})" if caps else "")
                )


@app.command("refresh")
@with_error_handling("models.refresh")
def models_refresh(
    ctx: typer.Context,
    provider: str = typer.Option(None, "--provider", help="Only refresh one provider."),
) -> None:
    """Refresh availability. Missing models are marked unavailable, never deleted."""
    with services(ctx) as s:
        results = s.models.refresh(provider)
        data = {
            alias: {
                "saved": r.saved,
                "still_available": r.still_available,
                "marked_unavailable": r.marked_unavailable,
                "discovered": r.discovered,
                "error": r.error,
            }
            for alias, r in results.items()
        }
        if is_json(ctx):
            emit_json(success_envelope("models.refresh", data))
            return
        for alias, r in results.items():
            line = (
                f"{alias}: {r.still_available}/{r.saved} available, "
                f"{r.marked_unavailable} marked unavailable"
            )
            if r.error:
                line += f" (error: {r.error})"
            typer.echo(line)


@app.command("add")
@with_error_handling("models.add")
def models_add(
    ctx: typer.Context,
    provider: str = typer.Option(..., "--provider", help="Provider the model belongs to."),
    model: str = typer.Option(..., "--model", help="Provider-side model ID (e.g. gpt-4o)."),
    name: str = typer.Option(..., "--name", help="Local alias for the model."),
) -> None:
    """Save a model under a local alias for a provider."""
    with services(ctx) as s:
        record = s.models.add(provider, model, name)
        data = model_dict(record, provider_alias=s.providers.get(provider).alias)
        if is_json(ctx):
            emit_json(success_envelope("models.add", data))
            return
        typer.echo(f"Model {record.alias!r} saved on {provider!r} (id {record.id}).")


@app.command("alias")
@with_error_handling("models.alias")
def models_alias(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="Model to rename (alias, provider ID, or model ID)."),
    new_alias: str = typer.Argument(...),
    provider: str = typer.Option(None, "--provider", help="Disambiguate across providers."),
) -> None:
    """Rename a saved model alias. Internal IDs are preserved."""
    with services(ctx) as s:
        record = s.models.alias(ref, new_alias, provider)
        data = model_dict(record, provider_alias=_provider_alias(s, record.provider_id))
        if is_json(ctx):
            emit_json(success_envelope("models.alias", data))
            return
        typer.echo(f"Renamed model -> {new_alias!r} (id {record.id} unchanged).")


@app.command("show")
@with_error_handling("models.show")
def models_show(
    ctx: typer.Context,
    ref: str = typer.Argument(...),
    provider: str = typer.Option(None, "--provider", help="Disambiguate across providers."),
) -> None:
    """Show one saved model's full record."""
    with services(ctx) as s:
        record = s.models.resolve(ref, provider)
        data = model_dict(record, provider_alias=_provider_alias(s, record.provider_id))
        if is_json(ctx):
            emit_json(success_envelope("models.show", data))
            return
        import json

        typer.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))


@app.command("test")
@with_error_handling("models.test")
def models_test(
    ctx: typer.Context,
    ref: str = typer.Argument(...),
    provider: str = typer.Option(None, "--provider", help="Disambiguate across providers."),
) -> None:
    """Validate access and catalog presence for a model."""
    with services(ctx) as s:
        result = s.models.test(ref, provider)
        data = {
            "alias": result.model.alias,
            "provider": result.provider.alias,
            "ok": result.ok,
            "detail": result.detail,
        }
        if not result.ok:
            err = ProviderModelError(result.detail)
            if is_json(ctx):
                fail(ctx, "models.test", err)
            typer.echo(f"FAIL {result.model.alias}: {result.detail}", err=True)
            raise typer.Exit(11)
        if is_json(ctx):
            emit_json(success_envelope("models.test", data))
            return
        typer.echo(f"OK {result.model.alias} on {result.provider.alias}: {result.detail}")


@app.command("remove")
@with_error_handling("models.remove")
def models_remove(
    ctx: typer.Context,
    ref: str = typer.Argument(...),
    provider: str = typer.Option(None, "--provider", help="Disambiguate across providers."),
) -> None:
    """Remove one saved model record. The provider and credentials are kept."""
    with services(ctx) as s:
        record = s.models.remove(ref, provider)
        data = model_dict(record, provider_alias=_provider_alias(s, record.provider_id))
        if is_json(ctx):
            emit_json(success_envelope("models.remove", data))
            return
        typer.echo(f"Removed model {record.alias!r} (id {record.id}).")
