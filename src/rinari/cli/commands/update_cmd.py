"""`rinari update`: check the published version (commands.md 57)."""

from __future__ import annotations

import re

import httpx
import typer

from rinari import __version__
from rinari.cli.deps import is_json
from rinari.cli.output import emit_json, success_envelope

PYPI_URL = "https://pypi.org/pypi/rinari-cli/json"


def _fetch_latest(client: httpx.Client, timeout_s: float = 10.0) -> str:
    response = client.get(PYPI_URL, timeout=timeout_s)
    response.raise_for_status()
    latest = response.json().get("info", {}).get("version")
    if not isinstance(latest, str):
        raise LookupError("unrecognized PyPI response")
    return latest


def _parse(version: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", version.split("+")[0].split("-")[0])
    return tuple(int(p) for p in parts) if parts else (0,)


def check_update(client: httpx.Client, timeout_s: float = 10.0) -> dict:
    """Caller owns the transport lifetime."""
    latest = _fetch_latest(client, timeout_s=timeout_s)
    current = __version__
    return {
        "current": current,
        "latest": latest,
        "update_available": _parse(latest) > _parse(current),
    }


def update(
    ctx: typer.Context,
    check: bool = typer.Option(False, "--check", help="Exit 1 when an update is available."),
) -> None:
    """Check for a new published version of Rinari."""
    client = httpx.Client()
    try:
        data = check_update(client=client)
    except (httpx.HTTPError, LookupError, OSError) as err:
        data = {"current": __version__, "latest": None, "update_available": None, "error": str(err)}
    finally:
        client.close()
    check_exit = 1 if (check and data.get("update_available")) else 0
    if is_json(ctx):
        emit_json(success_envelope("update", data))
        raise typer.Exit(check_exit)
    if data.get("latest") is None:
        typer.echo(f"rinari {__version__}  (could not check for updates: {data.get('error')})")
        return
    if data["update_available"]:
        typer.echo(f"update available: {__version__} -> {data['latest']}")
        typer.echo("upgrade with:  uv tool upgrade rinari-cli   (or your package manager)")
    else:
        typer.echo(f"rinari {__version__} is up to date")
    if check_exit:
        raise typer.Exit(check_exit)
