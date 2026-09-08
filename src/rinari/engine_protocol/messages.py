"""Engine Protocol wire envelopes (AGENTS.md Rinari Code §6.3-6.5)."""

from __future__ import annotations

from typing import Any

from rinari.engine_protocol import protocol


def hello() -> dict[str, Any]:
    return {
        "type": "hello",
        "protocol": protocol.PROTOCOL_NAME,
        "protocol_version": protocol.PROTOCOL_VERSION,
        "engine_version": protocol.engine_version(),
        "capabilities": dict(protocol.CAPABILITIES),
    }


def success(request_id: Any, result: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": request_id, "ok": True, "result": result or {}}


def failure(
    request_id: Any,
    code: str,
    message: str,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": request_id,
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "retryable": bool(retryable),
            "details": details or {},
        },
    }


def event(event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": "event", "event": event_type, "payload": payload or {}}
