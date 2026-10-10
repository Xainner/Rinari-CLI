"""`rinari bundles` group: Rinari profiles as workspaces (`rinari_profiles_v1`).

A Rinari profile (soul, mode, per-agent models) is also where projects and
conversations live. The active one is shared with the desktop
(`<home>/active_profile`): what `activate` changes here, the app picks up
when its window regains focus. Not to be confused with `rinari profiles`
(capability/config profiles).
"""

from __future__ import annotations

from pathlib import Path

import typer

from rinari.cli.deps import is_json, services, with_error_handling
from rinari.cli.output import emit_json, success_envelope
from rinari.shared.errors import InvalidUsageError, NotFoundError

app = typer.Typer(
    help="Rinari profiles (workspaces): list, activate, create, move work.",
    no_args_is_help=True,
)


def _agents(s, specs: list[str]) -> dict[str, dict[str, str]]:
    """`name=model[,fallback]` → the profile's agents map (validated)."""
    known = s.agents.list()
    agents: dict[str, dict[str, str]] = {}
    for spec in specs:
        name, sep, models = spec.partition("=")
        name, models = name.strip(), models.strip()
        if not sep or not name or not models:
            raise InvalidUsageError(
                f"Invalid --agent {spec!r}.", hint="Use --agent NAME=MODEL[,FALLBACK]."
            )
        if name not in known:
            raise NotFoundError(f"Unknown agent: {name}.", hint="See `rinari agents list`.")
        model, _, fallback = models.partition(",")
        entry: dict[str, str] = {}
        for key, alias in (("model", model.strip()), ("fallback", fallback.strip())):
            if alias:
                s.models.resolve(alias)  # NotFound for an unknown alias
                entry[key] = alias
        agents[name] = entry
    return agents


def _project_id(s, ref: str) -> str:
    """A project id, or a path inside one of its folders."""
    if s.ctx.project_repo.get(ref) is not None:
        return ref
    path = Path(ref).expanduser()
    if path.exists():
        owner = s.projects.folders.owner_of(path)
        if owner is not None:
            return owner
    raise NotFoundError(
        f"Project not found: {ref}", hint="Pass a project id or a path inside the project."
    )


def _row(summary: dict) -> dict:
    return {
        "id": summary["id"],
        "name": summary["name"],
        "description": summary["description"],
        "active": summary["active"],
        "builtin": summary["builtin"],
        "projects": summary["counts"]["projects"],
        "sessions": summary["counts"]["sessions"],
        "soul_id": summary["soul_id"],
        "mode": summary["mode"],
        "agents": summary["agents"],
    }


@app.command("list")
@with_error_handling("bundles.list")
def bundles_list(ctx: typer.Context) -> None:
    """List profiles with their projects and conversations; * marks the active one."""
    with services(ctx) as s:
        rows = [_row(summary) for summary in s.rinari_profiles.summaries()]
        if is_json(ctx):
            emit_json(
                success_envelope(
                    "bundles.list",
                    {"profiles": rows, "active_id": s.rinari_profiles.active_id()},
                )
            )
            return
        typer.echo(f"  {'ID':<20} {'NAME':<24} {'PROJECTS':>8} {'CONVERSATIONS':>13}")
        for row in rows:
            marker = "*" if row["active"] else " "
            typer.echo(
                f"{marker} {row['id']:<20} {row['name'][:24]:<24} "
                f"{row['projects']:>8} {row['sessions']:>13}"
            )


@app.command("activate")
@with_error_handling("bundles.activate")
def bundles_activate(ctx: typer.Context, profile_id: str = typer.Argument(...)) -> None:
    """Make a profile active: new projects and conversations go there."""
    with services(ctx) as s:
        previous, bundle = s.rinari_profiles.activate(profile_id)
        if is_json(ctx):
            emit_json(
                success_envelope(
                    "bundles.activate",
                    {"active_id": bundle.id, "previous_id": previous},
                )
            )
            return
        typer.echo(f"Active profile: {bundle.name} ({bundle.id}); was {previous}.")


@app.command("create")
@with_error_handling("bundles.create")
def bundles_create(
    ctx: typer.Context,
    profile_id: str = typer.Argument(..., help="Id: a-z, 0-9 and dashes."),
    name: str = typer.Option(..., "--name", help="Display name."),
    description: str = typer.Option("", "--description"),
    soul: str = typer.Option(None, "--soul", help="Soul id for its new conversations."),
    mode: str = typer.Option(None, "--mode", help="Mode for its new conversations."),
    agent: list[str] = typer.Option(
        [], "--agent", help="Per-agent model: NAME=MODEL[,FALLBACK]. Repeatable."
    ),
    activate: bool = typer.Option(False, "--activate", help="Make it active too."),
) -> None:
    """Create a profile."""
    with services(ctx) as s:
        bundle = s.rinari_profiles.store.create(
            profile_id,
            name=name,
            description=description,
            soul_id=soul,
            mode=mode,
            agents=_agents(s, agent),
        )
        if activate:
            s.rinari_profiles.activate(bundle.id)
        if is_json(ctx):
            emit_json(
                success_envelope(
                    "bundles.create", {"profile": bundle.to_summary(), "activated": activate}
                )
            )
            return
        typer.echo(
            f"Created profile {bundle.name} ({bundle.id})" + (", active." if activate else ".")
        )


@app.command("move-project")
@with_error_handling("bundles.move_project")
def bundles_move_project(
    ctx: typer.Context,
    project: str = typer.Argument(..., help="Project id, or a path inside the project."),
    profile_id: str = typer.Argument(..., help="Target profile id."),
) -> None:
    """Move a project, with all its conversations, to another profile."""
    with services(ctx) as s:
        moved = s.rinari_profiles.move_project(_project_id(s, project), profile_id)
        data = {
            "project_id": moved.project_id,
            "rinari_profile_id": moved.rinari_profile_id,
            "previous_rinari_profile_id": moved.previous_rinari_profile_id,
            "session_ids": list(moved.session_ids),
        }
        if is_json(ctx):
            emit_json(success_envelope("bundles.move_project", data))
            return
        typer.echo(
            f"Moved project {moved.project_id} and {len(moved.session_ids)} conversation(s) "
            f"from {moved.previous_rinari_profile_id} to {moved.rinari_profile_id}."
        )


@app.command("move-session")
@with_error_handling("bundles.move_session")
def bundles_move_session(
    ctx: typer.Context,
    session: str = typer.Argument(..., help="Session id (or a unique prefix)."),
    profile_id: str = typer.Argument(..., help="Target profile id."),
    with_project: bool = typer.Option(
        False,
        "--with-project",
        help="For a conversation inside a project: move the whole project with it.",
    ),
) -> None:
    """Move a conversation to another profile.

    A conversation inside a project has the project's profile: pass
    --with-project to move the whole project, or take the conversation out of
    the project first (in the app).
    """
    with services(ctx) as s:
        record = s.sessions.show(session)
        if record.project_id and with_project:
            moved = s.rinari_profiles.move_project(record.project_id, profile_id)
        elif record.project_id:
            project = s.projects.get(record.project_id)
            raise InvalidUsageError(
                f"This conversation belongs to the project {project.name!r}.",
                hint="Pass --with-project to move the whole project, "
                "or take the conversation out of the project first.",
            )
        else:
            moved = s.rinari_profiles.move_session(record.id, profile_id)
        data = {
            "session_id": record.id,
            "project_id": moved.project_id,
            "rinari_profile_id": moved.rinari_profile_id,
            "previous_rinari_profile_id": moved.previous_rinari_profile_id,
            "session_ids": list(moved.session_ids),
        }
        if is_json(ctx):
            emit_json(success_envelope("bundles.move_session", data))
            return
        what = (
            f"project {moved.project_id} with {len(moved.session_ids)} conversation(s)"
            if moved.project_id
            else f"conversation {record.id}"
        )
        typer.echo(
            f"Moved {what} from {moved.previous_rinari_profile_id} to {moved.rinari_profile_id}."
        )


__all__ = ["app"]
