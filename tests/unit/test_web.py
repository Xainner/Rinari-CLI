"""Web capability (phase 5): fetch/extract/links/find/cite/search + guard.

Deterministic: no sockets — httpx.MockTransport through the ctx.web factory
seam; FakeClock for provenance timestamps; NetworkGuard deny enforced in code.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from rinari.policy.approvals import ApprovalEngine
from rinari.policy.engine import PermissionProfile, PolicyEngine
from rinari.policy.network import NetworkGuard, NetworkPolicy
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.runtime.cancellation import CancellationToken
from rinari.shared.clock import FakeClock, iso_utc
from rinari.tools.definition import ToolContext
from rinari.tools.native.web import web_tools
from rinari.tools.registry import ToolRegistry
from rinari.tools.runtime import ToolRuntime
from rinari.web.client import WebRequestError, fetch, search
from rinari.web.html import parse_page

PAGE_HTML = """<html lang="en"><head>
<title>Doc &amp; Co</title>
<meta name="description" content="A test page">
<meta property="og:title" content="OG Title">
<meta charset="utf-8">
<script>var hidden = "not extracted";</script>
<style>.x { color: red }</style>
</head><body>
<h1>Main Heading</h1>
<p>Hello <a href="/rel/page">relative</a> and <a href="https://ex.com/a#f">absolute</a>.</p>
<ul><li>first item</li><li>second item</li></ul>
<pre><code>print("hi")</code></pre>
<table><tr><th>key</th><th>value</th></tr><tr><td>1</td><td>two</td></tr></table>
</body></html>
"""

DDG_HTML = """<html><body>
<div class="result">
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fx.com%2Fp">Example Title</a>
<a class="result__snippet" href="#">An example <b>snippet</b> here.</a>
</div>
<div class="result">
<a rel="nofollow" class="result__a" href="https://direct.com/doc">Second Result</a>
</div>
</body></html>
"""


def _handler(page_by_url: dict[str, httpx.Response]):
    def handler(request: httpx.Request) -> httpx.Response:
        key = str(request.url)
        if key in page_by_url:
            return page_by_url[key]
        if any(k.startswith(str(request.url)) for k in page_by_url):
            return next(v for k, v in page_by_url.items() if k.startswith(str(request.url)))
        return httpx.Response(404, text="no route")

    return handler


def _factory(handler) -> callable:
    def factory():
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)

    return factory


def _ctx(tmp_path: Path, *, network=None, web=None, clock: FakeClock | None = None) -> ToolContext:
    root = tmp_path / "work"
    root.mkdir(parents=True, exist_ok=True)
    return ToolContext(
        session_id="s-web",
        kind="CHAT",
        cwd=root,
        project_root=None,
        user_home=tmp_path,
        profile=PermissionProfile.WORKSPACE,
        sandbox=FilesystemSandbox(read_root=root, write_roots=(root,)),
        limits=ProcessLimits(timeout_s=30, max_output_bytes=65536),
        artifact_root=tmp_path / "artifacts",
        clock=clock or FakeClock(),
        cancellation=CancellationToken(),
        network=network,
        web=web,
    )


def _tool(name: str):
    return next(t for t in web_tools() if t.name == name)


# -- html extraction -------------------------------------------------------------


def test_parse_title_meta_and_language() -> None:
    page = parse_page(PAGE_HTML, "https://ex.com/dir/page")
    assert page.title == "Doc & Co"
    assert page.description == "A test page"
    assert page.meta["og:title"] == "OG Title"
    assert page.language == "en"
    assert page.charset == "utf-8"


def test_parse_links_relative_resolved_and_deduped() -> None:
    page = parse_page(PAGE_HTML, "https://ex.com/dir/page")
    hrefs = [link["href"] for link in page.links]
    assert "https://ex.com/rel/page" in hrefs
    assert "https://ex.com/a" in hrefs  # fragment dropped
    assert hrefs.count("https://ex.com/a") == 1
    texts = {link["href"]: link["text"] for link in page.links}
    assert texts["https://ex.com/rel/page"] == "relative"


def test_parse_text_skips_scripts() -> None:
    page = parse_page(PAGE_HTML, "https://ex.com/")
    text = page.text()
    assert "Main Heading" in text
    assert "Hello" in text and "relative" in text and "absolute" in text
    assert "first item" in text
    assert "print(" in text
    assert "hidden" not in text
    assert "not extracted" not in text
    assert "color" not in text


def test_parse_markdown_structure() -> None:
    page = parse_page(PAGE_HTML, "https://ex.com/")
    md = page.markdown()
    assert "# Main Heading" in md
    assert "[relative](https://ex.com/rel/page)" in md
    assert "- first item" in md
    assert 'print("hi")' in md


def test_parse_tables_bounded() -> None:
    page = parse_page(PAGE_HTML, "https://ex.com/")
    assert page.tables == [{"rows": [["key", "value"], ["1", "two"]]}]
    big = "<table>" + "".join(f"<tr><td>{i}</td></tr>" for i in range(60)) + "</table>"
    page = parse_page(big, "https://ex.com/")
    assert len(page.tables) == 1
    assert len(page.tables[0]["rows"]) == 50


# -- client (transport) ------------------------------------------------------------


def test_fetch_follows_redirects_and_reports() -> None:
    handler = _handler(
        {
            "https://ex.com/old": httpx.Response(
                302, headers={"location": "https://ex.com/new"}, text=""
            ),
            "https://ex.com/new": httpx.Response(200, text="<html><body>ok</body></html>"),
        }
    )
    fetched = fetch("https://ex.com/old", client_factory=_factory(handler))
    assert fetched.status == 200
    assert fetched.final_url == "https://ex.com/new"
    assert fetched.sha256
    assert not fetched.truncated


def test_fetch_error_mapping() -> None:
    for status, code in ((404, "NOT_FOUND"), (403, "PERMISSION_DENIED"), (429, "RATE_LIMITED")):
        response = httpx.Response(status, text="x")
        with pytest.raises(WebRequestError) as excinfo:
            fetch(
                "https://ex.com/x",
                client_factory=_factory(_handler({"https://ex.com/x": response})),
            )
        assert excinfo.value.code == code


def test_fetch_server_error_is_retryable() -> None:
    with pytest.raises(WebRequestError) as excinfo:
        fetch(
            "https://ex.com/x",
            client_factory=_factory(_handler({"https://ex.com/x": httpx.Response(500, text="x")})),
        )
    assert excinfo.value.code == "NETWORK_ERROR"
    assert excinfo.value.retryable is True


def test_fetch_connection_error_retryable() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(WebRequestError) as excinfo:
        fetch(
            "https://ex.com/x",
            client_factory=lambda: httpx.Client(transport=httpx.MockTransport(boom)),
        )
    assert excinfo.value.code == "NETWORK_ERROR"
    assert excinfo.value.retryable is True


def test_fetch_timeout_maps_to_timeout() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(WebRequestError) as excinfo:
        fetch(
            "https://ex.com/x",
            client_factory=lambda: httpx.Client(
                transport=httpx.MockTransport(boom), timeout=httpx.Timeout(1.0)
            ),
            timeout_s=1.0,
        )
    assert excinfo.value.code == "TIMEOUT"
    assert excinfo.value.retryable is True


# -- web.* tools ------------------------------------------------------------------------


def test_web_fetch_tool_extracts_and_provenances(tmp_path) -> None:
    clock = FakeClock(start=1_700_000_123.0)
    ctx = _ctx(
        tmp_path,
        web=_factory(_handler({"https://ex.com/page": httpx.Response(200, text=PAGE_HTML)})),
        clock=clock,
    )
    result = _tool("web.fetch").handler({"url": "https://ex.com/page"}, ctx)
    assert result.ok, result.error
    assert result.data["title"] == "Doc & Co"
    assert "Main Heading" in result.data["text"]
    assert result.data["sha256"]
    assert result.data["fetched_at"] == iso_utc(1_700_000_123.0)
    assert result.data["status"] == 200


def test_web_fetch_invalid_url(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    result = _tool("web.fetch").handler({"url": "ftp://nope"}, ctx)
    assert not result.ok
    assert result.error.code.value == "INVALID_ARGUMENT"


def test_web_tool_denied_by_network_guard(tmp_path) -> None:
    guard = NetworkGuard(NetworkPolicy(mode="off"))
    ctx = _ctx(
        tmp_path,
        network=guard,
        web=_factory(_handler({"https://ex.com/page": httpx.Response(200, text="x")})),
    )
    result = _tool("web.fetch").handler({"url": "https://ex.com/page"}, ctx)
    assert not result.ok
    assert result.error.code.value == "SANDBOX_VIOLATION"
    assert "network blocked" in result.error.message


def test_web_open_tool_summary(tmp_path) -> None:
    ctx = _ctx(
        tmp_path,
        web=_factory(_handler({"https://ex.com/page": httpx.Response(200, text=PAGE_HTML)})),
    )
    result = _tool("web.open").handler({"url": "https://ex.com/page"}, ctx)
    assert result.ok, result.error
    assert result.data["title"] == "Doc & Co"
    assert result.data["description"] == "A test page"
    assert any(link["href"] == "https://ex.com/rel/page" for link in result.data["links"])


def test_web_links_tool(tmp_path) -> None:
    ctx = _ctx(
        tmp_path,
        web=_factory(_handler({"https://ex.com/page": httpx.Response(200, text=PAGE_HTML)})),
    )
    result = _tool("web.links").handler({"url": "https://ex.com/page"}, ctx)
    assert result.ok, result.error
    hrefs = [link["href"] for link in result.data["links"]]
    assert "https://ex.com/rel/page" in hrefs
    assert "https://ex.com/a" in hrefs


def test_web_find_tool_hit_and_miss(tmp_path) -> None:
    ctx = _ctx(
        tmp_path,
        web=_factory(_handler({"https://ex.com/page": httpx.Response(200, text=PAGE_HTML)})),
    )
    hit = _tool("web.find").handler({"url": "https://ex.com/page", "pattern": "RELATIVE"}, ctx)
    assert hit.ok
    assert len(hit.data["hits"]) == 1
    assert "relative" in hit.data["hits"][0]["text"]
    miss = _tool("web.find").handler(
        {"url": "https://ex.com/page", "pattern": "zzz-not-there"}, ctx
    )
    assert miss.data["hits"] == []


def test_web_extract_metadata_tool(tmp_path) -> None:
    ctx = _ctx(
        tmp_path,
        web=_factory(_handler({"https://ex.com/page": httpx.Response(200, text=PAGE_HTML)})),
    )
    result = _tool("web.extract_metadata").handler({"url": "https://ex.com/page"}, ctx)
    assert result.ok
    assert result.data["title"] == "Doc & Co"
    assert result.data["meta"]["description"] == "A test page"
    assert result.data["language"] == "en"


def test_web_download_tool_writes_artifact_with_provenance(tmp_path) -> None:
    body = b"binary-ish content \x00\x01"
    ctx = _ctx(
        tmp_path,
        web=_factory(
            _handler(
                {
                    "https://ex.com/files/report.bin": httpx.Response(
                        200,
                        content=body,
                        headers={"content-type": "application/octet-stream"},
                    )
                }
            )
        ),
    )
    result = _tool("web.download").handler({"url": "https://ex.com/files/report.bin"}, ctx)
    assert result.ok, result.error
    path = Path(result.data["path"])
    assert path.exists()
    assert path.read_bytes() == body
    assert result.data["sha256"] == _sha256(body)
    assert result.data["provenance"]["status"] == 200


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def test_web_cite_tool_evidence_record(tmp_path) -> None:
    clock = FakeClock(start=1_750_000_000.0)
    ctx = _ctx(
        tmp_path,
        web=_factory(_handler({"https://ex.com/page": httpx.Response(200, text=PAGE_HTML)})),
        clock=clock,
    )
    result = _tool("web.cite").handler({"url": "https://ex.com/page"}, ctx)
    assert result.ok
    citation = result.data["citation"]
    assert citation["title"] == "Doc & Co"
    assert citation["fetched_at"] == iso_utc(1_750_000_000.0)
    assert citation["sha256"]
    assert "Main Heading" in citation["snippet"]


def test_web_sources_tool_mixed_outcomes(tmp_path) -> None:
    ctx = _ctx(
        tmp_path,
        web=_factory(_handler({"https://ex.com/page": httpx.Response(200, text=PAGE_HTML)})),
    )
    result = _tool("web.sources").handler(
        {"urls": ["https://ex.com/page", "not-a-url", "https://missing.example/nope"]}, ctx
    )
    assert result.ok
    entries = {e["url"]: e for e in result.data["sources"]}
    assert entries["https://ex.com/page"]["ok"] is True
    assert entries["not-a-url"]["ok"] is False
    assert entries["https://missing.example/nope"]["ok"] is False
    assert "404" in entries["https://missing.example/nope"]["error"]


def test_web_search_tool_keyless_ddg(tmp_path) -> None:
    import urllib.parse as _up

    ddg_url = "https://html.duckduckgo.com/html/?" + _up.urlencode({"q": "rinari cli"})
    ctx = _ctx(tmp_path, web=_factory(_handler({ddg_url: httpx.Response(200, text=DDG_HTML)})))
    result = _tool("web.search").handler({"query": "rinari cli"}, ctx)
    assert result.ok, result.error
    results = result.data["results"]
    assert results[0]["url"] == "https://x.com/p"
    assert results[0]["title"] == "Example Title"
    assert "snippet" in results[0]["snippet"]
    assert results[1]["url"] == "https://direct.com/doc"


def test_search_client_parses_and_bounds() -> None:
    import urllib.parse as _up

    ddg_url = "https://html.duckduckgo.com/html/?" + _up.urlencode({"q": "x"})
    results = search(
        "x",
        client_factory=_factory(_handler({ddg_url: httpx.Response(200, text=DDG_HTML)})),
        max_results=1,
    )
    assert len(results) == 1
    assert results[0]["url"] == "https://x.com/p"


def test_web_fetch_real_runtime_denied_by_policy(tmp_path) -> None:
    # Real definitions through the real runtime: the policy gate denies before
    # any dialing happens (network mode off), no transport involved at all.
    registry = ToolRegistry()
    registry.register_all(web_tools())
    policy = PolicyEngine(network=NetworkPolicy(mode="off"))
    runtime = ToolRuntime(
        registry,
        policy,
        ApprovalEngine(prompt=lambda req: "n"),
        clock=FakeClock(),
    )
    ctx = _ctx(tmp_path, network=NetworkGuard(NetworkPolicy(mode="off")))
    assert registry.get("web.fetch") is not None
    result = runtime.execute("web.fetch", {"url": "https://ex.com/page"}, ctx)
    assert not result.ok
    assert result.error.code.value == "POLICY_DENIED"


def test_web_fetch_real_runtime_allowed_end_to_end(tmp_path) -> None:
    # mode=allow + MockTransport through ctx.web: full path (schema -> policy
    # allow -> handler -> provenance) with no real sockets.
    handler = _handler({"https://ex.com/page": httpx.Response(200, text=PAGE_HTML)})
    registry = ToolRegistry()
    registry.register_all(web_tools())
    policy = PolicyEngine(network=NetworkPolicy(mode="allow"))
    runtime = ToolRuntime(
        registry,
        policy,
        ApprovalEngine(prompt=lambda req: "n"),
        clock=FakeClock(),
    )
    ctx = _ctx(tmp_path, network=NetworkGuard(NetworkPolicy(mode="allow")), web=_factory(handler))
    result = runtime.execute("web.fetch", {"url": "https://ex.com/page"}, ctx)
    assert result.ok, result.error
    assert result.data["title"] == "Doc & Co"
    assert result.data["sha256"]


def test_web_fetch_binary_not_text(tmp_path) -> None:
    ctx = _ctx(
        tmp_path,
        web=_factory(
            _handler(
                {
                    "https://ex.com/img.png": httpx.Response(
                        200, content=b"\x89PNG", headers={"content-type": "image/png"}
                    )
                }
            )
        ),
    )
    result = _tool("web.fetch").handler({"url": "https://ex.com/img.png"}, ctx)
    assert result.ok
    assert result.data["binary"] is True
    assert "text" not in result.data


def test_web_operations_share_snapshot_and_can_refresh(tmp_path):
    from rinari.tools.native.web import web_links, web_open

    calls = []

    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(200, text=PAGE_HTML, headers={"content-type": "text/html"})

    ctx = _ctx(tmp_path, web=_factory(handle))
    args = {"url": "https://example.com/"}
    assert web_open(args, ctx).ok
    assert web_links(args, ctx).ok
    assert len(calls) == 1
    assert web_open({**args, "refresh": True}, ctx).ok
    assert len(calls) == 2


def test_source_id_keeps_exact_snapshot_and_unknown_id_does_not_fetch(tmp_path):
    calls = []
    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(200, text=PAGE_HTML, headers={"content-type": "text/html"})
    ctx = _ctx(tmp_path, web=_factory(handle))
    registry = ToolRegistry()
    registry.register_all(web_tools())
    runtime = ToolRuntime(registry, PolicyEngine(), ApprovalEngine(prompt=lambda _: "y"))
    opened = runtime.execute("web.open", {"url": "https://example.com/"}, ctx)
    assert opened.ok
    reference = opened.data["source_id"]
    cited = runtime.execute("web.cite", {"source_id": reference}, ctx)
    assert cited.ok and cited.data["citation"]["sha256"] == opened.data["provenance"]["sha256"]
    assert len(calls) == 1
    assert not runtime.execute("web.cite", {"source_id": "expired"}, ctx).ok
    assert len(calls) == 1
