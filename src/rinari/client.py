"""Cliente multi-provider para rinari.

Habla con endpoints OpenAI-compatibles (vLLM, LiteLLM, llama.cpp, OpenRouter,
DeepSeek, Gemini, OpenAI) y con la API nativa de Anthropic.

Cada provider tiene un api_format:
- "openai": POST /chat/completions con Authorization Bearer
- "anthropic": POST /v1/messages con x-api-key + anthropic-version

Soporta:
- chat_stream(): streaming de deltas (str o dicts con tool_calls)
- chat(): llamada no-stream, devuelve content completo
- chat_message(): message completo (con tool_calls)
- list_models(): lista modelos del endpoint
"""

from __future__ import annotations

import json
from typing import Any, Generator

import httpx

ANTHROPIC_VERSION = "2023-06-01"


class LLMError(Exception):
    """Error de LLM con mensaje claro (HTTP, red, parse)."""


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        model: str = "qwen3.6-27b",
        timeout: float = 300.0,
        transport: httpx.BaseTransport | None = None,
        provider: str = "openai",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._transport = transport
        self.provider = provider
        self.api_format = "anthropic" if provider == "anthropic" else "openai"

    # ---------------------------------------------------------------- helpers
    def _headers(self) -> dict[str, str]:
        if self.api_format == "anthropic":
            headers = {
                "Content-Type": "application/json",
                "anthropic-version": ANTHROPIC_VERSION,
            }
            if self.api_key:
                headers["x-api-key"] = self.api_key
            return headers
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _client(self) -> httpx.Client:
        kwargs: dict[str, Any] = {"timeout": self.timeout, "headers": self._headers()}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.Client(**kwargs)

    def _chat_url(self) -> str:
        if self.api_format == "anthropic":
            return f"{self.base_url}/messages"
        return f"{self.base_url}/chat/completions"

    def _models_url(self) -> str:
        return f"{self.base_url}/models"

    def _build_payload(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int | None,
        tools: list[dict] | None,
        stream: bool,
    ) -> dict[str, Any]:
        if self.api_format == "anthropic":
            return self._anthropic_payload(messages, temperature, max_tokens, tools, stream)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
        return payload

    def _anthropic_payload(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int | None,
        tools: list[dict] | None,
        stream: bool,
    ) -> dict[str, Any]:
        """Convierte mensajes/tools OpenAI-style al formato nativo de Anthropic."""
        system_parts = [m["content"] for m in messages if m.get("role") == "system"]
        chat_messages = [m for m in messages if m.get("role") != "system"]
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": chat_messages,
            "max_tokens": max_tokens or 4096,  # obligatorio en Anthropic
            "temperature": temperature,
            "stream": stream,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if tools:
            payload["tools"] = [
                {
                    "name": t["function"]["name"],
                    "description": t["function"].get("description", ""),
                    "input_schema": t["function"].get("parameters", {"type": "object"}),
                }
                for t in tools
                if t.get("type") == "function"
            ]
        return payload

    @staticmethod
    def _parse_anthropic_message(data: dict) -> dict:
        """Convierte una respuesta Anthropic a forma OpenAI (message dict)."""
        content = ""
        tool_calls = []
        for block in data.get("content") or []:
            if block.get("type") == "text":
                content += block.get("text", "")
            elif block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                })
        msg = {"content": content, "role": "assistant"}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        return msg

    # ------------------------------------------------------------- chat_stream
    def chat_stream(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
    ) -> Generator[str | dict, None, None]:
        """Streaming de chat. Emite str (deltas de contenido) o dict (tool_calls)."""
        if self.api_format == "anthropic":
            yield from self._anthropic_stream(messages, temperature, max_tokens, tools)
            return
        payload = self._build_payload(messages, temperature, max_tokens, tools, stream=True)

        try:
            with self._client() as client:
                with client.stream("POST", self._chat_url(), json=payload) as resp:
                    if resp.status_code != 200:
                        body = resp.read().decode("utf-8", errors="replace")
                        raise LLMError(self._error_message(resp.status_code, body))
                    for line in resp.iter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[len("data:"):].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        if delta.get("tool_calls"):
                            yield {"tool_calls": delta["tool_calls"]}
                        if delta.get("content"):
                            yield delta["content"]
        except LLMError:
            raise
        except httpx.HTTPError as e:
            raise LLMError(f"No se pudo conectar a {self.base_url}: {e}") from e

    def _anthropic_stream(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int | None,
        tools: list[dict] | None,
    ) -> Generator[str | dict, None, None]:
        """Streaming nativo Anthropic: eventos content_block_delta / message_delta."""
        payload = self._build_payload(messages, temperature, max_tokens, tools, stream=True)
        try:
            with self._client() as client:
                with client.stream("POST", self._chat_url(), json=payload) as resp:
                    if resp.status_code != 200:
                        body = resp.read().decode("utf-8", errors="replace")
                        raise LLMError(self._error_message(resp.status_code, body))
                    for line in resp.iter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[len("data:"):].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            event = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        etype = event.get("type")
                        if etype == "content_block_delta":
                            delta = event.get("delta") or {}
                            if delta.get("type") == "text_delta" and delta.get("text"):
                                yield delta["text"]
                            elif delta.get("type") == "input_json_delta" and delta.get("partial_json"):
                                yield {"tool_call_delta": delta["partial_json"]}
                        elif etype == "message_stop":
                            break
        except LLMError:
            raise
        except httpx.HTTPError as e:
            raise LLMError(f"No se pudo conectar a {self.base_url}: {e}") from e

    # ------------------------------------------------------------------- chat
    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
    ) -> str:
        """Llamada no-stream. Devuelve el content completo."""
        if self.api_format == "anthropic":
            return self.chat_message(messages, temperature, max_tokens, tools).get("content") or ""
        payload = self._build_payload(messages, temperature, max_tokens, tools, stream=False)

        try:
            with self._client() as client:
                resp = client.post(self._chat_url(), json=payload)
                if resp.status_code != 200:
                    raise LLMError(self._error_message(resp.status_code, resp.text))
                data = resp.json()
                msg = data["choices"][0]["message"]
                return msg.get("content") or ""
        except LLMError:
            raise
        except httpx.HTTPError as e:
            raise LLMError(f"No se pudo conectar a {self.base_url}: {e}") from e

    # ------------------------------------------------------------ chat_message
    def chat_message(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
    ) -> dict:
        """Llamada no-stream que devuelve el message COMPLETO (con tool_calls).

        Para el modo agente: necesitamos los tool_calls, no solo el content.
        """
        payload = self._build_payload(messages, temperature, max_tokens, tools, stream=False)

        try:
            with self._client() as client:
                resp = client.post(self._chat_url(), json=payload)
                if resp.status_code != 200:
                    raise LLMError(self._error_message(resp.status_code, resp.text))
                data = resp.json()
                if self.api_format == "anthropic":
                    msg = self._parse_anthropic_message(data)
                    msg["model"] = self.model
                    return msg
                return data["choices"][0].get("message") or {}
        except LLMError:
            raise
        except httpx.HTTPError as e:
            raise LLMError(f"No se pudo conectar a {self.base_url}: {e}") from e

    # ------------------------------------------------------------- list_models
    def list_models(self) -> list[str]:
        return [m["id"] for m in self.list_models_detailed()]

    def list_models_detailed(self) -> list[dict]:
        """Modelos del endpoint como dicts completos ({id, owned_by, created, ...})."""
        try:
            with self._client() as client:
                resp = client.get(self._models_url())
                if resp.status_code != 200:
                    raise LLMError(self._error_message(resp.status_code, resp.text))
                data = resp.json()
                return list(data.get("data", []))
        except LLMError:
            raise
        except httpx.HTTPError as e:
            raise LLMError(f"No se pudo conectar a {self.base_url}: {e}") from e

    @staticmethod
    def _error_message(status: int, body: str) -> str:
        try:
            err = json.loads(body).get("error", {})
            if isinstance(err, dict) and err.get("message"):
                return f"Error HTTP {status}: {err['message']}"
        except json.JSONDecodeError:
            pass
        # formato Anthropic: {"type":"error","error":{"type":...,"message":...}}
        try:
            err2 = json.loads(body).get("error", {})
            if isinstance(err2, dict) and err2.get("message"):
                return f"Error HTTP {status}: {err2['message']}"
        except json.JSONDecodeError:
            pass
        return f"Error HTTP {status}: {body[:200] or '(sin cuerpo)'}"
