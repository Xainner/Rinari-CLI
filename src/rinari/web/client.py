"""Bounded HTTP transport for web.* tools (phase 5).

Every web tool passes its connection target through the session NetworkGuard
before dialing (policy.network). This module owns only the transport:
bounded bodies, httpx error mapping to tool error codes, and the
DuckDuckGo HTML search endpoint (keyless, no extra dependency).

`client_factory` is the test seam: an object with an httpx.Client interface
(production: real client; tests: httpx.MockTransport).
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote, urljoin

import httpx

USER_AGENT = "Rinari/0.1 CLI (web tools)"
DEFAULT_TIMEOUT_S = 15.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
SEARCH_ENDPOINT = "https://html.duckduckgo.com/html/"
SEARCH_MAX_RESULTS_DEFAULT = 10
SEARCH_MAX_RESULTS_LIMIT = 50


class WebRequestError(Exception):
    """Structured web transport failure (maps to ToolError codes)."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status = status


@dataclass(frozen=True, slots=True)
class Fetched:
    url: str
    final_url: str
    status: int
    content_type: str
    body: bytes
    truncated: bool
    elapsed_ms: float

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


def _default_client_factory(timeout_s: float) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(timeout_s),
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )


def _map_status(status: int) -> WebRequestError | None:
    if status < 400:
        return None
    if status == 404:
        return WebRequestError("NOT_FOUND", "HTTP 404 (not found)", status=status)
    if status in (401, 403):
        return WebRequestError(
            "PERMISSION_DENIED", f"HTTP {status} (authentication/forbidden)", status=status
        )
    if status == 429:
        return WebRequestError(
            "RATE_LIMITED", "HTTP 429 (rate limited)", retryable=True, status=status
        )
    if status >= 500:
        return WebRequestError(
            "NETWORK_ERROR", f"HTTP {status} (server error)", retryable=True, status=status
        )
    return WebRequestError("NOT_FOUND", f"HTTP {status}", status=status)


def fetch(
    url: str,
    *,
    client_factory: Any = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Fetched:
    """GET `url` with a bounded body; redirect-following is transport-level."""
    client = client_factory() if client_factory is not None else _default_client_factory(timeout_s)
    started = time.monotonic()
    try:
        response, body, truncated = _read_bounded(client, url)
    except httpx.StreamConsumed:
        # Test seam (httpx.MockTransport) serves pre-built responses without a
        # raw stream; fall back to a full read and truncate the same way.
        response = client.get(url)
        raw = response.content
        body = raw[:MAX_RESPONSE_BYTES]
        truncated = len(raw) > MAX_RESPONSE_BYTES
    except httpx.TimeoutException as exc:
        raise WebRequestError(
            "TIMEOUT",
            f"Request timed out after {timeout_s:.0f}s: {exc.__class__.__name__}",
            retryable=True,
        ) from exc
    except httpx.ConnectError as exc:
        raise WebRequestError(
            "NETWORK_ERROR", f"Connection failed: {exc.__class__.__name__}", retryable=True
        ) from exc
    except WebRequestError:
        raise
    except httpx.HTTPError as exc:
        raise WebRequestError(
            "NETWORK_ERROR", f"Request failed: {exc.__class__.__name__}", retryable=True
        ) from exc
    finally:
        client.close()
    return Fetched(
        url=url,
        final_url=str(response.url),
        status=response.status_code,
        content_type=response.headers.get("content-type", ""),
        body=bytes(body),
        truncated=truncated,
        elapsed_ms=(time.monotonic() - started) * 1000,
    )


def _read_bounded(client: httpx.Client, url: str) -> tuple[httpx.Response, bytes, bool]:
    """Stream the body, capping at MAX_RESPONSE_BYTES.

    NOTE: 4xx/5xx are mapped by the caller after the read; error pages are
    small in practice, so the cap behavior is identical for both paths.
    """
    error: WebRequestError | None
    with client.stream("GET", url) as response:
        error = _map_status(response.status_code)
        if error is not None:
            raise error
        body = bytearray()
        truncated = False
        for chunk in response.iter_raw():
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                truncated = True
                break
    return response, bytes(body), truncated


# -- search --------------------------------------------------------------------


class _SearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self._current: dict | None = None
        self._field: str | None = None
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag != "a":
            return
        attr = dict(attrs)
        classes = attr.get("class") or ""
        if "result__a" in classes:
            self._flush()
            self._current = {"href": attr.get("href") or "", "title": [], "snippet": []}
            self._field = "title"
            self._buffer = []
        elif "result__snippet" in classes and self._current is not None:
            self._field = "snippet"
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._field is None:
            return
        text = " ".join("".join(self._buffer).split())
        if self._current is not None and text:
            self._current[self._field].append(text)
        if self._field == "snippet":
            self._flush()
        self._field = None

    def handle_data(self, data: str) -> None:
        if self._field is not None:
            self._buffer.append(data)

    def _flush(self) -> None:
        if self._current is not None:
            self.results.append(self._current)
            self._current = None

    def close(self) -> None:
        super().close()
        self._flush()


_SEARCH_UDDG_RE = re.compile(r"uddg=([^&]+)")


def _result_url(href: str) -> str:
    href = _result_url_base(href)
    match = _SEARCH_UDDG_RE.search(href)
    if match:
        href = unquote(match.group(1))
    return href if href.startswith(("http://", "https://")) else ""


def _result_url_base(href: str) -> str:
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return urljoin(SEARCH_ENDPOINT, href)
    return href


def search(
    query: str,
    *,
    client_factory: Any = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_results: int = SEARCH_MAX_RESULTS_DEFAULT,
) -> list[dict]:
    """Keyless DuckDuckGo HTML search; returns bounded {title,url,snippet}."""
    max_results = max(1, min(int(max_results), SEARCH_MAX_RESULTS_LIMIT))
    client = client_factory() if client_factory is not None else _default_client_factory(timeout_s)
    try:
        response = client.get(SEARCH_ENDPOINT, params={"q": query})
    except httpx.TimeoutException as exc:
        raise WebRequestError(
            "TIMEOUT", f"Search timed out: {exc.__class__.__name__}", retryable=True
        ) from exc
    except httpx.ConnectError as exc:
        raise WebRequestError(
            "NETWORK_ERROR", f"Connection failed: {exc.__class__.__name__}", retryable=True
        ) from exc
    except httpx.HTTPError as exc:
        raise WebRequestError(
            "NETWORK_ERROR", f"Search failed: {exc.__class__.__name__}", retryable=True
        ) from exc
    finally:
        client.close()
    error = _map_status(response.status_code)
    if error is not None:
        raise error
    parser = _SearchParser()
    parser.feed(response.text)
    parser.close()
    results: list[dict] = []
    for entry in parser.results:
        href = _result_url(entry["href"].strip())
        if not href:
            continue
        title = " ".join(" ".join(entry["title"]).split()) if entry["title"] else ""
        snippet = " ".join(" ".join(entry["snippet"]).split()) if entry["snippet"] else ""
        results.append({"title": title, "url": href, "snippet": snippet[:300]})
        if len(results) >= max_results:
            break
    return results


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "MAX_RESPONSE_BYTES",
    "SEARCH_ENDPOINT",
    "Fetched",
    "WebRequestError",
    "fetch",
    "search",
]
