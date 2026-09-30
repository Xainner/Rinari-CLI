"""`rinari update`: the desktop app and this CLI, from the published release.

It used to ask PyPI, where `rinari-cli` is not published, so it could never
answer; and it knew nothing about the desktop app. Now it reads the latest
release of Xainner/Rinari-Agent and plans both pieces (see `rinari.update`).
"""

from __future__ import annotations

import httpx
import typer

from rinari import __version__
from rinari.cli.deps import is_json
from rinari.cli.output import emit_json, success_envelope
from rinari.shared.paths import resolve_home
from rinari.update.apply import reinstall_cli, update_desktop
from rinari.update.inventory import detect_cli, detect_desktop
from rinari.update.plan import Step, build_plan
from rinari.update.releases import ReleaseError, fetch_latest

_LABEL = {"desktop": "Rinari Agent", "cli": "rinari CLI"}


def _line(step: Step) -> str:
    versions = ""
    if step.current or step.target_version:
        versions = f"  {step.current or '—'} → {step.target_version or '—'}"
    return f"  {_LABEL[step.target]:<13} {step.action:<13}{versions}  ({step.reason})"


def update(
    ctx: typer.Context,
    check: bool = typer.Option(
        False, "--check", help="Only report; exit 1 when something is pending."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply without asking."),
    desktop_only: bool = typer.Option(False, "--desktop-only", help="Only the desktop app."),
    cli_only: bool = typer.Option(False, "--cli-only", help="Only this CLI."),
) -> None:
    """Update Rinari Agent and this CLI to the latest published release."""
    cli = detect_cli()
    desktop = None if cli_only else detect_desktop()
    client = httpx.Client()
    try:
        try:
            release = fetch_latest(client)
        except ReleaseError as err:
            data = {
                "current": __version__,
                "latest": None,
                "update_available": None,
                "error": str(err),
                "cli": cli.kind,
                "desktop": desktop.version if desktop else None,
            }
            if is_json(ctx):
                emit_json(success_envelope("update", data))
                return
            typer.echo(f"rinari {__version__}  (could not check for updates: {err})")
            return

        steps = build_plan(cli, desktop, release, desktop_only=desktop_only, cli_only=cli_only)
        pending = [step for step in steps if step.pending]
        data = {
            "current": __version__,
            "latest": release.version,
            "update_available": bool(pending),
            "engine_git_sha": release.engine_git_sha,
            "release_page": release.page,
            "cli": cli.kind,
            "desktop": (
                {
                    "version": desktop.version,
                    "running": desktop.running,
                    "path": str(desktop.install_dir),
                }
                if desktop
                else None
            ),
            "steps": [step.to_dict() for step in steps],
        }
        # JSON and --check only report: applying asks, and a script cannot answer.
        if check or (is_json(ctx) and not yes):
            if is_json(ctx):
                emit_json(success_envelope("update", data))
            else:
                typer.echo(f"Latest release: Rinari Agent {release.version}")
                for step in steps:
                    typer.echo(_line(step))
            raise typer.Exit(1 if check and pending else 0)

        human = not is_json(ctx)
        if human:
            typer.echo(f"Latest release: Rinari Agent {release.version}")
            for step in steps:
                typer.echo(_line(step))
        if not pending:
            if human:
                typer.echo("Everything is up to date.")
            else:
                emit_json(success_envelope("update", data))
            return
        if not yes and not typer.confirm("Apply these updates?", default=True):
            return

        results: dict[str, dict] = {}
        cache = resolve_home() / "cache" / "updates"
        for step in pending:
            if step.target == "desktop" and desktop is not None:
                results["desktop"] = update_desktop(
                    client, desktop, release, cache, detach=cli.kind == "bundled"
                )
            elif step.target == "cli":
                if (
                    step.action == "switch"
                    and not yes
                    and not typer.confirm(
                        f"This CLI is a development checkout ({cli.source}). Replace it "
                        f"with a normal install at {release.engine_git_sha[:7]}? "
                        "The checkout is not touched.",
                        default=False,
                    )
                ):
                    continue
                results["cli"] = reinstall_cli(release, cache / "cli-update.log")
        data["results"] = results
        if not human:
            emit_json(success_envelope("update", data))
            return
        messages = {
            "handed-to-app": "Rinari Agent is open: confirm «Restart and update» in its window.",
            "started": "The installer is running; it continues once this command exits.",
            "updated": "Rinari Agent updated.",
            "scheduled": "The CLI is reinstalled as soon as this command exits.",
        }
        for target, result in results.items():
            note = messages.get(result["status"], result["status"])
            if target == "cli" and result["status"] == "updated":
                note = "rinari CLI updated."
            log = f"  (log: {result['log']})" if result.get("log") else ""
            typer.echo(f"{_LABEL[target]}: {note}{log}")
    except ReleaseError as err:
        typer.echo(f"Update failed: {err}", err=True)
        raise typer.Exit(1) from err
    finally:
        client.close()
