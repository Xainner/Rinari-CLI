"""Anthropic adapter: https://api.anthropic.com (x-api-key + anthropic-version)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from rinari.models.types import (
    ROLE_SYSTEM,
    ROLE_TOOL,
    ChatMessage,
    ModelItem,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    StopReason,
    ToolCall,
    Usage,
)
from rinari.providers.adapters.base import AuthStatus, DiscoveredModel, ProviderAdapter
from rinari.providers.adapters.http import (
    MODEL_CALL_TIMEOUT,
    MODEL_STREAM_TIMEOUT,
    auth_failure,
    decode_json,
    provider_error,
    send_request,
    session_affinity_headers,
)
from rinari.shared.errors import InvalidUsageError, NetworkError, ProviderModelError

DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 8192


class AnthropicAdapter(ProviderAdapter):
    type = "anthropic"

    def __init__(self, *, client=None) -> None:
        super().__init__(client=client)

    def auth_methods(self) -> tuple[str, ...]:
        return ("api-key",)

    def base_url(self, endpoint: str | None, settings: dict[str, Any] | None = None) -> str:
        return (endpoint or DEFAULT_BASE_URL).rstrip("/")

    def _headers(self, secret: str | None) -> dict[str, str]:
        headers = {"anthropic-version": API_VERSION}
        if secret:
            headers["x-api-key"] = secret
        return headers

    def validate_credential(self, secret: str | None, endpoint: str | None = None) -> AuthStatus:
        url = f"{self.base_url(endpoint, None)}/v1/models?limit=1"
        response = send_request(self.client(), "GET", url, headers=self._headers(secret))
        if response.status_code in (401, 403):
            return AuthStatus(
                connected=False, detail=f"authentication failed (HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            return AuthStatus(
                connected=False, detail=f"endpoint error (HTTP {response.status_code})"
            )
        return AuthStatus(connected=True, detail="ok")

    def list_models(self, secret: str | None, endpoint: str | None = None) -> list[DiscoveredModel]:
        url = f"{self.base_url(endpoint, None)}/v1/models?limit=100"
        response = send_request(self.client(), "GET", url, headers=self._headers(secret))
        if response.status_code in (401, 403):
            raise auth_failure(response, url)
        if response.status_code >= 400:
            raise provider_error(response, url)
        data = decode_json(response, url)
        if not isinstance(data, dict):
            raise ProviderModelError(f"Unexpected model list payload for {url}")
        return [
            DiscoveredModel(provider_model_id=str(item["id"]), availability="available")
            for item in data.get("data", [])
            if isinstance(item, dict) and item.get("id")
        ]

    # -- model invocation ---------------------------------------------------

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=True,
            tool_calls=True,
            structured_output=False,
            reasoning_effort=False,
        )

    def _messages_url(self, endpoint: str | None) -> str:
        return f"{self.base_url(endpoint, None)}/v1/messages"

    def invoke(
        self,
        request: ModelRequest,
        secret: str | None,
        endpoint: str | None = None,
        *,
        transport: str = "chat",
        tool_aliases: dict[str, str] | None = None,
    ) -> ModelResponse:
        if transport != "chat":
            raise InvalidUsageError(
                f"transport {transport!r} is not supported by the anthropic adapter",
                hint="Use an OpenAI-compatible provider for the responses transport.",
            )
        url = self._messages_url(endpoint)
        headers = {**self._headers(secret), **session_affinity_headers(url, request.session_id)}
        response = send_request(
            self.client(),
            "POST",
            url,
            headers=headers,
            json_body=self._payload(request, stream=False),
            timeout=MODEL_CALL_TIMEOUT,
        )
        if response.status_code in (401, 403):
            raise auth_failure(response, url, model=request.model)
        if response.status_code >= 400:
            raise provider_error(response, url, model=request.model)
        return _response_from_anthropic(decode_json(response, url), url)

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
        if transport != "chat":
            raise InvalidUsageError(
                f"transport {transport!r} is not supported by the anthropic adapter",
                hint="Use an OpenAI-compatible provider for the responses transport.",
            )
        url = self._messages_url(endpoint)
        content_parts: list[str] = []
        calls = _ToolCallBlockAccumulator()
        usage = Usage()
        stop_reason = StopReason.END_TURN
        headers = {**self._headers(secret), **session_affinity_headers(url, request.session_id)}
        try:
            with self.client().stream(
                "POST",
                url,
                json=self._payload(request, stream=True),
                headers=headers,
                timeout=MODEL_STREAM_TIMEOUT,
            ) as response:
                if response.status_code in (401, 403):
                    raise auth_failure(response, url, model=request.model)
                if response.status_code >= 400:
                    raise provider_error(response, url, model=request.model)
                for line in response.iter_lines():
                    if not line:
                        continue
                    event = _parse_sse_line(line, url)
                    event_type = event.get("type")
                    if event_type == "message_start":
                        usage = _usage_from_anthropic(_event_message(event).get("usage"))
                    elif event_type == "content_block_start":
                        calls.begin(_optional_int(event.get("index")), event.get("content_block"))
                    elif event_type == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            content_parts.append(delta["text"])
                            on_delta(delta["text"])
                        elif delta.get("type") == "input_json_delta":
                            calls.append_json(
                                _optional_int(event.get("index")), delta.get("partial_json")
                            )
                    elif event_type == "message_delta":
                        stop_reason = _stop_reason_from_anthropic(
                            (event.get("delta") or {}).get("stop_reason")
                        )
                        output_tokens = _optional_int(
                            (event.get("usage") or {}).get("output_tokens")
                        )
                        usage = Usage(
                            input_tokens=usage.input_tokens,
                            output_tokens=output_tokens,
                            cached_input_tokens=usage.cached_input_tokens,
                        )
        except httpx.TimeoutException as exc:
            raise NetworkError(f"Timed out streaming from {url}") from exc
        except httpx.TransportError as exc:
            raise NetworkError(f"Stream interrupted: {exc.__class__.__name__}") from exc
        tool_calls = calls.finalize()
        if tool_calls and stop_reason is not StopReason.MAX_TOKENS:
            stop_reason = StopReason.TOOL_CALLS
        return ModelResponse(
            content="".join(content_parts),
            tool_calls=tool_calls,
            usage=usage,
            stop_reason=stop_reason,
        )

    def _payload(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        system, messages = _convert_to_anthropic(request.messages)
        payload: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens or DEFAULT_MAX_TOKENS,
            "messages": messages,
            "stream": stream,
        }
        if system:
            payload["system"] = "\n\n".join(system)
        if request.tools:
            payload["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters,
                }
                for tool in request.tools
            ]
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        # json_response: Anthropic has no native JSON mode; the runtime checks
        # capabilities.structured_output before requesting it.
        return payload


# -- helpers -----------------------------------------------------------------


def _convert_to_anthropic(
    messages: tuple[ChatMessage, ...],
) -> tuple[list[str], list[dict[str, Any]]]:
    system: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        if message.role == ROLE_SYSTEM:
            if message.content:
                system.append(message.content)
            continue
        if message.role == ROLE_TOOL:
            converted.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.tool_call_id,
                            "content": message.content or "",
                        }
                    ],
                }
            )
            continue
        if message.role == "assistant":
            if message.tool_calls:
                blocks: list[dict[str, Any]] = []
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                for tc in message.tool_calls:
                    blocks.append(
                        {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments}
                    )
                converted.append({"role": "assistant", "content": blocks})
            else:
                converted.append({"role": "assistant", "content": message.content or ""})
            continue
        content = message.content or ""
        if message.images:
            content = [{"type": "text", "text": content}] + [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": i.encoded()},
                }
                for i in message.images
            ]
        converted.append({"role": "user", "content": content})
    return system, _merge_adjacent(converted)


def _merge_adjacent(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for entry in messages:
        if merged and merged[-1]["role"] == entry["role"]:
            previous, current = merged[-1]["content"], entry["content"]
            if isinstance(previous, str) and isinstance(current, str):
                merged[-1]["content"] = previous + "\n" + current
            else:
                merged[-1]["content"] = _as_blocks(previous) + _as_blocks(current)
        else:
            merged.append(entry)
    return merged


def _as_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return list(content)


def _parse_sse_line(line: str, url: str) -> dict[str, Any]:
    if not line.startswith("data:"):
        return {}
    data = line[5:].strip()
    if not data or data == "[DONE]":
        return {}
    try:
        value = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ProviderModelError(f"Provider returned invalid streaming JSON for {url}") from exc
    return value if isinstance(value, dict) else {}


def _event_message(event: dict[str, Any]) -> dict[str, Any]:
    message = event.get("message")
    return message if isinstance(message, dict) else {}


def _tool_call_from_anthropic_block(b: dict) -> ToolCall:
    """tool_use block -> ToolCall, flagging non-object input as invalid."""
    raw_input = b.get("input")
    if raw_input is None:
        return ToolCall(id=str(b.get("id", "")), name=str(b.get("name", "")), arguments={})
    if isinstance(raw_input, dict):
        return ToolCall(id=str(b.get("id", "")), name=str(b.get("name", "")), arguments=raw_input)
    return ToolCall(
        id=str(b.get("id", "")),
        name=str(b.get("name", "")),
        arguments={},
        raw_arguments=str(raw_input),
        arguments_invalid=True,
    )


def _response_from_anthropic(data: Any, url: str) -> ModelResponse:
    if not isinstance(data, dict):
        raise ProviderModelError(f"Unexpected messages payload from {url}")
    blocks = data.get("content") or []
    text_parts = [
        b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
    ]
    tool_calls = tuple(
        _tool_call_from_anthropic_block(b)
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "tool_use"
    )
    # Every block is preserved as an item; unknown kinds (thinking, ...)
    # stay opaque in data so protocol-required blocks survive round-trips.
    items = tuple(
        ModelItem(
            type=str(b.get("type") or "unknown"),
            id=b.get("id") if isinstance(b.get("id"), str) else None,
            data={k: v for k, v in b.items() if k not in ("type", "id")},
        )
        for b in blocks
        if isinstance(b, dict)
    )
    return ModelResponse(
        content="".join(text_parts),
        tool_calls=tool_calls,
        usage=_usage_from_anthropic(data.get("usage")),
        stop_reason=_stop_reason_from_anthropic(data.get("stop_reason")),
        raw=data,
        items=items,
    )


def _usage_from_anthropic(raw: Any) -> Usage:
    if not isinstance(raw, dict):
        return Usage()
    return Usage(
        input_tokens=_optional_int(raw.get("input_tokens")),
        output_tokens=_optional_int(raw.get("output_tokens")),
        cached_input_tokens=_optional_int(raw.get("cache_read_input_tokens")),
    )


def _stop_reason_from_anthropic(reason: Any) -> StopReason:
    if reason == "tool_use":
        return StopReason.TOOL_CALLS
    if reason == "max_tokens":
        return StopReason.MAX_TOKENS
    return StopReason.END_TURN


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class _ToolCallBlockAccumulator:
    """Reassembles streamed tool_use blocks from content_block events."""

    def __init__(self) -> None:
        self._blocks: dict[int, dict[str, Any]] = {}

    def begin(self, index: int | None, header: Any) -> None:
        if not isinstance(header, dict) or header.get("type") != "tool_use":
            return
        key = index if isinstance(index, int) else len(self._blocks)
        self._blocks[key] = {
            "id": str(header.get("id", "")),
            "name": str(header.get("name", "")),
            "json": "",
        }

    def append_json(self, index: int | None, partial: Any) -> None:
        if not isinstance(index, int) or index not in self._blocks:
            return
        if isinstance(partial, str):
            self._blocks[index]["json"] += partial

    def finalize(self) -> tuple[ToolCall, ...]:
        result = []
        for key in sorted(self._blocks):
            block = self._blocks[key]
            if not block["name"]:
                continue
            raw = block["json"] or "{}"
            invalid = False
            try:
                arguments = json.loads(raw)
            except json.JSONDecodeError:
                arguments = {}
                invalid = True
            if not isinstance(arguments, dict):
                arguments = {}
                invalid = True
            result.append(
                ToolCall(
                    id=block["id"],
                    name=block["name"],
                    arguments=arguments,
                    raw_arguments=raw,
                    arguments_invalid=invalid,
                )
            )
        return tuple(result)
