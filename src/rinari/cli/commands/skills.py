"""`rinari skills` group (phase 6): catalog, lifecycle, activation.

Per docs/commands.md (section added in phase 6) and the TODO.md phase-6
"Skill CLI" checklist: list, search, show, activate, deactivate, install,
remove, update, validate, test, create.
"""

from __future__ import annotations

from pathlib import Path

import typer

from rinari.cli.deps import is_json, services, with_error_handling
from rinari.cli.output import emit_json, success_envelope
from rinari.shared.errors import InvalidUsageError, RinariError
from rinari.skills.manifest import SkillError

app = typer.Typer(
    help="Manage skills (SKILL.md catalog + session activation).", no_args_is_help=True
)


def _project() -> Path | None:
    from rinari.projects.detector import detect_project

    return detect_project(Path.cwd(), Path.home()).project_root


def _target_session(s, ref: str | None):
    if ref:
        try:
            return s.sessions.show(ref)
        except Exception:
            raise InvalidUsageError(f"unknown session ref: {ref!r}") from None
    rows = s.sessions.list(limit=1)
    if not rows:
        raise InvalidUsageError(
            "no session to attach the skill to", hint="Start one: `rinari` or `rinari chat`."
        )
    return rows[0]


def _fail(exc: SkillError) -> None:
    raise RinariError(exc.message, hint=f"({exc.code})")


@app.command("list")
@with_error_handling("skills.list")
def skills_list(
    ctx: typer.Context,
    full: bool = typer.Option(False, "--full", help="List with source/version/risk detail."),
) -> None:
    """List available skills (project overrides user, user overrides packaged)."""
    with services(ctx) as s:
        rows = s.skills.summaries(_project(), session=_last_session(s))
        if is_json(ctx):
            emit_json(success_envelope("skills.list", {"skills": rows}))
            return
        if not rows:
            typer.echo("No skills found.")
            return
        for row in rows:
            marker = "*" if row["active"] else " "
            if full:
                typer.echo(
                    f"{marker} {row['name']:<22} v{row['version']:<8} "
                    f"{row['source']:<10} {row['risk']:<7} {row['description']}"
                )
            else:
                typer.echo(f"{marker} {row['name']:<22} {row['description']}")


@app.command("search")
@with_error_handling("skills.search")
def skills_search_cmd(ctx: typer.Context, query: str = typer.Argument(...)) -> None:
    """Search skills by name, description, or trigger text."""
    with services(ctx) as s:
        rows = s.skills.search(query, _project())
        if is_json(ctx):
            emit_json(success_envelope("skills.search", {"query": query, "skills": rows}))
            return
        if not rows:
            typer.echo(f"No skills match {query!r}.")
            return
        for row in rows:
            typer.echo(f"{row['name']:<22} {row['description']}")


@app.command("show")
@with_error_handling("skills.show")
def skills_show_cmd(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """Show one skill's full manifest and procedure (lazy load)."""
    with services(ctx) as s:
        try:
            m = s.skills.show(name, _project())
        except SkillError as exc:
            _fail(exc)
        if is_json(ctx):
            emit_json(
                success_envelope(
                    "skills.show",
                    {
                        "name": m.name,
                        "description": m.description,
                        "version": m.version,
                        "source": m.source,
                        "risk": m.risk,
                        "can_delegate": m.can_delegate,
                        "triggers": list(m.triggers),
                        "required_tools": list(m.required_tools),
                        "optional_tools": list(m.optional_tools),
                        "body": m.body,
                    },
                )
            )
            return
        typer.echo(f"{m.name}  v{m.version}  ({m.source}, risk {m.risk})")
        typer.echo(m.description)
        if m.triggers:
            typer.echo(f"triggers: {', '.join(m.triggers)}")
        if m.required_tools:
            typer.echo(f"required: {', '.join(m.required_tools)}")
        if m.optional_tools:
            typer.echo(f"optional: {', '.join(m.optional_tools)}")
        typer.echo()
        typer.echo(m.body)


@app.command("activate")
@with_error_handling("skills.activate")
def skills_activate_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    session: str = typer.Option(None, "--session", "-s", help="Session id (default: most recent)."),
) -> None:
    """Pin a skill on a session (persisted + activation-trace event)."""
    with services(ctx) as s:
        try:
            target = _target_session(s, session)
            m = s.skills.activate(name, target.id, _project())
        except SkillError as exc:
            _fail(exc)
        if is_json(ctx):
            emit_json(
                success_envelope(
                    "skills.activate",
                    {"name": m.name, "version": m.version, "session": target.id},
                )
            )
            return
        typer.echo(f"activated {m.name} v{m.version} on session {target.id}")


@app.command("deactivate")
@with_error_handling("skills.deactivate")
def skills_deactivate_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    session: str = typer.Option(None, "--session", "-s", help="Session id (default: most recent)."),
) -> None:
    """Unpin a skill from a session."""
    with services(ctx) as s:
        try:
            target = _target_session(s, session)
            removed = s.skills.deactivate(name, target.id, _project())
        except SkillError as exc:
            _fail(exc)
        if is_json(ctx):
            emit_json(
                success_envelope(
                    "skills.deactivate", {"name": name, "removed": removed, "session": target.id}
                )
            )
            return
        if removed:
            typer.echo(f"deactivated {name} on session {target.id}")
        else:
            typer.echo(f"{name} is not active on session {target.id}")


@app.command("install")
@with_error_handling("skills.install")
def skills_install_cmd(
    ctx: typer.Context,
    source: str = typer.Argument(..., help="Directory containing SKILL.md."),
    name: str = typer.Option(None, "--name", help="Install under a specific name."),
) -> None:
    """Install a skill from a local directory into ~/.rinari/skills/."""
    with services(ctx) as s:
        try:
            m = s.skills.install(source, name)
        except SkillError as exc:
            _fail(exc)
        if is_json(ctx):
            emit_json(success_envelope("skills.install", {"name": m.name, "path": m.path}))
            return
        typer.echo(f"installed {m.name} v{m.version} -> {m.path}")


@app.command("update")
@with_error_handling("skills.update")
def skills_update_cmd(
    ctx: typer.Context,
    source: str = typer.Argument(..., help="Directory containing SKILL.md."),
    name: str = typer.Option(None, "--name", help="Update a specific installed skill."),
) -> None:
    """Replace an installed user skill with the contents of a directory."""
    with services(ctx) as s:
        try:
            m = s.skills.update(source, name)
        except SkillError as exc:
            _fail(exc)
        if is_json(ctx):
            emit_json(success_envelope("skills.update", {"name": m.name, "version": m.version}))
            return
        typer.echo(f"updated {m.name} -> v{m.version}")


@app.command("remove")
@with_error_handling("skills.remove")
def skills_remove_cmd(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """Remove an installed user skill (never touches packaged/project skills)."""
    with services(ctx) as s:
        removed = s.skills.remove(name)
        if is_json(ctx):
            emit_json(success_envelope("skills.remove", {"removed": removed}))
            return
        typer.echo(f"removed {name}" if removed else f"no user skill installed: {name}")


@app.command("validate")
@with_error_handling("skills.validate")
def skills_validate_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(None, help="One skill name (default: all)."),
) -> None:
    """Static validation: schema + required tools exist (no execution)."""
    with services(ctx) as s:
        report = s.skills.validate(name, _project())
        ok = all(entry["ok"] for entry in report)
        if is_json(ctx):
            emit_json(success_envelope("skills.validate", {"ok": ok, "report": report}))
            return
        for entry in report:
            mark = "OK " if entry["ok"] else "FAIL"
            typer.echo(f"{mark}  {entry['name']} ({entry['source']}, v{entry['version']})")
            for issue in entry["issues"]:
                typer.echo(f"      {issue['code']}: {issue['message']}")
        typer.echo(
            f"{sum(1 for e in report if e['ok'])}/{len(report)} skills valid"
            if report
            else "No skills found."
        )


@app.command("test")
@with_error_handling("skills.test")
def skills_test_cmd(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """Dry-run one skill: parse + validate (deterministic; no model, no tools)."""
    with services(ctx) as s:
        data = s.skills.test(name, _project())
        if is_json(ctx):
            emit_json(success_envelope("skills.test", data))
            return
        if data["ok"]:
            typer.echo(f"OK: {name} v{data['version']} ({data['source']}) parses and validates")
        else:
            typer.echo(f"FAIL: {name}: {data.get('message') or data.get('issues')}")


@app.command("create")
@with_error_handling("skills.create")
def skills_create_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    description: str = typer.Option("", "--description", "-d"),
) -> None:
    """Scaffold a new user skill template under ~/.rinari/skills/."""
    with services(ctx) as s:
        try:
            path = s.skills.create(name, description)
        except SkillError as exc:
            _fail(exc)
        if is_json(ctx):
            emit_json(success_envelope("skills.create", {"path": path}))
            return
        typer.echo(f"created skill template: {path}")
        typer.echo("Edit it, then validate: rinari skills validate " + name)


@app.command("path")
@with_error_handling("skills.path")
def skills_path_cmd(ctx: typer.Context, name: str = typer.Argument(...)) -> None:
    """Print the resolved SKILL.md path (project > user > packaged)."""
    with services(ctx) as s:
        try:
            m = s.skills.get(name, _project())
        except SkillError as exc:
            _fail(exc)
        if is_json(ctx):
            emit_json(
                success_envelope(
                    "skills.path", {"name": m.name, "source": m.source, "path": m.path}
                )
            )
            return
        typer.echo(m.path)


def _last_session(s):
    rows = s.sessions.list(limit=1)
    return rows[0] if rows else None


__all__ = ["app"]
