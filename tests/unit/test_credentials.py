import pytest

from rinari.application.credentials import (
    CredentialStore,
    FileCredentialStore,
    parse_secret_ref,
)
from rinari.shared.errors import AuthenticationRequiredError, InvalidUsageError
from rinari.shared.paths import layout_for


def test_parse_env_ref() -> None:
    parsed = parse_secret_ref("env://ANTHROPIC_API_KEY")
    assert parsed.scheme == "env"
    assert parsed.key == "ANTHROPIC_API_KEY"


def test_parse_file_ref() -> None:
    parsed = parse_secret_ref("file://providers/prov_123")
    assert parsed.scheme == "file"
    assert parsed.key == "providers/prov_123"


@pytest.mark.parametrize("ref", ["", "openai_key", "file://", "env://", "ftp://x", "file://../x"])
def test_parse_invalid_refs(ref: str) -> None:
    with pytest.raises(InvalidUsageError):
        parse_secret_ref(ref)


def test_parse_env_name_rules() -> None:
    with pytest.raises(InvalidUsageError):
        parse_secret_ref("env://9BAD")
    with pytest.raises(InvalidUsageError):
        parse_secret_ref("env://BAD NAME")
    with pytest.raises(InvalidUsageError):
        parse_secret_ref("env://has/slash")


def test_file_store_roundtrip_and_delete(tmp_path) -> None:
    store = FileCredentialStore(tmp_path / "credentials")
    ref = store.store("providers/prov_1", "sk-abc123secret")
    assert ref == "file://providers/prov_1"
    assert store.resolve("providers/prov_1") == "sk-abc123secret"
    assert store.exists("providers/prov_1")
    assert store.delete("providers/prov_1")
    assert not store.exists("providers/prov_1")
    assert not store.delete("providers/prov_1")


def test_file_store_missing_ref(tmp_path) -> None:
    store = FileCredentialStore(tmp_path / "credentials")
    with pytest.raises(AuthenticationRequiredError):
        store.resolve("providers/prov_missing")


def test_facade_env_resolution() -> None:
    layout = layout_for("/tmp/rinari-home")
    store = CredentialStore(layout, env={"OPENAI_API_KEY": "sk-env-123"})
    assert store.resolve("env://OPENAI_API_KEY") == "sk-env-123"
    assert store.exists("env://OPENAI_API_KEY")


def test_facade_env_missing_raises() -> None:
    layout = layout_for("/tmp/rinari-home")
    store = CredentialStore(layout, env={})
    with pytest.raises(AuthenticationRequiredError) as excinfo:
        store.resolve("env://NOT_SET_KEY")
    assert "NOT_SET_KEY" in str(excinfo.value.message)
    assert not store.exists("env://NOT_SET_KEY")


def test_facade_env_never_deleted() -> None:
    layout = layout_for("/tmp/rinari-home")
    store = CredentialStore(layout, env={"K": "v"})
    assert store.delete("env://K") is False
    assert store.exists("env://K")


def test_facade_file_roundtrip(tmp_path) -> None:
    layout = layout_for(tmp_path)
    store = CredentialStore(layout, keyring_backend=None)
    ref = store.store_provider_secret("prov_9", "sk-file-secret")
    assert ref == "file://providers/prov_9"
    assert store.resolve(ref) == "sk-file-secret"
    assert store.delete(ref)
    assert not store.exists(ref)


def test_no_plaintext_leak_outside_credentials_dir(tmp_path) -> None:
    """Secrets may exist only inside the credentials dir, never in other state."""
    layout = layout_for(tmp_path)
    for name in ("profiles", "credentials"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    store = CredentialStore(layout)
    secret = "sk-leak-check-1234567890"
    ref = store.store_provider_secret("prov_1", secret)
    assert store.resolve(ref) == secret

    leaked = []
    for path in tmp_path.rglob("*"):
        if (
            path.is_file()
            and "credentials" not in path.parts
            and secret in path.read_text(encoding="utf-8", errors="ignore")
        ):
            leaked.append(str(path))
    assert leaked == []
