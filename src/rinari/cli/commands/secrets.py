"""`rinari secrets` group: secret references (never plaintext values).

Commands.md 41. Values are stored through the OS keychain / credential store;
this surface only exposes references, existence, and health.
"""

from __future__ import annotations

import os

import typer

from rinari.application.credentials import SecretRef
from rinari.application.credentials_gc import apply_cleanup, plan_cleanup
from rinari.application.credentials_vault import delete_credential, enumerate_credentials
from rinari.cli.deps import is_json, services, with_error_handling
from rinari.shared.errors import (
    CredentialStoreUnavailableError,
    InvalidUsageError,
    NotFoundError,
)

from ..output import emit_json, success_envelope

app = typer.Typer(help="Manage secret references (values never shown).", no_args_is_help=True)


@app.command("list")
@with_error_handling("secrets.list")
def list_cmd(ctx: typer.Context) -> None:
    """List secret references (provider-bound), without values."""
    with services(ctx) as s:
        rows = []
        for record in s.providers.list():
            ref = _secret_ref(s, record)
            exists = s.credentials.exists(ref)
            rows.append(
                {
                    "provider": record.alias,
                    "provider_type": record.type,
                    "ref": ref,
                    "present": exists,
                }
            )
        if is_json(ctx):
            emit_json(success_envelope("secrets.list", rows))
            return
        if not rows:
            typer.echo("no providers with secrets")
            return
        for row in rows:
            state = "present" if row["present"] else "missing"
            typer.echo(f"  {row['provider']:<16} {row['provider_type']:<16} {state}")


@app.command("add")
@with_error_handling("secrets.add")
def add(
    ctx: typer.Context,
    provider: str = typer.Argument(..., help="Provider alias."),
    from_env: str = typer.Option(
        None, "--from-env", help="Read the secret from this environment variable."
    ),
) -> None:
    """Attach a secret to a provider. Value via --from-env or hidden prompt."""
    with services(ctx) as s:
        record = _find_provider(s, provider)
        if from_env:
            value = os.environ.get(from_env)
            if not value:
                raise InvalidUsageError(f"Environment variable is empty: {from_env}")
        else:
            value = typer.prompt("Secret", hide_input=True, confirmation_prompt=True)
        ref = s.credentials.store_provider_secret(record.id, value)
        data = {"provider": record.alias, "ref": ref, "present": s.credentials.exists(ref)}
        if is_json(ctx):
            emit_json(success_envelope("secrets.add", data))
        else:
            typer.echo(f"secret stored for {record.alias} -> {ref}")


@app.command("remove")
@with_error_handling("secrets.remove")
def remove(
    ctx: typer.Context,
    provider: str = typer.Argument(..., help="Provider alias."),
) -> None:
    """Remove a provider's stored secret."""
    with services(ctx) as s:
        record = _find_provider(s, provider)
        ref = s.credentials.provider_secret_ref(record.id)
        removed = s.credentials.delete(ref)
        if not removed:
            raise NotFoundError(f"No secret stored for {record.alias}")
        if is_json(ctx):
            emit_json(success_envelope("secrets.remove", {"provider": record.alias}))
        else:
            typer.echo(f"removed secret for {record.alias}")


@app.command("rotate")
@with_error_handling("secrets.rotate")
def rotate(
    ctx: typer.Context,
    provider: str = typer.Argument(..., help="Provider alias."),
    from_env: str = typer.Option(
        None, "--from-env", help="Read the new secret from this environment variable."
    ),
) -> None:
    """Replace a provider's stored secret with a new value."""
    with services(ctx) as s:
        record = _find_provider(s, provider)
        if not s.credentials.exists(s.credentials.provider_secret_ref(record.id)):
            raise NotFoundError(
                f"No secret stored for {record.alias}", hint="Use `rinari secrets add`."
            )
        if from_env:
            value = os.environ.get(from_env)
            if not value:
                raise InvalidUsageError(f"Environment variable is empty: {from_env}")
        else:
            value = typer.prompt("New secret", hide_input=True, confirmation_prompt=True)
        s.credentials.store_provider_secret(record.id, value)
        if is_json(ctx):
            emit_json(success_envelope("secrets.rotate", {"provider": record.alias}))
        else:
            typer.echo(f"rotated secret for {record.alias}")


@app.command("scopes")
@with_error_handling("secrets.scopes")
def scopes(ctx: typer.Context) -> None:
    """Show which providers are bound to a secret reference."""
    with services(ctx) as s:
        rows = []
        for record in s.providers.list():
            ref = _secret_ref(s, record)
            rows.append(
                {"provider": record.alias, "ref": ref, "present": s.credentials.exists(ref)}
            )
        if is_json(ctx):
            emit_json(success_envelope("secrets.scopes", rows))
            return
        for row in rows:
            typer.echo(f"  {row['provider']:<16} {row['ref']}")


@app.command("test")
@with_error_handling("secrets.test")
def test(
    ctx: typer.Context,
    provider: str = typer.Argument(..., help="Provider alias."),
) -> None:
    """Check that a provider's secret resolves (value is never printed)."""
    with services(ctx) as s:
        record = _find_provider(s, provider)
        ref = _secret_ref(s, record)
        try:
            value = s.credentials.resolve(ref)
            ok = bool(value)
        except Exception:
            ok = False
        data = {"provider": record.alias, "resolvable": ok}
        if is_json(ctx):
            emit_json(success_envelope("secrets.test", data))
            return
        typer.echo(f"secret for {record.alias}: {'resolvable' if ok else 'NOT resolvable'}")


@app.command("cleanup")
@with_error_handling("secrets.cleanup")
def cleanup(
    ctx: typer.Context,
    apply: bool = typer.Option(
        False, "--apply", help="Delete the orphaned entries (default: dry run)."
    ),
) -> None:
    """Inspect (and optionally retire) orphaned entries in the OS vault.

    Only entries this home can prove it owns are candidates: entries without a
    home scope, from another home, or from another application are reported
    and never deleted.
    """
    with services(ctx) as s:
        scope = s.credentials.scope
        try:
            entries = enumerate_credentials()
        except CredentialStoreUnavailableError as error:
            data = {"supported": False, "status": "unsupported", "detail": error.message}
            if is_json(ctx):
                emit_json(success_envelope("secrets.cleanup", data))
            else:
                typer.echo(f"credential vault cleanup: {error.message}")
            return
        plan = plan_cleanup(
            entries,
            live_provider_ids={record.id for record in s.providers.list()},
            retained_provider_ids=set(s.ctx.provider_repo.list_retained_credential_ids()),
            scope=scope,
        )
        counts = plan.summary()
        if not apply:
            data = {
                "supported": True,
                "status": "plan",
                "scope": scope,
                "summary": counts,
                "orphans": [entry.target for entry in plan.orphans],
                "unknown": [entry.target for entry in plan.unknown],
            }
            if is_json(ctx):
                emit_json(success_envelope("secrets.cleanup", data))
                return
            typer.echo(
                f"vault: {counts['live']} live, {counts['retained']} retained, "
                f"{counts['orphans']} orphaned, {counts['unknown']} unknown, "
                f"{counts['foreign']} foreign"
            )
            for entry in plan.orphans:
                typer.echo(f"  orphan  {entry.target}")
            typer.echo("dry run: pass --apply to delete the orphaned entries")
            return
        report = apply_cleanup(
            plan,
            delete=delete_credential,
            # Revalidación: un alta reciente gana aunque el plan la marcara.
            still_orphan=lambda provider_id: not s.ctx.provider_repo.credential_exists(provider_id),
        )
        data = {
            "supported": True,
            "status": "applied",
            "scope": scope,
            "summary": counts,
            "deleted": report.deleted,
            "skipped": report.skipped,
            "failed": report.failed,
            "failures": [
                {"target": target, "detail": detail} for target, detail in report.failures
            ],
        }
        if is_json(ctx):
            emit_json(success_envelope("secrets.cleanup", data))
            return
        typer.echo(
            f"deleted {report.deleted} orphaned entries "
            f"({report.skipped} kept after revalidation, {report.failed} failures)"
        )
        for target, detail in report.failures:
            typer.echo(f"  failed {target}: {detail}")


def _secret_ref(s, record) -> str:
    """Registered reference for the provider (keyring, file or environment)."""
    return s.providers.credential_ref(record) or s.credentials.provider_secret_ref(record.id)


def _find_provider(s, alias: str):
    for record in s.providers.list():
        if record.alias == alias:
            return record
    raise NotFoundError(f"Provider not found: {alias}", hint="See `rinari providers list`.")


__all__ = ["SecretRef", "app"]
