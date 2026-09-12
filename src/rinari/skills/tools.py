"""Model-facing skill tools (phase 6).

The catalog (summaries) is always in the system prompt; these tools let the
model inspect one skill fully (lazy load) and pin/unpin skills on the
session. Activation is session state + an event trace — it never changes
policy: a skill requesting tools still needs the normal approvals
(harness.md 51).
"""

from __future__ import annotations

from dataclasses import dataclass

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
        rows = service.summaries(_project())
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
                "body": m.body,
            },
            origin="skills",
        )

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
        return ToolResult(
            ok=True,
            data={
                "name": m.name,
                "version": m.version,
                "active": True,
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

    read = ("state.read",)
    write = ("state.write",)
    return [
        ToolDefinition(
            name="skills.list",
            description=(
                "List skill summaries (name, description, source, risk, triggers). "
                "Full bodies are large; use skills.show for one skill."
            ),
            input_schema={"type": "object"},
            capabilities=read,
            classify=lambda _i: ClassifiedAction("state.read"),
            handler=list_,
        ),
        ToolDefinition(
            name="skills.show",
            description="Load one skill's full manifest and procedure body (lazy load).",
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
            name="skills.activate",
            description=(
                "Pin a skill on this session so its procedure is injected into "
                "active-skills context. Requests tools but grants none."
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
