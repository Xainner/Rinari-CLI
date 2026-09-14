from unittest.mock import patch

from rinari.application.credentials import CredentialStore, KeyringCredentialStore
from rinari.application.provider_service import AddProviderInput, ProviderService


def test_postcommit_sql_cleanup_failure_preserves_success(app_ctx):
    store = CredentialStore(app_ctx.layout, keyring_backend=None)
    service = ProviderService(app_ctx, credentials=store)
    record = service.add(AddProviderInput(alias="review", provider_type="openai", secret="old"))
    old_ref = app_ctx.provider_repo.get_credential(record.id).secret_ref
    remove = app_ctx.provider_repo.remove_pending_credential_cleanup

    def fail_old(ref):
        if ref == old_ref:
            raise OSError("synthetic postcommit cleanup failure")
        return remove(ref)

    with patch.object(
        app_ctx.provider_repo, "remove_pending_credential_cleanup", side_effect=fail_old
    ):
        service.set_auth(record.id, secret="new")
    assert service.resolve_secret(service.get(record.id)) == "new"
    assert app_ctx.provider_repo.list_pending_credential_cleanup()
    assert service.run_pending_credential_cleanup() == 1


def test_locked_real_keyring_facade_does_not_drop_pending(app_ctx):
    class LockedVault:
        def get_password(self, *args):
            raise OSError("synthetic locked vault")

        def delete_password(self, *args):
            raise OSError("synthetic locked vault")

    store = CredentialStore(app_ctx.layout, keyring_backend=None)
    store.keyring = KeyringCredentialStore(LockedVault(), scope="review")
    service = ProviderService(app_ctx, credentials=store)
    app_ctx.provider_repo.add_pending_credential_cleanup(
        "review", "keyring://gen/providers/review/gen-1", "now"
    )
    assert service.run_pending_credential_cleanup() == 0
    assert len(app_ctx.provider_repo.list_pending_credential_cleanup()) == 1


def test_pending_current_reference_is_never_deleted(app_ctx):
    store = CredentialStore(app_ctx.layout, keyring_backend=None)
    service = ProviderService(app_ctx, credentials=store)
    record = service.add(AddProviderInput(alias="review", provider_type="openai", secret="current"))
    ref = app_ctx.provider_repo.get_credential(record.id).secret_ref
    app_ctx.provider_repo.add_pending_credential_cleanup(record.id, ref, "now")
    assert service.run_pending_credential_cleanup() == 0
    assert service.resolve_secret(record) == "current"
