import os
import sys
from io import BytesIO
from types import SimpleNamespace

import pytest

from rinari.browser import discovery
from rinari.browser.manager import BrowserError, BrowserManager


def test_explicit_missing_browser_never_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(discovery.shutil, "which", lambda name: None)
    monkeypatch.setattr(discovery, "windows_candidates", lambda: pytest.fail("fallback"))
    assert discovery.find_browser("missing.exe") is None
    manager = BrowserManager(session_id="test", home_root=tmp_path)
    with pytest.raises(BrowserError, match=r"missing\.exe"):
        manager.launch(command="missing.exe")


@pytest.mark.skipif(os.name != "nt", reason="Windows discovery")
def test_edge_outside_path_with_spaces(monkeypatch, tmp_path):
    edge = tmp_path / "Program Files" / "Microsoft/Edge/Application/msedge.exe"
    edge.parent.mkdir(parents=True)
    edge.write_bytes(b"fixture")
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "Program Files"))
    monkeypatch.setattr(
        discovery.shutil, "which", lambda name: str(edge) if name == str(edge) else None,
    )
    assert discovery.find_browser() == str(edge)


def test_explicit_browser_with_spaces(monkeypatch):
    path = "C:/Program Files/Browser/browser.exe"
    monkeypatch.setattr(discovery.shutil, "which", lambda name: path if name == path else None)
    assert discovery.find_browser(path) == path


def test_failed_launch_has_bounded_diagnostics_and_no_process(tmp_path):
    manager = BrowserManager(session_id="failure", home_root=tmp_path)
    with pytest.raises(BrowserError, match="Browser did not open"):
        manager.launch(command=sys.executable)  # Python rejects Chromium flags and exits.
    assert manager._process is None and not manager.connected
    assert manager.diagnostics()["process_exit_code"] != 0
    assert manager.diagnostics()["stderr"]
    manager._drain_stderr(SimpleNamespace(stderr=BytesIO(b"x" * 50000)))
    assert len(manager.diagnostics()["stderr"].encode()) == 16384


def test_ready_requires_a_successful_cdp_probe(tmp_path, monkeypatch):
    from rinari.browser import manager as module

    closed = []

    class BrokenSession:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def send(self, method):
            raise RuntimeError("probe failed")

        def close(self):
            closed.append(True)

    manager = BrowserManager(session_id="failure", home_root=tmp_path)
    monkeypatch.setattr(manager, "_browser_ws_url", lambda endpoint: endpoint)
    monkeypatch.setattr(module, "CdpSession", BrokenSession)
    assert not manager._ensure_session("ws://127.0.0.1:1", deadline_s=0.01)
    assert closed and not manager.connected
    assert manager.diagnostics()["last_error"] == "probe failed"


def test_profile_isolated_between_runtimes_of_same_session(tmp_path):
    first = BrowserManager(session_id="same", home_root=tmp_path)
    second = BrowserManager(session_id="same", home_root=tmp_path)
    assert first.profile_dir != second.profile_dir
    assert first.profile_dir.parent == second.profile_dir.parent


@pytest.mark.parametrize("broken_hook", [False, True])
def test_agent_session_end_always_closes_browser(broken_hook):
    from unittest.mock import Mock

    from rinari.cli.agent_runtime import AgentSession

    browser = Mock()
    hook = Mock(side_effect=RuntimeError("hook failed") if broken_hook else None)
    session = AgentSession(
        services=None, record=None, caller=None, loop=None,
        context=SimpleNamespace(tool_ctx=SimpleNamespace(browser=browser)), close=hook,
    )
    session.end()
    browser.close.assert_called_once()
    assert session.close is None
