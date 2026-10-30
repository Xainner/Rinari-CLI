"""`mcp.auth.*`: OAuth sign-in for remote MCP servers (plan de conectores, C1).

`mcp.auth.start` discovers the authorization server, picks the client
(the owner's own, dynamic registration or a client metadata document), opens
the loopback listener and returns the authorization URL at once (opening the
default browser unless `open_browser` is false). The stdio loop is never held
while the owner signs in: the callback and the code exchange run on the
listener's threads and end with an event. Events: `mcp.auth.progress {name,
flow_id, stage}` (discovering | registering | waiting_for_browser |
exchanging_code) and `mcp.auth.finished {name, flow_id, outcome, error?}`
(signed_in | failed | cancelled | timed_out).

Nothing here carries a token, a code, a verifier or a client secret.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

from rinari.engine_protocol.errors import INVALID_PARAMS, EngineProtocolError
from rinari.engine_protocol.messages import event
from rinari.mcp.config import (
    AUTH_OAUTH,
    TRANSPORT_HTTP,
    OAuthConfig,
    draft_from_row,
)
from rinari.mcp.oauth import DEFAULT_FLOW_TIMEOUT_S, OAuthError, OAuthManager
from rinari.shared.errors import NotFoundError, RinariError


class McpAuthMethods:
    def __init__(self, services, emit: Callable[[dict], None], *, manager=None) -> None:
        self._mcp = services.mcp
        self._credentials = services.credentials
        self._emit = emit
        self._manager: OAuthManager = manager or OAuthManager(
            services.credentials, home=services.ctx.home, on_event=self._on_event
        )

    @property
    def manager(self) -> OAuthManager:
        return self._manager

    def register(self, dispatcher) -> None:
        dispatcher.register("mcp.auth.start", self.start)
        dispatcher.register("mcp.auth.status", self.status)
        dispatcher.register("mcp.auth.cancel", self.cancel)
        dispatcher.register("mcp.auth.signout", self.signout)

    def close(self) -> None:
        self._manager.close()

    def _on_event(self, name: str, payload: dict[str, Any]) -> None:
        self._emit(event(name, payload))
        if name == "mcp.auth.finished" and payload.get("outcome") == "signed_in":
            # A server waiting for this sign-in (oauth_required, reauth_required)
            # is never retried on a timer (C2): connect it now with the new token.
            with contextlib.suppress(LookupError):
                self._mcp.reconnect(payload["name"])

    # -- helpers ---------------------------------------------------------------------

    def _row(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            raise EngineProtocolError(INVALID_PARAMS, "Param 'name' is required.")
        row = self._mcp.show(name) or self._mcp.show(name, "project")
        if row is None:
            raise NotFoundError(f"Unknown MCP server: {name}.")
        return row

    @staticmethod
    def _oauth(row: dict[str, Any], *, required: bool) -> OAuthConfig:
        draft = draft_from_row(row)
        if required and (draft.transport != TRANSPORT_HTTP or draft.auth_kind != AUTH_OAUTH):
            raise EngineProtocolError(
                INVALID_PARAMS,
                f"MCP server {row.get('name')} does not use OAuth; set auth.kind 'oauth' "
                "with mcp.update first.",
                details={"hint": "oauth_not_configured"},
            )
        return draft.oauth or OAuthConfig()

    def _client_secret(self, oauth: OAuthConfig) -> str | None:
        slot = oauth.client_secret
        if slot is None:
            return None
        if slot.literal is not None:
            return slot.literal
        try:
            return self._credentials.resolve(slot.ref or "")
        except RinariError as exc:
            raise EngineProtocolError(
                INVALID_PARAMS,
                f"The OAuth client secret of this server is not available: {exc.message}",
                details={"hint": "secret_missing"},
            ) from exc

    # -- methods ---------------------------------------------------------------------

    def start(self, params: dict[str, Any]) -> dict[str, Any]:
        params = params or {}
        row = self._row(params)
        oauth = self._oauth(row, required=True)
        open_browser = params.get("open_browser", True)
        if not isinstance(open_browser, bool):
            raise EngineProtocolError(INVALID_PARAMS, "Param 'open_browser' must be a boolean.")
        timeout = params.get("timeout_s", DEFAULT_FLOW_TIMEOUT_S)
        if isinstance(timeout, bool) or not isinstance(timeout, int | float):
            raise EngineProtocolError(INVALID_PARAMS, "Param 'timeout_s' must be a number.")
        try:
            flow = self._manager.start(
                row["id"],
                row["name"],
                row.get("url") or "",
                oauth,
                client_secret=self._client_secret(oauth),
                open_browser=open_browser,
                timeout_s=float(timeout),
            )
        except OAuthError as exc:
            # Discovery, client or port problems: the owner fixes the config.
            raise EngineProtocolError(
                "MCP_AUTH_FAILED", exc.message, details=exc.as_dict()
            ) from exc
        return {"name": row["name"], **flow}

    def status(self, params: dict[str, Any]) -> dict[str, Any]:
        row = self._row(params or {})
        draft = draft_from_row(row)
        return {
            "name": row["name"],
            "kind": draft.auth_kind,
            **self._manager.status(row["id"]),
        }

    def cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        row = self._row(params or {})
        return {"name": row["name"], "cancelled": self._manager.cancel(row["id"])}

    def signout(self, params: dict[str, Any]) -> dict[str, Any]:
        row = self._row(params or {})
        oauth = self._oauth(row, required=False)
        secret: str | None = None
        with contextlib.suppress(EngineProtocolError):
            secret = self._client_secret(oauth)
        result = self._manager.signout(row["id"], oauth, client_secret=secret)
        # The cached connection keeps the old token in memory: drop it.
        self._mcp.disconnect(row["name"])
        return {"name": row["name"], **result}


__all__ = ["McpAuthMethods"]
