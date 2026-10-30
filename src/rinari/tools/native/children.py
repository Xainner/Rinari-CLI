"""Child sessions from the mother's side (plan-sesiones-hijas, section 5).

The identity of the mother comes from the trusted host (`ToolContext`
`children_host`), never from the arguments. The host opens, lists and
messages children; merging a child's branch is never a tool.
"""

from __future__ import annotations

from typing import Any

from rinari.tools.definition import (
    RISK_LOW,
    RISK_MEDIUM,
    SIDE_EFFECT_LOCAL_REVERSIBLE,
    SIDE_EFFECT_NONE,
    ClassifiedAction,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)

START_DESCRIPTION = (
    "Open a child session: a separate conversation in this project (or chat) that starts "
    "a task now and reports back here when it finishes. Use it ONLY when the user asked "
    "for it in words (another/separate session, do it apart, in parallel); for your own "
    "ideas use followup.suggest. prompt must be self-contained: what, where, how to check "
    "it is done. context: brief (default) or fork (copies this conversation, for a "
    "subtask of the same topic while it is short). isolation: auto (own git worktree "
    "when the project is a repo), worktree or same."
)
OFFER_DESCRIPTION = (
    "Ask the user whether a task they asked for in this message should go to a new "
    "session (a child conversation) or be done here, and wait for the answer. Use it "
    "when the request is large, on another topic or could run in parallel. Same fields "
    "as session.start, plus reason (one line: why apart). If they choose here, do it here."
)
CHILDREN_DESCRIPTION = (
    "List the child sessions this conversation opened: state, branch and last report."
)
MESSAGE_DESCRIPTION = (
    "Send a follow-up to one of your own child sessions (more context, a correction). "
    "It is read now if the child is working, or starts its next turn."
)


def _run(host: Any, tool: str, args: dict[str, Any], ctx: Any) -> ToolResult:
    from rinari.engine_protocol.errors import EngineProtocolError
    from rinari.shared.errors import ConflictError, NotFoundError, RinariError

    try:
        return ToolResult(ok=True, data=host(tool, args or {}, ctx))
    except NotFoundError as exc:
        code, message = ToolErrorCode.NOT_FOUND, str(exc)
    except ConflictError as exc:
        code, message = ToolErrorCode.RESOURCE_EXHAUSTED, str(exc)
    except RinariError as exc:
        code, message = ToolErrorCode.INVALID_ARGUMENT, str(exc)
    except EngineProtocolError as exc:
        code, message = ToolErrorCode.DEPENDENCY_ERROR, exc.message
    return ToolResult(ok=False, error=ToolErrorInfo(code=code, message=message, retryable=False))


def child_tools(host: Any) -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="session.start",
            description=START_DESCRIPTION,
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "prompt"],
                "properties": {
                    "title": {"type": "string", "minLength": 3, "maxLength": 80},
                    "prompt": {"type": "string", "minLength": 10, "maxLength": 8000},
                    "done_when": {"type": "string", "maxLength": 500},
                    "context": {"type": "string", "enum": ["brief", "fork"]},
                    "isolation": {"type": "string", "enum": ["auto", "worktree", "same"]},
                },
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            idempotent=False,
            handler=lambda args, ctx: _run(host, "session.start", args, ctx),
            # The child runs under the mother's permissions, never more.
            classify=lambda _args: ClassifiedAction("agent.delegate"),
            capabilities=("agent.delegate",),
            namespace="session",
            always_loaded=False,
            timeout_ms=120_000,
            manifest={"source": "children"},
        ),
        ToolDefinition(
            name="session.offer",
            description=OFFER_DESCRIPTION,
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "prompt"],
                "properties": {
                    "title": {"type": "string", "minLength": 3, "maxLength": 80},
                    "prompt": {"type": "string", "minLength": 10, "maxLength": 8000},
                    "reason": {"type": "string", "maxLength": 300},
                    "done_when": {"type": "string", "maxLength": 500},
                    "context": {"type": "string", "enum": ["brief", "fork"]},
                    "isolation": {"type": "string", "enum": ["auto", "worktree", "same"]},
                },
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            idempotent=False,
            handler=lambda args, ctx: _run(host, "session.offer", args, ctx),
            # Nothing happens unless the user chooses it on the card.
            classify=lambda _args: ClassifiedAction("state.read"),
            capabilities=("state.read",),
            namespace="session",
            always_loaded=False,
            timeout_ms=3_600_000,
            manifest={"source": "children"},
        ),
        ToolDefinition(
            name="session.children",
            description=CHILDREN_DESCRIPTION,
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            idempotent=True,
            handler=lambda args, ctx: _run(host, "session.children", args, ctx),
            classify=lambda _args: ClassifiedAction("state.read"),
            capabilities=("state.read",),
            namespace="session",
            always_loaded=False,
            manifest={"source": "children"},
        ),
        ToolDefinition(
            name="session.message",
            description=MESSAGE_DESCRIPTION,
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["child_session_id", "message"],
                "properties": {
                    "child_session_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "message": {"type": "string", "minLength": 1, "maxLength": 8000},
                },
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            idempotent=False,
            handler=lambda args, ctx: _run(host, "session.message", args, ctx),
            classify=lambda _args: ClassifiedAction("agent.delegate"),
            capabilities=("agent.delegate",),
            namespace="session",
            always_loaded=False,
            timeout_ms=60_000,
            manifest={"source": "children"},
        ),
    ]


__all__ = ["child_tools"]
