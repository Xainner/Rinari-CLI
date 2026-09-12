"""Opt-in real browser regression. Uses only a temporary loopback page/profile."""

import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from rinari.browser.manager import BrowserManager


@pytest.mark.skipif(os.environ.get("RINARI_TEST_REAL_BROWSER") != "1", reason="opt-in browser")
def test_same_session_runtimes_do_not_share_chrome_profile(tmp_path):
    from rinari.cli.agent_runtime import AgentSession

    first = BrowserManager(session_id="same-session", home_root=tmp_path)
    second = BrowserManager(session_id="same-session", home_root=tmp_path)
    try:
        first.launch()
        second.launch()  # Previously Chrome exited with code 21 and empty stderr.
        assert first.profile_dir != second.profile_dir
        session = AgentSession(
            services=None, record=None, caller=None, loop=None,
            context=SimpleNamespace(tool_ctx=SimpleNamespace(browser=first)),
        )
        session.end()
        assert not first.connected and first._process is None
        assert second.connected
        target = second.new_page()["target_id"]
        assert second.evaluate(target, "1+1")["value"] == 2
    finally:
        first.close()
        second.close()


@pytest.mark.skipif(os.environ.get("RINARI_TEST_REAL_BROWSER") != "1", reason="opt-in browser")
def test_real_browser_idle_keyboard_capture_and_relaunch(tmp_path):
    html = b'''<!doctype html><meta charset="utf-8"><title>Rinari browser QA</title>
    <body style="background:#101426;color:white;font:24px sans-serif;padding:50px">
    <h1>Rinari browser QA</h1><p>Keyboard actions: <b id="count">0</b></p>
    <canvas width="400" height="200"></canvas><script>
    window.hits=0; const ctx=document.querySelector('canvas').getContext('2d');
    ctx.fillStyle='#8048ff';ctx.fillRect(150,130,70,40);
    addEventListener('keydown',e=>{if(e.code==='Space'){
      document.querySelector('#count').textContent=++window.hits;
      ctx.fillStyle='#42eeb0';ctx.fillRect(180,20,10,100);console.log('shot',hits);
    }});</script>'''

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    manager = BrowserManager(session_id="real-browser-qa", home_root=tmp_path)
    try:
        endpoint = manager.launch()
        process = manager._process
        print("Browser:", manager.diagnostics()["executable"], flush=True)
        target = manager.new_page()["target_id"]
        manager.call(target, "Runtime.enable", domain="Runtime")
        url = f"http://127.0.0.1:{server.server_port}/"
        manager.navigate(target, url)
        for _ in range(100):
            if manager.evaluate(target, "window.hits")["value"] == 0:
                break
            time.sleep(0.05)
        assert manager.evaluate(target, "window.hits")["value"] == 0
        before = manager.screenshot(target)
        print("Idle for 31 seconds", flush=True)
        time.sleep(31)
        assert manager.connected
        manager.navigate(target, url)
        for _ in range(100):
            if manager.evaluate(target, "window.hits")["value"] == 0:
                break
            time.sleep(0.05)
        for kind in ("keyDown", "keyUp"):
            manager.call(target, "Input.dispatchKeyEvent", {
                "type": kind, "key": " ", "code": "Space", "windowsVirtualKeyCode": 32,
            })
        assert manager.evaluate(target, "window.hits")["value"] == 1
        after = manager.screenshot(target)
        assert after.startswith(b"\x89PNG") and before != after
        capture = tmp_path / "browser-qa.png"
        capture.write_bytes(after)
        print("Capture:", capture, flush=True)
        events = manager.console_events(target)
        assert any("shot 1" in event["text"] for event in events)
        assert not any(event["type"] == "error" for event in events)
        runtime, session_id = manager._session_for(target, "Runtime")
        assert not runtime.events(session_id, methods={"Runtime.exceptionThrown"})
        manager.connect(endpoint)
        assert manager.evaluate(target, "window.hits")["value"] == 1
        manager._session.close()  # dead transport while the owned process is still alive
        manager.launch()
        assert process.poll() is not None
        assert manager.connected
        manager.close()
        assert not manager.connected and manager._process is None
    finally:
        manager.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
