"""OpenAI-compatible adapter: OpenAI, Ollama, LM Studio, custom endpoints."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from rinari.models.types import (
    ROLE_TOOL,
    ChatMessage,
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
    auth_failure,
    decode_json,
    provider_error,
    provider_error_detail,
    send_request,
)
from rinari.shared.errors import NetworkError, ProviderModelError


class OpenAICompatibleAdapter(ProviderAdapter):
    type = "openai-compatible"

    def __init__(
        self,
        default_base_url: str | None = None,
        *,
        client=None,
    ) -> None:
        super().__init__(client=client)
        self.default_base_url = default_base_url

    def auth_methods(self) -> tuple[str, ...]:
        return ("api-key", "none")

    def base_url(self, endpoint: str | None, settings: dict[str, Any] | None = None) -> str:
        if not (endpoint or self.default_base_url):
            from rinari.shared.errors import ConfigurationError

            raise ConfigurationError(
                "No endpoint configured",
                hint="Custom OpenAI-compatible providers need an endpoint, e.g. "
                "`--endpoint http://localhost:11434/v1`.",
            )
        return (endpoint or self.default_base_url).rstrip("/")

    def _headers(self, secret: str | None) -> dict[str, str]:
        if secret:
            return {"Authorization": f"Bearer {secret}"}
        return {}

    def validate_credential(self, secret: str | None, endpoint: str | None = None) -> AuthStatus:
        url = f"{self.base_url(endpoint, None)}/models"
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
        url = f"{self.base_url(endpoint, None)}/models"
        response = send_request(self.client(), "GET", url, headers=self._headers(secret))
        if response.status_code in (401, 403):
            raise auth_failure(response, url)
        if response.status_code >= 400:
            raise provider_error(response, url)
        data = decode_json(response, url) or {}
        entries = data.get("data", data if isinstance(data, list) else [])
        models: list[DiscoveredModel] = []
        for entry in entries:
            model_id = entry.get("id") if isinstance(entry, dict) else str(entry)
            if not model_id:
                continue
            models.append(
                DiscoveredModel(provider_model_id=str(model_id), availability="available")
            )
        return models

    # -- model invocation ---------------------------------------------------

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=True,
            tool_calls=True,
            structured_output=True,
            reasoning_effort=True,
        )

    def _chat_url(self, endpoint: str | None) -> str:
        return f"{self.base_url(endpoint, None)}/chat/completions"

    def _payload(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [_message_to_openai(m) for m in request.messages],
            "stream": stream,
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in request.tools
            ]
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.reasoning_effort:
            payload["reasoning_effort"] = request.reasoning_effort
        if request.json_response:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def invoke(
        self, request: ModelRequest, secret: str | None, endpoint: str | None = None
    ) -> ModelResponse:
        url = self._chat_url(endpoint)
        response = send_request(
            self.client(),
            "POST",
            url,
            headers=self._headers(secret),
            json_body=self._payload(request, stream=False),
            timeout=MODEL_CALL_TIMEOUT,
        )
        if response.status_code in (401, 403):
            raise auth_failure(response, url)
        if response.status_code >= 400:
            raise ProviderModelError(provider_error_detail(response, url))
        return _response_from_openai(decode_json(response, url), url)

    def invoke_stream(
        self,
        request: ModelRequest,
        secret: str | None,
        endpoint: str | None,
        on_delta: Callable[[str], None],
    ) -> ModelResponse:
        url = self._chat_url(endpoint)
        content_parts: list[str] = []
        calls = _ToolCallAccumulator()
        stop_reason = StopReason.END_TURN
        try:
            with self.client().stream(
                "POST",
                url,
                json=self._payload(request, stream=True),
                headers=self._headers(secret),
                timeout=MODEL_CALL_TIMEOUT,
            ) as response:
                if response.status_code in (401, 403):
                    raise auth_failure(response, url)
                if response.status_code >= 400:
                    raise ProviderModelError(provider_error_detail(response, url))
                for line in response.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    chunk = _parse_sse_payload(data, url)
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        content_parts.append(delta["content"])
                        on_delta(delta["content"])
                    calls.update(delta.get("tool_calls"))
                    reason = choice.get("finish_reason")
                    if reason:
                        stop_reason = _stop_reason_from_openai(reason)
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
            usage=Usage(),
            stop_reason=stop_reason,
        )


# -- helpers -----------------------------------------------------------------


def _message_to_openai(message: ChatMessage) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": message.role}
    if message.content is not None:
        msg["content"] = message.content
    if message.tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, sort_keys=True),
                },
            }
            for tc in message.tool_calls
        ]
    if message.role == ROLE_TOOL:
        msg["tool_call_id"] = message.tool_call_id
        if message.name:
            msg["name"] = message.name
    return msg


def _parse_sse_payload(data: str, url: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ProviderModelError(f"Provider returned invalid streaming JSON for {url}") from exc
    if not isinstance(value, dict):
        raise ProviderModelError(f"Unexpected streaming payload for {url}")
    return value


def _response_from_openai(data: Any, url: str) -> ModelResponse:
    try:
        choice = data["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderModelError(f"Unexpected chat completion payload from {url}") from exc
    tool_calls = tuple(_tool_call_from_openai(tc) for tc in message.get("tool_calls") or [])
    content = message.get("content")
    if content is None:
        content = ""
    elif not isinstance(content, str):
        content = str(content)
    return ModelResponse(
        content=content,
        tool_calls=tool_calls,
        usage=_usage_from_openai(data.get("usage")),
        stop_reason=_stop_reason_from_openai(
            choice.get("finish_reason"), has_tool_calls=bool(tool_calls)
        ),
        raw=data if isinstance(data, dict) else None,
    )


def _tool_call_from_openai(raw: Any) -> ToolCall:
    function = (raw or {}).get("function") or {}
    arguments_raw = function.get("arguments") or "{}"
    try:
        arguments = json.loads(arguments_raw) if isinstance(arguments_raw, str) else arguments_raw
    except json.JSONDecodeError:
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    return ToolCall(
        id=str((raw or {}).get("id") or ""),
        name=str(function.get("name") or ""),
        arguments=arguments,
    )


def _usage_from_openai(raw: Any) -> Usage:
    if not isinstance(raw, dict):
        return Usage()
    prompt_details = raw.get("prompt_tokens_details") or {}
    completion_details = raw.get("completion_tokens_details") or {}
    return Usage(
        input_tokens=_optional_int(raw.get("prompt_tokens")),
        output_tokens=_optional_int(raw.get("completion_tokens")),
        cached_input_tokens=_optional_int(prompt_details.get("cached_tokens")),
        reasoning_tokens=_optional_int(completion_details.get("reasoning_tokens")),
    )


def _stop_reason_from_openai(reason: Any, has_tool_calls: bool = False) -> StopReason:
    if reason == "tool_calls" or reason == "function_call" or (has_tool_calls and not reason):
        return StopReason.TOOL_CALLS
    if reason == "length":
        return StopReason.MAX_TOKENS
    return StopReason.END_TURN


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class _ToolCallAccumulator:
    """Reassembles tool calls streamed as incremental delta fragments."""

    def __init__(self) -> None:
        self._by_index: dict[int, dict[str, Any]] = {}

    def update(self, fragments: Any) -> None:
        for fragment in fragments or []:
            if not isinstance(fragment, dict):
                continue
            index = fragment.get("index")
            index = index if isinstance(index, int) else len(self._by_index)
            slot = self._by_index.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if fragment.get("id"):
                slot["id"] = fragment["id"]
            function = fragment.get("function") or {}
            if function.get("name"):
                slot["name"] += function["name"]
            if isinstance(function.get("arguments"), str):
                slot["arguments"] += function["arguments"]

    def finalize(self) -> tuple[ToolCall, ...]:
        result = []
        for index in sorted(self._by_index):
            slot = self._by_index[index]
            if not slot["name"]:
                continue
            try:
                arguments = json.loads(slot["arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            result.append(ToolCall(id=slot["id"], name=slot["name"], arguments=arguments))
        return tuple(result)
