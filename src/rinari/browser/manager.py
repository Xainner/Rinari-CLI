"""BrowserManager (phase 5): the session-scoped owner of the CDP connection.

Responsibilities:

- endpoint discovery/config: explicit endpoint (test/wiring seam) >
  ``RINARI_BROWSER_CDP`` env > launch a managed Chromium-family browser.
- browser lifecycle: connect to an existing CDP endpoint or launch a managed
  headless subprocess with an isolated per-session profile
  (``<home>/browser/profiles/<session_id>``) and ownership of it (only a
  process Rinari started is ever killed).
- bounded page operations over flattened target sessions (evaluate, snapshot,
  a11y, screenshot, input, cookies, console, network, uploads, downloads).

Security model (AGENTS.md 11, Browser policy in TODO.md):

- network effect of *navigations* is gated by the network policy in the tool
  layer (tools classify open/navigate as network.outbound on the URL);
- uploads must resolve through the session sandbox (tool layer) and their
  provenance (path, size, sha256) is returned;
- downloads only ever land in the session artifact directory, with the
  suggested name and integrity data returned as provenance;
- cookie values never leave this layer unredacted (they are credentials).
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import socket
import subprocess
import threading
import time
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rinari.browser.cdp import CdpError, CdpSession
from rinari.browser.discovery import CANDIDATES, find_browser

ENV_ENDPOINT = "RINARI_BROWSER_CDP"
ENV_COMMAND = "RINARI_BROWSER_COMMAND"
DEFAULT_COMMAND_CANDIDATES = CANDIDATES

MAX_SNAPSHOT_CHARS = 256 * 1024
MAX_SCREENSHOT_BYTES = 4 * 1024 * 1024
MAX_A11Y_NODES = 500
MAX_TYPED_CHARS = 200

_CDP_TO_BROWSER = {
    "CDP_ERROR": "BROWSER_PROTOCOL",
    "CDP_TIMEOUT": "BROWSER_TIMEOUT",
    "CDP_DISCONNECTED": "BROWSER_DISCONNECTED",
    "CDP_CLOSED": "BROWSER_DISCONNECTED",
}


class BrowserError(Exception):
    """Structured browser failure (maps to tool error codes)."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        # BROWSER_DISCONNECTED | BROWSER_DEPENDENCY | BROWSER_LAUNCH_FAILED |
        # BROWSER_TIMEOUT | BROWSER_PROTOCOL | JS_ERROR | TARGET_NOT_FOUND |
        # RESOURCE_EXHAUSTED | CANCELLED
        self.code = code
        self.message = message
        self.retryable = retryable


def _cdp_to_browser(exc: CdpError) -> BrowserError:
    return BrowserError(_CDP_TO_BROWSER[exc.code], exc.message, retryable=exc.retryable)


@dataclass(frozen=True, slots=True)
class DownloadRef:
    path: str
    bytes: int
    suggested_name: str


# -- JS snippets (% placeholders are filled with json.dumps values) ----------

_JS_POINT = (
    "(() => { const el = document.querySelector(%s);"
    " if (!el) throw new Error('selector not found');"
    " el.scrollIntoView({block: 'center', inline: 'center'});"
    " const r = el.getBoundingClientRect();"
    " return {x: r.x + r.width / 2, y: r.y + r.height / 2}; })()"
)
_JS_FOCUS = (
    "(() => { const el = document.querySelector(%s);"
    " if (!el) throw new Error('selector not found');"
    " el.focus(); return true; })()"
)
_JS_FILL = (
    "(() => { const el = document.querySelector(%s);"
    " if (!el) throw new Error('selector not found');"
    " const proto = el instanceof HTMLTextAreaElement"
    " ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;"
    " const d = Object.getOwnPropertyDescriptor(proto, 'value');"
    " if (d && d.set) d.set.call(el, %s); else el.value = %s;"
    " el.dispatchEvent(new Event('input', {bubbles: true}));"
    " el.dispatchEvent(new Event('change', {bubbles: true}));"
    " return true; })()"
)
_JS_SELECT = (
    "(() => { const el = document.querySelector(%s);"
    " if (!el) throw new Error('selector not found');"
    " if (!(el instanceof HTMLSelectElement)) throw new Error('not a select element');"
    " el.value = %s;"
    " if (el.selectedIndex < 0) throw new Error('no matching option');"
    " el.dispatchEvent(new Event('input', {bubbles: true}));"
    " el.dispatchEvent(new Event('change', {bubbles: true}));"
    " return {selected: el.value}; })()"
)
_JS_CHECK = (
    "(() => { const el = document.querySelector(%s);"
    " if (!el) throw new Error('selector not found');"
    " el.checked = %s;"
    " el.dispatchEvent(new Event('input', {bubbles: true}));"
    " el.dispatchEvent(new Event('change', {bubbles: true}));"
    " return el.checked; })()"
)
_JS_VIEWPORT = "[window.innerWidth, window.innerHeight]"


class BrowserManager:
    def __init__(
        self,
        *,
        session_id: str,
        home_root: Path,
        endpoint: str | None = None,
        command: str | None = None,
        headless: bool = True,
        timeout_s: float = 15.0,
    ) -> None:
        self.session_id = session_id
        self._home_root = Path(home_root)
        self._endpoint_cfg = endpoint
        self._command_cfg = command
        self._headless = headless
        self._timeout_s = timeout_s
        self._session: CdpSession | None = None
        self._process: subprocess.Popen | None = None
        self._endpoint: str | None = None
        self._attached: dict[str, str] = {}
        self._domains: set[tuple[str, str]] = set()
        self._download_dir: Path | None = None
        self._download_base: set[str] = set()
        self._executable: str | None = None
        self._last_error: str | None = None
        self._last_exit_code: int | None = None
        self._stderr = bytearray()
        self._stderr_lock = threading.Lock()
        self._stderr_reader: threading.Thread | None = None
        self._generation = 0
        self._elements: dict[str, tuple[str | None, int]] = {}
        # A desktop turn builds a new runtime. Never share Chromium's singleton
        # profile with a previous runtime (or another engine process).
        self._profile_instance = uuid.uuid4().hex

    # -- state ----------------------------------------------------------------

    @property
    def connected(self) -> bool:
        session = self._session
        return session is not None and not session.closed and session.fatal is None

    @property
    def profile_dir(self) -> Path:
        return self._home_root / "browser" / "profiles" / self.session_id / self._profile_instance

    def status(self) -> dict[str, Any]:
        state = (
            "connected"
            if self.connected
            else ("disconnected" if self._session is None or not self._session.fatal else "fatal")
        )
        return {
            "state": state,
            "endpoint": self._endpoint,
            "managed": self._process is not None,
            "headless": self._headless,
            "targets": len(self.targets()) if self.connected else 0,
            "diagnostics": self.diagnostics(),
        }

    def diagnostics(self) -> dict[str, Any]:
        with self._stderr_lock:
            stderr = self._stderr.decode("utf-8", "replace")
        return {"executable": self._executable, "last_error": self._last_error,
                "profile_dir": str(self.profile_dir), "headless": self._headless,
                "stderr": stderr, "stderr_limit_bytes": 16384,
                "process_exit_code": self._process.poll() if self._process
                else self._last_exit_code}

    def _drain_stderr(self, process: subprocess.Popen) -> None:
        assert process.stderr is not None
        try:
            while chunk := process.stderr.read1(1024):
                with self._stderr_lock:
                    self._stderr.extend(chunk)
                    del self._stderr[:-16384]
        finally:
            process.stderr.close()

    def _disconnect(self) -> None:
        if self._session is not None:
            self._session.close()
        self._session = None
        self._attached.clear()
        self._domains.clear()
        self._download_dir = None
        self._download_base.clear()
        self._generation += 1
        self._elements.clear()

    def _raise_if_cancelled(self, cancelled: Callable[[], bool] | None) -> None:
        fn = cancelled if cancelled is not None else None
        if fn is not None and fn():
            raise BrowserError("CANCELLED", "browser operation cancelled")

    # -- lifecycle ------------------------------------------------------------

    def connect(self, endpoint: str | None = None) -> str:
        target = endpoint or self._endpoint_cfg or os.environ.get(ENV_ENDPOINT) or self._endpoint
        if not target:
            raise BrowserError(
                "BROWSER_DEPENDENCY",
                "No CDP endpoint available; pass a ws:// endpoint or use browser.launch",
            )
        if self._endpoint and target != self._endpoint:
            self.close()
        else:
            self._disconnect()
        if not self._ensure_session(target):
            raise BrowserError(
                "BROWSER_DEPENDENCY",
                f"Could not reach the CDP endpoint {target}: {self._last_error}",
                retryable=True,
            )
        return self._endpoint or target

    def launch(self, command: str | None = None, port: int | None = None) -> str:
        if self.connected:
            return self._endpoint or ""
        external = self._endpoint_cfg or os.environ.get(ENV_ENDPOINT)
        if external and command is None:
            return self.connect(external)
        self.close()
        configured = command or self._command_cfg or os.environ.get(ENV_COMMAND)
        cmd = find_browser(configured)
        if not cmd:
            raise BrowserError(
                "BROWSER_DEPENDENCY",
                f"Browser executable unavailable ({configured or 'automatic discovery'}); "
                "install Edge/Chromium/Chrome or set "
                f"{ENV_COMMAND} (searched: {', '.join(DEFAULT_COMMAND_CANDIDATES)})",
            )
        port = port or _pick_free_port()
        profile = self.profile_dir
        profile.mkdir(parents=True, exist_ok=True)
        argv = [
            cmd,
            *(["--headless=new"] if self._headless else []),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-sync",
            "about:blank",
        ]
        try:
            self._executable = cmd
            self._last_error = None
            self._last_exit_code = None
            with self._stderr_lock:
                self._stderr.clear()
            self._process = subprocess.Popen(
                argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            self._stderr_reader = threading.Thread(
                target=self._drain_stderr, args=(self._process,), daemon=True,
                name="rinari-browser-stderr",
            )
            self._stderr_reader.start()
        except OSError as exc:
            raise BrowserError(
                "BROWSER_LAUNCH_FAILED", f"Could not start the browser: {exc}"
            ) from exc
        endpoint = f"ws://127.0.0.1:{port}"
        if not self._ensure_session(endpoint, deadline_s=25.0):
            self._kill_process()
            raise BrowserError(
                "BROWSER_LAUNCH_FAILED",
                f"Browser did not open the CDP endpoint {endpoint}: {self._last_error}",
            )
        return endpoint

    def close(self) -> dict[str, Any]:
        closed: list[str] = []
        if self._session is not None:
            try:
                self._session.close()
            finally:
                self._session = None
                self._attached.clear()
                self._domains.clear()
                closed.append("cdp-session")
        if self._process is not None:
            self._kill_process()
            closed.append("browser-process")
        self._endpoint = None
        self._download_dir = None
        self._download_base.clear()
        self._generation += 1
        self._elements.clear()
        return {"closed": closed}

    def _kill_process(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        # Edge may hand off to another process and exit its original launcher.
        # CDP Browser.close shuts down the owned browser and releases its profile
        # even when the original Popen PID has already exited.
        if self._endpoint:
            control = None
            try:
                control = CdpSession(self._browser_ws_url(self._endpoint), timeout_s=2)
                control.start()
                control.send("Browser.close")
                end = time.monotonic() + 3
                while time.monotonic() < end:
                    try:
                        self._browser_ws_url(self._endpoint)
                    except Exception:
                        break
                    time.sleep(0.05)
            except Exception:
                pass  # Fall back to terminating the owned process tree.
            finally:
                if control is not None:
                    control.close()
        if process.poll() is None and os.name == "nt":
            # Chromium is a process tree. Killing only its parent can leave a
            # profile lock behind and make the next launch immediately exit.
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run(
                    ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
        if process.poll() is None:
            with contextlib.suppress(OSError):
                process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                process.kill()
            process.wait(timeout=5)
        if self._stderr_reader is not None:
            self._stderr_reader.join(timeout=2)
            self._stderr_reader = None
        self._last_exit_code = process.poll()

    def _ensure_session(self, endpoint: str, deadline_s: float = 5.0) -> bool:
        end = time.monotonic() + deadline_s
        while time.monotonic() < end:
            session = None
            try:
                ws_url = self._browser_ws_url(endpoint)
                session = CdpSession(ws_url, timeout_s=min(self._timeout_s, deadline_s))
                session.start()
                session.send("Target.getTargets")
                self._session = session
                self._endpoint = endpoint
                self._last_error = None
                return True
            except Exception as exc:
                self._last_error = str(exc)[:2048]
                if session is not None:
                    session.close()
                if self._process is not None and self._process.poll() not in (None, 0):
                    return False
                time.sleep(0.25)
        return False

    @staticmethod
    def _browser_ws_url(endpoint: str) -> str:
        if "/devtools/" in endpoint:
            return endpoint
        http = endpoint.strip().replace("ws://", "http://").replace("wss://", "https://")
        if http.endswith("/"):
            http = http.rstrip("/")
        with urllib.request.urlopen(f"{http}/json/version", timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        url = payload.get("webSocketDebuggerUrl")
        if not url:
            raise BrowserError("BROWSER_PROTOCOL", "CDP endpoint has no webSocketDebuggerUrl")
        return url

    # -- targets ---------------------------------------------------------------

    def _require_session(self) -> CdpSession:
        session = self._session
        if session is None:
            raise BrowserError(
                "BROWSER_DISCONNECTED", "Not connected to a browser; use browser.launch"
            )
        fatal = session.fatal
        if fatal is not None:
            raise BrowserError("BROWSER_DISCONNECTED", f"CDP session is dead: {fatal.message}")
        return session

    def _session_for(
        self, target_id: str | None, domain: str | None = None
    ) -> tuple[CdpSession, str]:
        session = self._require_session()
        if target_id is None:
            infos = [
                info
                for info in session.send("Target.getTargets").get("targetInfos", [])
                if info.get("type") == "page"
            ]
            if not infos:
                raise BrowserError("TARGET_NOT_FOUND", "no page target; open one first")
            target_id = infos[0]["targetId"]
        if target_id not in self._attached:
            result = session.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
            self._attached[target_id] = result["sessionId"]
        if domain is not None and (target_id, domain) not in self._domains:
            session.send(f"{domain}.enable", {}, session_id=self._attached[target_id])
            self._domains.add((target_id, domain))
        return session, self._attached[target_id]

    def targets(self) -> list[dict[str, Any]]:
        session = self._require_session()
        result = session.send("Target.getTargets")
        out: list[dict[str, Any]] = []
        for info in result.get("targetInfos", []):
            if info.get("type") == "page":
                out.append(
                    {
                        "target_id": info.get("targetId"),
                        "title": info.get("title") or "",
                        "url": info.get("url") or "",
                    }
                )
        return out

    def new_page(self, url: str = "about:blank") -> dict[str, Any]:
        session = self._require_session()
        result = session.send("Target.createTarget", {"url": url})
        return {"target_id": result["targetId"], "url": url}

    def close_page(self, target_id: str) -> dict[str, Any]:
        session = self._require_session()
        session.send("Target.closeTarget", {"targetId": target_id})
        self._attached.pop(target_id, None)
        return {"closed": target_id}

    # -- page operations ---------------------------------------------------------

    def call(
        self,
        target_id: str | None,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        domain: str | None = None,
        timeout_s: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        self._raise_if_cancelled(cancelled)
        session, session_id = self._session_for(target_id, domain)
        try:
            return session.send(method, params, session_id=session_id, timeout_s=timeout_s)
        except CdpError as exc:
            raise _cdp_to_browser(exc) from exc

    def evaluate(
        self,
        target_id: str | None,
        expression: str,
        *,
        await_promise: bool = False,
        max_chars: int = 8000,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        self._raise_if_cancelled(cancelled)
        session, session_id = self._session_for(target_id, "Runtime")
        self._raise_if_cancelled(cancelled)
        try:
            result = session.send(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True, "awaitPromise": await_promise},
                session_id=session_id,
                timeout_s=10.0,
            )
        except CdpError as exc:
            raise _cdp_to_browser(exc) from exc
        if result.get("exceptionDetails"):
            raise BrowserError(
                "JS_ERROR", f"JavaScript error: {_exception_text(result['exceptionDetails'])}"
            )
        value = result.get("result", {}).get("value")
        out: dict[str, Any] = {"value": value}
        text = (
            value
            if isinstance(value, str)
            else ("" if value is None else json.dumps(value, ensure_ascii=False, default=str))
        )
        if isinstance(text, str) and len(text) > max_chars:
            out["truncated"] = True
        return out

    def navigate(
        self,
        target_id: str | None,
        url: str,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        self._raise_if_cancelled(cancelled)
        result = self.call(
            target_id,
            "Page.navigate",
            {"url": url},
            domain="Page",
            timeout_s=30.0,
            cancelled=cancelled,
        )
        if result.get("errorText"):
            raise BrowserError("BROWSER_PROTOCOL", f"Navigation failed: {result['errorText']}")
        return {"frame_id": result.get("frameId"), "loader_id": result.get("loaderId"), "url": url}

    def snapshot(
        self,
        target_id: str | None,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        out = self.evaluate(
            target_id,
            'document.documentElement ? document.documentElement.outerHTML : ""',
            max_chars=MAX_SNAPSHOT_CHARS + 1,
            cancelled=cancelled,
        )
        html = out.get("value") or ""
        if not isinstance(html, str):
            html = json.dumps(html, ensure_ascii=False, default=str)
        return {
            "html": html[:MAX_SNAPSHOT_CHARS],
            "bytes": len(html.encode("utf-8")),
            "truncated": len(html) > MAX_SNAPSHOT_CHARS,
        }

    def a11y_tree(
        self,
        target_id: str | None,
        *,
        max_nodes: int = MAX_A11Y_NODES,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        result = self.call(
            target_id,
            "Accessibility.getFullAXTree",
            {},
            domain="Accessibility",
            cancelled=cancelled,
        )
        nodes = result.get("nodes", [])
        kept: list[dict[str, Any]] = []
        self._elements = {key: value for key, value in self._elements.items()
                          if value[0] != target_id}
        for node in nodes[:max_nodes]:
            backend_id = node.get("backendDOMNodeId")
            element_id = f"node:{self._generation}:{backend_id}" if backend_id else None
            if element_id:
                self._elements[element_id] = (target_id, int(backend_id))
            kept.append(
                {
                    "node_id": node.get("nodeId"),
                    "element_id": element_id,
                    "role": (node.get("role") or {}).get("value"),
                    "name": _short_name((node.get("name") or {}).get("value")),
                    "value": _short_name(node.get("value")),
                    "ignored": bool(node.get("ignored")),
                    "child_ids": node.get("childIds") or [],
                }
            )
        return {"nodes": kept, "total": len(nodes), "truncated": len(nodes) > max_nodes}

    def screenshot(
        self,
        target_id: str | None,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> bytes:
        result = self.call(
            target_id,
            "Page.captureScreenshot",
            {"format": "png"},
            domain="Page",
            cancelled=cancelled,
        )
        data = base64.b64decode(result.get("data", ""))
        if len(data) > MAX_SCREENSHOT_BYTES:
            raise BrowserError(
                "RESOURCE_EXHAUSTED", f"Screenshot exceeds budget: {len(data)} bytes"
            )
        return data

    # -- input -------------------------------------------------------------------

    def _point(
        self,
        target_id: str | None,
        selector: str | None,
        x: float | None,
        y: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> tuple[float, float]:
        if x is None and y is None:
            if not selector:
                raise BrowserError("INVALID_ARGUMENT", "Provide a selector or x/y coordinates")
            if selector.startswith("node:"):
                reference = self._elements.get(selector)
                if reference is None or reference[0] != target_id:
                    raise BrowserError(
                        "INVALID_ARGUMENT", "Use an element_id from a fresh snapshot"
                    )
                node_id = reference[1]
                box = self.call(
                    target_id,
                    "DOM.getBoxModel",
                    {"backendNodeId": node_id},
                    domain="DOM",
                    cancelled=cancelled,
                )
                quad = box.get("model", {}).get("content", [])
                if len(quad) != 8:
                    raise BrowserError(
                        "TARGET_NOT_FOUND", "Element has no visible box; refresh snapshot"
                    )
                return sum(quad[::2]) / 4, sum(quad[1::2]) / 4
            out = self.evaluate(target_id, _JS_POINT % json.dumps(selector), cancelled=cancelled)
            point = out.get("value") or {}
            return float(point["x"]), float(point["y"])
        if x is None or y is None:
            raise BrowserError("INVALID_ARGUMENT", "x and y must be given together")
        return float(x), float(y)

    def click(
        self,
        target_id: str | None,
        *,
        selector: str | None = None,
        x: float | None = None,
        y: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        px, py = self._point(target_id, selector, x, y, cancelled)
        for event in (
            {"type": "mouseMoved", "x": px, "y": py, "button": "none"},
            {"type": "mousePressed", "x": px, "y": py, "button": "left", "clickCount": 1},
            {"type": "mouseReleased", "x": px, "y": py, "button": "left", "clickCount": 1},
        ):
            self.call(target_id, "Input.dispatchMouseEvent", event, cancelled=cancelled)
        return {"clicked": {"x": px, "y": py}}

    def fill(
        self,
        target_id: str | None,
        selector: str,
        value: str,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        self.evaluate(
            target_id,
            _JS_FILL % (json.dumps(selector), json.dumps(value), json.dumps(value)),
            cancelled=cancelled,
        )
        return {"filled": selector, "chars": len(value)}

    def type_text(
        self,
        target_id: str | None,
        selector: str,
        text: str,
        *,
        press_enter: bool = False,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if len(text) > MAX_TYPED_CHARS:
            raise BrowserError(
                "INVALID_ARGUMENT", f"text is limited to {MAX_TYPED_CHARS} characters"
            )
        self.evaluate(target_id, _JS_FOCUS % json.dumps(selector), cancelled=cancelled)
        for char in text:
            code = _virtual_key_code(char)
            for event_type in ("keyDown", "keyUp"):
                self.call(
                    target_id,
                    "Input.dispatchKeyEvent",
                    {"type": event_type, "text": char, "key": char, "windowsVirtualKeyCode": code},
                    cancelled=cancelled,
                )
        if press_enter:
            for event_type in ("keyDown", "keyUp"):
                self.call(
                    target_id,
                    "Input.dispatchKeyEvent",
                    {"type": event_type, "key": "Enter", "windowsVirtualKeyCode": 13},
                    cancelled=cancelled,
                )
        return {"typed": len(text), "enter": press_enter}

    def select_option(
        self,
        target_id: str | None,
        selector: str,
        value: str,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        out = self.evaluate(
            target_id, _JS_SELECT % (json.dumps(selector), json.dumps(value)), cancelled=cancelled
        )
        return {"selected": (out.get("value") or {}).get("selected", value)}

    def set_checked(
        self,
        target_id: str | None,
        selector: str,
        checked: bool,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        out = self.evaluate(
            target_id,
            _JS_CHECK % (json.dumps(selector), "true" if checked else "false"),
            cancelled=cancelled,
        )
        return {"checked": bool(out.get("value"))}

    def scroll(
        self,
        target_id: str | None,
        *,
        dx: float = 0.0,
        dy: float = 0.0,
        x: float | None = None,
        y: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if x is None or y is None:
            out = self.evaluate(target_id, _JS_VIEWPORT, cancelled=cancelled)
            size = out.get("value") or {}
            if isinstance(size, list):
                x = x if x is not None else size[0] / 2
                y = y if y is not None else size[1] / 2
            else:
                x = x if x is not None else float(size.get("w", 800)) / 2
                y = y if y is not None else float(size.get("h", 600)) / 2
        self.call(
            target_id,
            "Input.dispatchMouseEvent",
            {
                "type": "mouseWheel",
                "x": float(x),
                "y": float(y),
                "deltaX": float(dx),
                "deltaY": float(dy),
            },
            cancelled=cancelled,
        )
        return {"scrolled": {"dx": float(dx), "dy": float(dy)}}

    def drag(
        self,
        target_id: str | None,
        *,
        from_selector: str | None = None,
        from_x: float | None = None,
        from_y: float | None = None,
        to_x: float,
        to_y: float,
        steps: int = 5,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if steps < 1:
            raise BrowserError("INVALID_ARGUMENT", "steps must be >= 1")
        sx, sy = self._point(target_id, from_selector, from_x, from_y, cancelled)
        self.call(
            target_id,
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": sx, "y": sy, "button": "left", "clickCount": 1},
            cancelled=cancelled,
        )
        for i in range(1, steps + 1):
            t = i / steps
            self.call(
                target_id,
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseMoved",
                    "x": sx + (to_x - sx) * t,
                    "y": sy + (to_y - sy) * t,
                    "button": "left",
                },
                cancelled=cancelled,
            )
        self.call(
            target_id,
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": to_x, "y": to_y, "button": "left", "clickCount": 1},
            cancelled=cancelled,
        )
        return {"from": {"x": sx, "y": sy}, "to": {"x": to_x, "y": to_y}}

    # -- upload / download ---------------------------------------------------------

    def set_file_input(
        self,
        target_id: str | None,
        selector: str,
        file_path: Path,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        self._raise_if_cancelled(cancelled)
        session, session_id = self._session_for(target_id, "Runtime")
        try:
            result = session.send(
                "Runtime.evaluate",
                {"expression": f"document.querySelector({json.dumps(selector)})"},
                session_id=session_id,
                timeout_s=10.0,
            )
        except CdpError as exc:
            raise _cdp_to_browser(exc) from exc
        if result.get("exceptionDetails"):
            raise BrowserError("JS_ERROR", "JavaScript error picking the input element")
        object_id = (result.get("result") or {}).get("objectId")
        if not object_id:
            raise BrowserError("TARGET_NOT_FOUND", "file input not found for the selector")
        try:
            session.send("DOM.enable", {}, session_id=session_id)
            session.send(
                "DOM.setFileInputFiles",
                {"files": [str(Path(file_path).resolve())], "objectId": object_id},
                session_id=session_id,
            )
        except CdpError as exc:
            raise _cdp_to_browser(exc) from exc
        return {"selector": selector, "file": str(file_path)}

    def begin_download(self, download_dir: Path) -> Path:
        session = self._require_session()
        target = Path(download_dir)
        target.mkdir(parents=True, exist_ok=True)
        session.send(
            "Browser.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": str(target), "eventsEnabled": True},
        )
        self._download_dir = target
        self._download_base = {p.name for p in target.iterdir() if p.is_file()}
        return target

    def poll_download(
        self,
        *,
        timeout_s: float = 15.0,
        cancelled: Callable[[], bool] | None = None,
    ) -> DownloadRef | None:
        directory = self._download_dir
        if directory is None:
            raise BrowserError("BROWSER_PROTOCOL", "downloads are not enabled yet")
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            self._raise_if_cancelled(cancelled)
            try:
                entries = sorted(directory.iterdir())
            except OSError:
                entries = []
            for path in entries:
                if path.name in self._download_base or not path.is_file():
                    continue
                if path.name.endswith(".crdownload") or path.name.endswith(".tmp"):
                    self._raise_if_cancelled(cancelled)
                    time.sleep(0.25)
                    break
                return DownloadRef(
                    path=str(path), bytes=path.stat().st_size, suggested_name=path.name
                )
            time.sleep(0.25)
        return None

    # -- observation -----------------------------------------------------------------

    def console_events(
        self,
        target_id: str | None,
        *,
        limit: int = 100,
        cancelled: Callable[[], bool] | None = None,
    ) -> list[dict[str, Any]]:
        self._raise_if_cancelled(cancelled)
        session, session_id = self._session_for(target_id, "Runtime")
        events = session.events(
            session_id=session_id,
            methods={"Runtime.consoleAPICalled", "Runtime.exceptionThrown"},
            limit=limit,
            wait_s=0.5,
        )
        out: list[dict[str, Any]] = []
        for event in events:
            params = event.get("params", {})
            if event.get("method") == "Runtime.exceptionThrown":
                out.append({"type": "error", "text": _exception_text(
                    params.get("exceptionDetails", {}),
                )})
                continue
            text = " ".join(_arg_text(arg) for arg in params.get("args", []))
            out.append({"type": params.get("type", "log"), "text": text[:500]})
        return out[:limit]

    def network_events(
        self,
        target_id: str | None,
        *,
        limit: int = 100,
        cancelled: Callable[[], bool] | None = None,
    ) -> list[dict[str, Any]]:
        self._raise_if_cancelled(cancelled)
        session, session_id = self._session_for(target_id, "Network")
        events = session.events(
            session_id=session_id,
            methods={
                "Network.requestWillBeSent",
                "Network.responseReceived",
                "Network.loadingFailed",
            },
            limit=limit * 3,
            wait_s=0.5,
        )
        requests: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for event in events:
            params = event.get("params", {})
            request_id = params.get("requestId")
            method = event.get("method")
            if method == "Network.requestWillBeSent":
                request = params.get("request", {})
                requests[request_id] = {
                    "url": request.get("url"),
                    "method": request.get("method"),
                    "type": params.get("type"),
                }
                order.append(request_id)
            elif method == "Network.responseReceived" and request_id in requests:
                response = params.get("response", {})
                requests[request_id]["status"] = response.get("status")
                requests[request_id]["mimeType"] = response.get("mimeType")
            elif method == "Network.loadingFailed" and request_id in requests:
                requests[request_id]["error"] = params.get("errorText")
        return [req for rid, req in ((rid, requests[rid]) for rid in order) if req.get("url")][
            :limit
        ]

    def cookies(
        self,
        target_id: str | None,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> list[dict[str, Any]]:
        self._raise_if_cancelled(cancelled)
        session, session_id = self._session_for(target_id, "Network")
        try:
            result = session.send("Network.getCookies", {}, session_id=session_id)
        except CdpError as exc:
            raise _cdp_to_browser(exc) from exc
        out: list[dict[str, Any]] = []
        for cookie in result.get("cookies", []):
            out.append(
                {
                    "name": cookie.get("name"),
                    "domain": cookie.get("domain"),
                    "path": cookie.get("path"),
                    "http_only": bool(cookie.get("httpOnly")),
                    "secure": bool(cookie.get("secure")),
                    "value": "***",  # cookie values are credentials: redacted
                }
            )
        return out

    def set_cookie(
        self,
        target_id: str | None,
        name: str,
        value: str,
        url: str | None = None,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        self._raise_if_cancelled(cancelled)
        session, session_id = self._session_for(target_id, "Network")
        params: dict[str, Any] = {"name": name, "value": value}
        if url:
            params["url"] = url
        try:
            session.send("Network.setCookie", params, session_id=session_id)
        except CdpError as exc:
            raise _cdp_to_browser(exc) from exc
        return {"set": name}  # the value never comes back out


# -- helpers --------------------------------------------------------------------


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _virtual_key_code(char: str) -> int:
    if len(char) == 1 and 32 <= ord(char) < 127:
        return ord(char)
    return 0


def _exception_text(details: dict[str, Any]) -> str:
    exception = details.get("exception") or {}
    description = exception.get("description") if isinstance(exception, dict) else None
    if description:
        return str(description)[:500]
    return str(details.get("text", ""))[:500]


def _short_name(value: Any) -> str | None:
    return str(value)[:200] if value else None


def _arg_text(arg: dict[str, Any]) -> str:
    arg_type = arg.get("type")
    if arg_type == "string":
        return str(arg.get("value", ""))
    if arg_type == "object":
        return "Object"
    return str(arg.get("value", arg.get("description", arg_type or "")))


__all__ = [
    "DEFAULT_COMMAND_CANDIDATES",
    "ENV_COMMAND",
    "ENV_ENDPOINT",
    "BrowserError",
    "BrowserManager",
    "DownloadRef",
]
