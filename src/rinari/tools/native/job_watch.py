"""job.watch: leave a connector's job watched and be woken when it is done.

Instead of holding the turn on a long job (a song, a video, a batch), the
model names the connector's read-only status tool, its arguments and what
«done» looks like, then ends its turn. The Engine polls it and starts a new
turn in this conversation with the last answer (Ideas-2, 23-oct).
"""

from __future__ import annotations

from typing import Any

from rinari.tools.definition import (
    RISK_LOW,
    SIDE_EFFECT_NONE,
    ClassifiedAction,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)

DESCRIPTION = (
    "Watch a connector job instead of waiting for it: name the connector (server), its "
    "status tool (it must be read-only) with the arguments that identify the job, and "
    "until = {text: 'completed'} (any of the strings appears in the answer) or {path: "
    "'status', in: ['completed', 'failed']} (a field of the JSON answer). Then end your "
    "turn: when it is done, fails or times out, a new turn starts here with its last "
    "answer. note: what to do then. cancel: a watch_id to stop. Lost if Rinari restarts."
)

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "server": {"type": "string"},
        "tool": {"type": "string"},
        "arguments": {"type": "object"},
        "until": {
            "type": "object",
            "properties": {
                "text": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ]
                },
                "path": {"type": "string"},
                "in": {"type": "array"},
            },
        },
        "every_s": {"type": "number", "minimum": 15},
        "max_minutes": {"type": "number", "minimum": 1, "maximum": 720},
        "note": {"type": "string", "maxLength": 500},
        "cancel": {"type": "string"},
    },
}


class JobWatchHost:
    def __init__(self, services: Any, session_id: str) -> None:
        self.services = services
        self.session_id = session_id


def _fail(code: ToolErrorCode, message: str) -> ToolResult:
    return ToolResult(ok=False, error=ToolErrorInfo(code=code, message=message))


def job_watch_tools(host: JobWatchHost) -> list[ToolDefinition]:
    def watch(arguments: dict, _ctx: ToolContext) -> ToolResult:
        from rinari.application.job_watches import WatchError

        service = getattr(host.services, "job_watches", None)
        if service is None:
            return _fail(ToolErrorCode.DEPENDENCY_ERROR, "Job watches are not available here.")
        args = arguments or {}
        if args.get("cancel"):
            stopped = service.cancel(host.session_id, str(args["cancel"]))
            return ToolResult(
                ok=True, data={"cancelled": stopped, "watches": service.list(host.session_id)}
            )
        try:
            started = service.add(
                host.session_id,
                server=args.get("server"),
                tool=args.get("tool"),
                arguments=args.get("arguments"),
                until=args.get("until"),
                every_s=args.get("every_s"),
                max_minutes=args.get("max_minutes"),
                note=args.get("note") or "",
            )
        except WatchError as exc:
            return _fail(ToolErrorCode.INVALID_ARGUMENT, str(exc))
        return ToolResult(
            ok=True,
            data={
                **started,
                "next": "End your turn now; a new turn starts here when the job is done.",
            },
        )

    return [
        ToolDefinition(
            name="job.watch",
            description=DESCRIPTION,
            input_schema=INPUT_SCHEMA,
            capabilities=("state.mutate",),
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            idempotent=False,
            handler=watch,
            classify=lambda _input: ClassifiedAction("state.mutate"),
            namespace="job",
            # On demand (capability.search); the environment fact says it exists.
            always_loaded=False,
        )
    ]


__all__ = ["JobWatchHost", "job_watch_tools"]
