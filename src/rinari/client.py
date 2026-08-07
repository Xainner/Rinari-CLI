"""Cliente OpenAI-compatible con streaming SSE para rinari.

Habla con cualquier endpoint OpenAI-compatible: vLLM, LiteLLM, llama.cpp.
Soporta:
- chat_stream(): streaming de deltas (generator de str o dicts con tool_calls)
- chat(): llamada no-stream, devuelve content completo
- list_models(): lista modelos del endpoint
"""

from __future__ import annotations

import json
from typing import Any, Generator

import httpx


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
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _client(self) -> httpx.Client:
        kwargs: dict[str, Any] = {"timeout": self.timeout, "headers": self._headers()}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.Client(**kwargs)

    def chat_stream(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
    ) -> Generator[str | dict, None, None]:
        """Streaming de chat. Emite str (deltas de contenido) o dict (tool_calls)."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools

        try:
            with self._client() as client:
                with client.stream("POST", f"{self.base_url}/chat/completions", json=payload) as resp:
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

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
    ) -> str:
        """Llamada no-stream. Devuelve el content completo del primer choice."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools

        try:
            with self._client() as client:
                resp = client.post(f"{self.base_url}/chat/completions", json=payload)
                if resp.status_code != 200:
                    raise LLMError(self._error_message(resp.status_code, resp.text))
                data = resp.json()
                msg = data["choices"][0]["message"]
                return msg.get("content") or ""
        except LLMError:
            raise
        except httpx.HTTPError as e:
            raise LLMError(f"No se pudo conectar a {self.base_url}: {e}") from e

    def list_models(self) -> list[str]:
        try:
            with self._client() as client:
                resp = client.get(f"{self.base_url}/models")
                if resp.status_code != 200:
                    raise LLMError(self._error_message(resp.status_code, resp.text))
                data = resp.json()
                return [m["id"] for m in data.get("data", [])]
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
        return f"Error HTTP {status}: {body[:200] or '(sin cuerpo)'}"
