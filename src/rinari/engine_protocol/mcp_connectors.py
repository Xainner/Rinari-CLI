"""`mcp.catalog.*`, `mcp.import.*` and `mcp.project_offers.*` (plan de conectores C4).

- `mcp.catalog.list` → the connectors the Engine ships, each with `added`
  and the saved servers that match it.
- `mcp.catalog.add {id, name?, client_id?, client_secret?, redirect_port?,
  token?, secrets?, connection_string?}` → creates the global server the
  entry describes with what the owner typed, and says the next step
  (`sign_in` with `mcp.auth.start`, or `connect`: it is already connecting).
- `mcp.import.scan {sources?, json?}` → servers other apps on this PC
  declare (or a pasted JSON), to mark; nothing is created.
- `mcp.import.apply {keys, names?, sources?, json?}` → creates only the
  marked ones. The scan runs again here, so secret values never travel to
  the client and back.
- `mcp.project_offers.list {path}` / `mcp.project_offers.decide {path,
  decisions: [{key, accept, name?}]}` → the servers a trusted folder brings,
  asked about once per server.

Views carry secret names, never values (`importers.ImportCandidate.view`).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from rinari.engine_protocol.ecosystem import mcp_row_view
from rinari.engine_protocol.errors import INVALID_PARAMS, EngineProtocolError
from rinari.mcp import catalog
from rinari.mcp.config import McpConfigError
from rinari.mcp.importers import APP_SOURCES, parse_pasted, scan_apps, unique_name
from rinari.mcp.project_offers import (
    ProjectNotTrustedError,
    ProjectOffers,
    create_server,
    existing_by_fingerprint,
)
from rinari.shared.errors import NotFoundError

_ADD_INPUTS = (
    "client_id",
    "client_secret",
    "redirect_port",
    "token",
    "secrets",
    "connection_string",
)


class McpConnectorMethods:
    def __init__(
        self,
        services: Any,
        *,
        home: Callable[[], Path] | None = None,
        env: Callable[[], Mapping[str, str]] | None = None,
        platform: str | None = None,
    ) -> None:
        self._services = services
        self._mcp = services.mcp
        # Read at call time: the owner's home and environment, never cached.
        self._home = home or Path.home
        self._env = env or (lambda: os.environ)
        self._platform = platform or sys.platform
        self._offers = ProjectOffers(services.ctx, services.mcp, services.trust)

    def register(self, dispatcher) -> None:
        dispatcher.register("mcp.catalog.list", self.catalog_list)
        dispatcher.register("mcp.catalog.add", self.catalog_add)
        dispatcher.register("mcp.import.scan", self.import_scan)
        dispatcher.register("mcp.import.apply", self.import_apply)
        dispatcher.register("mcp.project_offers.list", self.offers_list)
        dispatcher.register("mcp.project_offers.decide", self.offers_decide)

    def _view(self, row: dict) -> dict[str, Any]:
        return mcp_row_view(row, self._mcp.is_connected(row["name"]), self._mcp.view(row))

    # -- catalog ---------------------------------------------------------------------

    def catalog_list(self, params: dict[str, Any]) -> dict[str, Any]:
        document = catalog.catalog()
        return {
            "version": document["version"],
            "verified_at": document.get("verified_at"),
            "entries": catalog.list_view(self._mcp.list(), self._mcp.status),
        }

    def catalog_add(self, params: dict[str, Any]) -> dict[str, Any]:
        params = params or {}
        entry_id = params.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            raise EngineProtocolError(INVALID_PARAMS, "Param 'id' is required.")
        item = catalog.entry(entry_id)
        if item is None:
            raise NotFoundError(f"Unknown catalog entry: {entry_id}.")
        name = params.get("name")
        if name is None:
            name = unique_name(item["id"], [row["name"] for row in self._mcp.list()])
        elif not isinstance(name, str) or not name.strip():
            raise EngineProtocolError(INVALID_PARAMS, "Param 'name' must be a non-empty string.")
        if self._mcp.show(name) is not None or self._mcp.show(name, "project") is not None:
            raise EngineProtocolError(
                INVALID_PARAMS,
                f"An MCP server named {name!r} already exists.",
                details={"hint": "name_taken"},
            )
        inputs = {key: params[key] for key in _ADD_INPUTS if key in params}
        try:
            fields = catalog.create_params(item, inputs)
            row = self._mcp.add(
                name,
                fields.get("command"),
                scope="global",
                transport=fields.get("transport"),
                url=fields.get("url"),
                env=fields.get("env"),
                auth=fields.get("auth"),
            )
        except ValueError as exc:  # McpConfigError: never carries a value
            raise EngineProtocolError(INVALID_PARAMS, str(exc)) from exc
        return {
            "server": self._view(row),
            "entry_id": item["id"],
            "next": catalog.next_step(item, fields),
        }

    # -- import from other apps or a pasted JSON ------------------------------------------

    def _sources(self, params: Mapping[str, Any]) -> list[str] | None:
        sources = params.get("sources")
        if sources is None:
            return None
        if not isinstance(sources, list) or not all(s in APP_SOURCES for s in sources):
            raise EngineProtocolError(
                INVALID_PARAMS, f"Param 'sources' must list some of: {', '.join(APP_SOURCES)}."
            )
        return list(sources)

    def _scan(self, params: Mapping[str, Any]):
        if params.get("json") is not None:
            try:
                return parse_pasted(params["json"]), []
            except McpConfigError as exc:
                raise EngineProtocolError(INVALID_PARAMS, str(exc)) from exc
        return scan_apps(
            self._home(), self._env(), platform=self._platform, sources=self._sources(params)
        )

    def import_scan(self, params: dict[str, Any]) -> dict[str, Any]:
        params = params or {}
        candidates, reports = self._scan(params)
        rows = self._mcp.list()
        existing = existing_by_fingerprint(rows)
        taken = [row["name"] for row in rows]
        views = []
        for candidate in candidates:
            view = candidate.view()
            view["existing"] = existing.get(candidate.fingerprint) if candidate.importable else None
            view["suggested_name"] = unique_name(candidate.name, taken)
            views.append(view)
        return {"sources": [report.view() for report in reports], "candidates": views}

    def import_apply(self, params: dict[str, Any]) -> dict[str, Any]:
        """Create only the marked servers (`keys`); each one on its own."""
        params = params or {}
        keys = params.get("keys")
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
            raise EngineProtocolError(INVALID_PARAMS, "Param 'keys' must list the marked servers.")
        names = params.get("names") or {}
        if not isinstance(names, dict):
            raise EngineProtocolError(INVALID_PARAMS, "Param 'names' must map keys to names.")
        candidates = {c.key: c for c in self._scan(params)[0]}
        taken = [row["name"] for row in self._mcp.list()]
        created: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for key in dict.fromkeys(keys):
            candidate = candidates.get(key)
            if candidate is None:
                failed.append({"key": key, "code": "NOT_FOUND", "message": "not in the source"})
                continue
            name = names.get(key) or unique_name(candidate.name, taken)
            # Claude Code's per-project servers stay tied to their folder.
            scope = "project" if candidate.project_root else "global"
            try:
                row = create_server(
                    self._mcp, candidate, name, scope=scope, project_root=candidate.project_root
                )
            except ValueError as exc:
                failed.append({"key": key, "code": "INVALID_PARAMS", "message": str(exc)})
                continue
            taken.append(row["name"])
            created.append({"key": key, "server": self._view(row)})
        return {"created": created, "failed": failed}

    # -- servers a trusted project folder brings -------------------------------------------

    @staticmethod
    def _path(params: Mapping[str, Any]) -> str:
        path = params.get("path")
        if not isinstance(path, str) or not path.strip():
            raise EngineProtocolError(INVALID_PARAMS, "Param 'path' is required.")
        return path

    def offers_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._offers.list(self._path(params or {}))

    def offers_decide(self, params: dict[str, Any]) -> dict[str, Any]:
        params = params or {}
        path = self._path(params)
        decisions = params.get("decisions")
        if (
            not isinstance(decisions, list)
            or not decisions
            or not all(
                isinstance(d, dict)
                and isinstance(d.get("key"), str)
                and isinstance(d.get("accept"), bool)
                for d in decisions
            )
        ):
            raise EngineProtocolError(
                INVALID_PARAMS, "Param 'decisions' must be a list of {key, accept, name?}."
            )
        try:
            result = self._offers.decide(path, decisions)
        except ProjectNotTrustedError:
            raise EngineProtocolError(
                INVALID_PARAMS,
                "The folder is not trusted: it offers no MCP servers.",
                details={"hint": "project_not_trusted"},
            ) from None
        except LookupError as exc:
            raise NotFoundError(f"The folder does not declare the MCP server {exc}.") from None
        result["created"] = [
            {"key": item["key"], "server": self._view(item["row"])} for item in result["created"]
        ]
        return result


__all__ = ["McpConnectorMethods"]
