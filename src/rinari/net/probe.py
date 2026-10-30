"""«Probar conexión»: can each enabled provider be reached with the network settings?

One cheap GET to the provider's base URL, without credentials: any HTTP answer
means the proxy and the certificates worked (a 401 or 404 is still a reached
server). A proxy that refuses (407) or a server error (5xx) is not «ok» but
still reports its status. Every request goes through the network factory, in
parallel, and the whole test ends within `overall_timeout` seconds.
"""

from __future__ import annotations

import ssl
import time
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any

import httpx

from rinari.net.http import _redact
from rinari.net.http import client as net_client

REQUEST_TIMEOUT = httpx.Timeout(8.0, connect=6.0)
OVERALL_TIMEOUT_S = 10.0
MAX_WORKERS = 6
_ERROR_CHARS = 300


def _target(record: Any) -> str | None:
    """The provider's HTTP base URL, or None for one without (external CLI)."""
    from rinari.providers.registry import adapter_for

    url = adapter_for(record).base_url(record.endpoint, record.settings or {})
    return url if url.startswith(("http://", "https://")) else None


def _kind(exc: BaseException) -> str:
    if isinstance(exc, httpx.ProxyError):
        return "proxy"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, ssl.SSLError):
            return "tls"
        cause = cause.__cause__ or cause.__context__
    text = str(exc).lower()
    if "certificate" in text or "ssl" in text:
        return "tls"
    if isinstance(exc, httpx.ConnectError):
        return "connect"
    return "network"


def _probe(home: Any, url: str) -> dict[str, Any]:
    started = time.monotonic()
    try:
        with net_client(home, timeout=REQUEST_TIMEOUT, follow_redirects=False) as http:
            response = http.get(url, headers={"User-Agent": "Rinari/network-test"})
    except Exception as exc:  # whatever stopped it is the answer
        message = f"{type(exc).__name__}: {exc}".strip()
        return {
            "ok": False,
            "status": None,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": message[:_ERROR_CHARS],
            "error_kind": _kind(exc),
        }
    status = response.status_code
    ok = status < 500 and status != 407
    return {
        "ok": ok,
        "status": status,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "error": None if ok else f"HTTP {status}",
        "error_kind": None if ok else ("proxy" if status == 407 else "http"),
    }


def probe_providers(
    records: list[Any], home: Any, *, overall_timeout: float = OVERALL_TIMEOUT_S
) -> list[dict[str, Any]]:
    """One row per enabled provider with an HTTP endpoint, in `records` order."""
    rows: list[dict[str, Any]] = []
    jobs: list[tuple[dict[str, Any], str]] = []
    for record in records:
        if not getattr(record, "enabled", True):
            continue
        row: dict[str, Any] = {"provider_id": record.id, "alias": record.alias, "url": None}
        try:
            url = _target(record)
        except Exception as exc:  # no endpoint, unknown protocol: say so in its row
            message = getattr(exc, "message", None) or str(exc) or type(exc).__name__
            rows.append(
                {
                    **row,
                    "ok": False,
                    "status": None,
                    "latency_ms": None,
                    "error": message[:_ERROR_CHARS],
                    "error_kind": "config",
                }
            )
            continue
        if url is None:
            continue
        row["url"] = _redact(url)
        rows.append(row)
        jobs.append((row, url))
    if not jobs:
        return rows
    pool = ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(jobs)), thread_name_prefix="net")
    try:
        futures = {pool.submit(_probe, home, url): row for row, url in jobs}
        done, _ = wait(futures, timeout=overall_timeout)
        for future, row in futures.items():
            if future in done:
                row.update(future.result())
            else:
                row.update(
                    {
                        "ok": False,
                        "status": None,
                        "latency_ms": int(overall_timeout * 1000),
                        "error": f"No answer within {overall_timeout:g} s.",
                        "error_kind": "timeout",
                    }
                )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return rows
