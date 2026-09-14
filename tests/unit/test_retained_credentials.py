"""Retención explícita: `keep_credentials` debe sobrevivir al borrado.

Revisión del PR (P1): la metadata de credenciales se borra en cascada con el
proveedor, así que la retención no puede vivir ahí. Se registra en su propia
tabla y el plan de saneamiento la usa para no borrar secretos conservados.
"""

from __future__ import annotations

from rinari.application.credentials import CredentialStore
from rinari.application.credentials_gc import plan_cleanup
from rinari.application.credentials_vault import VaultCredential
from rinari.application.provider_service import AddProviderInput, ProviderService

SCOPE = "home-test"


def _add(service: ProviderService, alias: str, provider_type: str, secret: str):
    return service.add(
        AddProviderInput(
            alias=alias,
            provider_type=provider_type,
            auth_method="api-key",
            secret=secret,
        )
    )


def _cred(target: str, username: str) -> VaultCredential:
    return VaultCredential(
        target=target,
        username=username,
        comment="Stored using python-keyring",
        last_written="2026-09-10T00:00:00",
        persist=3,
        cred_type=1,
    )


def test_keep_credentials_survives_removal_and_is_reported_as_retained(app_ctx) -> None:
    service = ProviderService(app_ctx)
    first = _add(service, "openai-one", "openai", "sk-a")
    second = _add(service, "anthropic-two", "anthropic", "sk-b")
    secret_ref = app_ctx.provider_repo.get_credential(first.id).secret_ref

    service.remove("openai-one", switch_to="anthropic-two", keep_credentials=True)

    # La metadata se fue en cascada, la retención no.
    assert app_ctx.provider_repo.get_credential(first.id) is None
    assert app_ctx.provider_repo.list_retained_credential_ids() == [first.id]

    retained = app_ctx.provider_repo.get_retained_credential(first.id)
    assert retained is not None
    assert retained.secret_ref == secret_ref

    reader = CredentialStore(app_ctx.layout, keyring_backend=None)
    assert reader.exists(secret_ref) is True

    target = f"rinari/{SCOPE}/providers/{first.id}"
    plan = plan_cleanup(
        [_cred(target, f"providers/{first.id}")],
        live_provider_ids={second.id},
        retained_provider_ids=set(app_ctx.provider_repo.list_retained_credential_ids()),
        scope=SCOPE,
    )
    assert plan.orphans == []
    assert [entry.target for entry in plan.retained] == [target]


def test_removal_without_keep_credentials_deletes_and_does_not_retain(app_ctx) -> None:
    service = ProviderService(app_ctx)
    first = _add(service, "openai-one", "openai", "sk-a")
    _add(service, "anthropic-two", "anthropic", "sk-b")
    secret_ref = app_ctx.provider_repo.get_credential(first.id).secret_ref

    service.remove("openai-one", switch_to="anthropic-two")

    assert app_ctx.provider_repo.list_retained_credential_ids() == []
    reader = CredentialStore(app_ctx.layout, keyring_backend=None)
    assert reader.exists(secret_ref) is False

    target = f"rinari/{SCOPE}/providers/{first.id}"
    plan = plan_cleanup(
        [_cred(target, f"providers/{first.id}")],
        live_provider_ids=set(),
        retained_provider_ids=set(app_ctx.provider_repo.list_retained_credential_ids()),
        scope=SCOPE,
    )
    assert [entry.target for entry in plan.orphans] == [target]
