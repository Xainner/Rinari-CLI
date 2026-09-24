"""Experimental subscription transports; inference stays inside Rinari."""

from rinari.providers.adapters.base import DiscoveredModel, ProviderHealth
from rinari.providers.adapters.http import auth_failure, provider_error, send_request
from rinari.providers.adapters.openai_compatible import OpenAICompatibleAdapter
from rinari.providers.adapters.responses import OpenAIResponsesAdapter
from rinari.providers.auth import account_id
from rinari.shared.errors import ProviderModelError

#: The subscription catalog only lists models whose `minimal_client_version`
#: the caller meets (0.144 to 0.155 on 2026-09-23), so an older value returns an
#: empty list. Rinari sends its own requests, so this names the catalog it
#: can read, not a client it imitates.
CODEX_CLIENT_VERSION = "1.0.0"
#: `visibility` values of models the catalog does not offer for selection.
HIDDEN = {"hide", "hidden"}


def chatgpt_headers(secret):
    headers = {
        "Authorization": f"Bearer {secret}",
        "User-Agent": "Rinari/0.1.0",
        "OpenAI-Beta": "responses=experimental",
    }
    account = account_id(secret or "")
    if account:
        headers["ChatGPT-Account-Id"] = account
    return headers


class CodexResponsesAdapter(OpenAIResponsesAdapter):
    def _headers(self, secret):
        return chatgpt_headers(secret)

    def _responses_payload(self, request, *, stream, tool_aliases=None):
        payload = super()._responses_payload(request, stream=True, tool_aliases=tool_aliases)
        payload["store"] = False
        payload["instructions"] = "\n\n".join(
            m.content or "" for m in request.messages if m.role == "system"
        )
        payload["input"] = [item for item in payload["input"] if item.get("role") != "system"]
        payload["include"] = ["reasoning.encrypted_content"]
        payload.pop("max_output_tokens", None)
        payload.pop("temperature", None)
        return payload

    def invoke(self, request, secret, endpoint=None, *, tool_aliases=None):
        # The subscription endpoint is stream-only, even for auxiliary calls.
        return self.invoke_stream(
            request, secret, endpoint, lambda delta: None, tool_aliases=tool_aliases
        )


class ChatGPTAdapter(OpenAICompatibleAdapter):
    def _headers(self, secret):
        return chatgpt_headers(secret)

    def _responses_adapter(self):
        return CodexResponsesAdapter(client=self.client())

    def health(self, secret, endpoint=None):
        models = self.list_models(secret, endpoint)
        return ProviderHealth(
            connected=True,
            detail="Subscription model discovery succeeded; inference not tested.",
            models_discovered=len(models),
            models=models,
        )

    def list_models(self, secret, endpoint=None):
        url = self.base_url(endpoint) + f"/models?client_version={CODEX_CLIENT_VERSION}"
        response = send_request(self.client(), "GET", url, headers=self._headers(secret))
        if response.status_code in (401, 403):
            raise auth_failure(response, url)
        if response.status_code >= 400:
            raise provider_error(response, url)
        data = response.json()
        entries = data.get("models", []) if isinstance(data, dict) else []
        if not isinstance(entries, list):
            raise ProviderModelError("Invalid subscription model catalog")
        result = []
        for model in entries:
            if (
                not isinstance(model, dict)
                or not model.get("slug")
                or model.get("visibility") in HIDDEN
            ):
                continue
            levels = model.get("supported_reasoning_levels", [])
            levels = [v.get("effort") if isinstance(v, dict) else v for v in levels]
            levels = [v for v in levels if isinstance(v, str)]
            from datetime import UTC, datetime

            caps = {
                "reasoning_effort": bool(levels),
                "reasoning_levels": levels,
                "source": "provider-discovery",
                "updated_at": datetime.now(UTC).isoformat(),
            }
            if type(model.get("context_window")) is int:
                caps["max_context_tokens"] = model["context_window"]
            if isinstance(model.get("input_modalities"), list):
                caps["vision"] = "image" in model["input_modalities"]
            result.append(DiscoveredModel(provider_model_id=model["slug"], capabilities=caps))
        return result


class CopilotAdapter(OpenAICompatibleAdapter):
    def list_models(self, secret, endpoint=None):
        from datetime import UTC, datetime

        from rinari.providers.metadata import claude_reasoning

        url = self.base_url(endpoint) + "/models"
        response = send_request(self.client(), "GET", url, headers=self._headers(secret))
        if response.status_code >= 400:
            raise provider_error(response, url)
        data = response.json()
        result = []
        for entry in data.get("data", []):
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            if (
                entry.get("model_picker_enabled") is False
                or (entry.get("policy") or {}).get("state") == "disabled"
            ):
                continue
            remote = entry.get("capabilities") or {}
            supports, limits = remote.get("supports") or {}, remote.get("limits") or {}
            endpoints = entry.get("supported_endpoints") or []
            transport = (
                "anthropic"
                if "/v1/messages" in endpoints
                else "responses"
                if "/responses" in endpoints
                else "chat"
                if "/chat/completions" in endpoints
                else "unsupported-copilot-route"
            )
            caps = {
                "transport": transport,
                "route_supported": transport != "unsupported-copilot-route",
                "source": "provider-discovery",
                "updated_at": datetime.now(UTC).isoformat(),
            }
            for key in ("streaming", "tool_calls", "vision"):
                if type(supports.get(key)) is bool:
                    caps[key] = supports[key]
            for source, target in (
                ("max_context_window_tokens", "max_context_tokens"),
                ("max_prompt_tokens", "max_input_tokens"),
                ("max_output_tokens", "max_output_tokens"),
            ):
                if type(limits.get(source)) is int and limits[source] > 0:
                    caps[target] = limits[source]
            levels = supports.get("reasoning_effort")
            if isinstance(levels, list):
                caps.update(
                    reasoning_effort=bool(levels),
                    reasoning_levels=[x for x in levels if isinstance(x, str)],
                )
            elif transport == "anthropic":
                mode, levels = claude_reasoning(entry["id"])
                caps.update(
                    reasoning_effort=bool(mode), reasoning_mode=mode, reasoning_levels=levels
                )
            result.append(DiscoveredModel(provider_model_id=entry["id"], capabilities=caps))
        return result

    def _headers(self, secret):
        return {
            "Authorization": f"Bearer {secret}",
            "User-Agent": "Rinari/0.1.0",
            "X-GitHub-Api-Version": "2026-06-01",
            "Openai-Intent": "conversation-edits",
        }

    def _responses_adapter(self):
        owner = self

        class Responses(OpenAIResponsesAdapter):
            def _headers(self, secret):
                return owner._headers(secret)

            def request_headers(self, secret, request):
                return owner.request_headers(secret, request)

        return Responses(client=self.client())

    def request_headers(self, secret, request):
        headers = self._headers(secret)
        headers["x-initiator"] = (
            "user" if request.messages and request.messages[-1].role == "user" else "agent"
        )
        if any(message.images for message in request.messages):
            headers["Copilot-Vision-Request"] = "true"
        if request.session_id:
            headers["X-Interaction-Id"] = request.session_id
        return headers

    def _anthropic_adapter(self):
        from rinari.providers.adapters.anthropic import API_VERSION, AnthropicAdapter

        owner = self

        class Messages(AnthropicAdapter):
            def request_headers(self, secret, request):
                return {**owner.request_headers(secret, request), "anthropic-version": API_VERSION}

        return Messages(client=self.client())
