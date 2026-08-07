"""Tests para la tool de búsqueda web (DuckDuckGo HTML, sin API key)."""

import httpx
import pytest

from rinari.agent.tools import ToolRegistry, web_search

DUCKDUCKGO_HTML = """<!DOCTYPE html>
<html>
<body>
<table>
<tr>
  <td>
    <a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Funo&amp;rut=abc" class='result-link'>Python tutorial</a>
  </td>
</tr>
<tr>
  <td class='result-snippet'>Aprende Python desde cero con ejemplos practicos.</td>
</tr>
<tr>
  <td>
    <a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdos&amp;rut=def" class='result-link'>Python docs</a>
  </td>
</tr>
<tr>
  <td class='result-snippet'>Documentacion oficial de Python.</td>
</tr>
</table>
</body>
</html>
"""


def make_client(resp_body: str, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=resp_body.encode("utf-8"))

    return httpx.MockTransport(handler)


class FakeDuckClient:
    """Fake de httpx.Client tolerante a kwargs, con respuesta configurable."""

    def __init__(self, resp_body: bytes = b"", status: int = 200, error: Exception | None = None, **kwargs):
        self.resp_body = resp_body
        self.status = status
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None, **kwargs):
        if self.error:
            raise self.error
        return httpx.Response(self.status, content=self.resp_body)


def test_web_search_parses_results(monkeypatch):
    """Extrae título, url y snippet de los resultados de DuckDuckGo."""
    import rinari.agent.tools as tools_mod

    captured = {}

    class FakeClient(FakeDuckClient):
        def get(self, url, params=None, **kwargs):
            captured["url"] = url
            captured["params"] = params
            return httpx.Response(200, content=DUCKDUCKGO_HTML.encode("utf-8"))

    monkeypatch.setattr(tools_mod.httpx, "Client", FakeClient)
    result = web_search({"query": "python tutorial"}, cwd="/tmp")
    assert result["ok"] is True
    assert len(result["results"]) == 2
    r = result["results"][0]
    assert r["title"] == "Python tutorial"
    assert r["url"] == "https://example.com/uno"
    assert "Python" in r["snippet"]
    # Verificar que llamó al endpoint correcto con la query
    assert "duckduckgo" in captured["url"]
    assert captured["params"]["q"] == "python tutorial"


def test_web_search_limit(monkeypatch):
    import rinari.agent.tools as tools_mod

    class FakeClient(FakeDuckClient):
        def get(self, url, params=None, **kwargs):
            return httpx.Response(200, content=DUCKDUCKGO_HTML.encode("utf-8"))

    monkeypatch.setattr(tools_mod.httpx, "Client", FakeClient)
    result = web_search({"query": "python", "limit": 1}, cwd="/tmp")
    assert result["ok"] is True
    assert len(result["results"]) == 1


def test_web_search_no_results(monkeypatch):
    import rinari.agent.tools as tools_mod

    class FakeClient(FakeDuckClient):
        def get(self, url, params=None, **kwargs):
            return httpx.Response(200, content=b"<html><body>no results found</body></html>")

    monkeypatch.setattr(tools_mod.httpx, "Client", FakeClient)
    result = web_search({"query": "asdfghjklxyz"}, cwd="/tmp")
    assert result["ok"] is True
    assert result["results"] == []


def test_web_search_http_error(monkeypatch):
    import rinari.agent.tools as tools_mod

    class FakeClient(FakeDuckClient):
        def get(self, url, params=None, **kwargs):
            return httpx.Response(503, content=b"nope")

    monkeypatch.setattr(tools_mod.httpx, "Client", FakeClient)
    result = web_search({"query": "x"}, cwd="/tmp")
    assert result["ok"] is False
    assert "error" in result


def test_web_search_requires_query():
    result = web_search({}, cwd="/tmp")
    assert result["ok"] is False
    assert "query" in result["error"].lower()


def test_registry_exposes_web_search():
    registry = ToolRegistry()
    names = {s["function"]["name"] for s in registry.openai_schemas()}
    assert "web_search" in names


def test_web_search_schema_has_query_param():
    registry = ToolRegistry()
    schemas = {s["function"]["name"]: s for s in registry.openai_schemas()}
    ws = schemas["web_search"]
    props = ws["function"]["parameters"]["properties"]
    assert "query" in props
    assert "limit" in props
