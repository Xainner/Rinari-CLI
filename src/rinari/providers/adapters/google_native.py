"""Google's native Gemini protocol (plan 0.4.0 #94).

OpenCode Go and Zen serve their `@ai-sdk/google` models only by Gemini's own
API (`…/google/v1beta/models/<id>:streamGenerateContent`); the OpenAI route
answers «Model does not support this protocol». This adapter speaks it with
SSE: text, the system instruction, tools (functionDeclarations, functionCall,
functionResponse), inline images and usage. The model's parts are kept as
they are in the continuation, to hand back the `thoughtSignature` Gemini
requires when calls are chained. It first ran as a hotfix on the owner's
installation (2026-10-15) and is ported here as it was proven there.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from rinari.models.images import expand_tool_images
from rinari.models.types import (
    ROLE_ASSISTANT,
    ROLE_SYSTEM,
    ROLE_TOOL,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    StopReason,
    ToolCall,
    Usage,
)
from rinari.providers.adapters.base import ProviderAdapter
from rinari.providers.adapters.http import (
    DEFAULT_MODEL_STREAM_READ_TIMEOUT_S,
    auth_failure,
    iter_model_lines,
    open_model_stream,
    provider_error,
    sanitize_tool_name,
    session_affinity_headers,
    stream_close_details,
    stream_timeout_error,
)
from rinari.shared.errors import NetworkError, ProviderModelError
from rinari.tools.schema import wire_input_schema

OPENCODE_GO_GOOGLE = "https://opencode.ai/inference/go/google/v1beta"
OPENCODE_ZEN_GOOGLE = "https://opencode.ai/inference/zen/google/v1beta"

# Lo que el subconjunto OpenAPI de Gemini acepta en `parameters`; el resto se
# descarta (una clave desconocida es un HTTP 400 de toda la petición).
_KEEP_KEYS = {
    "type",
    "description",
    "nullable",
    "enum",
    "items",
    "properties",
    "required",
    "anyOf",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "title",
}
_TYPES = {"string", "number", "integer", "boolean", "array", "object"}


class GoogleNativeAdapter(ProviderAdapter):
    type = "google-native"

    def auth_methods(self) -> tuple[str, ...]:
        return ("api-key",)

    def _headers(self, secret: str | None) -> dict[str, str]:
        if not secret:
            return {}
        return {"x-goog-api-key": secret, "Authorization": f"Bearer {secret}"}

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=True, tool_calls=True, reasoning_effort=False)

    def base_url(self, endpoint: str | None, settings: dict[str, Any] | None = None) -> str:
        parts = urlsplit(endpoint or "")
        if parts.netloc.endswith("opencode.ai"):
            return OPENCODE_GO_GOOGLE if "/go" in parts.path else OPENCODE_ZEN_GOOGLE
        return (endpoint or OPENCODE_GO_GOOGLE).rstrip("/")

    def _url(self, endpoint, model):
        return f"{self.base_url(endpoint)}/models/{model}:streamGenerateContent?alt=sse"

    def invoke(self, request, secret, endpoint=None, *, transport="chat", tool_aliases=None):
        return self.invoke_stream(
            request,
            secret,
            endpoint,
            lambda _d: None,
            transport=transport,
            tool_aliases=tool_aliases,
        )

    def invoke_stream(
        self,
        request: ModelRequest,
        secret: str | None,
        endpoint: str | None,
        on_delta: Callable[[str], None],
        *,
        transport: str = "chat",
        tool_aliases: dict[str, str] | None = None,
    ) -> ModelResponse:
        request = _patient(request)
        url = self._url(endpoint, request.model)
        text_parts: list[str] = []
        model_parts: list[dict[str, Any]] = []
        calls: list[ToolCall] = []
        usage_raw: dict[str, Any] | None = None
        finish = None
        stream_stats: dict[str, Any] = {}
        headers_received = False
        saw_payload = False
        started = time.monotonic()
        last_activity = started
        timeout_s = (
            request.stream_read_timeout_s
            if request.stream_read_timeout_s is not None
            else DEFAULT_MODEL_STREAM_READ_TIMEOUT_S
        )
        headers = {
            **self._headers(secret),
            **session_affinity_headers(url, request.session_id),
        }
        try:
            with open_model_stream(
                self.client(),
                request,
                started,
                "POST",
                url,
                json=_payload(request, tool_aliases),
                headers=headers,
            ) as response:
                headers_received = True
                if response.status_code >= 400:
                    raise _error_from(response, url, request.model)
                for line in iter_model_lines(response, request, started, stream_stats):
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    saw_payload = True
                    last_activity = time.monotonic()
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise ProviderModelError(
                            f"Provider returned invalid streaming JSON for {url}"
                        ) from exc
                    if not isinstance(chunk, dict):
                        continue
                    if chunk.get("error"):
                        from rinari.providers.errors import classify_stream_error

                        raise classify_stream_error(chunk["error"], model=request.model)
                    if isinstance(chunk.get("usageMetadata"), dict):
                        usage_raw = chunk["usageMetadata"]
                    candidate = (chunk.get("candidates") or [{}])[0] or {}
                    for part in (candidate.get("content") or {}).get("parts") or []:
                        if not isinstance(part, dict):
                            continue
                        model_parts.append(part)
                        if part.get("thought"):
                            continue
                        if isinstance(part.get("text"), str) and part["text"]:
                            text_parts.append(part["text"])
                            on_delta(part["text"])
                        call = part.get("functionCall")
                        if isinstance(call, dict) and call.get("name"):
                            args = call.get("args")
                            calls.append(
                                ToolCall(
                                    id=str(
                                        call.get("id")
                                        or f"call_{len(calls)}_{int(time.time() * 1000)}"
                                    ),
                                    name=_real_name(str(call["name"]), tool_aliases),
                                    arguments=args if isinstance(args, dict) else {},
                                    raw_arguments=json.dumps(args if args is not None else {}),
                                    arguments_invalid=not isinstance(args, (dict, type(None))),
                                )
                            )
                    if candidate.get("finishReason"):
                        finish = candidate["finishReason"]
                if finish is None:
                    raise NetworkError(
                        "Response stream closed without a terminal event",
                        details={
                            "kind": "STREAM_INTERRUPTED",
                            **stream_close_details(
                                response,
                                stream_stats,
                                transport="google",
                                url=url,
                                started_at=started,
                                partial_tool_calls=bool(calls),
                            ),
                        },
                    )
        except (NetworkError, ProviderModelError) as exc:
            exc.details.update({"partial_text": "".join(text_parts), "partial": bool(text_parts)})
            raise
        except httpx.TimeoutException as exc:
            error = stream_timeout_error(
                url,
                request.model,
                exc,
                timeout_s=timeout_s,
                headers_received=headers_received,
                saw_payload=saw_payload,
                stream_started_at=started,
                last_activity_at=last_activity,
                effective_timeouts=request.stream_timeouts,
                response=response if headers_received else None,
            )
            error.details.update({"partial_text": "".join(text_parts), "partial": bool(text_parts)})
            raise error from exc
        except httpx.TransportError as exc:
            raise NetworkError(
                f"Stream interrupted: {exc.__class__.__name__}",
                details={
                    "kind": "STREAM_INTERRUPTED",
                    "partial_text": "".join(text_parts),
                    "partial": bool(text_parts),
                },
            ) from exc
        if finish == "MAX_TOKENS":
            stop, calls = StopReason.MAX_TOKENS, []
        elif calls:
            stop = StopReason.TOOL_CALLS
        else:
            stop = StopReason.END_TURN
        usage = Usage(source="unavailable")
        if usage_raw:
            usage = Usage(
                input_tokens=_int(usage_raw.get("promptTokenCount")),
                output_tokens=(_int(usage_raw.get("candidatesTokenCount")) or 0)
                + (_int(usage_raw.get("thoughtsTokenCount")) or 0),
                cached_input_tokens=_int(usage_raw.get("cachedContentTokenCount")),
                reasoning_tokens=_int(usage_raw.get("thoughtsTokenCount")),
            )
        ids = [c.id for c in calls]
        return ModelResponse(
            content="".join(text_parts),
            tool_calls=tuple(calls),
            usage=usage,
            stop_reason=stop,
            continuation={"protocol": "google", "parts": model_parts, "call_ids": ids}
            if stop is not StopReason.MAX_TOKENS and model_parts
            else None,
        )


# Gemini no trocea una llamada a herramienta: escribir un archivo grande son
# minutos de silencio en el stream y el plazo de 120 s entre fragmentos lo
# cortaba. Mínimos solo para este adaptador; un valor mayor configurado manda.
_MIN_TIMEOUTS = {"first_byte": 600.0, "idle": 600.0, "total": 1800.0}


def _patient(request: ModelRequest) -> ModelRequest:
    from dataclasses import replace

    from rinari.providers.adapters.http import STREAM_DEFAULTS

    limits = {**STREAM_DEFAULTS, **(request.stream_timeouts or {})}
    for key, floor in _MIN_TIMEOUTS.items():
        if not isinstance(limits.get(key), (int, float)) or limits[key] < floor:
            limits[key] = floor
    return replace(request, stream_timeouts=limits, stream_read_timeout_s=limits["idle"])


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _wire_name(name, aliases):
    if aliases is None:
        return name
    return {real: alias for alias, real in aliases.items()}.get(name, sanitize_tool_name(name))


def _real_name(name, aliases):
    return (aliases or {}).get(name, name)


def _clean_schema(schema):
    """JSON Schema -> el subconjunto OpenAPI que Gemini acepta."""
    if not isinstance(schema, dict):
        return {"type": "string"}
    schema = dict(schema)
    for combinator in ("oneOf", "allOf"):
        if isinstance(schema.get(combinator), list):
            options = schema.pop(combinator)
            if combinator == "allOf" and len(options) == 1 and isinstance(options[0], dict):
                schema = {**options[0], **schema}
            else:
                schema.setdefault("anyOf", options)
    if "const" in schema and "enum" not in schema:
        schema["enum"] = [schema["const"]]
    out: dict[str, Any] = {}
    kind = schema.get("type")
    if isinstance(kind, list):
        kinds = [t for t in kind if t != "null"]
        if len(kinds) < len(kind):
            out["nullable"] = True
        kind = kinds[0] if kinds else "string"
    if isinstance(kind, str) and kind in _TYPES:
        out["type"] = kind
    elif kind == "null":
        out["type"], out["nullable"] = "string", True
    if isinstance(schema.get("description"), str):
        out["description"] = schema["description"][:1024]
    if isinstance(schema.get("enum"), list):
        values = [v for v in schema["enum"] if v is not None]
        if values:
            out["enum"] = [str(v) for v in values]
            out["type"] = "string"  # Gemini solo admite enum de strings.
    anyof = schema.get("anyOf")
    if isinstance(anyof, list) and anyof:
        options = [o for o in anyof if not (isinstance(o, dict) and o.get("type") == "null")]
        if len(options) < len(anyof):
            out["nullable"] = True
        if len(options) == 1:
            merged = _clean_schema(options[0])
            merged.update({k: v for k, v in out.items() if k in ("description", "nullable")})
            return merged
        if options:
            out.pop("type", None)
            out["anyOf"] = [_clean_schema(o) for o in options]
            return out
    if out.get("type") == "array" or "items" in schema:
        out.setdefault("type", "array")
        out["items"] = _clean_schema(
            schema.get("items") if isinstance(schema.get("items"), dict) else {"type": "string"}
        )
        for key in ("minItems", "maxItems"):
            if isinstance(schema.get(key), int) and not isinstance(schema.get(key), bool):
                out[key] = schema[key]
    props = schema.get("properties")
    if out.get("type") == "object" or isinstance(props, dict):
        out["type"] = "object"
        if isinstance(props, dict) and props:
            out["properties"] = {str(k): _clean_schema(v) for k, v in props.items()}
            required = [r for r in schema.get("required") or [] if r in out["properties"]]
            if required:
                out["required"] = required
    if out.get("type") in ("number", "integer"):
        for key in ("minimum", "maximum"):
            value = schema.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[key] = value
    if "type" not in out and "anyOf" not in out:
        out["type"] = "string"
    return out


def _error_from(response, url, model):
    """El 400 de Gemini llega como `[{"error": {...}}]`: Rinari solo lee
    objetos y perdía el motivo. Se lee aquí y se añade al mensaje."""
    import re

    from rinari.providers.errors import ProviderError, ProviderErrorCode

    detail = ""
    try:
        response.read()
        body = response.json()
        if isinstance(body, list) and body:
            body = body[0]
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            detail = str(error.get("message") or error.get("status") or "")
        elif isinstance(error, str):
            detail = error
    except Exception:
        try:
            detail = response.text
        except Exception:
            detail = ""
    detail = re.sub(
        r"[A-Za-z0-9_\-]{6}[A-Za-z0-9_\-*]{18,}",
        lambda m: m.group(0)[:6] + "…",
        " ".join(detail.split()),
    )[:600]
    if response.status_code in (401, 403):
        return auth_failure(response, url, model=model)
    if response.status_code == 429 or response.status_code >= 500 or not detail:
        error = provider_error(response, url, model=model)
        if detail and detail not in error.message:
            error.message = f"{error.message}: {detail}"
            error.args = (error.message,)
        return error
    error = ProviderError(
        f"Provider returned HTTP {response.status_code} for {url}: {detail}",
        code=ProviderErrorCode.SERVER_ERROR,
        retryable=False,
        model=model,
    )
    error.details["http_status"] = response.status_code
    return error


def _user_parts(message):
    parts = []
    if message.content:
        parts.append({"text": message.content})
    for image in message.images:
        parts.append(
            {
                "inlineData": {
                    "mimeType": getattr(image, "mime", None) or "image/jpeg",
                    "data": image.encoded(),
                }
            }
        )
    return parts


def _payload(request: ModelRequest, aliases) -> dict[str, Any]:
    system: list[str] = []
    contents: list[dict[str, Any]] = []
    names_by_id: dict[str, str] = {}

    def push(role, parts):
        if not parts:
            return
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": list(parts)})

    for message in expand_tool_images(request.messages):
        if message.role == ROLE_SYSTEM:
            if message.content:
                system.append(message.content)
            continue
        if message.role == ROLE_ASSISTANT:
            for tc in message.tool_calls:
                names_by_id[tc.id] = tc.name
            cont = message.continuation or {}
            if cont.get("protocol") == "google" and cont.get("parts"):
                push("model", cont["parts"])
                continue
            parts = []
            if message.content:
                parts.append({"text": message.content})
            for tc in message.tool_calls:
                # Sin firma (historia de otro proveedor): Gemini acepta este
                # valor documentado para saltarse la validación.
                parts.append(
                    {
                        "functionCall": {
                            "name": _wire_name(tc.name, aliases),
                            "args": tc.arguments,
                        },
                        "thoughtSignature": "skip_thought_signature_validator",
                    }
                )
            if parts:
                push("model", parts)
            continue
        if message.role == ROLE_TOOL:
            name = message.name or names_by_id.get(message.tool_call_id or "", "tool")
            push(
                "user",
                [
                    {
                        "functionResponse": {
                            "name": _wire_name(name, aliases),
                            "response": {"output": message.content or ""},
                        }
                    }
                ],
            )
            continue
        push("user", _user_parts(message))

    payload: dict[str, Any] = {"contents": contents}
    if system:
        payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system)}]}
    if request.tools:
        declarations = []
        for tool in request.tools:
            declaration = {
                "name": _wire_name(tool.name, aliases),
                "description": tool.description or tool.name,
            }
            parameters = _clean_schema(wire_input_schema(tool.parameters))
            if parameters.get("properties"):
                declaration["parameters"] = parameters
            declarations.append(declaration)
        payload["tools"] = [{"functionDeclarations": declarations}]
    config: dict[str, Any] = {}
    if request.temperature is not None:
        config["temperature"] = request.temperature
    if request.max_tokens is not None:
        config["maxOutputTokens"] = request.max_tokens
    if request.json_response:
        config["responseMimeType"] = "application/json"
    if config:
        payload["generationConfig"] = config
    return payload
