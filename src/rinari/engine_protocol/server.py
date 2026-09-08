"""Engine server: protocol methods bound to application services.

Slice 1: handshake info, session list/get/create/open, RuntimeSnapshot.
Slice 2: live turns (start/cancel with streaming events), approval
roundtrips, provider/model reads. Envelope contract is unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rinari.application.services import ServiceContainer
from rinari.cli.serializers import model_dict
from rinari.engine_protocol import protocol
from rinari.engine_protocol.dispatcher import EngineDispatcher
from rinari.engine_protocol.errors import INVALID_PARAMS, EngineProtocolError
from rinari.engine_protocol.messages import hello
from rinari.engine_protocol.snapshots import build_snapshot, provider_to_dict, session_to_dict
from rinari.engine_protocol.turns import TurnManager


class EngineServer:
    def __init__(self, services: ServiceContainer, user_home: Path | str | None = None) -> None:
        self._services = services
        home = Path(user_home) if user_home is not None else None
        self._turns = TurnManager(services, user_home=home)
        self._dispatcher = EngineDispatcher()
        self._dispatcher.register("engine.info", self._engine_info)
        self._dispatcher.register("session.list", self._session_list)
        self._dispatcher.register("session.get", self._session_get)
        self._dispatcher.register("session.create", self._session_create)
        self._dispatcher.register("session.open", self._session_open)
        self._dispatcher.register("session.turn.start", self._turn_start)
        self._dispatcher.register("session.turn.cancel", self._turn_cancel)
        self._dispatcher.register("approval.resolve", self._approval_resolve)
        self._dispatcher.register("provider.list", self._provider_list)
        self._dispatcher.register("model.list", self._model_list)
        self._dispatcher.register("runtime.snapshot.get", self._snapshot_get)

    @property
    def turns(self) -> TurnManager:
        return self._turns

    def hello(self) -> dict[str, Any]:
        return hello()

    def handle_line(self, line: str) -> dict[str, Any] | None:
        return self._dispatcher.dispatch(line)

    def drain_events(self) -> list[dict[str, Any]]:
        return self._turns.drain_events()

    def has_active_turns(self) -> bool:
        return self._turns.has_active_turns()

    def cancel_all_turns(self) -> None:
        self._turns.cancel_all_turns()

    def close(self) -> None:
        self._turns.close()

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

    # -- turns ------------------------------------------------------------

    def _turn_start(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = params.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise EngineProtocolError(
                INVALID_PARAMS, "Param 'session_id' must be a non-empty string."
            )
        message = params.get("message")
        if not isinstance(message, str) or not message.strip():
            raise EngineProtocolError(INVALID_PARAMS, "Param 'message' must be a non-empty string.")
        return self._turns.start_turn(session_id, message)

    def _turn_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = params.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise EngineProtocolError(
                INVALID_PARAMS, "Param 'session_id' must be a non-empty string."
            )
        return self._turns.cancel_turn(session_id)

    def _approval_resolve(self, params: dict[str, Any]) -> dict[str, Any]:
        approval_id = params.get("approval_id")
        if not isinstance(approval_id, str) or not approval_id:
            raise EngineProtocolError(
                INVALID_PARAMS, "Param 'approval_id' must be a non-empty string."
            )
        decision = params.get("decision")
        if not isinstance(decision, str) or not decision:
            raise EngineProtocolError(
                INVALID_PARAMS, "Param 'decision' must be a non-empty string."
            )
        return self._turns.resolve_approval(approval_id, decision)

    # -- providers / models -------------------------------------------------

    def _provider_list(self, params: dict[str, Any]) -> dict[str, Any]:
        _ = params
        current = self._services.providers.current()
        active_alias = current.provider.alias if current is not None else None
        items = []
        for record in self._services.providers.list():
            credential = self._services.ctx.provider_repo.get_credential(record.id)
            items.append(provider_to_dict(record, credential is not None))
        return {"providers": items, "active_alias": active_alias}

    def _model_list(self, params: dict[str, Any]) -> dict[str, Any]:
        provider = params.get("provider")
        if provider is not None and not isinstance(provider, str):
            raise EngineProtocolError(INVALID_PARAMS, "Param 'provider' must be a string.")
        current = self._services.providers.current()
        active_model_id = (
            current.model.id if current is not None and current.model is not None else None
        )
        items = [
            model_dict(
                model,
                provider_alias=self._provider_alias(model.provider_id),
                active=(model.id == active_model_id),
            )
            for model in self._services.models.list(provider)
        ]
        return {"models": items}

    def _provider_alias(self, provider_id: str) -> str | None:
        try:
            return self._services.providers.get(provider_id).alias
        except Exception:
            return None

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
