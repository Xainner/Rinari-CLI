"""Creating and editing images (plan 0.4.0 #28): connectors, settings, the
image.* tools, their policy and their cost in /usage."""

from __future__ import annotations

import base64
import io
import itertools
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.engine_protocol.server import EngineServer
from rinari.images import settings as image_settings
from rinari.images.connectors import (
    ChatGPTImagesConnector,
    ImageJob,
    InputImage,
    OpenAIImagesConnector,
)
from rinari.policy.engine import PermissionProfile, PolicyAction, PolicyEngine, SessionScope
from rinari.policy.network import NetworkGuard, NetworkPolicy
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.runtime.cancellation import CancellationToken
from rinari.shared.clock import FakeClock
from rinari.storage.records import SessionEventRecord
from rinari.tools.definition import ToolContext, ToolErrorCode, TurnState
from rinari.tools.native.image_create import ImageHost

_IDS = itertools.count()


def _png(color="purple", size=(64, 64), mode="RGB") -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, color).save(out, format="PNG")
    return out.getvalue()


@pytest.fixture
def server(app_ctx, tmp_path):
    user_home = tmp_path / "home"
    user_home.mkdir()
    services = build_services(app_ctx, user_home=user_home)
    services.providers.add(
        AddProviderInput(
            alias="fake",
            provider_type="openai",
            endpoint="http://127.0.0.1:9/v1",
            secret="dummy-secret-not-real",
        )
    )
    services.models.add("fake", "fake-model-1", "fake-one")
    services.providers.use("fake")
    engine = EngineServer(services)
    yield engine
    engine.close()


def _call(server, method, params=None):
    return server.handle_line(
        json.dumps({"id": f"i-{next(_IDS)}", "method": method, "params": params or {}})
    )


def _ctx(server, tmp_path: Path, session_id: str, events: list) -> ToolContext:
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    services = server._services
    return ToolContext(
        session_id=session_id,
        kind="PROJECT",
        cwd=root,
        project_root=root,
        user_home=tmp_path,
        profile=PermissionProfile.WORKSPACE,
        sandbox=FilesystemSandbox(root, write_roots=(root,), unrestricted=True),
        limits=ProcessLimits(timeout_s=5.0),
        artifact_root=services.artifacts._root(),
        artifact_store=services.artifacts,
        clock=FakeClock(start=1_700_000_000.0, step=0.1),
        cancellation=CancellationToken(),
        network=NetworkGuard(NetworkPolicy()),
        turn_state=TurnState(),
        activity_sink=lambda name, payload: events.append((name, payload)),
        tool_call_id="call_img",
    )


def _session(server) -> str:
    created = _call(server, "session.create", {"chat": True})
    assert created["ok"], created
    return created["result"]["session"]["id"]


def _openai_images(handler, monkeypatch):
    """Route the Engine's HTTP client (rinari.net.client) to a fake service."""
    import rinari.net

    monkeypatch.setattr(
        rinari.net,
        "client",
        lambda home=None, **kw: httpx.Client(transport=httpx.MockTransport(handler)),
    )


# -- connectors ------------------------------------------------------------------


def test_openai_generates_and_edits_through_the_images_api() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.url.path,
                request.headers.get("authorization"),
                request.headers.get("content-type", ""),
            )
        )
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(_png()).decode()}]})

    connector = OpenAIImagesConnector(
        id="openai",
        kind="openai",
        label="OpenAI",
        api_key="sk-test",
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    batch = connector.run(ImageJob("generate", "a moon", quality="low"))
    assert batch.images[0].data.startswith(b"\x89PNG")
    assert batch.cost_usd == pytest.approx(0.011)
    edit = connector.run(ImageJob("remove_background", images=(InputImage(_png()),)))
    assert edit.model == "gpt-image-1"
    assert [path for path, _auth, _type in seen] == ["/v1/images/generations", "/v1/images/edits"]
    assert all(auth == "Bearer sk-test" for _path, auth, _type in seen)
    assert seen[1][2].startswith("multipart/form-data")
    assert not connector.supports("upscale")


def test_chatgpt_subscription_streams_the_hosted_image_tool_and_refreshes_once() -> None:
    image = base64.b64encode(_png("gold")).decode()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, request.headers.get("authorization")))
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "models": [{"slug": "gpt-hidden", "visibility": "hide"}, {"slug": "gpt-sub"}]
                },
            )
        if request.headers.get("authorization") == "Bearer old":
            return httpx.Response(401, json={"detail": "expired"})
        body = json.loads(request.content)
        assert body["tools"][0]["type"] == "image_generation"
        assert body["tools"][0]["background"] == "transparent"
        assert body["input"][0]["content"][1]["type"] == "input_image"
        stream = "\n".join(
            [
                "data: " + json.dumps({"type": "response.created"}),
                "data: "
                + json.dumps(
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "image_generation_call",
                            "result": image,
                            "revised_prompt": "gold moon",
                        },
                    }
                ),
                "data: " + json.dumps({"type": "response.completed"}),
            ]
        )
        return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})

    tokens = iter(["old"])
    connector = ChatGPTImagesConnector(
        id="codex",
        kind="chatgpt",
        label="ChatGPT",
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
        token=lambda: next(tokens),
        refresh=lambda rejected: "fresh" if rejected == "old" else rejected,
    )
    batch = connector.run(ImageJob("remove_background", images=(InputImage(_png()),)))
    assert batch.model == "gpt-sub"
    assert batch.cost_usd == 0.0
    assert batch.images[0].revised_prompt == "gold moon"
    assert [auth for path, auth in calls if path.endswith("/responses")] == [
        "Bearer old",
        "Bearer fresh",
    ]
    assert connector.estimate(ImageJob("generate", "x")).label() == "included in the subscription"


# -- settings --------------------------------------------------------------------


def test_settings_keep_a_reference_never_the_key_and_reject_unknown_choices(server) -> None:
    saved = _call(
        server,
        "images.settings.set",
        {
            "connectors": [
                {"id": "openai", "kind": "openai", "label": "OpenAI", "api_key": "sk-secret-123"}
            ],
            "default_connector": "openai",
            "session_budget_usd": 2,
        },
    )
    assert saved["ok"], saved
    result = saved["result"]
    assert result["connectors"][0]["has_key"] is True
    home = Path(server._services.ctx.home)
    raw = (home / image_settings.FILE).read_text(encoding="utf-8")
    assert "sk-secret-123" not in raw
    assert "media/openai/api-key" in raw
    bad = _call(server, "images.settings.set", {"default_connector": "nope"})
    assert not bad["ok"]
    # Removing the connector takes its key along.
    ref = json.loads(raw)["connectors"][0]["api_key_ref"]
    assert server._services.credentials.exists(ref)
    cleared = _call(server, "images.settings.set", {"connectors": [], "default_connector": None})
    assert cleared["ok"], cleared
    assert not server._services.credentials.exists(ref)


# -- the tools -------------------------------------------------------------------


def test_without_a_connector_the_tools_say_so_and_never_improvise(server, tmp_path) -> None:
    session_id = _session(server)
    host = ImageHost(server._services)
    result = host.run("generate", {"prompt": "a moon"}, _ctx(server, tmp_path, session_id, []))
    assert not result.ok
    assert result.error.code is ToolErrorCode.UNSUPPORTED
    assert result.error.details["reason"] == "NO_IMAGE_CONNECTOR"
    assert "Creación de imágenes" in result.error.message


def test_generate_stores_the_image_shows_it_and_records_its_cost(
    server, tmp_path, monkeypatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(_png()).decode()}]})

    _openai_images(handler, monkeypatch)
    assert _call(
        server,
        "images.settings.set",
        {
            "connectors": [
                {"id": "openai", "kind": "openai", "label": "OpenAI", "api_key": "sk-x"}
            ],
            "default_connector": "openai",
        },
    )["ok"]
    session_id = _session(server)
    events: list = []
    ctx = _ctx(server, tmp_path, session_id, events)
    host = ImageHost(server._services)
    action = host.classify("generate", {"prompt": "a moon", "quality": "low"})
    assert action.capability == "media.generate"
    assert "OpenAI" in action.target and "~$0.011" in action.target
    result = host.run(
        "generate", {"prompt": "a moon", "quality": "low", "name": "Luna Morada"}, ctx
    )
    assert result.ok, result.error
    image = result.data["images"][0]
    assert image["name"].startswith("luna-morada-") and image["uri"].startswith("artifact://")
    assert Path(image["path"]).read_bytes().startswith(b"\x89PNG")
    # The model gets to look at it; the owner gets the delivery card.
    assert result.images and result.images[0].uri == image["uri"]
    names = [name for name, _payload in events]
    # P1: the model looks first; the owner's card comes from file.present.
    assert names == ["media.generated"]
    assert events[0][1]["cost_usd"] == pytest.approx(0.011)
    assert "file.present" in result.data["note"]
    from rinari.tools.native.present import present_tools

    present = present_tools()[0]
    # P3: presented by its store path, the card carries the artifact URI.
    shown = present.handler({"items": [{"path": image["path"], "caption": "Luna"}]}, ctx)
    assert shown.ok and shown.data["presented"] == 1
    card = events[-1][1]["items"][0]
    assert events[-1][0] == "file.presented" and card["kind"] == "image"
    assert card["uri"] == image["uri"]
    # One picture, one card: presenting it again (by URI) shows nothing new.
    again = present.handler({"items": [{"path": image["uri"]}]}, ctx)
    assert again.ok and again.data["presented"] == 0 and again.data["already_shown"] == 1
    assert [name for name, _ in events].count("file.presented") == 1
    # Already presented, so the end of the turn adds nothing.
    from rinari.cli.agent_runtime import _present_unshown

    _present_unshown(ctx)
    assert [name for name, _ in events].count("file.presented") == 1
    # An operation the connector cannot do is refused before asking anyone.
    upscale = host.precheck("upscale", {"image": image["uri"]}, ctx)
    assert upscale is not None and upscale.error.details["reason"] == "OPERATION_UNSUPPORTED"


def test_an_image_the_model_never_presents_is_shown_when_the_turn_ends(
    server, tmp_path, monkeypatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(_png()).decode()}]})

    _openai_images(handler, monkeypatch)
    assert _call(
        server,
        "images.settings.set",
        {
            "connectors": [
                {"id": "openai", "kind": "openai", "label": "OpenAI", "api_key": "sk-x"}
            ],
            "default_connector": "openai",
        },
    )["ok"]
    session_id = _session(server)
    events: list = []
    ctx = _ctx(server, tmp_path, session_id, events)
    result = ImageHost(server._services).run("generate", {"prompt": "a moon"}, ctx)
    assert result.ok, result.error
    from rinari.cli.agent_runtime import _present_unshown

    _present_unshown(ctx)
    cards = [payload for name, payload in events if name == "file.presented"]
    assert len(cards) == 1
    assert cards[0]["items"][0]["uri"] == result.data["images"][0]["uri"]
    _present_unshown(ctx)
    assert [name for name, _ in events].count("file.presented") == 1


def test_the_session_spend_cap_stops_a_paid_image_before_asking(server, tmp_path) -> None:
    assert _call(
        server,
        "images.settings.set",
        {
            "connectors": [
                {"id": "openai", "kind": "openai", "label": "OpenAI", "api_key": "sk-x"}
            ],
            "default_connector": "openai",
            "session_budget_usd": 0.06,
        },
    )["ok"]
    session_id = _session(server)
    repo = server._services.ctx.event_repo
    repo.insert(
        SessionEventRecord(
            id="evt-media-1",
            session_id=session_id,
            seq=repo.next_seq(session_id),
            type="media.generated",
            payload={"connector": "openai", "images": 1, "cost_usd": 0.042},
            created_at="2026-10-17T10:00:00Z",
        )
    )
    host = ImageHost(server._services)
    ctx = _ctx(server, tmp_path, session_id, [])
    rejected = host.precheck("generate", {"prompt": "x", "quality": "medium"}, ctx)
    assert rejected is not None
    assert rejected.error.code is ToolErrorCode.RESOURCE_EXHAUSTED
    assert host.precheck("generate", {"prompt": "x", "quality": "low"}, ctx) is None


def test_creating_an_image_asks_with_its_cost_except_in_full_access(tmp_path) -> None:
    engine = PolicyEngine(network=NetworkPolicy())
    target = "OpenAI · gpt-image-1 · 1 x auto · ~$0.042"
    for profile in (PermissionProfile.READ_ONLY, PermissionProfile.WORKSPACE):
        scope = SessionScope(kind="PROJECT", root=tmp_path, cwd=tmp_path, profile=profile)
        decision = engine.decide("media.generate", scope, target=target)
        assert decision.action is PolicyAction.ASK
        assert decision.target == target and decision.rule_id == "media_generate"
    full = SessionScope(
        kind="PROJECT", root=tmp_path, cwd=tmp_path, profile=PermissionProfile.FULL_ACCESS
    )
    assert engine.decide("media.generate", full, target=target).action is PolicyAction.ALLOW


def test_an_image_at_no_extra_cost_is_not_asked_for_in_project_mode(tmp_path) -> None:
    # P2: the owner was asked for an image their subscription already
    # includes. What decides is the cost the service declares, not its name.
    engine = PolicyEngine(network=NetworkPolicy())
    project = SessionScope(
        kind="PROJECT", root=tmp_path, cwd=tmp_path, profile=PermissionProfile.WORKSPACE
    )
    read_only = SessionScope(
        kind="PROJECT", root=tmp_path, cwd=tmp_path, profile=PermissionProfile.READ_ONLY
    )
    for basis in ("included", "local"):
        assert engine.decide("media.generate", project, cost_basis=basis).action is (
            PolicyAction.ALLOW
        )
        assert engine.decide("media.generate", read_only, cost_basis=basis).action is (
            PolicyAction.ASK
        )
    for basis in ("per_image", "unknown", None):
        assert engine.decide("media.generate", project, cost_basis=basis).action is (
            PolicyAction.ASK
        )


def test_usage_report_counts_images_and_adds_their_cost(server) -> None:
    session_id = _session(server)
    repo = server._services.ctx.event_repo
    for cost in (0.042, 0.0):
        repo.insert(
            SessionEventRecord(
                id=f"evt-media-{cost}",
                session_id=session_id,
                seq=repo.next_seq(session_id),
                type="media.generated",
                payload={"connector": "openai" if cost else "codex", "images": 1, "cost_usd": cost},
                created_at="2026-10-17T10:00:00Z",
            )
        )
    report = _call(server, "usage.report", {"scope": "session", "ref": session_id})
    assert report["ok"], report
    media = (
        report["result"]["report"]["media"]
        if "report" in report["result"]
        else report["result"]["media"]
    )
    assert media["images"] == 2
    assert media["cost"] == pytest.approx(0.042)


def test_the_service_builds_each_new_kind_from_the_settings(server, monkeypatch) -> None:
    from rinari.images.more_connectors import (
        ComfyUIConnector,
        FalImagesConnector,
        McpImagesConnector,
    )
    from rinari.images.service import ImageService

    saved = _call(
        server,
        "images.settings.set",
        {
            "connectors": [
                {"id": "fal", "kind": "fal", "label": "FAL", "api_key": "fal-secret"},
                {
                    "id": "comfy",
                    "kind": "comfyui",
                    "label": "ComfyUI",
                    "workflows": {"upscale": {"1": {}}},
                },
                {
                    "id": "lab",
                    "kind": "mcp",
                    "label": "Lab",
                    "server": "lab",
                    "tools": {"generate": {"tool": "make"}},
                },
            ],
            "default_connector": "fal",
            "edit_connector": "comfy",
        },
    )
    assert saved["ok"], saved
    assert saved["result"]["connectors"][0]["has_key"] is True
    service = ImageService(server._services)
    fal = service.connector("generate")
    assert isinstance(fal, FalImagesConnector) and fal.api_key == "fal-secret"
    assert isinstance(service.connector("upscale"), ComfyUIConnector)
    calls = []

    class Client:
        def call_tool(self, tool, arguments):
            calls.append((tool, arguments))
            return {
                "content": [
                    {
                        "type": "image",
                        "data": base64.b64encode(_png()).decode(),
                        "mimeType": "image/png",
                    }
                ]
            }

    monkeypatch.setattr(server._services.mcp, "client", lambda name, project=None: Client())
    entry = next(
        item
        for item in image_settings.load(Path(server._services.ctx.home))["connectors"]
        if item["id"] == "lab"
    )
    lab = service.build(entry)
    assert isinstance(lab, McpImagesConnector)
    assert lab.run(ImageJob("generate", "luna")).images
    assert calls == [("make", {})]
