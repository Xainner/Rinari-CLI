"""`rinari trust` group: project trust management (phase 3)."""

from __future__ import annotations

import typer

from rinari.cli.deps import is_json, services, with_error_handling
from rinari.cli.output import emit_json, success_envelope
from rinari.trust import STATE_TRUSTED, TrustStatus

app = typer.Typer(help="Manage project trust.", no_args_is_help=True)


def _status_dict(status: TrustStatus) -> dict:
    return {
        "path": status.path,
        "canonical_path": status.canonical_path,
        "state": status.state,
        "fingerprint": status.fingerprint,
        "trusted_at": status.trusted_at,
    }


def _print_status(status: TrustStatus) -> None:
    if status.state == STATE_TRUSTED:
        typer.echo(f"{status.path}: trusted (since {status.trusted_at})")
    elif status.state == "not-trusted":
        typer.echo(f"{status.path}: NOT trusted  (rinari trust add {status.path})")
    elif status.state == "revalidation-required":
        typer.echo(
            f"{status.path}: revalidation required (identity changed since the grant)\n"
            f"              re-grant explicitly: rinari trust add {status.path}"
        )
    else:
        typer.echo(f"{status.path}: trust entry exists but the path no longer exists")


@app.command("status")
@with_error_handling("trust.status")
def trust_status(
    ctx: typer.Context, path: str = typer.Argument(".", help="Project directory.")
) -> None:
    """Show trust state for a project directory."""
    with services(ctx) as s:
        status = s.trust.status(path)
        if is_json(ctx):
            emit_json(success_envelope("trust.status", _status_dict(status)))
            return
        _print_status(status)


@app.command("list")
@with_error_handling("trust.list")
def trust_list(ctx: typer.Context) -> None:
    """List all trust grants with their current state."""
    with services(ctx) as s:
        statuses = s.trust.list()
        if is_json(ctx):
            emit_json(success_envelope("trust.list", [_status_dict(x) for x in statuses]))
            return
        if not statuses:
            typer.echo("No trust grants yet (rinari trust add <path>).")
            return
        typer.echo(f"{'PATH':<44} {'STATE':<24} TRUSTED AT")
        for status in statuses:
            typer.echo(f"{status.canonical_path:<44} {status.state:<24} {status.trusted_at or '-'}")


@app.command("add")
@with_error_handling("trust.add")
def trust_add(
    ctx: typer.Context, path: str = typer.Argument(".", help="Project directory to trust.")
) -> None:
    """Grant trust for a project directory (captures its identity fingerprint)."""
    with services(ctx) as s:
        record = s.trust.add(path)
        if is_json(ctx):
            emit_json(
                success_envelope(
                    "trust.add",
                    {
                        "canonical_path": record.canonical_path,
                        "fingerprint": record.fingerprint,
                        "trusted_at": record.trusted_at,
                    },
                )
            )
            return
        typer.echo(f"Trusted: {record.canonical_path}")
        typer.echo("Project instructions (RINARI.md / AGENTS.md) are now loaded for it.")


@app.command("remove")
@with_error_handling("trust.remove")
def trust_remove(
    ctx: typer.Context, path: str = typer.Argument(".", help="Project directory to untrust.")
) -> None:
    """Revoke trust for a project directory."""
    with services(ctx) as s:
        removed = s.trust.remove(path)
        if is_json(ctx):
            emit_json(success_envelope("trust.remove", {"removed": removed}))
            return
        if removed:
            typer.echo(f"Trust removed: {path}")
        else:
            typer.echo(f"No trust entry for: {path}")
