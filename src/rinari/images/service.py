"""Image creation for the tools and the protocol (plan 0.4.0 #28).

Resolves the configured connectors into working ones (secrets from the
credential store, the ChatGPT subscription token from its provider), keeps
the per-session spend ledger (`media.generated` events) and stores API keys
as credential references, never in the settings file.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from rinari.images import settings as image_settings
from rinari.images.connectors import (
    ChatGPTImagesConnector,
    CostEstimate,
    ImageConnector,
    ImageConnectorError,
    ImageJob,
    OpenAIImagesConnector,
)

#: Event that records each batch: connector, model, images and cost.
MEDIA_EVENT = "media.generated"


def secret_slot(connector_id: str) -> str:
    return f"media/{connector_id}/api-key"


class ImageService:
    def __init__(self, services: Any) -> None:
        self._services = services

    @property
    def _home(self) -> Path:
        return Path(self._services.ctx.home)

    # -- settings --------------------------------------------------------------

    def settings(self) -> dict[str, Any]:
        value = image_settings.load(self._home)
        credentials = getattr(self._services, "credentials", None)
        for item in value["connectors"]:
            if item["kind"] in image_settings.KEYED and not item.get("provider_id"):
                ref = item.get("api_key_ref")
                item["has_key"] = bool(ref) and (credentials is None or _exists(credentials, ref))
        value["kinds"] = list(image_settings.KINDS)
        value["subscriptions"] = self.subscriptions()
        value["openrouter_providers"] = self.subscriptions("openrouter")
        value["mcp_servers"] = self.mcp_servers()
        return value

    def configure(self, params: dict[str, Any]) -> dict[str, Any]:
        """Replace the settings. A connector may carry `api_key` (a literal):
        it is stored in the credential store and only its reference is kept."""
        current = image_settings.load(self._home)
        previous = {item["id"]: item for item in current["connectors"]}
        connectors = []
        for raw in params.get("connectors", current["connectors"]):
            if not isinstance(raw, dict):
                raise ValueError("Each connector must be an object")
            item = {key: value for key, value in raw.items() if key not in ("api_key", "has_key")}
            literal = raw.get("api_key")
            if raw.get("kind") in image_settings.KEYED:
                if isinstance(literal, str) and literal.strip():
                    item["api_key_ref"] = self._services.credentials.store_secret(
                        secret_slot(str(raw.get("id"))), literal.strip()
                    )
                elif "api_key_ref" not in item and raw.get("id") in previous:
                    item["api_key_ref"] = previous[raw["id"]].get("api_key_ref")
            connectors.append(item)
        value = {
            "connectors": connectors,
            "default_connector": params.get("default_connector", current["default_connector"]),
            "edit_connector": params.get("edit_connector", current["edit_connector"]),
            "session_budget_usd": params.get("session_budget_usd", current["session_budget_usd"]),
        }
        saved = image_settings.save(self._home, value)
        # A removed OpenAI connector takes its key along.
        kept = {item["id"] for item in saved["connectors"]}
        for cid, item in previous.items():
            ref = item.get("api_key_ref")
            if (
                cid not in kept
                and isinstance(ref, str)
                and ref.startswith(("keyring://", "file://"))
            ):
                with contextlib.suppress(Exception):
                    self._services.credentials.delete(ref)
        return self.settings()

    def subscriptions(self, product: str = "chatgpt") -> list[dict[str, Any]]:
        """Providers already connected that a connector can reuse: the ChatGPT
        subscription (oauth) for `chatgpt`, an OpenRouter key for `openrouter`."""
        from rinari.providers.catalog import product_for

        found = []
        try:
            records = self._services.providers.list()
        except Exception:
            return []
        for record in records:
            if product_for(record) != product:
                continue
            if product == "chatgpt" and getattr(record, "auth_method", None) != "oauth":
                continue
            name = getattr(record, "name", None) or getattr(record, "alias", None) or record.id
            found.append({"provider_id": record.id, "name": name})
        return found

    def mcp_servers(self) -> list[str]:
        """MCP servers the owner registered: any of them can be assigned."""
        try:
            rows = self._services.mcp.list()
        except Exception:
            return []
        return sorted(
            {str(row.get("name")) for row in rows if isinstance(row, dict) and row.get("name")}
        )

    # -- connectors ------------------------------------------------------------

    def default_label(self) -> str | None:
        """The name of the service the owner chose by default, or None."""
        value = image_settings.load(self._home)
        choice = value["default_connector"]
        entry = next((item for item in value["connectors"] if item["id"] == choice), None)
        return (entry.get("label") or entry["id"]) if entry else None

    def connector(self, operation: str) -> ImageConnector | None:
        """The connector for this operation: the edit one for edits (when set), else the default."""
        value = image_settings.load(self._home)
        editing = operation != "generate"
        choice = (value["edit_connector"] if editing else None) or value["default_connector"]
        if not choice:
            return None
        entry = next((item for item in value["connectors"] if item["id"] == choice), None)
        return self.build(entry) if entry else None

    def build(self, entry: dict[str, Any]) -> ImageConnector:
        from rinari.net import client as net_client

        home = self._home

        def factory(timeout: float = 300.0):
            return net_client(home, timeout=timeout, follow_redirects=False)

        if entry["kind"] == "openai":
            ref = entry.get("api_key_ref")
            if not ref:
                raise ImageConnectorError("AUTH_REQUIRED", f"{entry['label']} has no API key")
            return OpenAIImagesConnector(
                id=entry["id"],
                kind="openai",
                label=entry["label"],
                model=entry.get("model") or "",
                client_factory=factory,
                api_key=self._services.credentials.resolve(ref),
                endpoint=entry.get("endpoint") or "https://api.openai.com/v1",
            )
        if entry["kind"] == "chatgpt":
            from rinari.providers.auth import token_for

            providers = self._services.providers
            try:
                record = providers.get(entry["provider_id"])
            except Exception as exc:
                raise ImageConnectorError(
                    "AUTH_REQUIRED", f"{entry['label']}: the subscription is no longer connected"
                ) from exc
            return ChatGPTImagesConnector(
                id=entry["id"],
                kind="chatgpt",
                label=entry["label"],
                model=entry.get("model") or "",
                client_factory=factory,
                endpoint=(record.endpoint or "https://chatgpt.com/backend-api/codex").rstrip("/"),
                token=lambda: token_for(providers, record),
                refresh=lambda rejected: token_for(providers, record, rejected_token=rejected),
            )
        if entry["kind"] in ("fal", "google"):
            from rinari.images.more_connectors import FalImagesConnector, GoogleImagesConnector

            key = self._key(entry)
            if entry["kind"] == "fal":
                return FalImagesConnector(
                    id=entry["id"],
                    kind="fal",
                    label=entry["label"],
                    model=entry.get("model") or "",
                    client_factory=factory,
                    api_key=key,
                    endpoint=entry.get("endpoint") or "https://fal.run",
                    models=dict(entry.get("models") or {}),
                )
            return GoogleImagesConnector(
                id=entry["id"],
                kind="google",
                label=entry["label"],
                model=entry.get("model") or "",
                client_factory=factory,
                api_key=key,
                endpoint=entry.get("endpoint")
                or "https://generativelanguage.googleapis.com/v1beta",
            )
        if entry["kind"] == "openrouter":
            from rinari.images.more_connectors import OpenRouterImagesConnector

            endpoint = entry.get("endpoint") or "https://openrouter.ai/api/v1"
            if entry.get("provider_id"):
                # The OpenRouter the owner already uses as a provider: its key.
                try:
                    record = self._services.providers.get(entry["provider_id"])
                except Exception as exc:
                    raise ImageConnectorError(
                        "AUTH_REQUIRED", f"{entry['label']}: the provider is no longer connected"
                    ) from exc
                key = self._services.providers.resolve_secret(record) or ""
                endpoint = entry.get("endpoint") or (record.endpoint or endpoint).rstrip("/")
            else:
                key = self._key(entry)
            return OpenRouterImagesConnector(
                id=entry["id"],
                kind="openrouter",
                label=entry["label"],
                model=entry.get("model") or "",
                client_factory=factory,
                api_key=key,
                endpoint=endpoint,
            )
        if entry["kind"] == "comfyui":
            from rinari.images.more_connectors import ComfyUIConnector

            return ComfyUIConnector(
                id=entry["id"],
                kind="comfyui",
                label=entry["label"],
                client_factory=factory,
                endpoint=entry.get("endpoint") or "http://127.0.0.1:8188",
                workflows=dict(entry.get("workflows") or {}),
            )
        if entry["kind"] == "mcp":
            from rinari.images.more_connectors import McpImagesConnector

            mcp = self._services.mcp
            server = entry["server"]

            def call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                return mcp.client(server).call_tool(tool, arguments)

            return McpImagesConnector(
                id=entry["id"],
                kind="mcp",
                label=entry["label"],
                client_factory=factory,
                call=call,
                tools=dict(entry.get("tools") or {}),
            )
        raise ImageConnectorError("UNSUPPORTED", f"Unknown connector kind {entry['kind']!r}")

    def _key(self, entry: dict[str, Any]) -> str:
        ref = entry.get("api_key_ref")
        if not ref:
            raise ImageConnectorError("AUTH_REQUIRED", f"{entry['label']} has no API key")
        return self._services.credentials.resolve(ref)

    def test(self, connector_id: str) -> dict[str, Any]:
        value = image_settings.load(self._home)
        entry = next((item for item in value["connectors"] if item["id"] == connector_id), None)
        if entry is None:
            raise ValueError(f"No connector {connector_id!r}")
        try:
            return {"connector_id": connector_id, **self.build(entry).check()}
        except ImageConnectorError as exc:
            return {
                "connector_id": connector_id,
                "ok": False,
                "code": exc.code,
                "message": str(exc),
            }

    # -- spend -----------------------------------------------------------------

    def budget(self) -> float | None:
        return image_settings.load(self._home)["session_budget_usd"]

    def spent(self, session_id: str) -> float:
        repo = self._services.ctx.event_repo
        total = 0.0
        for record in repo.recent(session_id, [MEDIA_EVENT], limit=10_000):
            cost = (record.payload or {}).get("cost_usd")
            if isinstance(cost, (int, float)):
                total += float(cost)
        return total

    def over_budget(self, session_id: str, estimate: CostEstimate) -> str | None:
        """Why this job would pass the session's cap, or None."""
        cap = self.budget()
        if cap is None or not estimate.usd:
            return None
        spent = self.spent(session_id)
        if spent + estimate.usd > cap + 1e-9:
            return (
                f"The image spend cap of this conversation (${cap:.2f}) would be passed: "
                f"${spent:.2f} spent, this costs {estimate.label()}"
            )
        return None


def _exists(credentials: Any, ref: str) -> bool:
    try:
        return bool(credentials.exists(ref))
    except Exception:
        return False


__all__ = ["MEDIA_EVENT", "ImageJob", "ImageService", "secret_slot"]
