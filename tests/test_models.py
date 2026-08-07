"""Tests para la gestión de modelos: listado detallado + cambio de modelo."""

import httpx
import pytest

from rinari.client import LLMClient


def make_client(resp_body: dict, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=resp_body)

    return LLMClient(base_url="http://x/v1", transport=httpx.MockTransport(handler))


def test_list_models_detailed_returns_dicts():
    """Devuelve los dicts completos de /v1/models."""
    client = make_client({
        "data": [
            {"id": "test-model", "owned_by": "local", "created": 1000},
            {"id": "llama-3.1-8b", "owned_by": "meta", "created": 2000},
        ]
    })
    models = client.list_models_detailed()
    assert len(models) == 2
    assert models[0]["id"] == "test-model"
    assert models[0]["owned_by"] == "local"


def test_list_models_detailed_missing_fields():
    """Modelos sin owned_by/created no rompen (dicts parciales)."""
    client = make_client({"data": [{"id": "solo-id"}]})
    models = client.list_models_detailed()
    assert models[0]["id"] == "solo-id"
    assert "owned_by" not in models[0] or models[0].get("owned_by") is None


def test_list_models_detailed_error():
    """Error HTTP → LLMError con mensaje claro."""
    client = make_client({"error": {"message": "bad key"}}, status=401)
    with pytest.raises(Exception) as exc:
        client.list_models_detailed()
    assert "bad key" in str(exc.value) or "401" in str(exc.value)


def test_list_models_still_works():
    """list_models() (ids) sigue funcionando sobre detailed."""
    client = make_client({"data": [{"id": "a"}, {"id": "b"}]})
    assert client.list_models() == ["a", "b"]
