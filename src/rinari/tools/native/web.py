"""Native web tools (tools.md section 5, phase 5).

Stateless web capability: every tool takes the target URL, passes it through
the session NetworkGuard (hard deny in code), fetches through a bounded
httpx client, and returns bounded data + provenance. Remote content is
untrusted input for the model, never code that runs.

`ctx.web` is an optional zero-arg factory returning an httpx-compatible
client (tests inject httpx.MockTransport); None -> fresh real client.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from rinari.shared.clock import now_iso
from rinari.tools.definition import (
    RISK_LOW,
    RISK_MEDIUM,
    SIDE_EFFECT_LOCAL_REVERSIBLE,
    SIDE_EFFECT_NONE,
    ClassifiedAction,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)
from rinari.web.client import (
    DEFAULT_TIMEOUT_S,
    SEARCH_MAX_RESULTS_DEFAULT,
    SEARCH_MAX_RESULTS_LIMIT,
    WebRequestError,
    fetch,
    search,
)
from rinari.web.html import parse_page

MAX_TEXT_CHARS = 50_000
MAX_OPEN_TEXT_CHARS = 8_000
MAX_OPEN_LINKS = 20
MAX_LINKS_RESULT = 200
MAX_FIND_HITS = 50
FIND_CONTEXT = 160
MAX_SOURCE_URLS = 10


def _ok(data: Any) -> ToolResult:
    return ToolResult(ok=True, data=data)


def _fail(code: ToolErrorCode, message: str, *, retryable: bool = False) -> ToolResult:
    return ToolResult(
        ok=False, error=ToolErrorInfo(code=code, message=message, retryable=retryable)
    )


def _validate_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return value.strip()


def _guard(ctx: ToolContext, url: str) -> ToolResult | None:
    guard = ctx.network
    if guard is None:
        return None
    try:
        guard.assert_reachable(url, source="web")
    except Exception as exc:  # SandboxViolationError
        return _fail(ToolErrorCode.SANDBOX_VIOLATION, getattr(exc, "message", str(exc)))
    return None


def _client_factory(ctx: ToolContext) -> Any:
    if ctx.web is not None:
        return ctx.web
    return None


_UNSAFE_FILENAME_CHARS = frozenset({"<", ">", "|", '"', "?", "*", "\\", "/", "'"})


def _safe_name(url: str, fallback: str) -> str:
    parsed = urlparse(url)
    name = Path(parsed.path or "/").name
    if (
        not name
        or name in {".", ".."}
        or not name.isascii()
        or any(ch in _UNSAFE_FILENAME_CHARS for ch in name)
    ):
        name = fallback
    return name[:120]


def _provenance(fetched, ctx: ToolContext) -> dict:
    return {
        "url": fetched.url,
        "final_url": fetched.final_url,
        "status": fetched.status,
        "content_type": fetched.content_type,
        "bytes": len(fetched.body),
        "sha256": fetched.sha256,
        "source_id": snapshot_id(fetched),
        "fetched_at": next(
            (v[2] for v in ctx.web_snapshots.values() if v[1] is fetched), now_iso(ctx.clock)
        ),
    }


def snapshot_id(fetched):
    return "source_" + hashlib.sha256((fetched.url + fetched.sha256).encode()).hexdigest()[:24]


def source_snapshot(ctx, identifier):
    return next(
        (entry[1] for entry in ctx.web_snapshots.values() if snapshot_id(entry[1]) == identifier),
        None,
    )


def _is_text(fetched) -> bool:
    ctype = (fetched.content_type or "").lower()
    return not ctype or "text" in ctype or "json" in ctype or "xml" in ctype


def _fetch_guarded(
    ctx: ToolContext,
    url: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    refresh: bool = False,
    source_id: str | None = None,
):
    """(Fetched, None) on success; (None, failed ToolResult) on any failure."""
    if source_id:
        cached = source_snapshot(ctx, source_id)
        if cached is None:
            return None, _fail(
                ToolErrorCode.NOT_FOUND, "Source snapshot expired; open the URL again"
            )
        denied = _guard(ctx, cached.url) or _guard(ctx, cached.final_url)
        return (None, denied) if denied is not None else (cached, None)
    denied = _guard(ctx, url)
    if denied is not None:
        return None, denied
    if ctx.cancellation:
        ctx.cancellation.throw_if_cancelled()
    if ctx.deadline_at is not None:
        timeout_s = min(timeout_s, ctx.deadline_at - time.time())
        if timeout_s <= 0:
            return None, _fail(ToolErrorCode.TIMEOUT, "Web deadline exhausted")
    cached = ctx.web_snapshots.get(url)
    if cached and not refresh and time.monotonic() - cached[0] < 30:
        denied = _guard(ctx, cached[1].final_url)
        return (None, denied) if denied else (cached[1], None)
    try:
        fetched = fetch(url, client_factory=_client_factory(ctx), timeout_s=timeout_s)
        if len(fetched.body) <= 1_000_000:
            if len(ctx.web_snapshots) >= 8:
                ctx.web_snapshots.pop(next(iter(ctx.web_snapshots)))
            ctx.web_snapshots[url] = (time.monotonic(), fetched, now_iso(ctx.clock))
        return fetched, None
    except WebRequestError as exc:
        return None, _fail(ToolErrorCode(exc.code), exc.message, retryable=exc.retryable)


# -- web.search -----------------------------------------------------------------


def web_search(input: dict, ctx: ToolContext) -> ToolResult:
    if "queries" in input:
        queries = input["queries"]
        if not isinstance(queries, list) or not 1 <= len(queries) <= 4:
            return _fail(ToolErrorCode.INVALID_ARGUMENT, "queries must contain 1 to 4 strings")
        rows = []
        for query in queries:
            if ctx.cancellation:
                ctx.cancellation.throw_if_cancelled()
            if ctx.deadline_at is not None and time.time() >= ctx.deadline_at:
                return _fail(ToolErrorCode.TIMEOUT, "Search deadline exhausted")
            result = web_search(
                {k: v for k, v in {**input, "query": query}.items() if k != "queries"}, ctx
            )
            rows.append(
                {
                    "query": query,
                    "ok": result.ok,
                    "data": result.data,
                    "error": result.error.message if result.error else None,
                }
            )
        return _ok({"searches": rows})
    query = input.get("query")
    if not isinstance(query, str) or not query.strip():
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "query must be a non-empty string")
    max_results = input.get("max_results", SEARCH_MAX_RESULTS_DEFAULT)
    try:
        max_results = max(1, min(int(max_results), SEARCH_MAX_RESULTS_LIMIT))
    except (TypeError, ValueError):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "max_results must be an integer")
    try:
        results = search(
            query.strip(), client_factory=_client_factory(ctx), max_results=max_results
        )
    except WebRequestError as exc:
        return _fail(ToolErrorCode(exc.code), exc.message, retryable=exc.retryable)
    return _ok({"query": query.strip(), "results": results})


# -- web.fetch --------------------------------------------------------------------


def web_fetch(input: dict, ctx: ToolContext) -> ToolResult:
    url = _validate_url(input.get("url"))
    if url is None:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "url must be an http(s) URL")
    fetched, error = _fetch_guarded(
        ctx, url, refresh=bool(input.get("refresh", False)), source_id=input.get("source_id")
    )
    if error is not None:
        return error
    data: dict = {"truncated": fetched.truncated}
    if _is_text(fetched):
        text = fetched.body.decode("utf-8", errors="replace")
        page = parse_page(text, fetched.final_url) if "<html" in text[:4096].lower() else None
        if page is not None:
            extracted = page.text()
            data.update(
                {
                    "title": page.title,
                    "text": extracted[:MAX_TEXT_CHARS],
                    "text_truncated": len(extracted) > MAX_TEXT_CHARS,
                }
            )
        else:
            data["text"] = text[:MAX_TEXT_CHARS]
            data["text_truncated"] = len(text) > MAX_TEXT_CHARS
    else:
        data["binary"] = True
    data.update(_provenance(fetched, ctx))
    return _ok(data)


# -- web.open -----------------------------------------------------------------------


def web_open(input: dict, ctx: ToolContext) -> ToolResult:
    url = _validate_url(input.get("url"))
    if url is None:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "url must be an http(s) URL")
    fetched, error = _fetch_guarded(
        ctx, url, refresh=bool(input.get("refresh", False)), source_id=input.get("source_id")
    )
    if error is not None:
        return error
    if not _is_text(fetched):
        return _fail(
            ToolErrorCode.INVALID_ARGUMENT,
            f"URL did not return text content (content-type: {fetched.content_type or 'unknown'})",
        )
    page = parse_page(fetched.body.decode("utf-8", errors="replace"), fetched.final_url)
    return _ok(
        {
            "url": fetched.final_url,
            "title": page.title,
            "description": page.description,
            "text": page.text()[:MAX_OPEN_TEXT_CHARS],
            "links": page.links[:MAX_OPEN_LINKS],
            "provenance": _provenance(fetched, ctx),
        }
    )


# -- web.links ------------------------------------------------------------------------


def web_links(input: dict, ctx: ToolContext) -> ToolResult:
    url = _validate_url(input.get("url"))
    if url is None:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "url must be an http(s) URL")
    fetched, error = _fetch_guarded(
        ctx, url, refresh=bool(input.get("refresh", False)), source_id=input.get("source_id")
    )
    if error is not None:
        return error
    page = parse_page(fetched.body.decode("utf-8", errors="replace"), fetched.final_url)
    links = page.links[:MAX_LINKS_RESULT]
    return _ok({"url": fetched.final_url, "links": links, "total": len(page.links)})


# -- web.find ----------------------------------------------------------------------------


def web_find(input: dict, ctx: ToolContext) -> ToolResult:
    url = _validate_url(input.get("url"))
    if url is None:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "url must be an http(s) URL")
    pattern = input.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "pattern must be a non-empty string")
    fetched, error = _fetch_guarded(
        ctx, url, refresh=bool(input.get("refresh", False)), source_id=input.get("source_id")
    )
    if error is not None:
        return error
    text = parse_page(fetched.body.decode("utf-8", errors="replace"), fetched.final_url).text()
    lines = text.splitlines()
    needle = pattern.lower()
    hits = []
    for i, line in enumerate(lines):
        if needle in line.lower():
            hits.append({"line": i + 1, "text": line.strip()[: FIND_CONTEXT * 2]})
            if len(hits) >= MAX_FIND_HITS:
                break
    return _ok(
        {
            "url": fetched.final_url,
            "pattern": pattern,
            "hits": hits,
            "truncated": len(hits) >= MAX_FIND_HITS,
        }
    )


# -- web.extract_* ------------------------------------------------------------------------


def web_extract_text(input: dict, ctx: ToolContext) -> ToolResult:
    url = _validate_url(input.get("url"))
    if url is None:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "url must be an http(s) URL")
    fetched, error = _fetch_guarded(
        ctx, url, refresh=bool(input.get("refresh", False)), source_id=input.get("source_id")
    )
    if error is not None:
        return error
    text = parse_page(fetched.body.decode("utf-8", errors="replace"), fetched.final_url).text()
    return _ok(
        {
            "url": fetched.final_url,
            "text": text[:MAX_TEXT_CHARS],
            "truncated": len(text) > MAX_TEXT_CHARS,
            "characters": len(text),
        }
    )


def web_extract_markdown(input: dict, ctx: ToolContext) -> ToolResult:
    url = _validate_url(input.get("url"))
    if url is None:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "url must be an http(s) URL")
    fetched, error = _fetch_guarded(
        ctx, url, refresh=bool(input.get("refresh", False)), source_id=input.get("source_id")
    )
    if error is not None:
        return error
    page = parse_page(fetched.body.decode("utf-8", errors="replace"), fetched.final_url)
    markdown = page.markdown()
    return _ok(
        {
            "url": fetched.final_url,
            "markdown": markdown[:MAX_TEXT_CHARS],
            "truncated": len(markdown) > MAX_TEXT_CHARS,
            "characters": len(markdown),
        }
    )


def web_extract_metadata(input: dict, ctx: ToolContext) -> ToolResult:
    url = _validate_url(input.get("url"))
    if url is None:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "url must be an http(s) URL")
    fetched, error = _fetch_guarded(
        ctx, url, refresh=bool(input.get("refresh", False)), source_id=input.get("source_id")
    )
    if error is not None:
        return error
    page = parse_page(fetched.body.decode("utf-8", errors="replace"), fetched.final_url)
    return _ok(
        {
            "url": fetched.final_url,
            "title": page.title,
            "description": page.description,
            "language": page.language,
            "charset": page.charset,
            "meta": page.meta,
        }
    )


# -- web.download ------------------------------------------------------------------------


def web_download(input: dict, ctx: ToolContext) -> ToolResult:
    url = _validate_url(input.get("url"))
    if url is None:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "url must be an http(s) URL")
    fetched, error = _fetch_guarded(ctx, url, timeout_s=60.0, refresh=True)
    if error is not None:
        return error
    artifact_root = Path(ctx.artifact_root)
    artifact_root.mkdir(parents=True, exist_ok=True)
    name = _safe_name(url, f"download-{fetched.sha256[:8]}")
    target = artifact_root / name
    try:
        target.write_bytes(fetched.body)
    except OSError as exc:
        return _fail(
            ToolErrorCode.PERMISSION_DENIED, f"Download write failed: {exc.__class__.__name__}"
        )
    return _ok(
        {
            "path": str(target),
            "bytes": len(fetched.body),
            "sha256": fetched.sha256,
            "content_type": fetched.content_type,
            "truncated": fetched.truncated,
            "provenance": _provenance(fetched, ctx),
        }
    )


# -- provenance: web.cite / web.sources ------------------------------------------------------


def _cite(fetched, ctx: ToolContext) -> dict:
    entry = {
        "url": fetched.url,
        "final_url": fetched.final_url,
        "status": fetched.status,
        "content_type": fetched.content_type,
        "sha256": fetched.sha256,
        "fetched_at": now_iso(ctx.clock),
    }
    if _is_text(fetched):
        page = parse_page(fetched.body.decode("utf-8", errors="replace"), fetched.final_url)
        entry["title"] = page.title
        entry["snippet"] = page.text()[:400]
    return entry


def web_cite(input: dict, ctx: ToolContext) -> ToolResult:
    url = _validate_url(input.get("url"))
    if url is None:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "url must be an http(s) URL")
    fetched, error = _fetch_guarded(
        ctx, url, refresh=bool(input.get("refresh", False)), source_id=input.get("source_id")
    )
    if error is not None:
        return error
    return _ok({"citation": _cite(fetched, ctx)})


def web_sources(input: dict, ctx: ToolContext) -> ToolResult:
    if not input.get("urls"):
        return _ok(
            {
                "sources": [_provenance(v[1], ctx) for v in ctx.web_snapshots.values()],
                "source": "session-snapshots",
                "network_requests": 0,
            }
        )
    urls = input.get("urls")
    if not isinstance(urls, list) or not urls or len(urls) > MAX_SOURCE_URLS:
        return _fail(
            ToolErrorCode.INVALID_ARGUMENT,
            f"urls must be a list of 1..{MAX_SOURCE_URLS} http(s) URLs",
        )
    sources = []
    for raw in urls:
        url = _validate_url(raw)
        if url is None:
            sources.append({"url": str(raw), "ok": False, "error": "invalid http(s) URL"})
            continue
        fetched, error = _fetch_guarded(
            ctx, url, refresh=bool(input.get("refresh", False)), source_id=input.get("source_id")
        )
        if error is not None:
            sources.append(
                {"url": url, "ok": False, "error": error.error.message if error.error else ""}
            )
            continue
        entry = _cite(fetched, ctx)
        entry["ok"] = True
        sources.append(entry)
    return _ok({"sources": sources})


# -- registry ------------------------------------------------------------------------------


def web_tools() -> list[ToolDefinition]:
    common = dict(capabilities=("network.outbound",), namespace="web")
    url_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "source_id": {
                "type": "string",
                "description": "Stable source_id from this runtime; reuses its exact snapshot.",
            },
            "refresh": {
                "type": "boolean",
                "description": "Bypass the 30-second session snapshot cache.",
            },
        },
        "anyOf": [{"required": ["url"]}, {"required": ["source_id"]}],
    }
    return [
        ToolDefinition(
            name="web.search",
            description="Search the web (keyless DuckDuckGo HTML endpoint).",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "queries": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "minItems": 1,
                        "maxItems": 4,
                    },
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "oneOf": [{"required": ["query"]}, {"required": ["queries"]}],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            classify=lambda i: ClassifiedAction("network.outbound", "html.duckduckgo.com"),
            handler=web_search,
            **common,
        ),
        ToolDefinition(
            name="web.fetch",
            description="Fetch a URL; returns bounded text (HTML extracted) + provenance.",
            input_schema=url_schema,
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=web_fetch,
            **common,
        ),
        ToolDefinition(
            name="web.open",
            description="Open a page: title, description, opening text and top links.",
            input_schema=url_schema,
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=web_open,
            **common,
        ),
        ToolDefinition(
            name="web.links",
            description="List the (deduplicated, absolute) links of a page.",
            input_schema=url_schema,
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=web_links,
            **common,
        ),
        ToolDefinition(
            name="web.find",
            description="Case-insensitive literal search within a page's extracted text.",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "source_id": {
                        "type": "string",
                        "description": "Stable source_id; reuses its exact snapshot.",
                    },
                    "pattern": {"type": "string"},
                },
                "required": ["pattern"],
                "anyOf": [{"required": ["url"]}, {"required": ["source_id"]}],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=web_find,
            **common,
        ),
        ToolDefinition(
            name="web.extract_text",
            description="Extract plain text from a page (bounded).",
            input_schema=url_schema,
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=web_extract_text,
            **common,
        ),
        ToolDefinition(
            name="web.extract_markdown",
            description="Extract lightweight markdown from a page (bounded).",
            input_schema=url_schema,
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=web_extract_markdown,
            **common,
        ),
        ToolDefinition(
            name="web.extract_metadata",
            description="Extract title, description and meta tags from a page.",
            input_schema=url_schema,
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=web_extract_metadata,
            **common,
        ),
        ToolDefinition(
            name="web.download",
            description="Download a URL into the session artifact directory (bounded 2 MiB).",
            input_schema={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
                "additionalProperties": False,
            },
            risk=RISK_MEDIUM,
            side_effects=SIDE_EFFECT_LOCAL_REVERSIBLE,
            timeout_ms=90_000,
            handler=web_download,
            **common,
        ),
        ToolDefinition(
            name="web.cite",
            description="Build a citation/evidence record for a URL (hash, timestamp, title).",
            input_schema=url_schema,
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=web_cite,
            **common,
        ),
        ToolDefinition(
            name="web.sources",
            description="Build citation records for up to 10 URLs; per-URL failures included.",
            input_schema={
                "type": "object",
                "properties": {"urls": {"type": "array", "items": {"type": "string"}}},
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            classify=_sources_classify,
            handler=web_sources,
            **common,
        ),
    ]


def _sources_classify(input: dict) -> ClassifiedAction:
    for raw in input.get("urls") or []:
        if _validate_url(raw) is not None:
            return ClassifiedAction("network.outbound", raw)
    return ClassifiedAction("network.outbound", "")


__all__ = ["web_tools"]
