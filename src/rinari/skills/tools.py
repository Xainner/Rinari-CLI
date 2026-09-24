"""Model-facing skill tools (phase 6).

The catalog (summaries) is always in the system prompt; these tools let the
model inspect one skill fully (lazy load) and pin/unpin skills on the
session. Activation is session state + an event trace — it never changes
policy: a skill requesting tools still needs the normal approvals
(harness.md 51).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rinari.skills.manifest import SkillError


@dataclass
class SkillToolHost:
    service: object
    project: object | None = None

    def _session_id(self, ctx) -> str:
        return getattr(ctx, "session_id", "") or ""


def skill_tools(host: SkillToolHost):
    from rinari.tools.definition import (
        ClassifiedAction,
        ToolDefinition,
        ToolErrorCode,
        ToolErrorInfo,
        ToolResult,
    )

    service = host.service

    def _project():
        return host.project

    def _sid(ctx) -> str:
        return getattr(ctx, "session_id", "") or ""

    def list_(arguments, ctx):
        query = str((arguments or {}).get("query") or "").strip()
        rows = service.search(query, _project()) if query else service.summaries(_project())
        return ToolResult(ok=True, data={"skills": rows}, origin="skills")

    def show(arguments, ctx):
        name = str((arguments or {}).get("name") or "")
        if not name:
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(ToolErrorCode.INVALID_ARGUMENT, "name required"),
                origin="skills",
            )
        try:
            m = service.get(name, _project())
        except SkillError as exc:
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(ToolErrorCode.NOT_FOUND, exc.message),
                origin="skills",
            )
        return ToolResult(
            ok=True,
            data={
                "name": m.name,
                "description": m.description,
                "version": m.version,
                "source": m.source,
                "risk": m.risk,
                "triggers": list(m.triggers),
                "required_tools": list(m.required_tools),
                "optional_tools": list(m.optional_tools),
                "format": m.format,
                "folder": str(Path(m.path).parent),
                "body": service.prompt_body(name, _project()),
                "references": service.references(name, _project()),
            },
            origin="skills",
        )

    def read_reference(arguments, ctx):
        args = arguments or {}
        name = str(args.get("name") or "")
        if not name or not args.get("path"):
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(ToolErrorCode.INVALID_ARGUMENT, "name and path required"),
                origin="skills",
            )
        try:
            data = service.read_reference(
                name,
                str(args["path"]),
                _project(),
                offset=args.get("offset") or 0,
                limit=args.get("limit") or 400,
            )
        except SkillError as exc:
            code = (
                ToolErrorCode.NOT_FOUND
                if exc.code == "SKILL_NOT_FOUND"
                else ToolErrorCode.INVALID_ARGUMENT
            )
            return ToolResult(ok=False, error=ToolErrorInfo(code, exc.message), origin="skills")
        return ToolResult(ok=True, data=data, origin="skills")

    def activate(arguments, ctx):
        name = str((arguments or {}).get("name") or "")
        if not name:
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(ToolErrorCode.INVALID_ARGUMENT, "name required"),
                origin="skills",
            )
        sid = _sid(ctx)
        if not sid:
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(ToolErrorCode.UNKNOWN, "no session context"),
                origin="skills",
            )
        try:
            m = service.activate(name, sid, _project())
        except SkillError as exc:
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(
                    ToolErrorCode.NOT_FOUND
                    if exc.code == "SKILL_NOT_FOUND"
                    else ToolErrorCode.UNKNOWN,
                    exc.message,
                ),
                origin="skills",
            )
        # The tools the skill requests become visible now, in the same call; a
        # policy decision still applies to each of them when it runs.
        exposure = getattr(ctx, "exposure", None)
        exposed = []
        if exposure is not None and m.required_tools:
            exposed = exposure.activate(
                list(m.required_tools), reason=f"skill {m.name}", scope="session"
            )
        return ToolResult(
            ok=True,
            data={
                "name": m.name,
                "version": m.version,
                "active": True,
                "tools_activated": exposed,
            },
            origin="skills",
        )

    def deactivate(arguments, ctx):
        name = str((arguments or {}).get("name") or "")
        sid = _sid(ctx)
        if not name or not sid:
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(ToolErrorCode.INVALID_ARGUMENT, "name required"),
                origin="skills",
            )
        try:
            removed = service.deactivate(name, sid, _project())
        except SkillError as exc:
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(ToolErrorCode.UNKNOWN, exc.message),
                origin="skills",
            )
        return ToolResult(
            ok=True,
            data={"name": name, "active": False, "removed": removed},
            origin="skills",
        )

    def propose(arguments, ctx):
        args = arguments or {}
        name = str(args.get("name") or "")
        skill_md = args.get("skill_md")
        if not name or not isinstance(skill_md, str) or not skill_md.strip():
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(ToolErrorCode.INVALID_ARGUMENT, "name and skill_md required"),
                origin="skills",
            )
        # The Engine, not the model, decides: only a turn the owner started
        # with /learn saves the skill active; anything else waits for approval.
        owner_asked = getattr(ctx, "turn_command", "") == "learn"
        try:
            result = service.propose(
                name,
                skill_md,
                args.get("references") or None,
                session_id=_sid(ctx),
                update_of=args.get("update_of") or None,
                owner_asked=owner_asked,
            )
        except SkillError as exc:
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(ToolErrorCode.INVALID_ARGUMENT, f"{exc.code}: {exc.message}"),
                origin="skills",
            )
        return ToolResult(ok=True, data=result, origin="skills")

    read = ("state.read",)
    write = ("state.write",)
    return [
        ToolDefinition(
            name="skills.list",
            description=(
                "List skill summaries (name, description, source, risk, triggers); with "
                "`query`, only the matching ones, best first. Full bodies are large; use "
                "skills.show for one skill."
            ),
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string", "maxLength": 200}},
            },
            capabilities=read,
            classify=lambda _i: ClassifiedAction("state.read"),
            handler=list_,
        ),
        ToolDefinition(
            name="skills.show",
            description=(
                "Load one skill's full manifest and procedure body (lazy load), and the "
                "reference files it ships (read them with skills.read)."
            ),
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            capabilities=read,
            classify=lambda _i: ClassifiedAction("state.read"),
            handler=show,
        ),
        ToolDefinition(
            name="skills.read",
            description=(
                "Read a reference file of a skill (listed by skills.show), a page at a "
                "time. Skills keep details there so they are read only when needed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "path": {"type": "string", "description": "e.g. references/recipes.md"},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 400},
                },
                "required": ["name", "path"],
                "additionalProperties": False,
            },
            capabilities=read,
            classify=lambda _i: ClassifiedAction("state.read"),
            handler=read_reference,
        ),
        ToolDefinition(
            name="skills.activate",
            description=(
                "Pin a skill on this session so its procedure is injected into "
                "active-skills context, and expose the tools it requires. Grants no "
                "permission: each tool call still goes through policy."
            ),
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            capabilities=write,
            side_effects="local_reversible",
            classify=lambda _i: ClassifiedAction("state.write"),
            handler=activate,
        ),
        ToolDefinition(
            name="skills.propose",
            description=(
                "Save a skill learned from this conversation: the full SKILL.md "
                "(frontmatter with name and description, then the procedure) plus "
                "optional text files under references/ or scripts/. It is saved active "
                "only when the owner asked with /learn; otherwise it waits for the "
                "owner's approval. Never include secrets: a token or password is refused. "
                "To improve an existing skill pass update_of with its name."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,63}$"},
                    "skill_md": {"type": "string", "minLength": 1, "maxLength": 60000},
                    "references": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                    "update_of": {"type": "string"},
                },
                "required": ["name", "skill_md"],
                "additionalProperties": False,
            },
            capabilities=write,
            side_effects="local_reversible",
            always_loaded=False,
            classify=lambda _i: ClassifiedAction("state.write"),
            handler=propose,
        ),
        ToolDefinition(
            name="skills.deactivate",
            description="Unpin a skill from this session.",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            capabilities=write,
            side_effects="local_reversible",
            classify=lambda _i: ClassifiedAction("state.write"),
            handler=deactivate,
        ),
    ]


__all__ = ["SkillToolHost", "skill_tools"]
