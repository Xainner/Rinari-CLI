"""Tests use isolated credential storage unless a case explicitly overrides it."""

import pytest


@pytest.fixture(autouse=True)
def isolate_native_credential_store(monkeypatch):
    monkeypatch.setenv("RINARI_KEYRING", "0")
