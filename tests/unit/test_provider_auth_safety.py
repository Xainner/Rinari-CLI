"""RotaciÃ³n de credenciales sin pÃ©rdida (P0 del informe CredWrite error 8).

`set_auth` borraba la credencial anterior *antes* de escribir la nueva dentro
de una transacciÃ³n cuyo rollback no restaura un secreto ya eliminado: si la
escritura fallaba (vault saturado, error 8), la API key quedaba perdida.
"""

from __future__ import annotations

import pytest

from rinari.application.credentials import CredentialStore
from rinari.application.provider_service import AddProviderInput, ProviderService
from rinari.shared.errors import CredentialWriteError


class _FailingStore(CredentialStore):
    """Delega en el store real y hace fallar la siguiente escritura de proveedor."""

    def __init__(self, layout, *, fail: bool = False) -> None:
        super().__init__(layout, keyring_backend=None)
        self.fail = fail

    def stage_unique_provider_secret(self, provider_id: str, secret: str, **kwargs) -> str:
        if self.fail:
            raise CredentialWriteError(
                "Could not write to the OS credential store",
                hint="Run `rinari secrets cleanup --apply` before retrying.",
            )
        return super().stage_unique_provider_secret(provider_id, secret)

    def store_provider_secret(self, provider_id: str, secret: str) -> str:
        if self.fail:
            raise CredentialWriteError(
                "Could not write to the OS credential store",
                hint="Run `rinari secrets cleanup --apply` before retrying.",
            )
        return super().store_provider_secret(provider_id, secret)


def _add(service: ProviderService, alias: str = "openai-personal", secret: str = "sk-old"):
    return service.add(
        AddProviderInput(alias=alias, provider_type="openai", auth_method="api-key", secret=secret)
    )


def test_rotation_keeps_previous_secret_when_the_new_write_fails(app_ctx) -> None:
    # Alta inicial con el store real (el fixture del conftest usa file storage).
    initial = ProviderService(app_ctx)
    record = _add(initial, secret="sk-old")
    before = app_ctx.provider_repo.get_credential(record.id)
    assert before is not None

    failing = ProviderService(app_ctx, credentials=_FailingStore(app_ctx.layout, fail=True))
    with pytest.raises(CredentialWriteError):
        failing.set_auth("openai-personal", secret="sk-new")

    after = app_ctx.provider_repo.get_credential(record.id)
    assert after is not None
    # La referencia anterior sigue registrada y su valor sigue resolviendo.
    assert after.secret_ref == before.secret_ref
    reader = CredentialStore(app_ctx.layout, keyring_backend=None)
    assert reader.resolve(after.secret_ref) == "sk-old"


def test_rotation_replaces_the_value_in_place(app_ctx) -> None:
    providers = ProviderService(app_ctx)
    record = _add(providers, secret="sk-old")
    ref_before = app_ctx.provider_repo.get_credential(record.id).secret_ref

    providers.set_auth("openai-personal", secret="sk-new")

    ref_after = app_ctx.provider_repo.get_credential(record.id).secret_ref
    # Rotation protocol (review P1): the confirmed reference points at a
    # fresh, non-destructive copy of the new value; the previous file is
    # retired only after the commit and stays resolvable until then.
    assert ref_after != ref_before
    reader = CredentialStore(app_ctx.layout, keyring_backend=None)
    assert reader.resolve(ref_after) == "sk-new"
    assert not reader.exists(ref_before)


def test_switching_to_an_env_reference_removes_the_stored_secret(app_ctx) -> None:
    providers = ProviderService(app_ctx)
    record = _add(providers, secret="sk-old")
    ref_before = app_ctx.provider_repo.get_credential(record.id).secret_ref

    providers.set_auth("openai-personal", secret_env="OPENAI_API_KEY")

    ref_after = app_ctx.provider_repo.get_credential(record.id).secret_ref
    assert ref_after == "env://OPENAI_API_KEY"
    reader = CredentialStore(app_ctx.layout, keyring_backend=None)
    assert not reader.exists(ref_before)


def test_failed_rotation_does_not_clear_the_registered_reference(app_ctx) -> None:
    initial = ProviderService(app_ctx)
    record = _add(initial, secret="sk-old")

    failing = ProviderService(app_ctx, credentials=_FailingStore(app_ctx.layout, fail=True))
    with pytest.raises(CredentialWriteError):
        failing.set_auth("openai-personal", secret="sk-new")

    # Sin credencial registrada el provider quedarÃ­a inservible: no debe pasar.
    assert app_ctx.provider_repo.get_credential(record.id) is not None
    assert initial.get("openai-personal").auth_method == "api-key"
