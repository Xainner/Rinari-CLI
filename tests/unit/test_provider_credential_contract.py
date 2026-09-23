"""Regresiones del plan de proveedores y credenciales (proveedores/PLAN_DE_TRABAJO.md).

F1: una referencia de entorno inválida no debe persistirse ni reflejarse en
errores. El alta y la rotación validan el nombre de la variable *antes* de
escribir configuración o secretos, y el mensaje de error nunca interpola la
referencia rechazada (puede contener una API key pegada por error).

F2: cambiar la fuente de credencial a una referencia inválida no retira la
credencial anterior. La fuente nueva se valida estructuralmente antes de
sustituir el registro.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from rinari.application.credentials import CredentialStore, parse_secret_ref
from rinari.application.provider_service import AddProviderInput, ProviderService
from rinari.shared.errors import InvalidUsageError

REPO_ROOT = Path(__file__).resolve().parents[2]


def _add(service: ProviderService, alias: str = "openai-personal") -> object:
    return service.add(
        AddProviderInput(
            alias=alias,
            provider_type="openai",
            auth_method="api-key",
            # Marcador sintético con forma de env-ref inválida: la auditoría
            # usó uno similar (con guiones) para activar la ruta defectuosa.
            secret_env="sk-synthetic-key-with-dashes",
        )
    )


def _reader(app_ctx) -> CredentialStore:
    return CredentialStore(app_ctx.layout, keyring_backend=None)


# ---------------------------------------------------------------------------
# F1 — alta: rechazo previo a la persistencia
# ---------------------------------------------------------------------------


def test_add_rejects_invalid_env_name_before_persisting(app_ctx) -> None:
    providers = ProviderService(app_ctx)
    with pytest.raises(InvalidUsageError) as excinfo:
        _add(providers)
    # Nada se persiste: ni provider, ni credencial, ni candidato.
    assert not list(providers.list())
    assert app_ctx.provider_repo.get_by_alias("openai-personal") is None
    message = str(excinfo.value)
    assert "sk-synthetic-key-with-dashes" not in message
    assert "sk-synthetic" not in repr(excinfo.value.__dict__)


def test_add_error_message_never_contains_the_rejected_reference(app_ctx) -> None:
    providers = ProviderService(app_ctx)
    with pytest.raises(InvalidUsageError) as excinfo:
        providers.add(
            AddProviderInput(
                alias="p2",
                provider_type="openai",
                auth_method="api-key",
                secret_env="env://ALSO-BAD",
            )
        )
    msg = str(excinfo.value)
    assert "ALSO-BAD" not in msg
    assert "env://ALSO-BAD" not in repr(excinfo.value.__dict__)


def test_add_accepts_a_valid_env_name(app_ctx) -> None:
    providers = ProviderService(app_ctx)
    record = providers.add(
        AddProviderInput(
            alias="p3",
            provider_type="openai",
            auth_method="api-key",
            secret_env="ANTHROPIC_API_KEY",
        )
    )
    credential = app_ctx.provider_repo.get_credential(record.id)
    assert credential is not None
    assert credential.secret_ref == "env://ANTHROPIC_API_KEY"


def test_parse_secret_ref_error_is_redacted() -> None:
    with pytest.raises(InvalidUsageError) as excinfo:
        parse_secret_ref("env://sk-synthetic-key-with-dashes")
    assert "sk-synthetic" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# F2 — rotación: la credencial anterior sobrevive a un rechazo
# ---------------------------------------------------------------------------


def test_set_auth_rejects_invalid_env_name_and_keeps_previous_secret(app_ctx) -> None:
    providers = ProviderService(app_ctx)
    providers.add(
        AddProviderInput(
            alias="p4",
            provider_type="openai",
            auth_method="api-key",
            secret="sk-old",
        )
    )
    before = app_ctx.provider_repo.get_credential(providers.get("p4").id)
    assert before is not None

    with pytest.raises(InvalidUsageError):
        providers.set_auth("p4", secret_env="sk-synthetic-key-with-dashes")

    after = app_ctx.provider_repo.get_credential(providers.get("p4").id)
    assert after is not None
    assert after.secret_ref == before.secret_ref
    reader = _reader(app_ctx)
    assert reader.resolve(before.secret_ref) == "sk-old"
    assert reader.exists(before.secret_ref)


def test_set_auth_accepts_a_valid_env_reference_and_replaces_in_place(app_ctx) -> None:
    providers = ProviderService(app_ctx)
    providers.add(
        AddProviderInput(
            alias="p5",
            provider_type="openai",
            auth_method="api-key",
            secret="sk-old",
        )
    )
    providers.set_auth("p5", secret_env="OPENAI_API_KEY")
    credential = app_ctx.provider_repo.get_credential(providers.get("p5").id)
    assert credential is not None
    assert credential.secret_ref == "env://OPENAI_API_KEY"
    # La credencial anterior (file://providers/<id>) queda retirada tras el
    # éxito; la referencia env no se resuelve aquí porque depende del entorno.
    assert not _reader(app_ctx).exists(f"file://providers/{providers.get('p5').id}")


def test_parse_secret_ref_accepts_official_shape() -> None:
    ref = parse_secret_ref("env://OPENAI_API_KEY")
    assert ref.scheme == "env"
    assert ref.key == "OPENAI_API_KEY"


def test_add_rejects_both_sources(app_ctx) -> None:
    providers = ProviderService(app_ctx)
    with pytest.raises(InvalidUsageError):
        providers.add(
            AddProviderInput(
                alias="p6",
                provider_type="openai",
                auth_method="api-key",
                secret="sk-x",
                secret_env="OPENAI_API_KEY",
            )
        )


# ---------------------------------------------------------------------------
# F4 — provider.update atómico: un rechazo no deja cambios parciales
# ---------------------------------------------------------------------------


def _server(app_ctx):
    from rinari.application.services import build_services
    from rinari.engine_protocol.server import EngineServer

    return EngineServer(build_services(app_ctx, user_home=app_ctx.layout.root))


def test_update_rpc_rejects_conflicting_sources_without_partial_changes(app_ctx) -> None:
    providers = ProviderService(app_ctx)
    providers.add(
        AddProviderInput(
            alias="p7",
            provider_type="openai",
            auth_method="api-key",
            secret="sk-old",
        )
    )
    server = _server(app_ctx)
    response = server._dispatcher.dispatch(
        json.dumps(
            {
                "id": "r1",
                "method": "provider.update",
                "params": {
                    "ref": providers.get("p7").id,
                    "alias": "changed-before-validation",
                    "secret": "sk-new-literal",
                    "secret_env": "VALID_ENV_NAME",
                },
            }
        )
    )
    assert response is not None and response["ok"] is False
    # Nada cambió: ni alias, ni credencial.
    record = providers.get("p7")
    assert record.alias == "p7"
    credential = app_ctx.provider_repo.get_credential(record.id)
    assert credential is not None
    assert credential.secret_ref.startswith("file://")


def test_update_rpc_rejects_invalid_settings_without_rotating_secret(app_ctx) -> None:
    providers = ProviderService(app_ctx)
    providers.add(
        AddProviderInput(
            alias="p8",
            provider_type="openai",
            auth_method="api-key",
            secret="sk-old",
        )
    )
    server = _server(app_ctx)
    record_id = providers.get("p8").id
    response = server._dispatcher.dispatch(
        json.dumps(
            {
                "id": "r2",
                "method": "provider.update",
                "params": {"ref": record_id, "secret": "sk-rotated", "settings": "not-an-object"},
            }
        )
    )
    assert response is not None and response["ok"] is False
    # La credencial original sigue resolviendo: la rotación no ocurrió.
    credential = app_ctx.provider_repo.get_credential(record_id)
    assert credential is not None
    reader = CredentialStore(app_ctx.layout, keyring_backend=None)
    assert reader.resolve(credential.secret_ref) == "sk-old"


# ---------------------------------------------------------------------------
# F5 — la validación del alta se reaplica en update
# ---------------------------------------------------------------------------


def test_update_rpc_rejects_unknown_protocol(app_ctx) -> None:
    providers = ProviderService(app_ctx)
    providers.add(
        AddProviderInput(
            alias="p9",
            provider_type="openai",
            auth_method="api-key",
            secret="sk-old",
        )
    )
    server = _server(app_ctx)
    record_id = providers.get("p9").id
    response = server._dispatcher.dispatch(
        json.dumps(
            {
                "id": "r3",
                "method": "provider.update",
                "params": {"ref": record_id, "settings": {"protocol": "audit-invalid-protocol"}},
            }
        )
    )
    assert response is not None and response["ok"] is False
    record = providers.get("p9")
    assert "protocol" not in record.settings


def test_add_rejects_custom_without_any_endpoint(app_ctx) -> None:
    providers = ProviderService(app_ctx)
    with pytest.raises(InvalidUsageError):
        providers.add(
            AddProviderInput(
                alias="p10",
                provider_type="custom",
                auth_method="none",
            )
        )


# ---------------------------------------------------------------------------
# F6 — catálogo de transporte de OpenCode Go con procedencia
# ---------------------------------------------------------------------------


def _resolve_transport(provider, model):
    from rinari.models.router import _resolve_transport

    return _resolve_transport(provider, model)


def _provider_with_model(app_ctx, *, alias, endpoint, model_id):
    providers = ProviderService(app_ctx)
    record = providers.add(
        AddProviderInput(
            alias=alias,
            provider_type="custom",
            auth_method="api-key",
            endpoint=endpoint,
            secret="sk-synthetic",
        )
    )
    from rinari.application.model_service import ModelService

    model = ModelService(app_ctx, providers).add(record.alias, model_id, f"{alias}-model")
    return record, model


def test_grok46_defaults_to_responses_transport_on_opencode(app_ctx) -> None:
    record, model = _provider_with_model(
        app_ctx,
        alias="go",
        endpoint="https://opencode.ai/zen/go/v1",
        model_id="grok-4.6",
    )
    assert _resolve_transport(record, model) == "responses"


def test_grok45_uses_responses_transport_on_opencode_zen(app_ctx) -> None:
    # Tabla de endpoints de https://opencode.ai/docs/zen/ (actualizada el
    # 2026-09-22): todos los grok, grok-4.5 incluido, van por /responses. La
    # prueba anterior daba grok-4.5 por /chat/completions en Go según la tabla
    # del 2026-09-13; en la actual, Go ya no ofrece grok-4.5.
    record, model = _provider_with_model(
        app_ctx,
        alias="zen45",
        endpoint="https://opencode.ai/zen/v1",
        model_id="grok-4.5",
    )
    assert _resolve_transport(record, model) == "responses"


def test_chat_documented_model_stays_on_chat_on_opencode_go(app_ctx) -> None:
    # No todo lo de OpenCode va por /responses: la misma tabla de Go pone
    # kimi-k3 en /chat/completions.
    record, model = _provider_with_model(
        app_ctx,
        alias="go-kimi",
        endpoint="https://opencode.ai/zen/go/v1",
        model_id="kimi-k3",
    )
    assert _resolve_transport(record, model) == "chat"


def test_explicit_transport_setting_wins_over_catalog(app_ctx) -> None:
    record, model = _provider_with_model(
        app_ctx,
        alias="go-explicit",
        endpoint="https://opencode.ai/zen/go/v1",
        model_id="grok-4.6",
    )
    model.settings = {"transport": "chat"}
    assert _resolve_transport(record, model) == "chat"


# ---------------------------------------------------------------------------
# Review P1: apply_update atómico ante fallo de persistencia
# ---------------------------------------------------------------------------


class _ChildRunner:
    """Lanza un subproceso REAL que comparte el home y muere con os._exit.

    Ni los snapshots ni los objetos del padre son visibles para él: solo lo
    persistido (SQL + files). Eso reproduce fielmente el estado post-crash.
    """

    _SCRIPT = """
import os, sys, json
sys.path.insert(0, sys.argv[1])  # checkout src, so this runs without the venv
home = sys.argv[2]
os.environ["RINARI_HOME"] = home
os.environ["RINARI_KEYRING"] = "0"
from rinari.application.context import build_app_context
from rinari.application.credentials import CredentialStore
from rinari.application.provider_service import AddProviderInput, ProviderService

class _DeleteFailingStore(CredentialStore):
    def delete(self, ref):
        raise RuntimeError("synthetic delete failure in child")
    def exists(self, ref):
        return False

ctx = build_app_context(home=home)
service = ProviderService(ctx, credentials=_DeleteFailingStore(ctx.layout, keyring_backend=None))
record_id = sys.argv[3]
record = service.get(record_id)
ref_before = ctx.provider_repo.get_credential(record.id).secret_ref
service.set_auth(record.id, secret="sk-new-crash")
print(json.dumps({
    "sql_committed": ctx.provider_repo.get_credential(record.id).secret_ref != ref_before,
    "ref_before": ref_before,
}))
ctx.close()
# os._exit skips atexit/buffers: flush forcibly so the parent sees the result.
sys.stdout.flush()
os._exit(0)
"""

    def __init__(self, app_ctx, src_path: str) -> None:
        self._src = src_path
        self._home = str(app_ctx.layout.root)
        self._proc = None

    def _spawn(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-c", self._SCRIPT, self._src, self._home, *args],
            capture_output=True,
            text=True,
            timeout=120,
        )

    def run_rotation_then_hard_exit_without_retire(self, record_id: str) -> dict:
        result = self._spawn(record_id)
        if result.returncode != 0:
            raise AssertionError(f"child crashed unexpectedly: {result.stderr[-800:]}")
        return json.loads(result.stdout.strip().splitlines()[-1])

    def cleanup(self) -> None:  # el subproceso ya terminó (no queda nada vivo)
        return None


class _FailingRepo:
    """Envuelve provider_repo y hace fallar el N-ésimo update del record."""

    def __init__(self, repo, target_id: str, fail_on: int = 1) -> None:
        self._repo = repo
        self._target = target_id
        self._fail_on = fail_on
        self._writes = 0

    def __getattr__(self, name):
        return getattr(self._repo, name)

    def update(self, record):
        if record.id == self._target:
            self._writes += 1
            if self._writes >= self._fail_on:
                raise RuntimeError("synthetic persistence failure")
        return self._repo.update(record)


def test_apply_update_failed_persistence_keeps_previous_secret(app_ctx) -> None:
    providers = ProviderService(app_ctx)
    record = providers.add(
        AddProviderInput(
            alias="p-atomic",
            provider_type="openai",
            auth_method="api-key",
            secret="sk-old",
        )
    )
    # Forzar el fallo en el primer provider_repo.update dentro de apply_update.
    realService = ProviderService(app_ctx)
    realService._ctx.provider_repo = _FailingRepo(
        realService._ctx.provider_repo, record.id, fail_on=1
    )
    with pytest.raises(RuntimeError):
        realService.apply_update(record.id, new_alias="renamed", secret="sk-new")

    # El alias volvió al anterior (rollback)...
    reader = CredentialStore(app_ctx.layout, keyring_backend=None)
    current = app_ctx.provider_repo.get_credential(record.id)
    assert current is not None
    # ...y el valor del store resuelve a la clave ANTERIOR, no a la nueva.
    assert reader.resolve(current.secret_ref) == "sk-old"


def test_set_auth_failed_persistence_keeps_previous_secret(app_ctx) -> None:
    providers = ProviderService(app_ctx)
    record = providers.add(
        AddProviderInput(
            alias="p-setauth",
            provider_type="openai",
            auth_method="api-key",
            secret="sk-old",
        )
    )
    failing = ProviderService(app_ctx)
    failing._ctx.provider_repo = _FailingRepo(failing._ctx.provider_repo, record.id, fail_on=1)
    with pytest.raises(RuntimeError):
        failing.set_auth(record.id, secret="sk-new")

    reader = CredentialStore(app_ctx.layout, keyring_backend=None)
    current = app_ctx.provider_repo.get_credential(record.id)
    assert current is not None
    # La credencial anterior sigue resolviendo tras el rollback.
    assert reader.resolve(current.secret_ref) == "sk-old"


def test_apply_update_double_failure_still_keeps_previous_secret(app_ctx) -> None:
    providers = ProviderService(app_ctx)
    record = providers.add(
        AddProviderInput(
            alias="p-double-fail",
            provider_type="openai",
            auth_method="api-key",
            secret="sk-old",
        )
    )
    failing = ProviderService(app_ctx)
    failing._ctx.provider_repo = _FailingRepo(failing._ctx.provider_repo, record.id, fail_on=1)
    with pytest.raises(RuntimeError):
        failing.apply_update(record.id, new_alias="renamed", secret="sk-new")

    reader = CredentialStore(app_ctx.layout, keyring_backend=None)
    current = app_ctx.provider_repo.get_credential(record.id)
    assert current is not None
    assert reader.resolve(current.secret_ref) == "sk-old"


class _DeleteFailingStore(CredentialStore):
    """Store real cuyo delete siempre falla (para post-commit cleanup)."""

    def __init__(self, layout) -> None:
        super().__init__(layout, keyring_backend=None)

    def delete(self, ref: str) -> bool:
        raise RuntimeError("synthetic delete failure")


def test_failed_retire_after_commit_reports_success_with_pending_cleanup(app_ctx) -> None:
    """Review P1 defecto 2: el retiro post-commit nunca convierte una
    actualización confirmada en un error. La referencia anterior queda
    como limpieza pendiente (persistente, tabla propia) y el barrido
    idempotente la completa. Cubre apply_update y set_auth."""
    mutations = (
        lambda svc, rid: svc.apply_update(rid, secret="sk-new"),
        lambda svc, rid: svc.set_auth(rid, secret="sk-new"),
    )
    for index, mutate in enumerate(mutations):
        record = ProviderService(app_ctx).add(
            AddProviderInput(
                alias=f"p-bal-{index}",
                provider_type="openai",
                auth_method="api-key",
                secret="sk-old",
            )
        )
        reader = CredentialStore(app_ctx.layout, keyring_backend=None)
        ref_before = app_ctx.provider_repo.get_credential(record.id).secret_ref

        service = ProviderService(app_ctx, credentials=_DeleteFailingStore(app_ctx.layout))
        # NO debe lanzar: la operación ya está confirmada; el fallo del
        # delete se registra como limpieza pendiente.
        record = mutate(service, record.id)

        current = app_ctx.provider_repo.get_credential(record.id)
        assert current is not None
        assert current.secret_ref != ref_before
        assert reader.resolve(current.secret_ref) == "sk-new"
        # La referencia anterior quedó como limpieza pendiente (persistente,
        # en su tabla propia; la retención voluntaria no participa).
        pending = [
            entry
            for entry in app_ctx.provider_repo.list_pending_credential_cleanup()
            if entry["secret_ref"] == ref_before
        ]
        assert pending, "old reference missing from pending cleanup"
        # La entrada pendiente aún resuelve (no se perdió la clave).
        assert reader.exists(ref_before)


def test_rotation_survives_process_kill_between_commit_and_retire(app_ctx) -> None:
    """Interrupción REAL: un subproceso hijo rota la clave y muere con
    `os._exit` justo tras el commit, sin retirar la referencia anterior
    (delete en fallo). Un proceso nuevo reabre el home: la rotación está
    confirmada, el pendiente persiste en su tabla propia y el barrido
    idempotente completo la limpieza."""
    providers = ProviderService(app_ctx)
    record = providers.add(
        AddProviderInput(
            alias="p-kill",
            provider_type="openai",
            auth_method="api-key",
            secret="sk-old",
        )
    )
    ref_before = app_ctx.provider_repo.get_credential(record.id).secret_ref

    child = _ChildRunner(app_ctx, str(REPO_ROOT / "src"))
    try:
        outcome = child.run_rotation_then_hard_exit_without_retire(record.id)
        # Proceso fresco sobre el home: estado post-crash real.
        reader = CredentialStore(app_ctx.layout, keyring_backend=None)
        reopened = ProviderService(app_ctx)
        current = app_ctx.provider_repo.get_credential(record.id)
        assert current is not None
        # El commit de la rotación sobrevive al crash del hijo...
        assert current.secret_ref != ref_before
        assert reader.resolve(current.secret_ref) == "sk-new-crash"
        # ...y la referencia anterior quedó huérfana resolvable.
        assert reader.exists(ref_before)
        # El pendiente está registrado (persistente, tabla propia).
        pending = [
            entry
            for entry in app_ctx.provider_repo.list_pending_credential_cleanup()
            if entry["secret_ref"] == ref_before
        ]
        assert pending, "old reference missing from pending cleanup after crash"
        # El barrido idempotente de un proceso nuevo completa la limpieza.
        assert reopened.run_pending_credential_cleanup() >= 1
        assert not reader.exists(ref_before)
        assert not app_ctx.provider_repo.list_pending_credential_cleanup()
        # El provider rota correctamente tras el reinicio (resolución viva).
        assert reopened.resolve_secret(reopened.get(record.id)) == "sk-new-crash"
        assert outcome["sql_committed"] is True
    finally:
        child.cleanup()


# ---------------------------------------------------------------------------
# Review: ventanas de interrupción y coherencia del ciclo de limpieza
# ---------------------------------------------------------------------------


def test_uncommitted_candidate_is_cleaned_while_provider_lives(app_ctx) -> None:
    """Guardar candidato y fallar SQL antes del commit: la clave vigente no
    cambia; el candidato queda localizable y el barrido lo elimina."""
    providers = ProviderService(app_ctx)
    record = providers.add(
        AddProviderInput(
            alias="p-candidate",
            provider_type="openai",
            auth_method="api-key",
            secret="sk-old",
        )
    )
    ref_before = app_ctx.provider_repo.get_credential(record.id).secret_ref
    reader = CredentialStore(app_ctx.layout, keyring_backend=None)

    failing = ProviderService(app_ctx)
    failing._ctx.provider_repo = _FailingRepo(failing._ctx.provider_repo, record.id, fail_on=1)
    with pytest.raises(RuntimeError):
        failing.apply_update(record.id, new_alias="renamed", secret="sk-new")

    # Configuración anterior intacta; ninguna referencia retirada.
    current = app_ctx.provider_repo.get_credential(record.id)
    assert current is not None and current.secret_ref == ref_before
    assert reader.resolve(ref_before) == "sk-old"
    pending = app_ctx.provider_repo.list_pending_credential_cleanup()
    assert pending == []
    # El provider sigue funcionando con su clave original.
    assert providers.resolve_secret(providers.get(record.id)) == "sk-old"


def test_multiple_failed_retire_rotations_keep_every_pending_reference(app_ctx) -> None:
    """Varias rotaciones con borrado fallido: cada referencia anterior queda
    como su propia fila de pendiente (no se pisan entre sí)."""
    record = ProviderService(app_ctx).add(
        AddProviderInput(
            alias="p-multi",
            provider_type="openai",
            auth_method="api-key",
            secret="sk-old",
        )
    )
    refs = []
    for secret in ("sk-new-1", "sk-new-2", "sk-new-3"):
        service = ProviderService(app_ctx, credentials=_DeleteFailingStore(app_ctx.layout))
        record = service.set_auth(record.id, secret=secret)
        credential = app_ctx.provider_repo.get_credential(record.id)
        refs.append(credential.secret_ref)
    pending_refs = {
        entry["secret_ref"] for entry in app_ctx.provider_repo.list_pending_credential_cleanup()
    }
    # Cada rotación dejó su huérfano anterior, rastreado individualmente.
    assert len(pending_refs) == 3
    current = app_ctx.provider_repo.get_credential(record.id)
    assert current is not None
    assert current.secret_ref not in pending_refs
    # El barrido completo retira todas las huérfanas.
    assert ProviderService(app_ctx).run_pending_credential_cleanup() == 3
    reader = CredentialStore(app_ctx.layout, keyring_backend=None)
    assert all(not reader.exists(ref) for ref in pending_refs)
    assert reader.resolve(current.secret_ref) == "sk-new-3"


def test_voluntarily_retained_reference_is_never_cleaned_as_pending(app_ctx) -> None:
    """Retención voluntaria (`retain_credential`) no debe tocarla el barrido
    de pendientes: son intenciones distintas."""
    record = ProviderService(app_ctx).add(
        AddProviderInput(
            alias="p-retained",
            provider_type="openai",
            auth_method="api-key",
            secret="sk-old",
        )
    )
    reader = CredentialStore(app_ctx.layout, keyring_backend=None)
    ref_before = app_ctx.provider_repo.get_credential(record.id).secret_ref
    # El usuario retiró el provider conservando la credencial a propósito.
    # Necesita otro provider activo para poder remover el primero.
    other = ProviderService(app_ctx).add(
        AddProviderInput(
            alias="p-retained-other",
            provider_type="openai",
            auth_method="api-key",
            secret="sk-other",
        )
    )
    ProviderService(app_ctx).use(other.id)
    ProviderService(app_ctx).remove(record.id + "", keep_credentials=True)
    # Es retención, no pendiente: fuera del barrido.
    assert app_ctx.provider_repo.list_pending_credential_cleanup() == []
    assert reader.exists(ref_before)
    # También sigue clasificada como retained por el GC.

    assert record.id in set(app_ctx.provider_repo.list_retained_credential_ids())


def test_pending_cleanup_sweep_missing_store_value_completes_idempotently(app_ctx) -> None:
    """Borrado exitoso + muerte antes de retirar la fila: el reintento ve el
    valor ausente y completa la limpieza sin confundir 'no existe' con un
    error de acceso."""
    record = ProviderService(app_ctx).add(
        AddProviderInput(
            alias="p-idem",
            provider_type="openai",
            auth_method="api-key",
            secret="sk-old",
        )
    )
    ref_before = app_ctx.provider_repo.get_credential(record.id).secret_ref
    service = ProviderService(app_ctx)
    service.set_auth(record.id, secret="sk-new")
    # Simula el estado tras un commit+delete+crash: valor borrado, fila viva.
    service._ctx.provider_repo.add_pending_credential_cleanup(
        record.id, ref_before, "2026-09-14T00:00:00"
    )
    reader = CredentialStore(app_ctx.layout, keyring_backend=None)
    assert not reader.exists(ref_before)  # ya no existe en el store

    completed = ProviderService(app_ctx).run_pending_credential_cleanup()
    assert completed >= 1
    assert not app_ctx.provider_repo.list_pending_credential_cleanup()
