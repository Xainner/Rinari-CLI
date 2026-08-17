"""Browser runtime (phase 5): CDP driver + session-scoped manager.

Engine decision (2026-08-17, see TODO.md "Browser engine"): direct CDP over a
minimal in-repo RFC6455 WebSocket client (no new dependencies). Documented
alternative: Playwright — swappable behind `BrowserManager`.
"""

from rinari.browser.cdp import CdpError, CdpSession
from rinari.browser.manager import BrowserError, BrowserManager, DownloadRef
from rinari.browser.ws import WebSocketClient, WsError

__all__ = [
    "BrowserError",
    "BrowserManager",
    "CdpError",
    "CdpSession",
    "DownloadRef",
    "WebSocketClient",
    "WsError",
]
