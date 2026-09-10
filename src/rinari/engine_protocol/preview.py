"""Engine-owned web previews. Static content never receives desktop RPC access."""

from __future__ import annotations

import contextlib
import http.client
import json
import mimetypes
import re
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from rinari.application.session_service import profile_for_session
from rinari.engine_protocol.errors import EngineProtocolError
from rinari.policy.engine import PermissionProfile, is_sensitive_file
from rinari.tools.native.process import ProcessRegistry

MAX_ASSET = 16 * 1024 * 1024
WEB_EXTENSIONS = {
    ".html",
    ".htm",
    ".css",
    ".js",
    ".mjs",
    ".json",
    ".map",
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".avif",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".wasm",
    ".mp3",
    ".ogg",
    ".wav",
    ".mp4",
    ".webm",
    ".txt",
}
RELOAD_SCRIPT = b"""<script>
(() => { if (window !== window.top) return; let revision; setInterval(async () => { try {
const response = await fetch('/_rinari/revision', {cache:'no-store'});
if (!response.ok) return;
const next = (await response.json()).revision;
if (revision !== undefined && next !== revision) location.reload();
revision = next;
} catch {} }, 1000); })();
</script>"""


@dataclass
class Preview:
    id: str
    session_id: str
    root: Path
    path: Path
    url: str
    kind: str = "static"
    command: str | None = None
    process_id: str | None = None
    server: ThreadingHTTPServer | None = None
    revision: int = 0
    touched: float = field(default_factory=time.monotonic)
    files: dict[Path, tuple[int, int] | None] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def check(self) -> int:
        with self.lock:
            changed = False
            for path, previous in list(self.files.items()):
                current = stamp(path)
                if previous != current:
                    self.files[path] = current
                    changed = True
            if changed:
                self.revision += 1
            self.touched = time.monotonic()
            return self.revision


def stamp(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return None


class WebPreviews:
    def __init__(self, desktop):
        self.desktop = desktop
        self.processes = ProcessRegistry()
        self._items: dict[str, Preview] = {}
        self._lock = threading.RLock()
        self._closed = threading.Event()
        threading.Thread(target=self._reap, daemon=True).start()

    def start(self, params):
        # Serialize with workspace moves; the original resolved root is retained.
        with self.desktop.server._turns._lock, self._lock:
            path, workspace = self.desktop.resolve_file(params)
            if path.suffix.lower() not in {".html", ".htm"}:
                raise EngineProtocolError("UNSUPPORTED_FILE", "Web preview requires an HTML file.")
            if len(self._items) >= 8:
                raise EngineProtocolError(
                    "PREVIEW_LIMIT", "Close a preview before opening another."
                )
            session = self.desktop.services.sessions.show(params["session_id"])
            identity = secrets.token_hex(16)
            item = Preview(identity, session.id, path.parent, path, "")
            item.files[path] = stamp(path)
            dev_url = params.get("dev_url")
            if params.get("run_dev") not in (None, False, True):
                raise EngineProtocolError("INVALID_PARAMS", "run_dev must be a boolean.")
            if dev_url:
                try:
                    parsed = urlsplit(dev_url) if isinstance(dev_url, str) else None
                    valid = (
                        parsed
                        and parsed.scheme == "http"
                        and parsed.hostname in {"127.0.0.1", "localhost"}
                        and not parsed.username
                        and not parsed.password
                        and parsed.port
                    )
                except ValueError:
                    valid = False
                if not valid:
                    raise EngineProtocolError(
                        "INVALID_PARAMS", "Use an HTTP localhost URL with a port."
                    )
                item.kind, item.url = "development", str(dev_url)
            else:
                detected = self._project_server(path.parent, workspace)
                if detected:
                    project, script, script_name = detected
                    if not re.match(r"^vite(?:\s|$)", script.strip()):
                        raise EngineProtocolError(
                            "PREVIEW_SERVER_REQUIRED",
                            "Conecta la URL local del servidor de desarrollo de este proyecto.",
                        )
                    command = (
                        f"npm run {script_name} -- --host 127.0.0.1 --port <auto> --strictPort"
                    )
                    if params.get("run_dev") is not True:
                        raise EngineProtocolError(
                            "PREVIEW_DEV_REQUIRED",
                            "Este proyecto usa Vite.",
                            details={"command": command, "cwd": str(project)},
                        )
                    if profile_for_session(session) is PermissionProfile.READ_ONLY:
                        raise EngineProtocolError(
                            "PERMISSION_DENIED",
                            "Cambia a BUILD con permisos de workspace para iniciar Vite, "
                            "o usa un servidor existente.",
                        )
                    # Same process backend as process.start, with bounded output and tree cleanup.
                    with socket.socket() as reservation:
                        reservation.bind(("127.0.0.1", 0))
                        port = reservation.getsockname()[1]
                    item.kind, item.root = "development", project
                    item.command = command.replace("<auto>", str(port))
                    item.url = (
                        f"http://127.0.0.1:{port}/{quote(path.relative_to(project).as_posix())}"
                    )
                    item.process_id = self.processes.start(item.command, str(project))
                else:
                    item.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler(item))
                    item.server.daemon_threads = True
                    port = item.server.server_port
                    # A random .localhost host isolates each preview and rejects DNS rebinding.
                    item.url = f"http://{identity}.localhost:{port}/{quote(path.name)}"
                    item.files[path] = stamp(path)
                    threading.Thread(target=item.server.serve_forever, daemon=True).start()
            self._items[identity] = item
            return self._describe(item)

    @staticmethod
    def _project_server(directory: Path, workspace: Path) -> tuple[Path, str, str] | None:
        while directory.is_relative_to(workspace):
            package = directory / "package.json"
            try:
                if package.stat().st_size < 512 * 1024:
                    data = json.loads(package.read_text(encoding="utf-8-sig"))
                    scripts = data.get("scripts", {})
                    script = scripts.get("dev") or scripts.get("start", "")
                    if isinstance(script, str) and script.strip():
                        return directory, script, "dev" if scripts.get("dev") else "start"
            except (OSError, ValueError, AttributeError):
                pass
            if directory == workspace:
                break
            directory = directory.parent
        return None

    def _need(self, params):
        if not isinstance(params.get("preview_id"), str):
            raise EngineProtocolError("INVALID_PARAMS", "preview_id is required.")
        with self._lock:
            item = self._items.get(params.get("preview_id"))
        if not item or item.session_id != params.get("session_id"):
            raise EngineProtocolError("NOT_FOUND", "Preview ended. Open the file again.")
        return item

    def status(self, params):
        return self._describe(self._need(params))

    def _describe(self, item):
        ready, error = True, None
        if item.kind == "development":
            handle = self.processes.get(item.process_id) if item.process_id else None
            if handle and handle.process.poll() is not None:
                error = (handle.stderr.text() or handle.stdout.text() or "El servidor terminó.")[
                    -2000:
                ]
                ready = False
            else:
                parsed = urlsplit(item.url)
                connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=0.25)
                try:
                    connection.request("GET", parsed.path or "/")
                    ready = connection.getresponse().status < 500
                except OSError:
                    ready = False
                finally:
                    connection.close()
        return {
            "preview_id": item.id,
            "session_id": item.session_id,
            "url": item.url,
            "kind": item.kind,
            "ready": ready,
            "revision": item.check(),
            "command": item.command,
            "error": error,
        }

    def stop(self, params):
        item = self._need(params)
        self._stop(item.id)
        return {"stopped": True}

    def _stop(self, identity):
        with self._lock:
            item = self._items.pop(identity, None)
        if item:
            if item.server:
                item.server.shutdown()
                item.server.server_close()
            if item.process_id:
                handle = self.processes.get(item.process_id)
                if handle:
                    self.processes.kill(handle)

    def stop_session(self, session_id):
        with self._lock:
            identities = [item.id for item in self._items.values() if item.session_id == session_id]
        for identity in identities:
            self._stop(identity)

    def busy(self, session_id):
        with self._lock:
            for item in self._items.values():
                if item.session_id == session_id and item.process_id:
                    handle = self.processes.get(item.process_id)
                    if handle and handle.process.poll() is None:
                        return True
        return False

    def close(self):
        self._closed.set()
        with self._lock:
            identities = list(self._items)
        for identity in identities:
            self._stop(identity)

    def _reap(self):
        while not self._closed.wait(30):
            with self._lock:
                stale = [
                    item.id
                    for item in self._items.values()
                    if time.monotonic() - item.touched > 600
                ]
            for identity in stale:
                self._stop(identity)

    def _handler(self, item):
        private = self.desktop.services.changes.blobs.root.resolve()

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def log_message(self, *_args):
                pass  # Never write HTTP access logs to protocol stdout.

            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                if self.headers.get("Host") != urlsplit(item.url).netloc:
                    self.send_error(403)
                    return
                raw = unquote(urlsplit(self.path).path)
                if raw == "/_rinari/revision":
                    self.reply(json.dumps({"revision": item.check()}).encode(), "application/json")
                    return
                try:
                    parts = raw.lstrip("/").split("/")
                    if any(p.startswith(".") or ":" in p or "\\" in p for p in parts):
                        raise PermissionError()
                    path = (item.root / raw.lstrip("/")).resolve()
                    if not path.is_relative_to(item.root) or path.is_relative_to(private):
                        raise PermissionError()
                    if path.is_dir():
                        path = (path / "index.html").resolve()
                    if (
                        not path.is_relative_to(item.root)
                        or path.is_relative_to(private)
                        or is_sensitive_file(path)
                        or path.suffix.lower() not in WEB_EXTENSIONS
                    ):
                        raise PermissionError()
                    with path.open("rb") as stream:
                        data = stream.read(MAX_ASSET + 1)
                    if len(data) > MAX_ASSET:
                        self.send_error(413)
                        return
                    with item.lock:
                        if len(item.files) < 2048:
                            item.files.setdefault(path, stamp(path))
                    kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                    if path.suffix.lower() in {".js", ".mjs"}:
                        kind = "text/javascript"
                    if path.suffix.lower() in {".html", ".htm"}:
                        kind = "text/html; charset=utf-8"
                        data += RELOAD_SCRIPT.replace(
                            b"let revision;", f"let revision = {item.check()};".encode()
                        )
                    self.reply(data, kind)
                except PermissionError:
                    self.send_error(403)
                except (OSError, ValueError):
                    self.send_error(404)

            def reply(self, data, kind):
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    "sandbox allow-scripts allow-same-origin allow-forms; object-src 'none'",
                )
                self.end_headers()
                if self.command != "HEAD":
                    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                        self.wfile.write(data)

        return Handler
