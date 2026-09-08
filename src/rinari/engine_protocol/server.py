"""Engine server: protocol methods bound to application services.

Slice 1: handshake info, session list/get/create/open, RuntimeSnapshot.
Slice 2: live turns (start/cancel with streaming events), approval
roundtrips, provider/model reads. Envelope contract is unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rinari.application.provider_service import AddProviderInput
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
        self._dispatcher.register("provider.create", self._provider_create)
        self._dispatcher.register("provider.get", self._provider_get)
        self._dispatcher.register("provider.update", self._provider_update)
        self._dispatcher.register("provider.remove", self._provider_remove)
        self._dispatcher.register("provider.test", self._provider_test)
        self._dispatcher.register("provider.discover", self._provider_discover)
        self._dispatcher.register("provider.use", self._provider_use)
        self._dispatcher.register("model.list", self._model_list)
        self._dispatcher.register("model.get", self._model_get)
        self._dispatcher.register("model.add", self._model_add)
        self._dispatcher.register("model.alias", self._model_alias)
        self._dispatcher.register("model.remove", self._model_remove)
        self._dispatcher.register("model.use", self._model_use)
        self._dispatcher.register("model.discover", self._model_discover)
        self._dispatcher.register("model.refresh", self._model_refresh)
        self._dispatcher.register("model.test", self._model_test)
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

    # -- provider CRUD / health / discovery -----------------------------------

    @staticmethod
    def _need_str(params: dict[str, Any], name: str) -> str:
        value = params.get(name)
        if not isinstance(value, str) or not value:
            raise EngineProtocolError(INVALID_PARAMS, f"Param {name!r} must be a non-empty string.")
        return value

    @staticmethod
    def _opt_str(params: dict[str, Any], name: str) -> str | None:
        value = params.get(name)
        if value is None:
            return None
        if not isinstance(value, str):
            raise EngineProtocolError(INVALID_PARAMS, f"Param {name!r} must be a string.")
        return value

    @staticmethod
    def _opt_settings(params: dict[str, Any]) -> dict[str, Any] | None:
        value = params.get("settings")
        if value is None:
            return None
        if not isinstance(value, dict):
            raise EngineProtocolError(INVALID_PARAMS, "Param 'settings' must be an object.")
        return dict(value)

    def _provider_view(self, record: Any) -> dict[str, Any]:
        providers = self._services.providers
        ref = providers.credential_ref(record)
        view = provider_to_dict(record, ref is not None)
        current = providers.current()
        view["active"] = current is not None and current.provider.id == record.id
        return view

    def _model_view(self, model: Any) -> dict[str, Any]:
        current = self._services.providers.current()
        active_id = current.model.id if current is not None and current.model is not None else None
        return model_dict(
            model,
            provider_alias=self._provider_alias(model.provider_id),
            active=(model.id == active_id),
        )

    def _provider_create(self, params: dict[str, Any]) -> dict[str, Any]:
        alias = self._need_str(params, "alias")
        provider_type = self._need_str(params, "type")
        auth_method = params.get("auth_method") or "api-key"
        if not isinstance(auth_method, str) or not auth_method:
            raise EngineProtocolError(
                INVALID_PARAMS, "Param 'auth_method' must be a non-empty string."
            )
        secret = params.get("secret")
        secret_env = params.get("secret_env")
        if secret is not None and not isinstance(secret, str):
            raise EngineProtocolError(INVALID_PARAMS, "Param 'secret' must be a string.")
        if secret_env is not None and not isinstance(secret_env, str):
            raise EngineProtocolError(INVALID_PARAMS, "Param 'secret_env' must be a string.")
        record = self._services.providers.add(
            AddProviderInput(
                alias=alias,
                provider_type=provider_type,
                auth_method=auth_method,
                endpoint=self._opt_str(params, "endpoint"),
                account_hint=self._opt_str(params, "account_hint"),
                secret=secret or None,
                secret_env=secret_env or None,
                settings=self._opt_settings(params) or {},
            )
        )
        return {"provider": self._provider_view(record)}

    def _provider_get(self, params: dict[str, Any]) -> dict[str, Any]:
        record = self._services.providers.get(self._need_str(params, "ref"))
        return {"provider": self._provider_view(record)}

    def _provider_update(self, params: dict[str, Any]) -> dict[str, Any]:
        providers = self._services.providers
        record = providers.get(self._need_str(params, "ref"))
        changed = False
        if "alias" in params:
            record = providers.rename(record.id, self._need_str(params, "alias"))
            changed = True
        if "secret" in params or "secret_env" in params:
            secret = params.get("secret")
            secret_env = params.get("secret_env")
            if secret is not None and not isinstance(secret, str):
                raise EngineProtocolError(INVALID_PARAMS, "Param 'secret' must be a string.")
            if secret_env is not None and not isinstance(secret_env, str):
                raise EngineProtocolError(INVALID_PARAMS, "Param 'secret_env' must be a string.")
            record = providers.set_auth(
                record.id, secret=secret or None, secret_env=secret_env or None
            )
            changed = True
        patch = {
            key: params[key] for key in ("endpoint", "settings", "account_hint") if key in params
        }
        if patch:
            if "settings" in patch and not isinstance(patch["settings"], dict):
                raise EngineProtocolError(INVALID_PARAMS, "Param 'settings' must be an object.")
            record = providers.update(
                record.id,
                endpoint=patch.get("endpoint"),
                settings=dict(patch["settings"]) if patch.get("settings") is not None else None,
                account_hint=patch.get("account_hint"),
            )
            changed = True
        if not changed:
            raise EngineProtocolError(
                INVALID_PARAMS,
                "Nothing to update: pass alias, endpoint, settings, account_hint, "
                "secret or secret_env.",
            )
        return {"provider": self._provider_view(providers.get(record.id))}

    def _provider_remove(self, params: dict[str, Any]) -> dict[str, Any]:
        ref = self._need_str(params, "ref")
        switch_to = self._opt_str(params, "switch_to")
        keep = params.get("keep_credentials", False)
        if not isinstance(keep, bool):
            raise EngineProtocolError(INVALID_PARAMS, "Param 'keep_credentials' must be a boolean.")
        record = self._services.providers.remove(ref, switch_to=switch_to, keep_credentials=keep)
        return {"removed": {"id": record.id, "alias": record.alias}}

    def _provider_test(self, params: dict[str, Any]) -> dict[str, Any]:
        health = self._services.providers.test(self._need_str(params, "ref"))
        return {
            "connected": health.connected,
            "detail": health.detail,
            "models_discovered": health.models_discovered,
            "models": [
                {
                    "provider_model_id": model.provider_model_id,
                    "capabilities": model.capabilities,
                    "availability": model.availability,
                }
                for model in health.models
            ],
        }

    def _provider_discover(self, params: dict[str, Any]) -> dict[str, Any]:
        _ = params
        return {
            "candidates": [
                {
                    "source": candidate.source,
                    "name": candidate.name,
                    "detail": candidate.detail,
                    "provider_type": candidate.provider_type,
                    "endpoint": candidate.endpoint,
                }
                for candidate in self._services.providers.discover()
            ]
        }

    def _provider_use(self, params: dict[str, Any]) -> dict[str, Any]:
        selection = self._services.providers.use(self._need_str(params, "ref"))
        return {
            "provider": self._provider_view(selection.provider),
            "model": self._model_view(selection.model) if selection.model is not None else None,
        }

    # -- models ---------------------------------------------------------------

    def _model_get(self, params: dict[str, Any]) -> dict[str, Any]:
        model = self._services.models.resolve(
            self._need_str(params, "ref"), self._opt_str(params, "provider")
        )
        return {"model": self._model_view(model)}

    def _model_add(self, params: dict[str, Any]) -> dict[str, Any]:
        provider = self._need_str(params, "provider")
        provider_model_id = self._need_str(params, "provider_model_id")
        alias = self._need_str(params, "alias")
        capabilities = params.get("capabilities")
        if capabilities is not None and not isinstance(capabilities, dict):
            raise EngineProtocolError(INVALID_PARAMS, "Param 'capabilities' must be an object.")
        model = self._services.models.add(
            provider,
            provider_model_id,
            alias,
            capabilities=dict(capabilities) if capabilities is not None else None,
            settings=self._opt_settings(params),
        )
        return {"model": self._model_view(model)}

    def _model_alias(self, params: dict[str, Any]) -> dict[str, Any]:
        model = self._services.models.alias(
            self._need_str(params, "ref"),
            self._need_str(params, "new_alias"),
            self._opt_str(params, "provider"),
        )
        return {"model": self._model_view(model)}

    def _model_remove(self, params: dict[str, Any]) -> dict[str, Any]:
        model = self._services.models.remove(
            self._need_str(params, "ref"), self._opt_str(params, "provider")
        )
        return {"removed": {"id": model.id, "alias": model.alias}}

    def _model_use(self, params: dict[str, Any]) -> dict[str, Any]:
        resolved = self._services.models.use(
            self._need_str(params, "ref"), self._opt_str(params, "provider")
        )
        return {
            "model": self._model_view(resolved.model),
            "provider": self._provider_view(resolved.provider),
            "switched_provider": resolved.switched_provider,
        }

    def _model_discover(self, params: dict[str, Any]) -> dict[str, Any]:
        found = self._services.models.available(self._opt_str(params, "provider"))
        return {
            "providers": {
                alias: [
                    {
                        "provider_model_id": model.provider_model_id,
                        "capabilities": model.capabilities,
                        "availability": model.availability,
                    }
                    for model in models
                ]
                for alias, models in found.items()
            }
        }

    def _model_refresh(self, params: dict[str, Any]) -> dict[str, Any]:
        results = self._services.models.refresh(self._opt_str(params, "provider"))
        providers: dict[str, Any] = {}
        for alias, result in results.items():
            providers[alias] = {
                "saved": result.saved,
                "still_available": result.still_available,
                "marked_unavailable": result.marked_unavailable,
                "discovered": result.discovered,
                "error": result.error,
            }
        return {"providers": providers}

    def _model_test(self, params: dict[str, Any]) -> dict[str, Any]:
        result = self._services.models.test(
            self._need_str(params, "ref"), self._opt_str(params, "provider")
        )
        return {
            "ok": result.ok,
            "detail": result.detail,
            "model": self._model_view(result.model),
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
