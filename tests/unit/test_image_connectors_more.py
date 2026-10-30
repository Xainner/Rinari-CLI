"""More image connectors (plan 0.4.0 block 29): FAL.ai, Google, OpenRouter,
a local ComfyUI and an assigned MCP server, each against a fake service."""

from __future__ import annotations

import base64
import io
import json

import httpx
import pytest
from PIL import Image

from rinari.images.connectors import ImageConnectorError, ImageJob, InputImage
from rinari.images.more_connectors import (
    ComfyUIConnector,
    FalImagesConnector,
    GoogleImagesConnector,
    McpImagesConnector,
    OpenRouterImagesConnector,
)
from rinari.images.settings import validate


def _png(color="purple") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(out, format="PNG")
    return out.getvalue()


def _client(handler):
    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


def test_fal_generates_downloads_and_runs_the_right_model_per_operation() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.fal.media":
            return httpx.Response(200, content=_png(), headers={"content-type": "image/png"})
        seen.append(
            (request.url.path, request.headers.get("authorization"), json.loads(request.content))
        )
        return httpx.Response(200, json={"images": [{"url": "https://cdn.fal.media/out.png"}]})

    fal = FalImagesConnector(
        id="fal", kind="fal", label="FAL", api_key="k1", client_factory=_client(handler)
    )
    batch = fal.run(ImageJob("generate", "a moon", size="1536x1024"))
    assert batch.images[0].data.startswith(b"\x89PNG")
    assert batch.cost_usd == pytest.approx(0.003)
    cut = fal.run(ImageJob("remove_background", images=(InputImage(_png()),)))
    up = fal.run(ImageJob("upscale", images=(InputImage(_png()),)))
    assert [path for path, _auth, _body in seen] == [
        "/fal-ai/flux/schnell",
        "/fal-ai/birefnet",
        "/fal-ai/clarity-upscaler",
    ]
    assert all(auth == "Key k1" for _path, auth, _body in seen)
    assert seen[0][2]["image_size"] == "landscape_4_3"
    assert seen[1][2]["image_url"].startswith("data:image/png;base64,")
    assert cut.model == "fal-ai/birefnet" and up.model == "fal-ai/clarity-upscaler"
    # FAL can upscale; a model the owner picked has no known price.
    assert fal.supports("upscale")
    custom = FalImagesConnector(
        id="fal", kind="fal", label="FAL", api_key="k1", models={"generate": "fal-ai/flux/dev"}
    )
    assert custom.estimate(ImageJob("generate", "x")).label() == "cost unknown"


def test_google_returns_the_inline_image_and_sends_the_key_as_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models/gemini-2.5-flash-image:generateContent")
        assert request.headers["x-goog-api-key"] == "g1"
        body = json.loads(request.content)
        assert body["generationConfig"]["responseModalities"] == ["IMAGE"]
        assert body["contents"][0]["parts"][1]["inline_data"]["mime_type"] == "image/png"
        data = base64.b64encode(_png("gold")).decode()
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "here"},
                                {"inlineData": {"mimeType": "image/png", "data": data}},
                            ]
                        }
                    }
                ]
            },
        )

    google = GoogleImagesConnector(
        id="g", kind="google", label="Google", api_key="g1", client_factory=_client(handler)
    )
    batch = google.run(ImageJob("edit", "make it gold", images=(InputImage(_png()),)))
    assert batch.images[0].mime == "image/png" and batch.cost_usd == pytest.approx(0.039)
    assert not google.supports("upscale")


def test_openrouter_reads_images_and_the_cost_it_reports() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["modalities"] == ["image", "text"]
        url = "data:image/png;base64," + base64.b64encode(_png()).decode()
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"images": [{"image_url": {"url": url}}]}}],
                "usage": {"cost": 0.0123},
            },
        )

    router = OpenRouterImagesConnector(
        id="or",
        kind="openrouter",
        label="OpenRouter",
        api_key="r1",
        client_factory=_client(handler),
    )
    assert router.estimate(ImageJob("generate", "x")).label() == "cost unknown"
    batch = router.run(ImageJob("generate", "a moon"))
    assert batch.cost_usd == pytest.approx(0.0123)
    assert batch.images[0].data.startswith(b"\x89PNG")


def test_comfyui_fills_the_workflow_waits_for_it_and_fetches_the_output() -> None:
    posted = []
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/upload/image":
            return httpx.Response(200, json={"name": "rinari-up.png"})
        if request.url.path == "/prompt":
            posted.append(json.loads(request.content)["prompt"])
            return httpx.Response(200, json={"prompt_id": "p1"})
        if request.url.path == "/history/p1":
            polls["n"] += 1
            if polls["n"] == 1:
                return httpx.Response(200, json={})
            return httpx.Response(
                200,
                json={
                    "p1": {
                        "outputs": {
                            "9": {
                                "images": [
                                    {"filename": "out.png", "subfolder": "", "type": "output"}
                                ]
                            }
                        }
                    }
                },
            )
        if request.url.path == "/view":
            assert request.url.params["filename"] == "out.png"
            return httpx.Response(200, content=_png("teal"), headers={"content-type": "image/png"})
        return httpx.Response(404)

    workflow = {"3": {"inputs": {"text": "{{prompt}}"}}, "5": {"inputs": {"image": "{{image}}"}}}
    comfy = ComfyUIConnector(
        id="comfy",
        kind="comfyui",
        label="ComfyUI",
        client_factory=_client(handler),
        workflows={"upscale": workflow},
        sleep=lambda _s: None,
    )
    assert comfy.supports("upscale") and not comfy.supports("generate")
    assert comfy.estimate(ImageJob("upscale")).label() == "free (runs on your computer)"
    batch = comfy.run(ImageJob("upscale", 'say "hi"', images=(InputImage(_png()),)))
    assert batch.images[0].data.startswith(b"\x89PNG")
    assert posted[0]["3"]["inputs"]["text"] == 'say "hi"'
    assert posted[0]["5"]["inputs"]["image"] == "rinari-up.png"
    assert polls["n"] == 2


def test_an_assigned_mcp_tool_answers_with_image_blocks_links_or_an_error() -> None:
    calls = []

    def call(tool, arguments):
        calls.append((tool, arguments))
        if tool == "make":
            return {
                "content": [
                    {
                        "type": "image",
                        "data": base64.b64encode(_png()).decode(),
                        "mimeType": "image/png",
                    }
                ]
            }
        if tool == "link":
            return {"content": [{"type": "text", "text": "Done: https://media.example.com/a.png"}]}
        return {"isError": True, "content": [{"type": "text", "text": "quota exceeded"}]}

    def download(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_png("green"), headers={"content-type": "image/png"})

    mcp = McpImagesConnector(
        id="lab",
        kind="mcp",
        label="Visual",
        call=call,
        client_factory=_client(download),
        tools={
            "generate": {"tool": "make", "prompt_arg": "prompt"},
            "edit": {"tool": "link", "prompt_arg": "p", "image_arg": "img"},
            "upscale": {"tool": "fail"},
        },
    )
    assert mcp.run(ImageJob("generate", "moon")).images[0].mime == "image/png"
    edited = mcp.run(ImageJob("edit", "gold", images=(InputImage(_png()),)))
    assert edited.images[0].data.startswith(b"\x89PNG")
    assert calls[1][1]["p"] == "gold" and calls[1][1]["img"].startswith("data:image/png;base64,")
    with pytest.raises(ImageConnectorError, match="quota exceeded"):
        mcp.run(ImageJob("upscale", images=(InputImage(_png()),)))
    assert not mcp.supports("remove_background")


def test_settings_accept_the_new_kinds_and_keep_comfyui_on_the_local_network() -> None:
    value = validate(
        {
            "connectors": [
                {
                    "id": "fal",
                    "kind": "fal",
                    "api_key_ref": "file://media/fal/api-key",
                    "models": {"upscale": "fal-ai/esrgan"},
                },
                {
                    "id": "comfy",
                    "kind": "comfyui",
                    "endpoint": "http://192.168.1.20:8188",
                    "workflows": {"generate": {"1": {}}},
                },
                {
                    "id": "lab",
                    "kind": "mcp",
                    "server": "visual-lab",
                    "tools": {"generate": {"tool": "make", "prompt_arg": "prompt"}},
                },
                {"id": "or", "kind": "openrouter", "provider_id": "prov_or"},
            ],
            "default_connector": "fal",
        }
    )
    assert [item["kind"] for item in value["connectors"]] == ["fal", "comfyui", "mcp", "openrouter"]
    with pytest.raises(ValueError, match="local network"):
        validate(
            {"connectors": [{"id": "c", "kind": "comfyui", "endpoint": "http://example.com:8188"}]}
        )
    with pytest.raises(ValueError, match="at least one operation"):
        validate({"connectors": [{"id": "m", "kind": "mcp", "server": "x", "tools": {}}]})
