"""OpenCode's per-protocol addresses (plan 0.4.0 #94, point 2).

OpenCode documents one address per wire protocol
(`https://opencode.ai/inference/go/openai/v1`, `/inference/go/anthropic/v1`,
`/inference/go/google/v1beta`) while saved providers keep the older shared one
(`https://opencode.ai/zen/go/v1`, `https://opencode.ai/zen/v1`), which still
answers. Calls go to the documented address; when it answers 404 and the old
one works, the old one is remembered for that provider and protocol for the
rest of the process. Google's own protocol maps its address in its adapter.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TypeVar
from urllib.parse import urlsplit

_log = logging.getLogger(__name__)

T = TypeVar("T")

#: The saved shared address -> the plan segment of the new ones.
_PLANS = {"/zen/go/v1": "go", "/zen/v1": "zen"}
#: Wire transport -> the protocol segment of the new address.
_PROTOCOLS = {"chat": "openai", "responses": "openai", "anthropic": "anthropic"}

_legacy: set[tuple[str, str]] = set()
_lock = threading.Lock()


def documented_route(endpoint: str | None, wire: str) -> str | None:
    """The documented address for this protocol, or None when the endpoint is
    not one of OpenCode's shared ones (custom addresses are left alone)."""
    if not endpoint:
        return None
    parts = urlsplit(endpoint)
    if parts.scheme != "https" or parts.netloc != "opencode.ai":
        return None
    plan = _PLANS.get(parts.path.rstrip("/"))
    protocol = _PROTOCOLS.get(wire)
    if plan is None or protocol is None:
        return None
    return f"https://opencode.ai/inference/{plan}/{protocol}/v1"


def route_for(endpoint: str | None, wire: str) -> str | None:
    """Where a call with this protocol goes now."""
    new = documented_route(endpoint, wire)
    if new is None:
        return endpoint
    with _lock:
        if (str(endpoint), _PROTOCOLS[wire]) in _legacy:
            return endpoint
    return new


def call_on_route(endpoint: str | None, wire: str, call: Callable[[str | None], T]) -> T:
    """`call(address)` on the documented address; on a 404 there, the saved
    one, which is remembered when it works."""
    target = route_for(endpoint, wire)
    if target == endpoint:
        return call(endpoint)
    try:
        return call(target)
    except Exception as exc:
        details = getattr(exc, "details", None) or {}
        if details.get("http_status") != 404:
            raise
        result = call(endpoint)
        with _lock:
            _legacy.add((str(endpoint), _PROTOCOLS[wire]))
        _log.warning("OpenCode %s answered 404; using %s for %s", target, endpoint, wire)
        return result


def forget() -> None:
    """Tests: back to the documented addresses."""
    with _lock:
        _legacy.clear()
