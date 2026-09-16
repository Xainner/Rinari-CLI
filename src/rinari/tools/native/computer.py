"""Native desktop-control tools (computer use) over the session GraphicControlService.

Capability model:

- computer.state reports service status (grantless discovery).
- computer.capture classifies computer.observe; embedding the capture as a
  model image additionally needs the grant send scope.
- computer.click and computer.type classify computer.operate (high risk).
- Grants are issued ONLY by the session host on explicit user gesture.
"""

from __future__ import annotations

from typing import Any

from rinari.computer.backend import ComputerError
from rinari.computer.grants import GrantDenied
from rinari.computer.service import GraphicControlService
from rinari.tools.definition import (
    RISK_HIGH,
    RISK_LOW,
    ArtifactRef,
    ClassifiedAction,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)

_CODE_MAP: dict[str, ToolErrorCode] = {
    "BACKEND_UNAVAILABLE": ToolErrorCode.DEPENDENCY_ERROR,
    "TARGET_NOT_FOUND": ToolErrorCode.NOT_FOUND,
    "CAPTURE_FAILED": ToolErrorCode.UNKNOWN,
    "INPUT_FAILED": ToolErrorCode.UNKNOWN,
    "INVALID_ARGUMENT": ToolErrorCode.INVALID_ARGUMENT,
    "RESOURCE_EXHAUSTED": ToolErrorCode.RESOURCE_EXHAUSTED,
    "CANCELLED": ToolErrorCode.CANCELLED,
}

MAX_TYPE_CHARS = 500


def _service(ctx: ToolContext) -> GraphicControlService | None:
    service = getattr(ctx, "computer", None)
    return service if isinstance(service, GraphicControlService) else None


def _cancelled_fn(ctx: ToolContext):
    import time

    token = getattr(ctx, "cancellation", None)
    return lambda: (
        bool(token and token.cancelled)
        or (ctx.deadline_at is not None and time.time() >= ctx.deadline_at)
    )


def _no_service() -> ToolResult:
    return ToolResult(
        ok=False,
        error=ToolErrorInfo(
            code=ToolErrorCode.DEPENDENCY_ERROR,
            message="graphic-control service is not available in this session",
        ),
    )


def _computer_error(exc: ComputerError) -> ToolResult:
    code = _CODE_MAP.get(exc.code, ToolErrorCode.UNKNOWN)
    return ToolResult(
        ok=False,
        error=ToolErrorInfo(code=code, message=exc.message, retryable=exc.retryable),
    )


def _grant_error(exc: GrantDenied) -> ToolResult:
    return ToolResult(
        ok=False,
        error=ToolErrorInfo(
            code=ToolErrorCode.PERMISSION_DENIED,
            message=str(exc),
            details={"next_action": "The user must issue a graphic-control grant first."},
        ),
    )


def _observe_classify(_input: dict[str, Any]) -> ClassifiedAction:
    return ClassifiedAction("computer.observe")


def _operate_classify(_input: dict[str, Any]) -> ClassifiedAction:
    return ClassifiedAction("computer.operate")


def _require_target(input: dict) -> str | ToolResult:
    target = input.get("target")
    if not isinstance(target, str) or not target:
        return ToolResult(
            ok=False,
            error=ToolErrorInfo(
                code=ToolErrorCode.INVALID_ARGUMENT,
                message="target must be a non-empty authorized surface id",
            ),
        )
    return target


def computer_state(input: dict, ctx: ToolContext) -> ToolResult:
    service = _service(ctx)
    if service is None:
        return _no_service()
    return ToolResult(ok=True, data=service.state())


def computer_capture(input: dict, ctx: ToolContext) -> ToolResult:
    from rinari.models.images import ImageReference

    service = _service(ctx)
    if service is None:
        return _no_service()
    target = _require_target(input)
    if isinstance(target, ToolResult):
        return target
    include_images = input.get("include_images", True)
    if not isinstance(include_images, bool):
        return ToolResult(
            ok=False,
            error=ToolErrorInfo(
                code=ToolErrorCode.INVALID_ARGUMENT,
                message="include_images must be a boolean",
            ),
        )
    try:
        observed = service.capture(
            target, send_to_model=include_images, cancelled=_cancelled_fn(ctx)
        )
    except GrantDenied as exc:
        return _grant_error(exc)
    except ComputerError as exc:
        return _computer_error(exc)
    data = {k: v for k, v in observed.items() if k != "record"}
    artifacts = (ArtifactRef(observed["uri"], observed["uri"].rsplit("/", 1)[-1], "screenshot"),)
    if not include_images:
        return ToolResult(ok=True, data={**data, "visual": False}, artifacts=artifacts)
    store = ctx.artifact_store if ctx.artifact_store is not None else service.artifact_store
    try:
        record = observed["record"]
        ref = ImageReference(
            record.uri(),
            store._storage_path(record.storage_path),
            record.sha256,
            record.content_type,
        )
        ref.encoded()
        from PIL import Image as _PILImage

        with _PILImage.open(ref.path) as _img:
            data["width"], data["height"] = _img.size
        sent_scale = min(1.0, 2048.0 / max(data["width"], data["height"]))
        data["sent_scale"] = sent_scale
        data["sent_width"] = int(data["width"] * sent_scale)
        data["sent_height"] = int(data["height"] * sent_scale)
        data["visual"] = True
        return ToolResult(
            ok=True,
            data=data,
            images=(ref,),
            artifacts=artifacts,
            presentation={"kind": "image", "image": data},
        )
    except Exception as vex:
        data["visual"] = False
        data["visual_error"] = vex.__class__.__name__ + ": " + str(vex)
        return ToolResult(ok=True, data=data, artifacts=artifacts)


def _require_number(value: object, name: str) -> float | ToolResult:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ToolResult(
            ok=False,
            error=ToolErrorInfo(
                code=ToolErrorCode.INVALID_ARGUMENT,
                message=name + " must be a number",
            ),
        )
    return float(value)


def _require_observation_id(input: dict) -> str | ToolResult | None:
    observation_id = input.get("observation_id")
    if observation_id is not None and not isinstance(observation_id, str):
        return ToolResult(
            ok=False,
            error=ToolErrorInfo(
                code=ToolErrorCode.INVALID_ARGUMENT,
                message="observation_id must be a string",
            ),
        )
    return observation_id


def computer_click(input: dict, ctx: ToolContext) -> ToolResult:
    service = _service(ctx)
    if service is None:
        return _no_service()
    target = _require_target(input)
    if isinstance(target, ToolResult):
        return target
    x = _require_number(input.get("x"), "x")
    if isinstance(x, ToolResult):
        return x
    y = _require_number(input.get("y"), "y")
    if isinstance(y, ToolResult):
        return y
    observation_id = _require_observation_id(input)
    if isinstance(observation_id, ToolResult):
        return observation_id
    try:
        entry = service.click(
            target,
            x,
            y,
            observation_id=observation_id,
            cancelled=_cancelled_fn(ctx),
        )
    except GrantDenied as exc:
        return _grant_error(exc)
    except ComputerError as exc:
        return _computer_error(exc)
    return ToolResult(
        ok=True,
        data={
            "action_id": entry["action_id"],
            "dispatch": entry["dispatch"],
            "clicked": entry["result"]["clicked"],
            "target_id": target,
            "observation_id": observation_id,
        },
    )


def computer_type(input: dict, ctx: ToolContext) -> ToolResult:
    service = _service(ctx)
    if service is None:
        return _no_service()
    target = _require_target(input)
    if isinstance(target, ToolResult):
        return target
    text = input.get("text")
    if not isinstance(text, str) or not text:
        return ToolResult(
            ok=False,
            error=ToolErrorInfo(
                code=ToolErrorCode.INVALID_ARGUMENT,
                message="text must be a non-empty string",
            ),
        )
    if len(text) > MAX_TYPE_CHARS:
        return ToolResult(
            ok=False,
            error=ToolErrorInfo(
                code=ToolErrorCode.INVALID_ARGUMENT,
                message="text is limited to 500 characters",
            ),
        )
    submit = input.get("submit", False)
    if not isinstance(submit, bool):
        return ToolResult(
            ok=False,
            error=ToolErrorInfo(
                code=ToolErrorCode.INVALID_ARGUMENT,
                message="submit must be a boolean",
            ),
        )
    observation_id = _require_observation_id(input)
    if isinstance(observation_id, ToolResult):
        return observation_id
    try:
        entry = service.type_text(
            target,
            text,
            submit=submit,
            observation_id=observation_id,
            cancelled=_cancelled_fn(ctx),
        )
    except GrantDenied as exc:
        return _grant_error(exc)
    except ComputerError as exc:
        return _computer_error(exc)
    return ToolResult(
        ok=True,
        data={
            "action_id": entry["action_id"],
            "dispatch": entry["dispatch"],
            "typed": entry["result"]["typed"],
            "submit": entry["result"]["submit"],
            "target_id": target,
            "observation_id": observation_id,
        },
    )


def computer_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="computer.state",
            description="Graphic-control status: backend, authorized targets, active observations.",
            input_schema={"type": "object", "properties": {}},
            risk=RISK_LOW,
            side_effects="none",
            classify=_observe_classify,
            handler=computer_state,
            namespace="computer",
            capabilities=("computer.observe",),
            always_loaded=False,
        ),
        ToolDefinition(
            name="computer.capture",
            description=(
                "Capture the authorized graphic target as PNG; returns an immutable "
                "artifact URI plus a visual observation when the grant covers sending."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "include_images": {"type": "boolean", "default": True},
                },
                "required": ["target"],
            },
            risk=RISK_LOW,
            side_effects="local-reversible",
            classify=_observe_classify,
            handler=computer_capture,
            namespace="computer",
            capabilities=("computer.observe",),
            always_loaded=False,
        ),
        ToolDefinition(
            name="computer.click",
            description="Click at x/y pixels on the authorized graphic target.",
            input_schema={
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                    "observation_id": {"type": "string"},
                },
                "required": ["target", "x", "y"],
            },
            risk=RISK_HIGH,
            side_effects="remote-reversible",
            classify=_operate_classify,
            handler=computer_click,
            namespace="computer",
            capabilities=("computer.operate",),
            always_loaded=False,
            idempotent=False,
        ),
        ToolDefinition(
            name="computer.type",
            description="Type text into the authorized graphic target; optionally submit.",
            input_schema={
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "text": {"type": "string"},
                    "submit": {"type": "boolean", "default": False},
                    "observation_id": {"type": "string"},
                },
                "required": ["target", "text"],
            },
            risk=RISK_HIGH,
            side_effects="remote-reversible",
            classify=_operate_classify,
            handler=computer_type,
            namespace="computer",
            capabilities=("computer.operate",),
            always_loaded=False,
            idempotent=False,
        ),
    ]


__all__ = ["computer_tools"]
