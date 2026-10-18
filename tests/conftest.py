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


@pytest.fixture(autouse=True)
def titles_in_order(monkeypatch, request):
    """Scripted models answer in order; a title asked beside the turn would race
    for their answers. Cases marked `background_title` keep it beside (V2-4)."""
    if request.node.get_closest_marker("background_title") is None:
        monkeypatch.setattr("rinari.cli.agent_runtime.TITLE_IN_BACKGROUND", False)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "background_title: ask the conversation title beside the turn (V2-4)"
    )
