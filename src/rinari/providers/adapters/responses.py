"""OpenAI Responses API adapter (P0.7/Etapa D).

Dedicated to the modern ``/responses`` wire API — function tools, response
items, reasoning controls, ``previous_response_id``-friendly state — instead
of reducing these providers to the ``/chat/completions`` least common
denominator. ``OpenAICompatibleAdapter`` keeps serving chat transports and
delegates ``transport="responses"`` here, so adapter selection is unchanged.

Rule (review §P0.7): ``provider_state`` (e.g. ``response_id``) is a replay
optimization only. Rinari session history stays the single source of truth;
there is no ``previous_response_id`` chaining here until privacy review
allows it, and any future chaining must keep the local replay fallback.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from rinari.models.types import (
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
    sanitize_tool_name,
    send_request,
    session_affinity_headers,
)
from rinari.shared.errors import NetworkError, ProviderModelError


class OpenAIResponsesAdapter(ProviderAdapter):
    """Responses-API transport for OpenAI-style providers."""

    type = "openai-responses"

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
                hint="Responses providers need an endpoint, e.g. `--endpoint https://api.openai.com/v1`.",
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

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=True,
            tool_calls=True,
            structured_output=True,
            reasoning_effort=True,
        )

    def _responses_url(self, endpoint: str | None) -> str:
        return f"{self.base_url(endpoint, None)}/responses"

    # -- invocation ------------------------------------------------------

    def invoke(
        self,
        request: ModelRequest,
        secret: str | None,
        endpoint: str | None = None,
        *,
        tool_aliases: dict[str, str] | None = None,
    ) -> ModelResponse:
        url = self._responses_url(endpoint)
        headers = {**self._headers(secret), **session_affinity_headers(url, request.session_id)}
        response = send_request(
            self.client(),
            "POST",
            url,
            headers=headers,
            json_body=self._responses_payload(request, stream=False, tool_aliases=tool_aliases),
            timeout=MODEL_CALL_TIMEOUT,
        )
        if response.status_code in (401, 403):
            raise auth_failure(response, url, model=request.model)
        if response.status_code >= 400:
            raise provider_error(response, url, model=request.model)
        return _response_from_responses(decode_json(response, url), url)

    def invoke_stream(
        self,
        request: ModelRequest,
        secret: str | None,
        endpoint: str | None,
        on_delta: Callable[[str], None],
        *,
        tool_aliases: dict[str, str] | None = None,
    ) -> ModelResponse:
        url = self._responses_url(endpoint)
        headers = {**self._headers(secret), **session_affinity_headers(url, request.session_id)}
        acc = _ResponsesStreamAccumulator()
        try:
            with self.client().stream(
                "POST",
                url,
                json=self._responses_payload(request, stream=True, tool_aliases=tool_aliases),
                headers=headers,
                timeout=MODEL_STREAM_TIMEOUT,
            ) as response:
                if response.status_code in (401, 403):
                    raise auth_failure(response, url, model=request.model)
                if response.status_code >= 400:
                    raise provider_error(response, url, model=request.model)
                for line in response.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    acc.update(_parse_sse_payload(data, url), on_delta)
        except httpx.TimeoutException as exc:
            raise NetworkError(f"Timed out streaming from {url}") from exc
        except httpx.TransportError as exc:
            raise NetworkError(f"Stream interrupted: {exc.__class__.__name__}") from exc
        return acc.finalize(url)

    def _responses_payload(
        self,
        request: ModelRequest,
        *,
        stream: bool,
        tool_aliases: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "input": [
                item for m in request.messages for item in _message_to_responses(m, tool_aliases)
            ],
            "stream": stream,
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": _wire_tool_name(tool.name, tool_aliases),
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
                for tool in request.tools
            ]
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_output_tokens"] = request.max_tokens
        if request.reasoning_effort:
            payload["reasoning"] = {"effort": request.reasoning_effort}
        if request.json_response:
            payload["text"] = {"format": {"type": "json_object"}}
        return payload


# -- helpers -----------------------------------------------------------------


def _wire_tool_name(name: str, tool_aliases: dict[str, str] | None) -> str:
    """Registry name -> wire name (strict-pattern fallback under aliasing)."""
    if tool_aliases is None:
        return name
    alias_of = {real: alias for alias, real in tool_aliases.items()}
    return alias_of.get(name, sanitize_tool_name(name))


def _message_to_responses(
    message: ChatMessage, tool_aliases: dict[str, str] | None
) -> list[dict[str, Any]]:
    """Chat history -> Responses input items (tool calls keep wire names)."""
    if message.role == ROLE_TOOL:
        return [
            {
                "type": "function_call_output",
                "call_id": message.tool_call_id,
                "output": message.content or "",
            }
        ]
    items: list[dict[str, Any]] = []
    if message.content:
        allowed = ("system", "developer", "user", "assistant")
        items.append(
            {
                "role": message.role if message.role in allowed else "user",
                "content": message.content,
            }
        )
    for tc in message.tool_calls:
        items.append(
            {
                "type": "function_call",
                "call_id": tc.id,
                "name": _wire_tool_name(tc.name, tool_aliases),
                "arguments": json.dumps(tc.arguments, sort_keys=True),
            }
        )
    return items


def _parse_sse_payload(data: str, url: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ProviderModelError(f"Provider returned invalid streaming JSON for {url}") from exc
    if not isinstance(value, dict):
        raise ProviderModelError(f"Unexpected streaming payload for {url}")
    return value


def _function_call_from_responses(raw: Any) -> ToolCall | None:
    if not isinstance(raw, dict) or raw.get("type") != "function_call":
        return None
    arguments_raw = raw.get("arguments") or "{}"
    invalid = False
    try:
        arguments = json.loads(arguments_raw) if isinstance(arguments_raw, str) else arguments_raw
    except json.JSONDecodeError:
        arguments = {}
        invalid = True
    if not isinstance(arguments, dict):
        arguments = {}
        invalid = True
    call_id = raw.get("call_id") or raw.get("id") or ""
    return ToolCall(
        id=str(call_id),
        name=str(raw.get("name") or ""),
        arguments=arguments,
        raw_arguments=arguments_raw if isinstance(arguments_raw, str) else None,
        arguments_invalid=invalid,
    )


def _usage_from_responses(raw: Any) -> Usage:
    if not isinstance(raw, dict):
        return Usage()
    return Usage(
        input_tokens=_optional_int(raw.get("input_tokens")),
        output_tokens=_optional_int(raw.get("output_tokens")),
    )


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _response_from_responses(data: Any, url: str) -> ModelResponse:
    if not isinstance(data, dict):
        raise ProviderModelError(f"Unexpected responses payload from {url}")
    if data.get("status") == "failed":
        err = data.get("error") or {}
        detail = err.get("message") if isinstance(err, dict) else None
        raise ProviderModelError(
            f"Provider returned an errored response for {url}"
            + (f": {str(detail)[:300]}" if detail else "")
        )
    output = data.get("output")
    if not isinstance(output, list):
        raise ProviderModelError(f"Unexpected responses payload from {url}")
    content_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    items: list[ModelItem] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        items.append(
            ModelItem(
                type=str(item.get("type") or "unknown"),
                id=item.get("id") if isinstance(item.get("id"), str) else None,
                data={k: v for k, v in item.items() if k not in ("type", "id")},
            )
        )
        if item.get("type") == "message":
            for block in item.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "output_text":
                    content_parts.append(str(block.get("text") or ""))
        else:
            call = _function_call_from_responses(item)
            if call is not None and call.name:
                tool_calls.append(call)
    status = data.get("status")
    incomplete = data.get("incomplete_details") or {}
    if status == "incomplete" and incomplete.get("reason") == "max_output_tokens":
        stop_reason = StopReason.MAX_TOKENS
    elif tool_calls:
        stop_reason = StopReason.TOOL_CALLS
    else:
        stop_reason = StopReason.END_TURN
    provider_state: dict[str, Any] | None = None
    if isinstance(data.get("id"), str):
        provider_state = {"response_id": data["id"]}
    return ModelResponse(
        content="".join(content_parts),
        tool_calls=tuple(tool_calls),
        usage=_usage_from_responses(data.get("usage")),
        stop_reason=stop_reason,
        raw=data,
        items=tuple(items),
        provider_state=provider_state,
    )


class _ResponsesStreamAccumulator:
    """Reassembles a Responses API SSE stream.

    Text deltas go live to on_delta; the terminal response.completed event
    is authoritative for content, tool calls, and usage.
    """

    def __init__(self) -> None:
        self._text_parts: list[str] = []
        self._completed: dict[str, Any] | None = None

    def update(self, event: dict[str, Any], on_delta: Callable[[str], None]) -> None:
        event_type = event.get("type")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                self._text_parts.append(delta)
                on_delta(delta)
        elif event_type == "response.completed":
            response = event.get("response")
            if isinstance(response, dict):
                self._completed = response

    def finalize(self, url: str) -> ModelResponse:
        if self._completed is None:
            return ModelResponse(content="".join(self._text_parts), tool_calls=())
        return _response_from_responses(self._completed, url)
