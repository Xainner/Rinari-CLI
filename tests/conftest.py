"""Tests use isolated credential storage unless a case explicitly overrides it."""

import pytest


@pytest.fixture(autouse=True)
def isolate_native_credential_store(monkeypatch):
    monkeypatch.setenv("RINARI_KEYRING", "0")


@pytest.fixture(autouse=True)
def instant_model_retries(monkeypatch):
    """A transient model failure is retried without the real wait."""
    monkeypatch.setattr("rinari.runtime.agent.MODEL_RETRY_DELAYS_S", (0.0, 0.0))
    monkeypatch.setattr("rinari.runtime.agent.MAX_RETRY_AFTER_S", 0.0)
