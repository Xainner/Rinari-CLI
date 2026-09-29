"""Local HTML files, served read-only on loopback instead of opened as file://.

`file://` stays refused in the browser: a page loaded that way could read the
disk. Opening a local page is still common (a generated report, a static site
being built), and the agent used to start its own static server by hand. Here
the Engine serves the file's folder itself:

- bound to 127.0.0.1 on a free port, GET/HEAD only, no directory listings;
- under a random path prefix, so another page cannot guess the address;
- dot-files and dot-folders (``.env``, ``.git``) are never served.
"""

from __future__ import annotations

import http.server
import secrets
import threading
from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from urllib.request import url2pathname

#: What counts as a page. Other local files are not opened in the browser.
PAGE_SUFFIXES = (".html", ".htm", ".xhtml", ".svg")
#: Folders served at once; the oldest stops when a new one is needed.
MAX_FOLDERS = 16


def local_page_path(value: object) -> str | None:
    """The local file a navigation names, or None for a web address.

    Accepts a ``file://`` URL or a plain path ending in a page suffix.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.lower().startswith("file:"):
        parsed = urlparse(text)
        path = url2pathname(unquote(parsed.path))
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            path = f"//{parsed.netloc}{path}"
        return path or None
    if "://" in text or text == "about:blank":
        return None
    return text if text.lower().endswith(PAGE_SUFFIXES) else None


def _handler(folder: Path, token: str) -> type[http.server.SimpleHTTPRequestHandler]:
    prefix = f"/{token}/"

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(folder), **kwargs)

        def translate_path(self, path: str) -> str:
            route = urlparse(path).path
            if not route.startswith(prefix):
                return str(folder / ".rinari-not-served")
            relative = unquote(route[len(prefix) :])
            if any(part.startswith(".") for part in relative.split("/") if part):
                return str(folder / ".rinari-not-served")
            return super().translate_path("/" + route[len(prefix) :])

        def list_directory(self, path):
            self.send_error(404)
            return None

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def log_message(self, format, *args):
            return

    return Handler


class LocalPages:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._servers: OrderedDict[Path, tuple[http.server.ThreadingHTTPServer, str]] = (
            OrderedDict()
        )

    def url_for(self, file: Path) -> str:
        folder = file.parent.resolve()
        with self._lock:
            entry = self._servers.get(folder)
            if entry is None:
                token = secrets.token_urlsafe(12)
                server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _handler(folder, token))
                server.daemon_threads = True
                threading.Thread(
                    target=server.serve_forever, name="rinari-local-page", daemon=True
                ).start()
                entry = (server, token)
                self._servers[folder] = entry
                while len(self._servers) > MAX_FOLDERS:
                    _old, (stale, _t) = self._servers.popitem(last=False)
                    stale.shutdown()
                    stale.server_close()
            else:
                self._servers.move_to_end(folder)
            server, token = entry
        port = server.server_address[1]
        return f"http://127.0.0.1:{port}/{token}/{quote(file.name)}"

    def close(self) -> None:
        with self._lock:
            servers = list(self._servers.values())
            self._servers.clear()
        for server, _token in servers:
            server.shutdown()
            server.server_close()


PAGES = LocalPages()

__all__ = ["MAX_FOLDERS", "PAGES", "PAGE_SUFFIXES", "LocalPages", "local_page_path"]
