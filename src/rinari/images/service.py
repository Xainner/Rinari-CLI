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
            if item["kind"] == "openai":
                ref = item.get("api_key_ref")
                item["has_key"] = bool(ref) and (credentials is None or _exists(credentials, ref))
        value["kinds"] = list(image_settings.KINDS)
        value["subscriptions"] = self.subscriptions()
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
            if raw.get("kind") == "openai":
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

    def subscriptions(self) -> list[dict[str, Any]]:
        """ChatGPT subscriptions connected as providers: what a `chatgpt` connector can use."""
        from rinari.providers.catalog import product_for

        found = []
        try:
            records = self._services.providers.list()
        except Exception:
            return []
        for record in records:
            if getattr(record, "auth_method", None) == "oauth" and product_for(record) == "chatgpt":
                found.append({"provider_id": record.id, "name": getattr(record, "name", record.id)})
        return found

    # -- connectors ------------------------------------------------------------

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
        raise ImageConnectorError("UNSUPPORTED", f"Unknown connector kind {entry['kind']!r}")

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
