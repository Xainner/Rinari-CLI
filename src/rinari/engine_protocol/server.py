"""Engine server: protocol methods bound to application services.

Slice 1 (Phase 1 foundation): handshake info, session list/get/create/open,
and RuntimeSnapshot. Turn execution, approvals, and live events arrive in
the next slice without changing this envelope contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rinari.application.services import ServiceContainer
from rinari.engine_protocol import protocol
from rinari.engine_protocol.dispatcher import EngineDispatcher
from rinari.engine_protocol.errors import INVALID_PARAMS, EngineProtocolError
from rinari.engine_protocol.messages import hello
from rinari.engine_protocol.snapshots import build_snapshot, session_to_dict


class EngineServer:
    def __init__(self, services: ServiceContainer) -> None:
        self._services = services
        self._dispatcher = EngineDispatcher()
        self._dispatcher.register("engine.info", self._engine_info)
        self._dispatcher.register("session.list", self._session_list)
        self._dispatcher.register("session.get", self._session_get)
        self._dispatcher.register("session.create", self._session_create)
        self._dispatcher.register("session.open", self._session_open)
        self._dispatcher.register("runtime.snapshot.get", self._snapshot_get)

    def hello(self) -> dict[str, Any]:
        return hello()

    def handle_line(self, line: str) -> dict[str, Any] | None:
        return self._dispatcher.dispatch(line)

    # -- engine ----------------------------------------------------------

    def _engine_info(self, params: dict[str, Any]) -> dict[str, Any]:
        _ = params
        return {
            "protocol": protocol.PROTOCOL_NAME,
            "protocol_version": protocol.PROTOCOL_VERSION,
            "engine_version": protocol.engine_version(),
            "capabilities": dict(protocol.CAPABILITIES),
        }

    # -- sessions --------------------------------------------------------

    def _session_list(self, params: dict[str, Any]) -> dict[str, Any]:
        kind = params.get("kind")
        if kind is not None and kind not in ("CHAT", "PROJECT"):
            raise EngineProtocolError(INVALID_PARAMS, "Param 'kind' must be CHAT or PROJECT.")
        limit = params.get("limit", 50)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise EngineProtocolError(INVALID_PARAMS, "Param 'limit' must be an int in 1..500.")
        records = self._services.sessions.list(kind=kind, limit=limit)
        return {"sessions": [session_to_dict(record) for record in records]}

    def _session_get(self, params: dict[str, Any]) -> dict[str, Any]:
        ref = params.get("ref")
        if not isinstance(ref, str) or not ref:
            raise EngineProtocolError(INVALID_PARAMS, "Param 'ref' must be a non-empty string.")
        return {"session": session_to_dict(self._services.sessions.show(ref))}

    def _session_create(self, params: dict[str, Any]) -> dict[str, Any]:
        chat = params.get("chat", False)
        if not isinstance(chat, bool):
            raise EngineProtocolError(INVALID_PARAMS, "Param 'chat' must be a boolean.")
        title = params.get("title")
        if title is not None and not isinstance(title, str):
            raise EngineProtocolError(INVALID_PARAMS, "Param 'title' must be a string.")
        cwd = self._resolve_cwd(params.get("cwd"))
        record = self._services.sessions.new(cwd=cwd, title=title, forced_chat=chat)
        return {"session": session_to_dict(record), "created": True}

    def _session_open(self, params: dict[str, Any]) -> dict[str, Any]:
        ref = params.get("ref")
        if not isinstance(ref, str) or not ref:
            raise EngineProtocolError(INVALID_PARAMS, "Param 'ref' must be a non-empty string.")
        started = self._services.sessions.resume(ref=ref)
        return {
            "session": session_to_dict(started.session),
            "created": started.created,
            "warnings": list(started.warnings),
        }

    # -- snapshot --------------------------------------------------------

    def _snapshot_get(self, params: dict[str, Any]) -> dict[str, Any]:
        _ = params
        return {"snapshot": build_snapshot(self._services)}

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _resolve_cwd(raw: Any) -> Path:
        if raw is None:
            return Path.cwd()
        if not isinstance(raw, str) or not raw:
            raise EngineProtocolError(INVALID_PARAMS, "Param 'cwd' must be a non-empty string.")
        path = Path(raw).expanduser()
        if not path.is_dir():
            raise EngineProtocolError(INVALID_PARAMS, f"Param 'cwd' is not a directory: {raw}.")
        return path.resolve()
