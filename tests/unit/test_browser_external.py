"""`browser.external.open` (plan 0.4.0 #45)."""

from __future__ import annotations

import time

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.browser.manager import BrowserError
from rinari.engine_protocol.errors import EngineProtocolError
from rinari.engine_protocol.server import EngineServer


@pytest.fixture
def desktop(app_ctx, tmp_path):
    services = build_services(app_ctx, user_home=tmp_path)
    services.providers.add(
        AddProviderInput(
            alias="fake",
            provider_type="openai",
            endpoint="http://127.0.0.1:9/v1",
            secret="test-secret",
        )
    )
    services.models.add("fake", "test-model", "test-model")
    services.providers.use("fake")
    server = EngineServer(services, user_home=tmp_path)
    yield server
    server.close()


def _events(desktop, name, timeout=3.0):
    deadline = time.monotonic() + timeout
    seen = []
    while time.monotonic() < deadline:
        seen += [e for e in desktop._turns.drain_events() if e.get("event") == name]
        if seen:
            return seen
        time.sleep(0.02)
    return seen


def test_only_web_pages_move_to_chrome(desktop):
    session = desktop._session_create({"chat": True})["session"]
    with pytest.raises(EngineProtocolError):
        desktop._browser_external_open({"session_id": session["id"], "url": "file:///C:/x"})


def test_a_session_without_a_browser_says_so(desktop):
    session = desktop._session_create({"chat": True})["session"]
    with pytest.raises(EngineProtocolError, match="no browser"):
        desktop._browser_external_open(
            {"session_id": session["id"], "url": "https://login.example/"}
        )


def test_the_window_opens_with_the_project_profile_and_reports_back(desktop, monkeypatch):
    session = desktop._session_create({"chat": True})["session"]
    calls = []

    class Manager:
        def use_external(self, url, profile):
            calls.append((url, profile))
            return {"target_id": "t", "url": url}

    monkeypatch.setattr(desktop._browser_registry, "acquire", lambda _sid: Manager())
    reply = desktop._browser_external_open(
        {"session_id": session["id"], "url": "https://login.example/"}
    )
    assert reply["state"] == "opening"
    events = _events(desktop, "browser.external.changed")
    assert events and events[0]["payload"]["state"] == "open"
    url, profile = calls[0]
    assert url == "https://login.example/"
    # A chat has no project: its own «chat» profile, never the owner's.
    assert profile.parts[-3:] == ("profiles", "external", "chat")


def test_a_failed_launch_is_reported(desktop, monkeypatch):
    session = desktop._session_create({"chat": True})["session"]

    class Manager:
        def use_external(self, url, profile):
            raise BrowserError("BROWSER_DEPENDENCY", "no Chrome or Edge installed")

    monkeypatch.setattr(desktop._browser_registry, "acquire", lambda _sid: Manager())
    desktop._browser_external_open({"session_id": session["id"], "url": "https://a.example/"})
    events = _events(desktop, "browser.external.changed")
    assert events[0]["payload"]["state"] == "failed"
    assert events[0]["payload"]["error"]["code"] == "BROWSER_DEPENDENCY"
