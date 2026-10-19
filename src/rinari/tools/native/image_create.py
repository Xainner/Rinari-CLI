"""Create and edit images (plan 0.4.0 #28): `image.generate`, `image.edit`,
`image.variations`, `image.upscale` and `image.remove_background`.

The tools ask the connector the owner chose in Ajustes → Visión e imágenes →
Creación de imágenes (`rinari.images`). Without one they say so and stop:
Rinari never improvises an image with other tools. Each result is a session
artifact, delivered with the same card as `file.present`, and comes back to
the model as an image so it can look at it before handing it over. Paying for
an image asks first in read-only and project mode, with its estimated cost;
full access does not ask, as with everything else (permissions v4).
"""

from __future__ import annotations

import io
import re
import time
from typing import Any

from rinari.images.connectors import (
    QUALITIES,
    SIZES,
    ImageConnectorError,
    ImageJob,
    InputImage,
)
from rinari.images.service import MEDIA_EVENT, ImageService
from rinari.models.images import ImageReference
from rinari.tools.definition import (
    RISK_MEDIUM,
    SIDE_EFFECT_REMOTE_REVERSIBLE,
    ArtifactRef,
    ClassifiedAction,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)
from rinari.tools.native.fs import _resolve_read

#: The capability policy decides on: asks with the cost, except in full access.
CAPABILITY = "media.generate"
MAX_INPUT_BYTES = 20 * 1024 * 1024
NO_CONNECTOR = (
    "No image connector is set up, so I cannot create or edit images. Tell the owner: "
    "they can add one in Ajustes → Visión e imágenes → Creación de imágenes. Do not "
    "try to make the image another way."
)
REVIEW_NOTE = (
    "Look at the image before delivering it. If it does not match what was asked "
    "(wrong subject, text, hands, missing parts), say so and edit or regenerate it, "
    "at most twice."
)
_CODES = {code.value for code in ToolErrorCode}


def _fail(
    code: str, message: str, *, retryable: bool = False, details: dict | None = None
) -> ToolResult:
    value = code if code in _CODES else ToolErrorCode.DEPENDENCY_ERROR.value
    return ToolResult(
        ok=False,
        error=ToolErrorInfo(
            code=ToolErrorCode(value), message=message, retryable=retryable, details=details
        ),
    )


class ImageHost:
    def __init__(self, services: Any) -> None:
        self.service = ImageService(services)
        self._services = services

    # -- jobs from the arguments -------------------------------------------------

    def job(
        self, operation: str, args: dict[str, Any], ctx: ToolContext | None
    ) -> tuple[ImageJob | None, ToolResult | None]:
        images: list[InputImage] = []
        sources = []
        if operation == "generate":
            sources = list(args.get("references") or [])[:4]
        elif args.get("image"):
            sources = [args["image"]]
        if ctx is not None:
            for source in sources:
                loaded, error = _load(ctx, source)
                if error is not None:
                    return None, error
                images.append(loaded)
        mask = None
        if args.get("mask") and ctx is not None:
            mask, error = _load(ctx, args["mask"])
            if error is not None:
                return None, error
        count = args.get("count")
        if not isinstance(count, int) or count < 1:
            count = 2 if operation == "variations" else 1
        return ImageJob(
            operation=operation,
            prompt=str(args.get("prompt") or "").strip(),
            images=tuple(images),
            mask=mask,
            size=args.get("size") if args.get("size") in SIZES else "auto",
            quality=args.get("quality") if args.get("quality") in QUALITIES else "auto",
            count=min(count, 4),
            transparent=bool(args.get("transparent")),
        ), None

    def classify(self, operation: str, args: dict[str, Any]) -> ClassifiedAction:
        connector = self._connector_or_none(operation)
        if connector is None:
            return ClassifiedAction(CAPABILITY, target="no connector")
        job, _ = self.job(operation, args, None)
        estimate = connector.estimate(job) if job else None
        what = f"{job.count} x {job.size}" if job else "1"
        cost = estimate.label() if estimate else "cost unknown"
        model = connector.model or "default model"
        return ClassifiedAction(
            CAPABILITY,
            target=f"{connector.label} · {model} · {what} · {cost}",
            cost_basis=estimate.basis if estimate else None,
        )

    def _connector_or_none(self, operation: str):
        try:
            return self.service.connector(operation)
        except ImageConnectorError:
            return None

    def precheck(self, operation: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult | None:
        try:
            connector = self.service.connector(operation)
        except ImageConnectorError as exc:
            return _fail(exc.code, str(exc))
        if connector is None:
            return _fail(
                ToolErrorCode.UNSUPPORTED.value,
                NO_CONNECTOR,
                details={"reason": "NO_IMAGE_CONNECTOR"},
            )
        if not connector.supports(operation):
            return _fail(
                ToolErrorCode.UNSUPPORTED.value,
                f"{connector.label} cannot {operation.replace('_', ' ')}. Tell the owner; another "
                "connector (for example FAL.ai or a local ComfyUI) can do it.",
                details={"reason": "OPERATION_UNSUPPORTED", "connector": connector.id},
            )
        if operation != "generate" and not args.get("image"):
            return _fail(ToolErrorCode.INVALID_ARGUMENT.value, "image is required")
        if operation in ("generate", "edit") and not str(args.get("prompt") or "").strip():
            return _fail(ToolErrorCode.INVALID_ARGUMENT.value, "prompt is required")
        job, _ = self.job(operation, args, None)
        reason = self.service.over_budget(ctx.session_id, connector.estimate(job)) if job else None
        if reason:
            return _fail(
                ToolErrorCode.RESOURCE_EXHAUSTED.value,
                reason + ". Tell the owner; they can raise it in Ajustes.",
                details={"reason": "IMAGE_BUDGET"},
            )
        return None

    # -- the call -------------------------------------------------------------

    def run(self, operation: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        rejected = self.precheck(operation, args, ctx)
        if rejected is not None:
            return rejected
        connector = self.service.connector(operation)
        job, error = self.job(operation, args, ctx)
        if error is not None:
            return error
        try:
            batch = connector.run(job)
        except ImageConnectorError as exc:
            return _fail(exc.code, str(exc), retryable=exc.retryable)
        except Exception as exc:  # network and the like: never crash the turn
            return _fail(
                ToolErrorCode.NETWORK_ERROR.value, f"{connector.label}: {exc}", retryable=True
            )
        store = ctx.artifact_store
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = _slug(args.get("name")) or operation.replace("_", "-")
        saved: list[dict[str, Any]] = []
        refs: list[ImageReference] = []
        artifacts: list[ArtifactRef] = []
        items: list[dict[str, Any]] = []
        for index, image in enumerate(batch.images, 1):
            name = f"{base}-{stamp}-{index}.png" if len(batch.images) > 1 else f"{base}-{stamp}.png"
            record = store.create(
                ctx.session_id,
                "media",
                name,
                image.data,
                content_type=image.mime,
                summary=(job.prompt or operation)[:200],
                provenance=f"image.{operation}:{connector.id}:{batch.model}",
            )
            uri = record.uri()
            path = store._storage_path(record.storage_path)
            width, height = _size(image.data)
            info = {
                "uri": uri,
                "name": name,
                "path": str(path),
                "bytes": len(image.data),
                "sha256": record.sha256,
                "width": width,
                "height": height,
                "mime_type": image.mime,
                "markdown": f"![{name}]({uri})",
            }
            if image.revised_prompt:
                info["revised_prompt"] = image.revised_prompt
            saved.append(info)
            refs.append(ImageReference(uri, path, record.sha256, image.mime))
            artifacts.append(ArtifactRef(uri, name, "image"))
            items.append(
                {
                    "path": str(path),
                    "name": name,
                    "caption": job.prompt[:160] or None,
                    "uri": uri,
                    "kind": "image",
                    "mime": image.mime,
                    "size_bytes": len(image.data),
                }
            )
        cost = batch.cost_usd
        estimate = connector.estimate(job)
        self._record(
            ctx,
            {
                "tool_call_id": ctx.tool_call_id,
                "operation": operation,
                "connector": connector.id,
                "kind": connector.kind,
                "model": batch.model,
                "images": len(saved),
                "cost_usd": cost,
                "cost_basis": estimate.basis,
            },
        )
        sink = getattr(ctx, "activity_sink", None)
        if callable(sink):
            sink(
                "file.presented",
                {"present_id": ctx.tool_call_id or f"image-{stamp}", "items": items},
            )
        data = {
            "operation": operation,
            "connector": connector.label,
            "model": batch.model,
            "cost": estimate.label()
            if cost is None or estimate.basis == "included"
            else f"${cost:.3f}",
            "images": saved,
            "note": REVIEW_NOTE,
        }
        return ToolResult(ok=True, data=data, images=tuple(refs), artifacts=tuple(artifacts))

    def _record(self, ctx: ToolContext, payload: dict[str, Any]) -> None:
        """The spend ledger: the desktop sink persists it; the CLI writes it directly."""
        sink = getattr(ctx, "activity_sink", None)
        if callable(sink):
            sink(MEDIA_EVENT, payload)
            return
        try:
            from rinari.cli.agent_runtime import _persist_event

            _persist_event(self._services, ctx.session_id, MEDIA_EVENT, payload)
        except Exception:
            pass


def _slug(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")[:40]


def _size(data: bytes) -> tuple[int | None, int | None]:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            return image.width, image.height
    except Exception:
        return None, None


def _load(ctx: ToolContext, source: Any) -> tuple[InputImage | None, ToolResult | None]:
    path, error = _resolve_read(ctx, source)
    if error is not None:
        return None, error
    if path.stat().st_size > MAX_INPUT_BYTES:
        return None, _fail(ToolErrorCode.RESOURCE_EXHAUSTED.value, f"{source} is larger than 20 MB")
    data = path.read_bytes()
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            fmt = (image.format or "").upper()
    except Exception:
        return None, _fail(ToolErrorCode.INVALID_ARGUMENT.value, f"{source} is not an image")
    mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(fmt)
    if mime is None:
        return None, _fail(ToolErrorCode.INVALID_ARGUMENT.value, f"{source}: use PNG, JPEG or WEBP")
    return InputImage(data, mime, path.name), None


_IMAGE_REF = {
    "type": "string",
    "minLength": 1,
    "maxLength": 2048,
    "description": "A workspace path or an artifact:// image of this conversation.",
}
_SIZE = {"type": "string", "enum": list(SIZES)}
_QUALITY = {"type": "string", "enum": list(QUALITIES)}

DESCRIPTIONS = {
    "generate": (
        "Create images from a text prompt (optionally with up to 4 reference images). "
        "Returns the images as files of this conversation, shows them to the owner and "
        "lets you look at them. Paid services ask the owner first with the cost."
    ),
    "edit": (
        "Edit an image with a prompt: change a part, add or remove something, change the "
        "background. An optional mask marks the area to change."
    ),
    "variations": "Make variations of an image that keep its subject and style.",
    "upscale": "Enlarge an image with more detail (only connectors that support it).",
    "remove_background": "Remove the background of an image, leaving the subject on transparency.",
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "generate": {
        "type": "object",
        "additionalProperties": False,
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string", "minLength": 1, "maxLength": 8000},
            "size": _SIZE,
            "quality": _QUALITY,
            "count": {"type": "integer", "minimum": 1, "maximum": 4},
            "transparent": {"type": "boolean"},
            "references": {"type": "array", "maxItems": 4, "items": _IMAGE_REF},
            "name": {
                "type": "string",
                "maxLength": 60,
                "description": "Short file name, without extension.",
            },
        },
    },
    "edit": {
        "type": "object",
        "additionalProperties": False,
        "required": ["image", "prompt"],
        "properties": {
            "image": _IMAGE_REF,
            "prompt": {"type": "string", "minLength": 1, "maxLength": 8000},
            "mask": _IMAGE_REF,
            "size": _SIZE,
            "quality": _QUALITY,
            "transparent": {"type": "boolean"},
            "name": {"type": "string", "maxLength": 60},
        },
    },
    "variations": {
        "type": "object",
        "additionalProperties": False,
        "required": ["image"],
        "properties": {
            "image": _IMAGE_REF,
            "prompt": {"type": "string", "maxLength": 2000},
            "count": {"type": "integer", "minimum": 1, "maximum": 4},
            "size": _SIZE,
            "name": {"type": "string", "maxLength": 60},
        },
    },
    "upscale": {
        "type": "object",
        "additionalProperties": False,
        "required": ["image"],
        "properties": {
            "image": _IMAGE_REF,
            "scale": {"type": "integer", "enum": [2, 4]},
            "name": {"type": "string", "maxLength": 60},
        },
    },
    "remove_background": {
        "type": "object",
        "additionalProperties": False,
        "required": ["image"],
        "properties": {"image": _IMAGE_REF, "name": {"type": "string", "maxLength": 60}},
    },
}


def image_create_tools(host: ImageHost) -> list[ToolDefinition]:
    # A service the owner chose loads these tools from the start and says
    # which one it is; without one they stay on demand and say there is none
    # (plan 0.4.0 v2, V2-1). The name comes from the owner's settings.
    try:
        label = host.service.default_label()
    except Exception:
        label = None
    chosen = (
        f" Uses the image service the owner chose: {label}. For images use these tools, "
        "not another skill or MCP server, unless the owner names one."
        if label
        else ""
    )
    tools = []
    for operation in ("generate", "edit", "variations", "upscale", "remove_background"):
        tools.append(
            ToolDefinition(
                name=f"image.{operation}",
                description=DESCRIPTIONS[operation] + chosen,
                input_schema=SCHEMAS[operation],
                capabilities=(CAPABILITY,),
                risk=RISK_MEDIUM,
                side_effects=SIDE_EFFECT_REMOTE_REVERSIBLE,
                # Paid calls are never retried on their own.
                idempotent=False,
                timeout_ms=10 * 60_000,
                handler=lambda args, ctx, op=operation: host.run(op, args, ctx),
                classify=lambda args, op=operation: host.classify(op, args),
                precheck=lambda args, ctx, op=operation: host.precheck(op, args, ctx),
                namespace="image",
                always_loaded=bool(label),
                manifest={"source": "images", **({"configured_service": label} if label else {})},
            )
        )
    return tools
