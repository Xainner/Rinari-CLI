"""Peer messaging between agent sessions (Boards).

Identity comes from the trusted host binding (`ToolContext.peer_host`), never
from tool arguments: the model cannot name its own session, choose a group,
set hops or claim approval. `session.send` is classified as
`session.message` with the destination as target so policy and approvals
bind to exactly one receiver. A delivery is accepted, not executed: the
receiver runs its own turn under its own permissions.
"""

from __future__ import annotations

from rinari.policy.engine import CAPABILITY_SESSION_MESSAGE
from rinari.tools.definition import (
    RISK_HIGH,
    RISK_LOW,
    SIDE_EFFECT_COMMUNICATION,
    SIDE_EFFECT_NONE,
    ClassifiedAction,
    ToolDefinition,
)

MAX_PEER_MESSAGE_CHARS = 32_000


def peer_tools(host) -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="session.peers",
            description=(
                "List the agent sessions of this board you may message: id, label, project, "
                "model, whether they are busy and whether sending is currently allowed. "
                "Sessions outside the group are never listed."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={"type": "object"},
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            idempotent=True,
            classify=lambda _args: ClassifiedAction("state.read"),
            handler=lambda args, ctx: host("session.peers", args, ctx),
            namespace="session",
            capabilities=("state.read",),
            always_loaded=False,
            manifest={"source": "peers"},
        ),
        ToolDefinition(
            name="session.send",
            description=(
                "Send a text message to another agent session of this board. The receiver "
                "treats it as untrusted data from another agent (not as its owner's "
                "instruction) and answers in its own turn under its own permissions. "
                "Requires the owner's consent per destination; limited per turn and per chain."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "target_session_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "message": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_PEER_MESSAGE_CHARS,
                    },
                },
                "required": ["target_session_id", "message"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            risk=RISK_HIGH,
            side_effects=SIDE_EFFECT_COMMUNICATION,
            idempotent=False,
            classify=lambda args: ClassifiedAction(
                CAPABILITY_SESSION_MESSAGE, str(args.get("target_session_id") or "")
            ),
            handler=lambda args, ctx: host("session.send", args, ctx),
            namespace="session",
            capabilities=(CAPABILITY_SESSION_MESSAGE,),
            always_loaded=False,
            timeout_ms=30_000,
            manifest={"source": "peers"},
        ),
    ]


__all__ = ["MAX_PEER_MESSAGE_CHARS", "peer_tools"]
