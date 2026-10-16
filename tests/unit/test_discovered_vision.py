"""What an endpoint announces about vision, and the image limit (plan 0.4.0 #14)."""

from types import SimpleNamespace

from rinari.context.windows import normalize
from rinari.models.router import _vision_constraints


def test_xainner_llama_server_shape():
    entry = {
        "id": "qwen3.8-27b-fp8",
        "supports_vision": True,
        "input_modalities": ["text", "image"],
        "max_images_per_request": 20,
        "vision_limits": {"max_images": 20},
    }
    assert normalize(entry)["vision"] is True
    assert normalize(entry)["max_images"] == 20
    assert normalize({"id": "ultra", "vision_limits": {"max_images": 1}})["max_images"] == 1


def test_openrouter_and_llama_cpp_shapes():
    assert normalize({"architecture": {"input_modalities": ["text", "image"]}})["vision"] is True
    assert normalize({"architecture": {"input_modalities": ["text"]}})["vision"] is False
    assert normalize({"modalities": {"input": ["text", "image"]}})["vision"] is True
    assert normalize({"capabilities": ["completion", "multimodal"]})["vision"] is True
    assert "vision" not in normalize({"id": "plain", "context_length": 8192})


def test_the_announced_limit_reaches_the_call_and_settings_still_win():
    model = SimpleNamespace(settings={"discovered_capabilities": {"max_images": 20}})
    provider = SimpleNamespace(settings={})
    assert _vision_constraints(provider, model) == {"max_images": 20}
    model.settings["vision_limits"] = {"max_images": 4}
    assert _vision_constraints(provider, model)["max_images"] == 4
