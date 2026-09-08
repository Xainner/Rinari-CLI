"""`rinari models` - model registry management (docs/commands.md 18)."""

from __future__ import annotations

import os

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from rinari.cli.deps import fail, is_json, services, with_error_handling
from rinari.cli.output import emit_json, success_envelope
from rinari.cli.serializers import discovered_model_dict, model_dict
from rinari.providers.adapters.http import is_opencode_endpoint
from rinari.providers.catalog import OPENCODE_RESPONSES_MODELS
from rinari.shared.errors import InvalidUsageError, ProviderModelError

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


@app.command("pick")
@with_error_handling("models.pick")
def models_pick(
    ctx: typer.Context,
    provider: str = typer.Option(
        None, "--provider", help="Provider alias to pick from (defaults to a menu)."
    ),
    model_name: str = typer.Option(
        None, "--name", help="Alias for the chosen model (defaults to the model ID)."
    ),
    non_interactive: bool = typer.Option(
        False, "--non-interactive", help="Fail instead of prompting."
    ),
) -> None:
    """Interactive picker: choose a provider, discover its real models, activate one."""
    console = Console()
    with services(ctx) as s:
        if is_json(ctx):
            data = {
                alias: [discovered_model_dict(m) for m in models]
                for alias, models in s.models.available(provider).items()
            }
            emit_json(success_envelope("models.pick", data))
            return
        if provider:
            record = s.providers.get(provider)
        else:
            record = _pick_provider(s, console, non_interactive)
        model_id = _select_model(s, record, console, non_interactive)
        chosen = _save_and_activate(s, record, model_id, model_name)
        console.print(
            Text(
                f"Model {chosen.alias!r} saved and active on {record.alias!r} "
                f"({chosen.provider_model_id}).",
                style="bold green",
            )
        )


def _pick_provider(s, console, non_interactive: bool):
    saved = s.providers.list()
    options: list[tuple[str, object]] = [(f"{p.alias} ({p.type})", p) for p in saved]
    options.append(("+ add a new provider", None))
    options.append(("cancel", "cancel"))
    table = Table(box=None, pad_edge=False)
    table.add_column("#", no_wrap=True)
    table.add_column("provider")
    for i, (label, _) in enumerate(options, 1):
        table.add_row(str(i), label)
    console.print(Text("Choose a provider", style="bold bright_magenta"))
    console.print(table)
    if non_interactive:
        raise InvalidUsageError(
            "Interactive provider selection required.",
            hint="Pass --provider <alias>, or `rinari providers add ...` first.",
        )
    while True:
        raw = (typer.prompt("Provider (number or alias)") or "").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            _, value = options[int(raw) - 1]
            if value == "cancel":
                raise InvalidUsageError("Cancelled.")
            if value is None:
                return _add_provider(s, console)
            return value  # type: ignore[return-value]
        for p in saved:
            if p.alias == raw:
                return p
        if raw.lower() in ("add", "new"):
            return _add_provider(s, console)
        console.print("  unknown choice - pick a number or alias.")


def _add_provider(s, console):
    from rinari.application.provider_service import AddProviderInput
    from rinari.providers.catalog import PROVIDER_CATALOG

    table = Table(box=None, pad_edge=False)
    table.add_column("#", no_wrap=True)
    table.add_column("provider", style="bold")
    table.add_column("base url")
    table.add_column("auth")
    for i, preset in enumerate(PROVIDER_CATALOG, 1):
        auth = "none" if preset.local else f"env {preset.default_env or 'key'}"
        table.add_row(str(i), preset.name, preset.base_url or "-", auth)
    console.print(Text("Add a provider", style="bold bright_magenta"))
    console.print(table)
    while True:
        raw = (typer.prompt("Provider (number or name)") or "").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(PROVIDER_CATALOG):
            preset = PROVIDER_CATALOG[int(raw) - 1]
            break
        for preset in PROVIDER_CATALOG:
            if raw.lower() in (preset.key, preset.name.lower()):
                break
        else:
            console.print("  unknown choice - pick a number or name.")
            continue
        break

    alias = (typer.prompt("Provider alias", default=preset.key) or preset.key).strip()
    endpoint = preset.base_url
    if preset.provider_type == "custom" and preset.base_url is None:
        while True:
            endpoint = (
                typer.prompt("Endpoint (base URL, e.g. https://api.example.com/v1)") or ""
            ).strip()
            if endpoint.lower().startswith(("http://", "https://")):
                break
            console.print("  endpoint must start with http:// or https://")

    no_auth = preset.local
    secret = None
    secret_env = None
    if not no_auth:
        source = _prompt_auth_source(s, preset, console)
        if source == "none":
            no_auth = True
        elif source == "env":
            secret_env = (
                typer.prompt("Environment variable name", default=preset.default_env) or ""
            ).strip() or preset.default_env
        else:
            secret = _prompt_hidden("API key (hidden)")

    record = s.providers.add(
        AddProviderInput(
            alias=alias,
            provider_type=preset.provider_type,
            auth_method="none" if no_auth else "api-key",
            endpoint=endpoint,
            secret=secret,
            secret_env=secret_env,
        )
    )
    console.print(Text(f"Provider {record.alias!r} added.", style="bold green"))
    return record


def _prompt_auth_source(s, preset, console) -> str:
    options = ("api-key", "env", "none")
    default = "env" if (preset.default_env and os.environ.get(preset.default_env)) else "api-key"
    while True:
        raw = (
            (typer.prompt(f"Auth source ({'/'.join(options)})", default=default) or "")
            .strip()
            .lower()
        )
        if raw in options:
            return raw
        console.print("  pick one of: api-key, env, none")


def _prompt_hidden(text: str) -> str:
    while True:
        value = typer.prompt(text, hide_input=True)
        if (value or "").strip():
            return value.strip()


def _select_model(s, record, console, non_interactive: bool) -> str:
    from rinari.cli import render

    discovered = []
    live = render.thinking_status(console, label=f"discovering models on {record.alias}…")
    live.start()
    try:
        try:
            discovered = s.models.available(record.alias).get(record.alias, [])
        except Exception as exc:  # discovery is best-effort; fall back to manual
            discovered = []
            if not non_interactive:
                console.print(Text(f"  ! could not auto-discover models: {exc}", style="yellow"))
    finally:
        live.stop()

    saved_ids = {m.provider_model_id for m in s.models.list(record.alias)}
    if discovered:
        table = Table(box=None, pad_edge=False)
        table.add_column("#", no_wrap=True)
        table.add_column("model id", style="bold")
        table.add_column("state")
        table.add_column("capabilities")
        for i, m in enumerate(discovered, 1):
            caps = ", ".join(k for k in (m.capabilities or {}) if (m.capabilities or {}).get(k))
            state = "saved *" if m.provider_model_id in saved_ids else "available"
            table.add_row(str(i), m.provider_model_id, state, caps)
        console.print(
            Text(
                f"Models for {record.alias!r} ({record.type})",
                style="bold bright_magenta",
            )
        )

        console.print(table)
        if saved_ids:
            console.print(Text("  * already saved", style="dim"))
    else:
        console.print(
            Text(
                f"No models discovered for {record.alias!r}; enter the model ID manually.",
                style="dim",
            )
        )
    if non_interactive:
        raise InvalidUsageError(
            "Interactive model selection required.",
            hint="Pass --provider <alias> --model <id> for non-interactive use.",
        )
    while True:
        raw = (typer.prompt("Model (number or custom ID)") or "").strip()
        if not raw:
            continue
        if raw.isdigit() and 1 <= int(raw) <= len(discovered):
            return discovered[int(raw) - 1].provider_model_id
        return raw


def _save_and_activate(s, record, model_id: str, model_name: str | None):
    existing = s.ctx.model_repo.get_by_provider_model_id(record.id, model_id)
    if existing is not None:
        s.models.use(existing.id, record.alias)
        return existing
    settings: dict = {}
    if is_opencode_endpoint(record.endpoint) and model_id in OPENCODE_RESPONSES_MODELS:
        settings["transport"] = "responses"
    created = s.models.add(record.alias, model_id, model_name or model_id, settings=settings)
    s.models.use(created.id, record.alias)
    return created


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
    transport: str = typer.Option(
        None,
        "--transport",
        help="Wire transport: chat (/chat/completions) or responses (/responses).",
    ),
) -> None:
    """Save a model under a local alias for a provider."""
    if transport is not None and transport not in ("chat", "responses"):
        raise InvalidUsageError(
            f"unknown transport {transport!r}",
            hint="Expected one of: chat, responses.",
        )
    with services(ctx) as s:
        record = s.models.add(
            provider, model, name, settings={"transport": transport} if transport else {}
        )
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
