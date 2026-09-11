"""Bounded, cancellable requests to the owning channel host over Engine Protocol."""

from __future__ import annotations

import hashlib
import json
import threading
import time

from rinari.engine_protocol.errors import INVALID_PARAMS, EngineProtocolError
from rinari.tools.definition import ToolErrorCode, ToolErrorInfo, ToolResult


def validate_channel(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"binding_id", "capabilities"}:
        raise EngineProtocolError(INVALID_PARAMS, "Invalid channel binding")
    if not isinstance(value["binding_id"], str) or not 1 <= len(value["binding_id"]) <= 128:
        raise EngineProtocolError(INVALID_PARAMS, "Invalid channel binding identity")
    allowed = {"channel.send_attachment", "channel.reply", "channel.delivery_get"}
    caps = value["capabilities"]
    if not isinstance(caps, list) or any(not isinstance(c, str) or c not in allowed for c in caps):
        raise EngineProtocolError(INVALID_PARAMS, "Invalid channel capabilities")
    return {"binding_id": value["binding_id"], "capabilities": sorted(set(caps))}


class ChannelBroker:
    def __init__(self):
        self.lock = threading.RLock()
        self.pending = {}

    def call(self, turn, emit, tool, arguments, ctx):
        binding = turn.channel
        if not binding or tool not in binding["capabilities"] or not ctx.tool_call_id:
            return ToolResult(
                ok=False,
                error=ToolErrorInfo(
                    ToolErrorCode.PERMISSION_DENIED, "Channel capability unavailable"
                ),
            )
        identity = hashlib.sha256((turn.operation_id + ":" + ctx.tool_call_id).encode()).hexdigest()
        payload = {
            "request_id": identity,
            "operation_id": turn.operation_id,
            "turn_id": turn.turn_id,
            "session_id": turn.session_id,
            "binding_id": binding["binding_id"],
            "tool": tool,
            "arguments": arguments,
        }
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        wait = threading.Event()
        entry = {"event": wait, "payload": payload, "fingerprint": fingerprint, "result": None}
        with self.lock:
            if identity in self.pending:
                raise EngineProtocolError(INVALID_PARAMS, "Channel call already pending")
            self.pending[identity] = entry
        try:
            emit("channel.requested", payload)
            deadline = min(ctx.deadline_at or float("inf"), time.time() + 120)
            while not wait.wait(0.05):
                if ctx.cancellation:
                    ctx.cancellation.throw_if_cancelled()
                if turn.cancel_requested.is_set() or turn.done.is_set():
                    return ToolResult(
                        ok=False,
                        error=ToolErrorInfo(ToolErrorCode.CANCELLED, "Channel call cancelled"),
                    )
                if time.time() >= deadline:
                    return ToolResult(
                        ok=False,
                        data={"delivery_id": identity, "state": "uncertain"},
                        error=ToolErrorInfo(
                            ToolErrorCode.TIMEOUT,
                            "Delivery not confirmed; query status before retrying",
                        ),
                    )
            result = entry["result"]
            if result.get("ok") is True:
                return ToolResult(ok=True, data=result.get("data", {}))
            return ToolResult(
                ok=False,
                data=result.get("data"),
                error=ToolErrorInfo(
                    ToolErrorCode(result["error"]["code"]), result["error"]["message"]
                ),
            )
        finally:
            with self.lock:
                self.pending.pop(identity, None)

    def resolve(self, params):
        with self.lock:
            entry = self.pending.get(params.get("request_id"))
            if entry is None:
                return {"status": "expired"}
            if params.get("binding_id") != entry["payload"]["binding_id"]:
                raise EngineProtocolError(INVALID_PARAMS, "Channel binding mismatch")
            result = params.get("result")
            if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                raise EngineProtocolError(INVALID_PARAMS, "Invalid channel result")
            if not result["ok"]:
                error = result.get("error", {})
                if error.get("code") not in set(ToolErrorCode) or not isinstance(
                    error.get("message"), str
                ):
                    raise EngineProtocolError(INVALID_PARAMS, "Invalid channel error")
            if entry["result"] is not None and entry["result"] != result:
                raise EngineProtocolError(INVALID_PARAMS, "Conflicting channel result")
            entry["result"] = result
            entry["event"].set()
            return {"status": "resolved"}

    def list(self):
        with self.lock:
            return {"requests": [e["payload"] for e in self.pending.values()]}
