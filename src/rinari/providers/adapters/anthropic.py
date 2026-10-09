"""Anthropic adapter: https://api.anthropic.com (x-api-key + anthropic-version)."""

from __future__ import annotations

import json
import time
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
    DEFAULT_MODEL_STREAM_READ_TIMEOUT_S,
    MODEL_CALL_TIMEOUT,
    auth_failure,
    decode_json,
    iter_model_lines,
    open_model_stream,
    provider_error,
    sanitize_tool_name,
    send_request,
    session_affinity_headers,
    stream_close_details,
    stream_timeout_error,
)
from rinari.providers.urls import api_url
from rinari.shared.errors import InvalidUsageError, NetworkError, ProviderModelError

DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 8192


class AnthropicAdapter(ProviderAdapter):
    type = "anthropic"
    default_max_tokens = DEFAULT_MAX_TOKENS
    tool_image_transport = "tool-result"
    # Explicit prompt-cache breakpoints; a gateway that rejects them turns it off.
    prompt_caching = True

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
        url = api_url(self.base_url(endpoint), "models", version="v1") + "?limit=1"
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
        url = api_url(self.base_url(endpoint), "models", version="v1") + "?limit=100"
        response = send_request(self.client(), "GET", url, headers=self._headers(secret))
        if response.status_code in (401, 403):
            raise auth_failure(response, url)
        if response.status_code >= 400:
            raise provider_error(response, url)
        data = decode_json(response, url)
        if not isinstance(data, dict):
            raise ProviderModelError(f"Unexpected model list payload for {url}")
        from rinari.context.windows import normalize

        return [
            DiscoveredModel(
                provider_model_id=str(item["id"]),
                availability="available",
                capabilities=normalize(item) or None,
            )
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
        return api_url(self.base_url(endpoint), "messages", version="v1")

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
        headers = {
            **self.request_headers(secret, request),
            **session_affinity_headers(url, request.session_id),
        }
        response = send_request(
            self.client(),
            "POST",
            url,
            headers=headers,
            json_body=self._payload(request, stream=False, tool_aliases=tool_aliases),
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
        blocks: dict[int, dict[str, Any]] = {}
        calls = _ToolCallBlockAccumulator()
        usage = Usage()
        stop_reason = StopReason.END_TURN
        terminal_seen = False
        stream_stats: dict[str, Any] = {}
        headers_received = False
        saw_payload = False
        stream_started_at = time.monotonic()
        last_activity_at = stream_started_at
        stream_timeout_s = (
            request.stream_read_timeout_s
            if request.stream_read_timeout_s is not None
            else DEFAULT_MODEL_STREAM_READ_TIMEOUT_S
        )
        headers = {
            **self.request_headers(secret, request),
            **session_affinity_headers(url, request.session_id),
        }
        try:
            with open_model_stream(
                self.client(),
                request,
                stream_started_at,
                "POST",
                url,
                json=self._payload(request, stream=True, tool_aliases=tool_aliases),
                headers=headers,
            ) as response:
                headers_received = True
                if response.status_code in (401, 403):
                    raise auth_failure(response, url, model=request.model)
                if response.status_code >= 400:
                    raise provider_error(response, url, model=request.model)
                for line in iter_model_lines(response, request, stream_started_at, stream_stats):
                    if not line:
                        continue
                    saw_payload = True
                    last_activity_at = time.monotonic()
                    event = _parse_sse_line(line, url)
                    event_type = event.get("type")
                    if event_type == "message_stop":
                        terminal_seen = True
                        break
                    elif event_type == "error":
                        from rinari.providers.errors import classify_stream_error

                        raise classify_stream_error(event.get("error"), model=request.model)
                    elif event_type == "message_start":
                        usage = _usage_from_anthropic(_event_message(event).get("usage"))
                    elif event_type == "content_block_start":
                        block = event.get("content_block")
                        if isinstance(block, dict) and type(event.get("index")) is int:
                            blocks[event["index"]] = dict(block)
                        calls.begin(_optional_int(event.get("index")), event.get("content_block"))
                    elif event_type == "content_block_delta":
                        delta = event.get("delta") or {}
                        block = blocks.get(event.get("index"))
                        if block is not None:
                            field = {
                                "text_delta": "text",
                                "thinking_delta": "thinking",
                                "signature_delta": "signature",
                                "input_json_delta": "_json",
                            }.get(delta.get("type"))
                            if field:
                                block[field] = block.get(field, "") + delta.get(
                                    "partial_json" if field == "_json" else field, ""
                                )
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
                if not terminal_seen:
                    raise NetworkError(
                        "Response stream closed without a terminal event",
                        details={
                            "kind": "STREAM_INTERRUPTED",
                            **stream_close_details(
                                response,
                                stream_stats,
                                transport="anthropic",
                                url=url,
                                started_at=stream_started_at,
                                partial_tool_calls=any(
                                    block.get("type") == "tool_use" for block in blocks.values()
                                ),
                            ),
                        },
                    )
        except (NetworkError, ProviderModelError) as exc:
            exc.details.update(
                {"partial_text": "".join(content_parts), "partial": bool(content_parts)}
            )
            raise
        except httpx.TimeoutException as exc:
            error = stream_timeout_error(
                url,
                request.model,
                exc,
                timeout_s=stream_timeout_s,
                headers_received=headers_received,
                saw_payload=saw_payload,
                stream_started_at=stream_started_at,
                last_activity_at=last_activity_at,
                effective_timeouts=request.stream_timeouts,
                response=response if headers_received else None,
            )
            error.details.update(
                {"partial_text": "".join(content_parts), "partial": bool(content_parts)}
            )
            raise error from exc
        except httpx.TransportError as exc:
            raise NetworkError(
                f"Stream interrupted: {exc.__class__.__name__}",
                details={
                    "kind": "STREAM_INTERRUPTED",
                    "partial_text": "".join(content_parts),
                    "partial": bool(content_parts),
                },
            ) from exc
        tool_calls = () if stop_reason is StopReason.MAX_TOKENS else calls.finalize()
        if tool_calls and stop_reason is not StopReason.MAX_TOKENS:
            stop_reason = StopReason.TOOL_CALLS
        preserved = []
        for block in blocks.values():
            raw = block.pop("_json", None)
            if raw is not None:
                # Same rule as the accumulator: a call without arguments streams
                # `partial_json: ""`, and unparseable input still is a call the
                # loop answers. Dropping the block here left a tool_result
                # without its tool_use, which the API rejects with HTTP 400.
                try:
                    parsed = json.loads(raw or "{}")
                except ValueError:
                    parsed = {}
                block["input"] = parsed if isinstance(parsed, dict) else {}
            if stop_reason is StopReason.MAX_TOKENS and block.get("type") == "tool_use":
                continue
            preserved.append(block)
        return ModelResponse(
            content="".join(content_parts),
            tool_calls=tool_calls,
            usage=usage,
            stop_reason=stop_reason,
            continuation={"protocol": "anthropic", "blocks": preserved} if preserved else None,
            items=tuple(
                ModelItem(type=b["type"], data={k: v for k, v in b.items() if k != "type"})
                for b in preserved
            ),
        )

    def _payload(
        self,
        request: ModelRequest,
        *,
        stream: bool,
        tool_aliases: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        conversation, notes = _split_turn_context(request.messages)
        system, messages = _convert_to_anthropic(conversation, tool_aliases)
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
                    "name": _wire_tool_name(tool.name, tool_aliases),
                    "description": tool.description,
                    "input_schema": _anthropic_input_schema(tool.parameters),
                }
                for tool in request.tools
            ]
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.reasoning_effort is not None:
            from rinari.providers.metadata import claude_reasoning

            mode, levels = claude_reasoning(request.model)
            if request.reasoning_effort not in levels:
                raise InvalidUsageError(
                    f"Unsupported reasoning level for {request.model}: {request.reasoning_effort}"
                )
            if request.temperature is not None and request.temperature != 1:
                raise InvalidUsageError("Claude thinking requires default temperature.")
            payload.pop("temperature", None)
            if mode == "adaptive":
                payload["thinking"] = {"type": "adaptive"}
                payload["output_config"] = {"effort": request.reasoning_effort}
            else:
                budget = {"low": 1024, "medium": 2048, "high": 4096}[request.reasoning_effort]
                if payload["max_tokens"] <= budget:
                    raise InvalidUsageError(
                        f"max_tokens must exceed the thinking budget ({budget})."
                    )
                payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
        # json_response: Anthropic has no native JSON mode; the runtime checks
        # capabilities.structured_output before requesting it.
        if self.prompt_caching:
            _mark_cache_breakpoints(payload)
        # After the breakpoints: the note changes on every call, so the cached
        # prefix must end before it for the next call to read it back.
        _append_turn_context(payload, notes)
        return payload


# -- helpers -----------------------------------------------------------------

_EPHEMERAL = {"type": "ephemeral"}
# Blocks that may carry a cache breakpoint (thinking blocks may not).
_CACHEABLE_BLOCKS = frozenset({"text", "image", "document", "tool_use", "tool_result"})


def _mark_cache_breakpoints(payload: dict[str, Any]) -> None:
    """Ask Anthropic to cache the prompt prefix: tools, system and history.

    Anthropic caches nothing without `cache_control`, so every call paid for
    the whole system prompt, tool schemas and conversation again. Three of
    the four allowed breakpoints: the last tool, the system prompt and the
    last block of the newest message; each call then reads the prefix the
    previous one wrote. Copies what it marks: message blocks can belong to
    a stored continuation.
    """
    tools = payload.get("tools")
    if tools:
        tools[-1] = {**tools[-1], "cache_control": _EPHEMERAL}
    system = payload.get("system")
    if isinstance(system, str) and system:
        payload["system"] = [{"type": "text", "text": system, "cache_control": _EPHEMERAL}]
    messages = payload.get("messages") or []
    if not messages:
        return
    last = messages[-1]
    content = last.get("content")
    if isinstance(content, str):
        if content:
            blocks = [{"type": "text", "text": content, "cache_control": _EPHEMERAL}]
            messages[-1] = {**last, "content": blocks}
        return
    if not isinstance(content, list):
        return
    for index in range(len(content) - 1, -1, -1):
        block = content[index]
        if isinstance(block, dict) and block.get("type") in _CACHEABLE_BLOCKS:
            marked = list(content)
            marked[index] = {**block, "cache_control": _EPHEMERAL}
            messages[-1] = {**last, "content": marked}
            return


def _split_turn_context(
    messages: tuple[ChatMessage, ...],
) -> tuple[tuple[ChatMessage, ...], list[str]]:
    """The conversation, and the trailing turn-context notes that close it."""
    end = len(messages)
    while end and messages[end - 1].is_turn_context:
        end -= 1
    return messages[:end], [m.content or "" for m in messages[end:] if m.content]


def _append_turn_context(payload: dict[str, Any], notes: list[str]) -> None:
    """Close the request with the notes, as their own text blocks.

    A separate block (never concatenated into the previous text) keeps the
    block that carries the breakpoint identical between calls. Roles must
    alternate, so after a user turn (the owner's message or tool results)
    the note joins it; otherwise it opens a user turn of its own.
    """
    if not notes:
        return
    blocks = [{"type": "text", "text": note} for note in notes]
    messages = payload.setdefault("messages", [])
    if messages and messages[-1].get("role") == "user":
        last = messages[-1]
        existing = last.get("content")
        messages[-1] = {**last, "content": (_as_blocks(existing) if existing else []) + blocks}
    else:
        messages.append({"role": "user", "content": blocks})


def _anthropic_input_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Anthropic rejects root combinators; see `wire_input_schema`."""
    from rinari.tools.schema import wire_input_schema

    return wire_input_schema(schema)


def _replayed_blocks(
    message: ChatMessage, tool_aliases: dict[str, str] | None
) -> list[dict[str, Any]]:
    """Signed blocks as received, plus any tool_use the capture lost.

    Every tool_result that follows needs its tool_use in this message.
    Sessions saved before the capture fix lack argument-less calls and would
    fail with HTTP 400 on every retry; the call itself is in `tool_calls`.
    """
    blocks = list(message.continuation["blocks"])
    present = {
        block.get("id")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "tool_use"
    }
    for tc in message.tool_calls:
        if tc.id not in present:
            blocks.append(
                {
                    "type": "tool_use",
                    "id": tc.id,
                    "name": _wire_tool_name(tc.name, tool_aliases),
                    "input": tc.arguments,
                }
            )
    return blocks


def _wire_tool_name(name: str, tool_aliases: dict[str, str] | None) -> str:
    """Registry name -> wire name (F3).

    With an alias map, conforming names pass through and non-conforming
    ones use their collision-safe alias; without a map names are
    sanitized in place (unaliasing happens in the router).
    """
    if tool_aliases is None:
        return sanitize_tool_name(name)
    alias_of = {real: alias for alias, real in tool_aliases.items()}
    return alias_of.get(name, sanitize_tool_name(name))


def _convert_to_anthropic(
    messages: tuple[ChatMessage, ...],
    tool_aliases: dict[str, str] | None = None,
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
                            "content": (
                                [{"type": "text", "text": message.content or "Image loaded"}]
                                + [
                                    {
                                        "type": "image",
                                        "source": {
                                            "type": "base64",
                                            "media_type": "image/jpeg",
                                            "data": image.encoded(),
                                        },
                                    }
                                    for image in message.images
                                ]
                            )
                            if message.images
                            else message.content or "",
                        }
                    ],
                }
            )
            continue
        if message.role == "assistant":
            if message.continuation and message.continuation.get("protocol") == "anthropic":
                converted.append(
                    {"role": "assistant", "content": _replayed_blocks(message, tool_aliases)}
                )
                continue
            if message.tool_calls:
                blocks: list[dict[str, Any]] = []
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                for tc in message.tool_calls:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": _wire_tool_name(tc.name, tool_aliases),
                            "input": tc.arguments,
                        }
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
        tool_calls=() if data.get("stop_reason") == "max_tokens" else tool_calls,
        usage=_usage_from_anthropic(data.get("usage")),
        stop_reason=_stop_reason_from_anthropic(data.get("stop_reason")),
        raw=data,
        items=items,
        continuation={
            "protocol": "anthropic",
            "blocks": [
                b
                for b in blocks
                if isinstance(b, dict)
                and not (data.get("stop_reason") == "max_tokens" and b.get("type") == "tool_use")
            ],
        },
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
