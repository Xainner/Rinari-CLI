"""OpenAI-compatible adapter: OpenAI, Ollama, LM Studio, custom endpoints."""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from rinari.models.images import expand_tool_images
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
    DEFAULT_MODEL_STREAM_READ_TIMEOUT_S,
    MODEL_CALL_TIMEOUT,
    auth_failure,
    decode_json,
    iter_model_lines,
    open_model_stream,
    openai_cache_key,
    provider_error,
    sanitize_tool_name,
    send_request,
    session_affinity_headers,
    stream_close_details,
    stream_timeout_error,
)
from rinari.shared.errors import InvalidUsageError, NetworkError, ProviderModelError
from rinari.tools.schema import wire_input_schema


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
        entries = data if isinstance(data, list) else data.get("data", [])
        models: list[DiscoveredModel] = []
        for entry in entries:
            model_id = entry.get("id") if isinstance(entry, dict) else str(entry)
            if not model_id:
                continue
            from rinari.context.windows import normalize

            models.append(
                DiscoveredModel(
                    provider_model_id=str(model_id),
                    availability="available",
                    capabilities=normalize(entry) or None,
                )
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

    def _responses_url(self, endpoint: str | None) -> str:
        return f"{self.base_url(endpoint, None)}/responses"

    @staticmethod
    def _check_transport(transport: str) -> None:
        if transport not in ("chat", "responses"):
            raise InvalidUsageError(
                f"unknown transport {transport!r}",
                hint="Expected one of: chat, responses.",
            )

    def _payload(
        self,
        request: ModelRequest,
        *,
        stream: bool,
        tool_aliases: dict[str, str] | None = None,
        fold_system: bool = False,
    ) -> dict[str, Any]:
        messages = _with_reasoning_slots(
            _joined_user_messages(
                [_message_to_openai(m, tool_aliases) for m in expand_tool_images(request.messages)]
            )
        )
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": _folded_system(messages) if fold_system else messages,
            "stream": stream,
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": _wire_tool_name(tool.name, tool_aliases),
                        "description": tool.description,
                        "parameters": wire_input_schema(tool.parameters),
                    },
                }
                for tool in request.tools
            ]
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.reasoning_effort:
            if request.reasoning_dialect == "openrouter":
                payload["reasoning"] = {"effort": request.reasoning_effort}
            else:
                payload["reasoning_effort"] = request.reasoning_effort
        if request.json_response:
            payload["response_format"] = {"type": "json_object"}
        if stream:
            # Ask for authoritative usage in the terminal chunk (§6.2); when
            # the endpoint stays silent the response is marked unavailable.
            payload["stream_options"] = {"include_usage": True}
        return payload

    def invoke(
        self,
        request: ModelRequest,
        secret: str | None,
        endpoint: str | None = None,
        *,
        transport: str = "chat",
        tool_aliases: dict[str, str] | None = None,
    ) -> ModelResponse:
        self._check_transport(transport)
        if transport == "responses":
            return self._responses_adapter().invoke(
                request, secret, endpoint, tool_aliases=tool_aliases
            )
        url = self._chat_url(endpoint)
        key = (url, request.model)
        fold = _SYSTEM_FOLD.get(key) is True
        try:
            return self._chat_once(request, secret, url, tool_aliases, fold_system=fold)
        except ProviderModelError as exc:
            if fold or not _may_refuse_system(exc, request, key):
                raise
            try:
                response = self._chat_once(request, secret, url, tool_aliases, fold_system=True)
            except ProviderModelError:
                _SYSTEM_FOLD[key] = False
                raise exc from None
            _SYSTEM_FOLD[key] = True
            return response

    def _chat_once(
        self,
        request: ModelRequest,
        secret: str | None,
        url: str,
        tool_aliases: dict[str, str] | None,
        *,
        fold_system: bool,
    ) -> ModelResponse:
        headers = {
            **self.request_headers(secret, request),
            **session_affinity_headers(url, request.session_id),
        }
        response = send_request(
            self.client(),
            "POST",
            url,
            headers=headers,
            json_body=openai_cache_key(
                self._payload(
                    request, stream=False, tool_aliases=tool_aliases, fold_system=fold_system
                ),
                url,
                request,
            ),
            timeout=MODEL_CALL_TIMEOUT,
        )
        if response.status_code in (401, 403):
            raise auth_failure(response, url, model=request.model)
        if response.status_code >= 400:
            raise provider_error(response, url, model=request.model)
        return _response_from_openai(decode_json(response, url), url)

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
        self._check_transport(transport)
        if transport == "responses":
            return self._responses_adapter().invoke_stream(
                request, secret, endpoint, on_delta, tool_aliases=tool_aliases
            )
        url = self._chat_url(endpoint)
        key = (url, request.model)
        fold = _SYSTEM_FOLD.get(key) is True
        sent = False

        def relay(text: str) -> None:
            nonlocal sent
            sent = True
            on_delta(text)

        try:
            return self._chat_stream(request, secret, url, relay, tool_aliases, fold_system=fold)
        except ProviderModelError as exc:
            if fold or sent or not _may_refuse_system(exc, request, key):
                raise
            try:
                response = self._chat_stream(
                    request, secret, url, on_delta, tool_aliases, fold_system=True
                )
            except ProviderModelError:
                _SYSTEM_FOLD[key] = False
                raise exc from None
            _SYSTEM_FOLD[key] = True
            return response

    def _chat_stream(
        self,
        request: ModelRequest,
        secret: str | None,
        url: str,
        on_delta: Callable[[str], None],
        tool_aliases: dict[str, str] | None,
        *,
        fold_system: bool,
    ) -> ModelResponse:
        content_parts: list[str] = []
        continuation_message: dict[str, Any] = {"role": "assistant"}
        continuation_tools: dict[int, dict[str, Any]] = {}
        calls = _ToolCallAccumulator()
        stop_reason = StopReason.END_TURN
        terminal_seen = False
        done_seen = False
        stream_stats: dict[str, Any] = {}
        stream_usage: dict[str, Any] | None = None
        stream_timings: dict[str, Any] | None = None
        tool_names: dict[int, str] = {}
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
                json=openai_cache_key(
                    self._payload(
                        request, stream=True, tool_aliases=tool_aliases, fold_system=fold_system
                    ),
                    url,
                    request,
                ),
                headers=headers,
            ) as response:
                headers_received = True
                if response.status_code in (401, 403):
                    raise auth_failure(response, url, model=request.model)
                if response.status_code >= 400:
                    raise provider_error(response, url, model=request.model)
                for line in iter_model_lines(response, request, stream_started_at, stream_stats):
                    if not line or not line.startswith("data:"):
                        continue
                    saw_payload = True
                    last_activity_at = time.monotonic()
                    data = line[5:].strip()
                    if data == "[DONE]":
                        done_seen = True
                        break
                    chunk = _parse_sse_payload(data, url)
                    if chunk.get("error"):
                        from rinari.providers.errors import classify_stream_error

                        raise classify_stream_error(chunk["error"], model=request.model)
                    if isinstance(chunk.get("usage"), dict):
                        stream_usage = chunk["usage"]
                    if isinstance(chunk.get("timings"), dict):
                        stream_timings = chunk["timings"]
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    _continuation_delta(continuation_message, continuation_tools, delta)
                    if delta.get("content"):
                        content_parts.append(delta["content"])
                        on_delta(delta["content"])
                    calls.update(delta.get("tool_calls"))
                    if request.tool_input_observer is not None:
                        _report_tool_input(
                            request.tool_input_observer, tool_names, delta.get("tool_calls")
                        )
                    reason = choice.get("finish_reason")
                    if reason:
                        terminal_seen = True
                        stop_reason = _stop_reason_from_openai(reason)
                if not terminal_seen:
                    raise NetworkError(
                        "Response stream closed without a terminal event",
                        details={
                            "kind": "STREAM_INTERRUPTED",
                            **stream_close_details(
                                response,
                                stream_stats,
                                transport="chat",
                                url=url,
                                started_at=stream_started_at,
                                done_seen=done_seen,
                                partial_tool_calls=bool(continuation_tools),
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
        # §6.2: captured chunk usage is authoritative; silence is marked,
        # never zero-filled.
        usage = (
            _usage_from_openai(stream_usage, stream_timings)
            if stream_usage
            else Usage(source="unavailable")
        )
        if continuation_tools:
            continuation_message["tool_calls"] = [
                continuation_tools[k] for k in sorted(continuation_tools)
            ]
        return ModelResponse(
            content="".join(content_parts),
            tool_calls=tool_calls,
            usage=usage,
            stop_reason=stop_reason,
            continuation={"protocol": "chat", "message": continuation_message}
            if stop_reason is not StopReason.MAX_TOKENS
            else None,
        )

    def _responses_adapter(self):  # OpenAIResponsesAdapter (P0.7 split)
        from rinari.providers.adapters.responses import OpenAIResponsesAdapter

        return OpenAIResponsesAdapter(default_base_url=self.default_base_url, client=self.client())

    # -- Responses API (/responses) -------------------------------------------
    # Implemented by OpenAIResponsesAdapter; this class only delegates so
    # adapter selection (registry.py) is unchanged.


# -- helpers -----------------------------------------------------------------


def _report_tool_input(observer, names: dict[int, str], tool_calls) -> None:
    """How much of each streamed tool call is written so far (F26)."""
    for call in tool_calls or ():
        if not isinstance(call, dict):
            continue
        index = call.get("index", 0) if isinstance(call.get("index", 0), int) else 0
        function = call.get("function") or {}
        if function.get("name"):
            names[index] = str(function["name"])
        with contextlib.suppress(Exception):
            observer(names.get(index), function.get("arguments"))


def _continuation_delta(message, tools, delta):
    """Preserve signed extensions without publishing private reasoning deltas."""

    def merge(target, fragment):
        for key, value in fragment.items():
            if isinstance(value, dict):
                merge(target.setdefault(key, {}), value)
            elif isinstance(value, str):
                target[key] = target.get(key, "") + value

    merge(
        message,
        {
            k: v
            for k, v in delta.items()
            if k in ("content", "reasoning_content", "reasoning", "extra_content")
        },
    )
    for fragment in delta.get("tool_calls") or []:
        index = fragment.get("index", 0)
        slot = tools.setdefault(index, {"type": "function"})
        if fragment.get("id"):
            slot["id"] = fragment["id"]
        merge(slot, {k: v for k, v in fragment.items() if k in ("function", "extra_content")})


def _wire_tool_name(name: str, tool_aliases: dict[str, str] | None) -> str:
    """Registry name -> wire name.

    With an alias map (aliasing active) unknown names fall back to the
    strict-pattern rewrite; without a map names pass through untouched so
    off-vendor payloads stay byte-identical.
    """
    if tool_aliases is None:
        return name
    alias_of = {real: alias for alias, real in tool_aliases.items()}
    return alias_of.get(name, sanitize_tool_name(name))


# Servers whose chat template refused a system message, by (url, model):
# True once the same request went through with the system folded into the
# first user message, False when folding did not help either.
_SYSTEM_FOLD: dict[tuple[str, str], bool] = {}
# A plain refusal of the request: no provider code that says something more
# specific (a limit, the context, a tool schema), and not an overload.
_REFUSAL_STATUSES = frozenset({400, 422, 500})


def _may_refuse_system(error: ProviderModelError, request: ModelRequest, key) -> bool:
    """Whether to try the request once more with the system message folded.

    Templates such as Gemma 2's reject the system role outright and llama.cpp
    or vLLM answer with a plain 400/500. Nothing is read from the message:
    only the status, the absence of a more specific code, and whether this
    server and model were already tried.
    """
    if key in _SYSTEM_FOLD or error.details.get("partial"):
        return False
    if not any(message.role == "system" for message in request.messages):
        return False
    code = error.details.get("provider_error_code")
    if code != "SERVER_ERROR" or error.details.get("http_status") not in _REFUSAL_STATUSES:
        return False
    # A refusal with a cause of its own is handled where that cause is: the
    # reasoning control is repeated without it by the router.
    from rinari.models.router import rejects_reasoning_control

    return not (request.reasoning_effort and rejects_reasoning_control(error))


def _folded_system(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The system message as the start of the first user message."""
    system = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    text = "\n\n".join(str(m.get("content") or "") for m in system if m.get("content"))
    if not text:
        return rest
    for index, message in enumerate(rest):
        if message.get("role") == "user":
            rest[index] = {**message, "content": _joined_content(text, message.get("content"))}
            return rest
    return [{"role": "user", "content": text}, *rest]


def _with_reasoning_slots(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every tool-calling assistant message carries `reasoning_content` once the
    conversation does.

    A thinking model can return a tool step with no reasoning at all; sent back
    without the field, an endpoint that requires its reasoning to be returned
    (DeepSeek's thinking mode) refused the whole request with a 400 and the turn
    failed. Only a conversation that already carries the field gets it.
    """
    if not any(m.get("role") == "assistant" and "reasoning_content" in m for m in messages):
        return messages
    return [
        {**m, "reasoning_content": ""}
        if m.get("role") == "assistant" and m.get("tool_calls") and "reasoning_content" not in m
        else m
        for m in messages
    ]


def _joined_user_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Consecutive user messages as one: the turn's message and the harness notes
    after it (the local time, the tools note, the turn context).

    Chat templates that require user and assistant to alternate (Gemma and
    Mistral on llama.cpp or vLLM) reject two user messages in a row. The join
    is the same on every request, so the cached prefix does not move.
    """
    joined: list[dict[str, Any]] = []
    for message in messages:
        previous = joined[-1] if joined else None
        if (
            previous is not None
            and message.get("role") == "user"
            and previous.get("role") == "user"
            and set(message) <= {"role", "content"}
            and set(previous) <= {"role", "content"}
        ):
            joined[-1] = {
                "role": "user",
                "content": _joined_content(previous.get("content"), message.get("content")),
            }
            continue
        joined.append(message)
    return joined


def _joined_content(first: Any, second: Any) -> Any:
    if isinstance(first, list) or isinstance(second, list):
        return _content_parts(first) + _content_parts(second)
    texts = [text for text in (first, second) if text]
    return "\n\n".join(str(text) for text in texts)


def _content_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list):
        return list(content)
    return [{"type": "text", "text": str(content)}] if content else []


def _message_to_openai(
    message: ChatMessage, tool_aliases: dict[str, str] | None = None
) -> dict[str, Any]:
    if (
        message.role == "assistant"
        and message.continuation
        and message.continuation.get("protocol") == "chat"
    ):
        return message.continuation["message"]
    msg: dict[str, Any] = {"role": message.role}
    if message.content is not None:
        msg["content"] = message.content
    if message.images:
        msg["content"] = [{"type": "text", "text": message.content or ""}] + [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + i.encoded()}}
            for i in message.images
        ]
    if message.tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": _wire_tool_name(tc.name, tool_aliases),
                    "arguments": json.dumps(tc.arguments, sort_keys=True),
                },
            }
            for tc in message.tool_calls
        ]
    if message.role == ROLE_TOOL:
        # Chat Completions tool messages carry role, content and tool_call_id
        # only. "name" belonged to the retired function role; strict
        # endpoints (OpenCode Go/Zen) reject it with HTTP 400.
        msg["tool_call_id"] = message.tool_call_id
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
    items: list[ModelItem] = []
    if content:
        items.append(ModelItem(type="text", data={"text": content}))
    for tc in message.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        function = tc.get("function") or {}
        items.append(
            ModelItem(
                type="function_call",
                id=str(tc.get("id") or "") or None,
                data={
                    "name": str(function.get("name") or ""),
                    "arguments": function.get("arguments"),
                },
            )
        )
    return ModelResponse(
        content=content,
        tool_calls=() if choice.get("finish_reason") == "length" else tool_calls,
        usage=_usage_from_openai(data.get("usage"), data.get("timings")),
        stop_reason=_stop_reason_from_openai(
            choice.get("finish_reason"), has_tool_calls=bool(tool_calls)
        ),
        raw=data if isinstance(data, dict) else None,
        items=tuple(items),
        continuation={
            "protocol": "chat",
            "message": {
                k: v
                for k, v in message.items()
                if k
                in (
                    "role",
                    "content",
                    "tool_calls",
                    "reasoning_content",
                    "reasoning",
                    "extra_content",
                )
            },
        }
        if choice.get("finish_reason") != "length"
        else None,
    )


def _tool_call_from_openai(raw: Any) -> ToolCall:
    function = (raw or {}).get("function") or {}
    arguments_raw = function.get("arguments") or "{}"
    invalid = False
    try:
        arguments = json.loads(arguments_raw) if isinstance(arguments_raw, str) else arguments_raw
    except json.JSONDecodeError:
        arguments = {}
        invalid = True
    if not isinstance(arguments, dict):
        arguments = {}
        invalid = True
    return ToolCall(
        id=str((raw or {}).get("id") or ""),
        name=str(function.get("name") or ""),
        arguments=arguments,
        raw_arguments=arguments_raw if isinstance(arguments_raw, str) else None,
        arguments_invalid=invalid,
    )


def _usage_from_openai(raw: Any, timings: Any = None) -> Usage:
    if not isinstance(raw, dict):
        return Usage()
    prompt_details = raw.get("prompt_tokens_details") or {}
    completion_details = raw.get("completion_tokens_details") or {}
    # The cache read, in whichever of the known shapes the server reports it
    # (plan 0.4.0 v2, V2-11): the standard field, `prompt_cache_hit_tokens`
    # (DeepSeek's own API) or llama.cpp's `timings.cache_n`. By protocol shape,
    # for any provider or model that uses it; none of them is "no data", not 0.
    cached = _optional_int(
        prompt_details.get("cached_tokens") if isinstance(prompt_details, dict) else None
    )
    if cached is None:
        cached = _optional_int(raw.get("prompt_cache_hit_tokens"))
    if cached is None and isinstance(timings, dict):
        cached = _optional_int(timings.get("cache_n"))
    return Usage(
        input_tokens=_optional_int(raw.get("prompt_tokens")),
        output_tokens=_optional_int(raw.get("completion_tokens")),
        cached_input_tokens=cached,
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
            raw = slot["arguments"] or "{}"
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
                    id=slot["id"],
                    name=slot["name"],
                    arguments=arguments,
                    raw_arguments=raw,
                    arguments_invalid=invalid,
                )
            )
        return tuple(result)
