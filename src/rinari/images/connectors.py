"""Image connectors: who creates and edits images for Rinari (plan 0.4.0 #28).

A connector turns an `ImageJob` into PNG bytes. The tools (`image.*`) never
know which service is behind it: they ask the configured connector, which
says what it can do (`supports`) and what a job costs (`estimate`). Nothing
is hardcoded to one service; a connector only wraps a documented API.

- `chatgpt`: the ChatGPT subscription already connected as a provider (the
  same one Codex uses). The Responses endpoint of the subscription runs the
  hosted `image_generation` tool; images are included in the plan.
- `openai`: the OpenAI Images API with an API key (`gpt-image-*`), paid per
  image.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

#: The five operations the tools expose.
OPERATIONS = ("generate", "edit", "variations", "upscale", "remove_background")
SIZES = ("auto", "1024x1024", "1024x1536", "1536x1024")
QUALITIES = ("auto", "low", "medium", "high")

#: What each operation asks the model when the service has no dedicated call.
VARIATION_PROMPT = (
    "Create a variation of this image: keep its subject, composition and style, change the details."
)
REMOVE_BACKGROUND_PROMPT = (
    "Remove the background completely. Keep the subject exactly as it is, with clean "
    "edges, no halo and no shadow, on a fully transparent background."
)


class ImageConnectorError(RuntimeError):
    """A connector failed; `code` is a ToolErrorCode value."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class InputImage:
    data: bytes
    mime: str = "image/png"
    name: str = "image.png"


@dataclass(frozen=True, slots=True)
class ImageJob:
    operation: str
    prompt: str = ""
    images: tuple[InputImage, ...] = ()
    mask: InputImage | None = None
    size: str = "auto"
    quality: str = "auto"
    count: int = 1
    transparent: bool = False


@dataclass(frozen=True, slots=True)
class ImageOutput:
    data: bytes
    mime: str = "image/png"
    revised_prompt: str | None = None


@dataclass(frozen=True, slots=True)
class ImageBatch:
    images: tuple[ImageOutput, ...]
    #: What it cost in USD; None when the service does not say (an estimate
    #: is shown before running instead).
    cost_usd: float | None
    model: str


@dataclass(frozen=True, slots=True)
class CostEstimate:
    usd: float | None
    #: `per_image` (paid by image), `included` (subscription), `unknown`.
    basis: str

    def label(self) -> str:
        if self.basis == "included":
            return "included in the subscription"
        if self.usd is None:
            return "cost unknown"
        return f"~${self.usd:.3f}"


@dataclass
class ImageConnector:
    """Base: id, kind and label from the settings; subclasses do the work."""

    id: str
    kind: str
    label: str
    model: str = ""
    client_factory: Callable[[], httpx.Client] | None = field(default=None, repr=False)

    def supports(self, operation: str) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def estimate(self, job: ImageJob) -> CostEstimate:  # pragma: no cover - abstract
        raise NotImplementedError

    def run(self, job: ImageJob) -> ImageBatch:  # pragma: no cover - abstract
        raise NotImplementedError

    def check(self) -> dict[str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError

    def _client(self, timeout: float) -> httpx.Client:
        if self.client_factory is not None:
            return self.client_factory()
        return httpx.Client(timeout=timeout)


def effective_prompt(job: ImageJob) -> str:
    if job.operation == "variations":
        return f"{VARIATION_PROMPT} {job.prompt}".strip()
    if job.operation == "remove_background":
        return f"{REMOVE_BACKGROUND_PROMPT} {job.prompt}".strip()
    return job.prompt


def _http_error(response: httpx.Response, service: str) -> ImageConnectorError:
    detail = ""
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else None
        detail = (
            (error.get("message") if isinstance(error, dict) else None) or body.get("detail") or ""
        )
    except Exception:
        detail = response.text[:300]
    status = response.status_code
    if status in (401, 403):
        return ImageConnectorError(
            "AUTH_REQUIRED", f"{service} rejected the credentials ({status}). {detail}".strip()
        )
    if status == 429:
        return ImageConnectorError(
            "RATE_LIMITED",
            f"{service} is rate limiting ({status}). {detail}".strip(),
            retryable=True,
        )
    if status == 400 and "safety" in str(detail).lower():
        return ImageConnectorError("POLICY_DENIED", f"{service} refused the request: {detail}")
    return ImageConnectorError(
        "PROVIDER_ERROR", f"{service} answered {status}. {detail}".strip(), retryable=status >= 500
    )


# -- OpenAI Images API ---------------------------------------------------------

#: USD per image for gpt-image-1 (OpenAI pricing page, 2026): quality x size.
#: An estimate: input image tokens of an edit are not counted.
OPENAI_PRICES = {
    "low": {"square": 0.011, "tall": 0.016},
    "medium": {"square": 0.042, "tall": 0.063},
    "high": {"square": 0.167, "tall": 0.25},
}


@dataclass
class OpenAIImagesConnector(ImageConnector):
    api_key: str = field(default="", repr=False)
    endpoint: str = "https://api.openai.com/v1"

    def supports(self, operation: str) -> bool:
        # There is no upscaler in the Images API.
        return operation in ("generate", "edit", "variations", "remove_background")

    def estimate(self, job: ImageJob) -> CostEstimate:
        quality = job.quality if job.quality in OPENAI_PRICES else "medium"
        shape = "square" if job.size in ("auto", "1024x1024") else "tall"
        return CostEstimate(OPENAI_PRICES[quality][shape] * max(1, job.count), "per_image")

    def _model(self) -> str:
        return self.model or "gpt-image-1"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def run(self, job: ImageJob) -> ImageBatch:
        if not self.supports(job.operation):
            raise ImageConnectorError("UNSUPPORTED", f"{self.label} cannot {job.operation}")
        options: dict[str, Any] = {"model": self._model(), "n": max(1, min(job.count, 4))}
        if job.size != "auto":
            options["size"] = job.size
        if job.quality != "auto":
            options["quality"] = job.quality
        if job.transparent or job.operation == "remove_background":
            options["background"] = "transparent"
            options["output_format"] = "png"
        prompt = effective_prompt(job)
        with self._client(300) as client:
            if job.operation == "generate" and not job.images:
                response = client.post(
                    f"{self.endpoint}/images/generations",
                    headers=self._headers(),
                    json={**options, "prompt": prompt},
                )
            else:
                files: list[tuple[str, tuple[str, bytes, str]]] = [
                    ("image[]", (image.name, image.data, image.mime)) for image in job.images
                ]
                if job.mask is not None:
                    files.append(("mask", (job.mask.name, job.mask.data, job.mask.mime)))
                form = {key: str(value) for key, value in {**options, "prompt": prompt}.items()}
                response = client.post(
                    f"{self.endpoint}/images/edits", headers=self._headers(), data=form, files=files
                )
        if response.status_code >= 400:
            raise _http_error(response, self.label)
        body = response.json()
        images = tuple(
            ImageOutput(base64.b64decode(item["b64_json"]), "image/png", item.get("revised_prompt"))
            for item in body.get("data", [])
            if isinstance(item, dict) and item.get("b64_json")
        )
        if not images:
            raise ImageConnectorError("PROVIDER_ERROR", f"{self.label} returned no image")
        return ImageBatch(images, self.estimate(job).usd, self._model())

    def check(self) -> dict[str, Any]:
        with self._client(30) as client:
            response = client.get(
                f"{self.endpoint}/models/{self._model()}", headers=self._headers()
            )
        if response.status_code >= 400:
            raise _http_error(response, self.label)
        return {"ok": True, "model": self._model()}


# -- ChatGPT subscription (Codex) ----------------------------------------------


@dataclass
class ChatGPTImagesConnector(ImageConnector):
    """The subscription's Responses endpoint with the hosted image tool.

    `token` returns a valid bearer token (refreshed by the provider service);
    `refresh` is called once with the rejected one after a 401.
    """

    endpoint: str = "https://chatgpt.com/backend-api/codex"
    token: Callable[[], str] | None = field(default=None, repr=False)
    refresh: Callable[[str], str] | None = field(default=None, repr=False)

    def supports(self, operation: str) -> bool:
        # The hosted tool tops out around 1536 px: no real upscaling.
        return operation in ("generate", "edit", "variations", "remove_background")

    def estimate(self, job: ImageJob) -> CostEstimate:
        return CostEstimate(0.0, "included")

    def _headers(self, token: str) -> dict[str, str]:
        from rinari.providers.adapters.subscriptions import chatgpt_headers

        return chatgpt_headers(token)

    def _model(self, client: httpx.Client, token: str) -> str:
        if self.model:
            return self.model
        from rinari.providers.adapters.subscriptions import CODEX_CLIENT_VERSION, HIDDEN

        response = client.get(
            f"{self.endpoint}/models?client_version={CODEX_CLIENT_VERSION}",
            headers=self._headers(token),
        )
        if response.status_code >= 400:
            raise _http_error(response, self.label)
        for entry in response.json().get("models", []):
            if (
                isinstance(entry, dict)
                and entry.get("slug")
                and entry.get("visibility") not in HIDDEN
            ):
                self.model = entry["slug"]
                return self.model
        raise ImageConnectorError(
            "PROVIDER_ERROR", f"{self.label}: the subscription lists no model"
        )

    def _payload(self, model: str, job: ImageJob) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "input_text", "text": effective_prompt(job)}]
        for image in job.images:
            data = base64.b64encode(image.data).decode("ascii")
            content.append({"type": "input_image", "image_url": f"data:{image.mime};base64,{data}"})
        tool: dict[str, Any] = {"type": "image_generation", "output_format": "png"}
        if job.size != "auto":
            tool["size"] = job.size
        if job.quality != "auto":
            tool["quality"] = job.quality
        if job.transparent or job.operation == "remove_background":
            tool["background"] = "transparent"
        if job.mask is not None:
            mask = base64.b64encode(job.mask.data).decode("ascii")
            tool["input_image_mask"] = {"image_url": f"data:{job.mask.mime};base64,{mask}"}
        return {
            "model": model,
            "instructions": (
                "Create or edit exactly the requested image with the image_generation tool."
            ),
            "input": [{"role": "user", "content": content}],
            "tools": [tool],
            "tool_choice": {"type": "image_generation"},
            "stream": True,
            "store": False,
        }

    def _one(
        self, client: httpx.Client, token: str, payload: dict[str, Any]
    ) -> tuple[ImageOutput | None, httpx.Response | None]:
        image: ImageOutput | None = None
        with client.stream(
            "POST", f"{self.endpoint}/responses", headers=self._headers(token), json=payload
        ) as response:
            if response.status_code >= 400:
                response.read()
                return None, response
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except ValueError:
                    continue
                item = event.get("item") or {}
                if (
                    event.get("type") == "response.output_item.done"
                    and item.get("type") == "image_generation_call"
                    and item.get("result")
                ):
                    image = ImageOutput(
                        base64.b64decode(item["result"]), "image/png", item.get("revised_prompt")
                    )
                if event.get("type") in ("response.failed", "error"):
                    message = (
                        (event.get("response") or {}).get("error") or event.get("error") or {}
                    ).get("message") or "the image could not be created"
                    raise ImageConnectorError("PROVIDER_ERROR", f"{self.label}: {message}")
        return image, None

    def run(self, job: ImageJob) -> ImageBatch:
        if not self.supports(job.operation):
            raise ImageConnectorError("UNSUPPORTED", f"{self.label} cannot {job.operation}")
        if self.token is None:
            raise ImageConnectorError(
                "AUTH_REQUIRED", f"{self.label} has no signed-in subscription"
            )
        token = self.token()
        outputs: list[ImageOutput] = []
        with self._client(300) as client:
            model = self._model(client, token)
            payload = self._payload(model, job)
            # The hosted tool makes one image per call.
            for _ in range(max(1, min(job.count, 4))):
                image, failed = self._one(client, token, payload)
                if failed is not None and failed.status_code == 401 and self.refresh is not None:
                    token = self.refresh(token)
                    image, failed = self._one(client, token, payload)
                if failed is not None:
                    raise _http_error(failed, self.label)
                if image is None:
                    raise ImageConnectorError("PROVIDER_ERROR", f"{self.label} returned no image")
                outputs.append(image)
        return ImageBatch(tuple(outputs), 0.0, model)

    def check(self) -> dict[str, Any]:
        if self.token is None:
            raise ImageConnectorError(
                "AUTH_REQUIRED", f"{self.label} has no signed-in subscription"
            )
        with self._client(30) as client:
            model = self._model(client, self.token())
        return {"ok": True, "model": model}
