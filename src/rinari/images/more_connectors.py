"""More image connectors (plan 0.4.0 block 29): FAL.ai, Google (Gemini),
OpenRouter, a local ComfyUI and an assigned MCP server.

Each one wraps a documented API and declares what it can do; nothing is
written for one particular server. Costs are estimates shown before running
(`CostEstimate`); a service that does not say its price shows «cost unknown».
"""

from __future__ import annotations

import base64
import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from rinari.images.connectors import (
    CostEstimate,
    ImageBatch,
    ImageConnector,
    ImageConnectorError,
    ImageJob,
    ImageOutput,
    _http_error,
    effective_prompt,
)

_DATA_URL = re.compile(r"^data:(image/[a-z0-9.+-]+);base64,(.+)$", re.IGNORECASE | re.DOTALL)
_HTTP_URL = re.compile(r"https?://[^\s\"'<>)]+", re.IGNORECASE)


def _data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _decode_data_url(value: str) -> ImageOutput | None:
    match = _DATA_URL.match(value or "")
    if not match:
        return None
    return ImageOutput(base64.b64decode(match.group(2)), match.group(1).lower())


def _download(client: httpx.Client, url: str, label: str) -> ImageOutput:
    response = client.get(url)
    if response.status_code >= 400:
        raise _http_error(response, label)
    mime = (response.headers.get("content-type") or "image/png").split(";")[0].strip().lower()
    if not mime.startswith("image/"):
        raise ImageConnectorError("PROVIDER_ERROR", f"{label} returned {mime}, not an image")
    return ImageOutput(response.content, mime)


# -- FAL.ai ---------------------------------------------------------------------

#: One model per operation (FAL's documented ids) and its price per image (USD,
#: fal.ai pricing pages, 2026; an estimate).
FAL_MODELS = {
    "generate": ("fal-ai/flux/schnell", 0.003),
    "edit": ("fal-ai/flux-pro/kontext", 0.04),
    "variations": ("fal-ai/flux-pro/kontext", 0.04),
    "upscale": ("fal-ai/clarity-upscaler", 0.03),
    "remove_background": ("fal-ai/birefnet", 0.002),
}
FAL_SIZES = {"1024x1024": "square_hd", "1024x1536": "portrait_4_3", "1536x1024": "landscape_4_3"}


@dataclass
class FalImagesConnector(ImageConnector):
    api_key: str = field(default="", repr=False)
    endpoint: str = "https://fal.run"
    #: Overrides per operation: {"generate": "fal-ai/flux/dev", …}.
    models: dict[str, str] = field(default_factory=dict)

    def supports(self, operation: str) -> bool:
        return operation in FAL_MODELS

    def _model_for(self, operation: str) -> str:
        return self.models.get(operation) or (
            self.model if operation == "generate" and self.model else FAL_MODELS[operation][0]
        )

    def estimate(self, job: ImageJob) -> CostEstimate:
        if job.operation not in FAL_MODELS:
            return CostEstimate(None, "unknown")
        if self._model_for(job.operation) != FAL_MODELS[job.operation][0]:
            return CostEstimate(None, "unknown")
        return CostEstimate(FAL_MODELS[job.operation][1] * max(1, job.count), "per_image")

    def _arguments(self, job: ImageJob) -> dict[str, Any]:
        args: dict[str, Any] = {}
        prompt = effective_prompt(job)
        if job.operation in ("generate", "edit", "variations") and prompt:
            args["prompt"] = prompt
        if job.operation in ("generate", "variations"):
            args["num_images"] = max(1, min(job.count, 4))
        if job.operation == "generate" and job.size in FAL_SIZES:
            args["image_size"] = FAL_SIZES[job.size]
        if job.images:
            first = job.images[0]
            args["image_url"] = _data_url(first.data, first.mime)
        if job.operation == "upscale":
            args["upscale_factor"] = 2
        return args

    def run(self, job: ImageJob) -> ImageBatch:
        if not self.supports(job.operation):
            raise ImageConnectorError("UNSUPPORTED", f"{self.label} cannot {job.operation}")
        model = self._model_for(job.operation)
        with self._client(600) as client:
            response = client.post(
                f"{self.endpoint}/{model}",
                headers={"Authorization": f"Key {self.api_key}"},
                json=self._arguments(job),
            )
            if response.status_code >= 400:
                raise _http_error(response, self.label)
            body = response.json()
            entries = body.get("images") or (
                [body["image"]] if isinstance(body.get("image"), dict) else []
            )
            images = []
            for entry in entries:
                url = entry.get("url") if isinstance(entry, dict) else None
                if not url:
                    continue
                inline = _decode_data_url(url)
                images.append(inline or _download(client, url, self.label))
        if not images:
            raise ImageConnectorError("PROVIDER_ERROR", f"{self.label} returned no image")
        return ImageBatch(tuple(images), self.estimate(job).usd, model)

    def check(self) -> dict[str, Any]:
        if not self.api_key:
            raise ImageConnectorError("AUTH_REQUIRED", f"{self.label} has no API key")
        # FAL has no free «whoami»; a key that looks wrong is caught on first use.
        return {"ok": True, "model": self._model_for("generate")}


# -- Google (Gemini image models) -------------------------------------------------

#: gemini-2.5-flash-image («Nano Banana»): USD per output image (Google AI
#: pricing, 2026; an estimate).
GOOGLE_PRICE = 0.039


@dataclass
class GoogleImagesConnector(ImageConnector):
    api_key: str = field(default="", repr=False)
    endpoint: str = "https://generativelanguage.googleapis.com/v1beta"

    def supports(self, operation: str) -> bool:
        return operation in ("generate", "edit", "variations", "remove_background")

    def _model(self) -> str:
        return self.model or "gemini-2.5-flash-image"

    def estimate(self, job: ImageJob) -> CostEstimate:
        if self.model and self.model != "gemini-2.5-flash-image":
            return CostEstimate(None, "unknown")
        return CostEstimate(GOOGLE_PRICE * max(1, job.count), "per_image")

    def run(self, job: ImageJob) -> ImageBatch:
        if not self.supports(job.operation):
            raise ImageConnectorError("UNSUPPORTED", f"{self.label} cannot {job.operation}")
        parts: list[dict[str, Any]] = [{"text": effective_prompt(job)}]
        for image in job.images:
            parts.append(
                {
                    "inline_data": {
                        "mime_type": image.mime,
                        "data": base64.b64encode(image.data).decode("ascii"),
                    }
                }
            )
        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {"responseModalities": ["IMAGE"]},
        }
        outputs: list[ImageOutput] = []
        with self._client(300) as client:
            for _ in range(max(1, min(job.count, 4))):
                response = client.post(
                    f"{self.endpoint}/models/{self._model()}:generateContent",
                    headers={"x-goog-api-key": self.api_key},
                    json=payload,
                )
                if response.status_code >= 400:
                    raise _http_error(response, self.label)
                for candidate in response.json().get("candidates", []):
                    for part in (candidate.get("content") or {}).get("parts", []):
                        blob = part.get("inline_data") or part.get("inlineData")
                        if isinstance(blob, dict) and blob.get("data"):
                            mime = blob.get("mime_type") or blob.get("mimeType") or "image/png"
                            outputs.append(ImageOutput(base64.b64decode(blob["data"]), mime))
        if not outputs:
            raise ImageConnectorError(
                "PROVIDER_ERROR", f"{self.label} returned no image (it may have refused the prompt)"
            )
        return ImageBatch(tuple(outputs), self.estimate(job).usd, self._model())

    def check(self) -> dict[str, Any]:
        with self._client(30) as client:
            response = client.get(
                f"{self.endpoint}/models/{self._model()}", headers={"x-goog-api-key": self.api_key}
            )
        if response.status_code >= 400:
            raise _http_error(response, self.label)
        return {"ok": True, "model": self._model()}


# -- OpenRouter -------------------------------------------------------------------


@dataclass
class OpenRouterImagesConnector(ImageConnector):
    """Models that answer images through OpenRouter's chat completions
    (`modalities: ["image", "text"]`). The key is the one of the OpenRouter
    provider the owner already has, or its own."""

    api_key: str = field(default="", repr=False)
    endpoint: str = "https://openrouter.ai/api/v1"

    def supports(self, operation: str) -> bool:
        return operation in ("generate", "edit", "variations", "remove_background")

    def _model(self) -> str:
        return self.model or "google/gemini-2.5-flash-image"

    def estimate(self, job: ImageJob) -> CostEstimate:
        # OpenRouter prices each model differently; it reports the cost after.
        return CostEstimate(None, "unknown")

    def run(self, job: ImageJob) -> ImageBatch:
        if not self.supports(job.operation):
            raise ImageConnectorError("UNSUPPORTED", f"{self.label} cannot {job.operation}")
        content: list[dict[str, Any]] = [{"type": "text", "text": effective_prompt(job)}]
        for image in job.images:
            content.append(
                {"type": "image_url", "image_url": {"url": _data_url(image.data, image.mime)}}
            )
        payload = {
            "model": self._model(),
            "messages": [{"role": "user", "content": content}],
            "modalities": ["image", "text"],
            "usage": {"include": True},
        }
        outputs: list[ImageOutput] = []
        cost = 0.0
        priced = False
        with self._client(300) as client:
            for _ in range(max(1, min(job.count, 4))):
                response = client.post(
                    f"{self.endpoint}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
                if response.status_code >= 400:
                    raise _http_error(response, self.label)
                body = response.json()
                usage = body.get("usage") or {}
                if isinstance(usage.get("cost"), (int, float)):
                    cost += float(usage["cost"])
                    priced = True
                for choice in body.get("choices", []):
                    for image in (choice.get("message") or {}).get("images") or []:
                        url = ((image or {}).get("image_url") or {}).get("url") or ""
                        inline = _decode_data_url(url)
                        if inline is not None:
                            outputs.append(inline)
                        elif url.startswith("http"):
                            outputs.append(_download(client, url, self.label))
        if not outputs:
            raise ImageConnectorError(
                "PROVIDER_ERROR", f"{self.label}: {self._model()} returned no image"
            )
        return ImageBatch(tuple(outputs), round(cost, 6) if priced else None, self._model())

    def check(self) -> dict[str, Any]:
        with self._client(30) as client:
            response = client.get(
                f"{self.endpoint}/models", headers={"Authorization": f"Bearer {self.api_key}"}
            )
        if response.status_code >= 400:
            raise _http_error(response, self.label)
        return {"ok": True, "model": self._model()}


# -- ComfyUI (local) --------------------------------------------------------------


@dataclass
class ComfyUIConnector(ImageConnector):
    """A ComfyUI server on this PC or the LAN, with one workflow (API format)
    per operation the owner set up. In the workflow, the strings `{{prompt}}`
    and `{{image}}` are replaced by the prompt and the uploaded input image.
    Free: it runs on the owner's GPU."""

    endpoint: str = "http://127.0.0.1:8188"
    #: {"generate": {...workflow...}, "upscale": {...}}
    workflows: dict[str, dict[str, Any]] = field(default_factory=dict)
    poll_interval_s: float = 1.0
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)

    def supports(self, operation: str) -> bool:
        return operation in self.workflows

    def estimate(self, job: ImageJob) -> CostEstimate:
        return CostEstimate(0.0, "local")

    def run(self, job: ImageJob) -> ImageBatch:
        if not self.supports(job.operation):
            raise ImageConnectorError(
                "UNSUPPORTED", f"{self.label} has no workflow to {job.operation}"
            )
        outputs: list[ImageOutput] = []
        with self._client(900) as client:
            uploaded = ""
            if job.images:
                first = job.images[0]
                response = client.post(
                    f"{self.endpoint}/upload/image",
                    files={"image": (f"rinari-{uuid.uuid4().hex[:8]}.png", first.data, first.mime)},
                    data={"overwrite": "true"},
                )
                if response.status_code >= 400:
                    raise _http_error(response, self.label)
                uploaded = response.json().get("name") or ""
            graph = json.loads(
                json.dumps(self.workflows[job.operation])
                .replace("{{prompt}}", json.dumps(effective_prompt(job))[1:-1])
                .replace("{{image}}", json.dumps(uploaded)[1:-1])
            )
            for _ in range(max(1, min(job.count, 4))):
                response = client.post(
                    f"{self.endpoint}/prompt", json={"prompt": graph, "client_id": "rinari"}
                )
                if response.status_code >= 400:
                    raise _http_error(response, self.label)
                prompt_id = response.json().get("prompt_id")
                if not prompt_id:
                    raise ImageConnectorError(
                        "PROVIDER_ERROR", f"{self.label} did not queue the workflow"
                    )
                deadline = time.monotonic() + 900
                history: dict[str, Any] = {}
                while time.monotonic() < deadline:
                    response = client.get(f"{self.endpoint}/history/{prompt_id}")
                    if response.status_code < 400:
                        history = response.json().get(prompt_id) or {}
                        if history.get("outputs"):
                            break
                        status = (history.get("status") or {}).get("status_str")
                        if status == "error":
                            raise ImageConnectorError(
                                "PROVIDER_ERROR", f"{self.label}: the workflow failed"
                            )
                    self.sleep(self.poll_interval_s)
                else:
                    raise ImageConnectorError(
                        "TIMEOUT", f"{self.label} did not finish in 15 minutes", retryable=True
                    )
                for node in (history.get("outputs") or {}).values():
                    for image in node.get("images") or []:
                        if image.get("type") not in (None, "output"):
                            continue
                        response = client.get(
                            f"{self.endpoint}/view",
                            params={
                                "filename": image.get("filename"),
                                "subfolder": image.get("subfolder", ""),
                                "type": image.get("type", "output"),
                            },
                        )
                        if response.status_code >= 400:
                            raise _http_error(response, self.label)
                        outputs.append(
                            ImageOutput(
                                response.content,
                                (response.headers.get("content-type") or "image/png").split(";")[0],
                            )
                        )
        if not outputs:
            raise ImageConnectorError(
                "PROVIDER_ERROR", f"{self.label}: the workflow saved no image"
            )
        return ImageBatch(tuple(outputs), 0.0, "comfyui")

    def check(self) -> dict[str, Any]:
        with self._client(10) as client:
            response = client.get(f"{self.endpoint}/system_stats")
        if response.status_code >= 400:
            raise _http_error(response, self.label)
        return {"ok": True, "model": "comfyui", "workflows": sorted(self.workflows)}


# -- An assigned MCP server -------------------------------------------------------


@dataclass
class McpImagesConnector(ImageConnector):
    """Any MCP server with image tools, mapped by the owner: which tool does
    each operation and the names of its prompt / image arguments. The result
    may carry image content blocks, data URLs or http(s) links; all three are
    read. Nothing here knows a particular server."""

    #: Calls `tool` with `arguments` on the server and returns the raw result.
    call: Callable[[str, dict[str, Any]], dict[str, Any]] | None = field(default=None, repr=False)
    #: {"generate": {"tool": "make_image", "prompt_arg": "prompt", "image_arg": "image_url"}}
    tools: dict[str, dict[str, str]] = field(default_factory=dict)

    def supports(self, operation: str) -> bool:
        return operation in self.tools

    def estimate(self, job: ImageJob) -> CostEstimate:
        return CostEstimate(None, "unknown")

    def run(self, job: ImageJob) -> ImageBatch:
        if not self.supports(job.operation):
            raise ImageConnectorError("UNSUPPORTED", f"{self.label} has no tool to {job.operation}")
        if self.call is None:
            raise ImageConnectorError("DEPENDENCY_ERROR", f"{self.label} is not available")
        spec = self.tools[job.operation]
        arguments: dict[str, Any] = {}
        prompt = effective_prompt(job)
        if prompt and spec.get("prompt_arg"):
            arguments[spec["prompt_arg"]] = prompt
        if job.images and spec.get("image_arg"):
            first = job.images[0]
            arguments[spec["image_arg"]] = _data_url(first.data, first.mime)
        try:
            result = self.call(spec["tool"], arguments)
        except ImageConnectorError:
            raise
        except Exception as exc:
            raise ImageConnectorError("DEPENDENCY_ERROR", f"{self.label}: {exc}") from exc
        if result.get("isError"):
            text = " ".join(
                str(block.get("text") or "")
                for block in result.get("content") or []
                if isinstance(block, dict)
            )
            raise ImageConnectorError(
                "PROVIDER_ERROR", f"{self.label}: {text or 'the tool failed'}"
            )
        outputs: list[ImageOutput] = []
        links: list[str] = []
        for block in result.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "image" and block.get("data"):
                outputs.append(
                    ImageOutput(
                        base64.b64decode(block["data"]), block.get("mimeType") or "image/png"
                    )
                )
            elif block.get("type") == "text":
                links += _HTTP_URL.findall(block.get("text") or "")
                inline = _decode_data_url((block.get("text") or "").strip())
                if inline:
                    outputs.append(inline)
            elif block.get("type") in ("resource_link", "resource"):
                uri = block.get("uri") or (block.get("resource") or {}).get("uri") or ""
                blob = (block.get("resource") or {}).get("blob")
                if blob:
                    outputs.append(
                        ImageOutput(
                            base64.b64decode(blob),
                            (block.get("resource") or {}).get("mimeType") or "image/png",
                        )
                    )
                elif uri.startswith("http"):
                    links.append(uri)
        structured = result.get("structuredContent")
        if structured is not None:
            links += _HTTP_URL.findall(json.dumps(structured))
        if not outputs and links:
            with self._client(300) as client:
                for url in dict.fromkeys(links):
                    try:
                        outputs.append(_download(client, url, self.label))
                    except ImageConnectorError:
                        continue
        if not outputs:
            raise ImageConnectorError(
                "PROVIDER_ERROR", f"{self.label}: {spec['tool']} returned no image Rinari can read"
            )
        return ImageBatch(tuple(outputs), None, spec["tool"])

    def check(self) -> dict[str, Any]:
        if self.call is None:
            raise ImageConnectorError("DEPENDENCY_ERROR", f"{self.label} is not available")
        return {
            "ok": True,
            "model": ", ".join(sorted({spec["tool"] for spec in self.tools.values()})),
        }
