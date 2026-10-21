"""The cached prefix holds across a conversation, per protocol (F42 guard).

Two requests in a row of the same conversation, with an activation, a call
through the door and a new turn in between, must start byte for byte the same
on the wire: same tools, same system prompt, and the earlier messages
unchanged, except the volatile note that closes each request.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from itertools import pairwise

import pytest

from rinari.capability_search import capability_activation_tools, capability_search_tool
from rinari.models.router import _alias_map_for_request
from rinari.models.types import ModelRequest, ModelResponse, ToolCall
from rinari.policy.engine import PermissionProfile
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.prompts.assembler import AssemblerContext, PromptAssembler
from rinari.providers.adapters.anthropic import AnthropicAdapter
from rinari.providers.adapters.openai_compatible import OpenAICompatibleAdapter
from rinari.providers.adapters.responses import OpenAIResponsesAdapter
from rinari.runtime.agent import AgentContext, AgentLoop
from rinari.runtime.cancellation import CancellationToken
from rinari.shared.clock import FakeClock
from rinari.tools.definition import ToolContext
from rinari.tools.door import door_tool
from rinari.tools.exposure import ToolExposure
from tests.unit.test_agent_loop import FakeModel, make_tool_runtime


def _conversation(tmp_path, agent_identity: str | None = None) -> list[ModelRequest]:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    (root / "b.txt").write_text("two\n", encoding="utf-8")
    clock = FakeClock(start=1_700_000_000.0, step=0.05)
    runtime, _scope = make_tool_runtime(tmp_path, root, clock)
    registry = runtime.registry
    registry.register(capability_search_tool(registry))
    registry.register_all(capability_activation_tools(registry))
    registry.register(door_tool())
    tool_ctx = ToolContext(
        session_id="s1",
        kind="PROJECT",
        cwd=root,
        project_root=root,
        user_home=tmp_path,
        profile=PermissionProfile.WORKSPACE,
        sandbox=FilesystemSandbox(read_root=root, write_roots=(root,)),
        limits=ProcessLimits(timeout_s=30, max_output_bytes=65536),
        artifact_root=tmp_path / "artifacts",
        clock=clock,
        cancellation=CancellationToken(),
        exposure=ToolExposure(key={"engine": "t", "model": "fake-model", "compaction": 0}),
    )
    ctx = AgentContext(
        session_id="s1",
        model_ref="fake-model",
        tool_ctx=tool_ctx,
        # "repository" is volatile: it closes every request as the turn note.
        assembler_base=AssemblerContext(
            session_kind="PROJECT",
            constitution="You are RINARI.",
            environment={"cwd": str(root), "repository": "files: a.txt, b.txt"},
            agent_identity=agent_identity,
        ),
    )
    diff = '{"a": "a.txt", "b": "b.txt"}'
    model = FakeModel(
        scripted=[
            ModelResponse(
                content="",
                tool_calls=(ToolCall("act", "capability.activate", {"names": ["fs.diff"]}),),
            ),
            ModelResponse(
                content="",
                tool_calls=(ToolCall("door", "tool.call", {"name": "fs.diff", "arguments": diff}),),
            ),
            ModelResponse(content="They differ in one line."),
            ModelResponse(content="Yes."),
        ]
    )
    loop = AgentLoop(model, runtime, PromptAssembler())
    loop.turn(ctx, "compare a and b")
    ctx.tool_ctx = replace(ctx.tool_ctx, exposure=ctx.tool_ctx.exposure)
    loop.turn(ctx, "sure?")
    assert len(model.requests) == 4
    return model.requests


def _strip(value):
    """Cache breakpoints mark the end of a request, not its content."""
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


def _chat(request):
    payload = OpenAICompatibleAdapter()._payload(
        request, stream=False, tool_aliases=_alias_map_for_request(None, request)
    )
    return payload["tools"], payload["messages"]


def _responses(request):
    payload = OpenAIResponsesAdapter()._responses_payload(
        request, stream=False, tool_aliases=_alias_map_for_request(None, request)
    )
    return payload["tools"], payload["input"]


def _anthropic(request):
    payload = AnthropicAdapter()._payload(
        request, stream=False, tool_aliases=_alias_map_for_request(None, request)
    )
    return payload["tools"], [payload.get("system"), *payload["messages"]]


@pytest.mark.parametrize(
    "wire", [_chat, _responses, _anthropic], ids=["chat", "responses", "anthropic"]
)
def test_every_request_starts_like_the_one_before(tmp_path, wire) -> None:
    requests = _conversation(tmp_path)
    for before, after in pairwise(requests):
        tools_before, messages_before = wire(before)
        tools_after, messages_after = wire(after)
        assert tools_after == tools_before
        # The last element carries the volatile note (and, for Anthropic, the
        # tool results it joins): everything before it is the cached prefix.
        kept = _strip(messages_before[:-1])
        assert _strip(messages_after[: len(kept)]) == kept


@pytest.mark.parametrize(
    "wire", [_chat, _responses, _anthropic], ids=["chat", "responses", "anthropic"]
)
def test_an_agents_card_stays_in_the_cached_prefix(tmp_path, wire) -> None:
    """Rinari Agency: the agent's block is fixed per session, so the prefix holds."""
    card = "## Your role in Rinari Agency\nYou work as the agent «Writer»."
    requests = _conversation(tmp_path, agent_identity=card)
    for before, after in pairwise(requests):
        tools_before, messages_before = wire(before)
        tools_after, messages_after = wire(after)
        assert tools_after == tools_before
        kept = _strip(messages_before[:-1])
        assert _strip(messages_after[: len(kept)]) == kept
    assert "«Writer»" in str(wire(requests[0])[1][0])


def test_the_door_and_search_are_in_the_list_and_the_activated_tool_is_not(tmp_path) -> None:
    requests = _conversation(tmp_path)
    names = {tool.name for tool in requests[0].tools}
    assert {"tool.call", "capability.search"} <= names
    assert "fs.diff" not in names


def test_a_changed_prefix_is_logged_with_what_changed(tmp_path, caplog) -> None:
    from rinari.models.types import ChatMessage, ToolSchema
    from rinari.runtime.agent import _watch_prefix

    exposure = ToolExposure(key={"model": "m1"})
    ctx = AgentContext(
        session_id="s9",
        model_ref="m1",
        tool_ctx=ToolContext(
            session_id="s9",
            kind="CHAT",
            cwd=tmp_path,
            project_root=None,
            user_home=tmp_path,
            profile=PermissionProfile.WORKSPACE,
            sandbox=FilesystemSandbox(read_root=tmp_path, write_roots=(tmp_path,)),
            limits=ProcessLimits(timeout_s=30, max_output_bytes=65536),
            artifact_root=tmp_path / "artifacts",
            clock=FakeClock(start=0.0, step=1.0),
            cancellation=CancellationToken(),
            exposure=exposure,
        ),
        assembler_base=AssemblerContext(),
    )
    one = ModelRequest(
        model="m1",
        messages=(ChatMessage.system("rules"),),
        tools=(ToolSchema("fs.read", "read", {"type": "object"}),),
    )
    two = replace(one, tools=(*one.tools, ToolSchema("fs.stat", "stat", {"type": "object"})))
    with caplog.at_level(logging.INFO, logger="rinari.cache"):
        _watch_prefix(ctx, one)
        _watch_prefix(ctx, one)
        assert not caplog.records
        _watch_prefix(ctx, two)
        (warned,) = caplog.records
        assert warned.levelno == logging.WARNING and "tools" in warned.getMessage()
        caplog.clear()
        exposure.key = {"model": "m2"}
        _watch_prefix(ctx, one)
        (expected,) = caplog.records
        assert (
            expected.levelno == logging.INFO and "new model or compaction" in expected.getMessage()
        )
        # A system change names the segments that moved.
        caplog.clear()
        ctx.prompt_segments = {"constitution": "c1", "environment": "e1"}
        _watch_prefix(ctx, one)
        caplog.clear()
        ctx.prompt_segments = {"constitution": "c1", "environment": "e2"}
        moved = replace(one, messages=(ChatMessage.system("rules, cwd moved"),))
        _watch_prefix(ctx, moved)
        (named,) = caplog.records
        assert "system (segments: environment)" in named.getMessage()


def _late_server_conversation(tmp_path, app_ctx) -> tuple[list[ModelRequest], str]:
    """Two turns while an MCP server connects in the middle of the first (C2)."""
    import json
    import threading

    from rinari.application.services import build_services
    from rinari.mcp.transport import InProcessTransport
    from rinari.tools.index import connected_index

    gate, connected = threading.Event(), threading.Event()

    def handle(frame: str):
        message = json.loads(frame)
        if "id" not in message:
            return None
        if message["method"] == "initialize":
            gate.wait(10)
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}}
        else:
            tool = {"name": "lookup", "description": "Look a record up", "inputSchema": {}}
            result = {"tools": [tool]}
        return json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result})

    services = build_services(app_ctx, user_home=tmp_path)
    services.mcp.add("tracker", ["fake"])
    services.mcp._transport_for = lambda row: InProcessTransport(handle)
    services.mcp.on_status = lambda view: view["status"] == "connected" and connected.set()

    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    clock = FakeClock(start=1_700_000_000.0, step=0.05)
    runtime, _scope = make_tool_runtime(tmp_path, root, clock)
    registry = runtime.registry
    registry.register(capability_search_tool(registry))
    registry.register_all(capability_activation_tools(registry))
    registry.register(door_tool())
    services.mcp.start(None)
    services.mcp.follow(registry, None)
    tool_ctx = ToolContext(
        session_id="s2",
        kind="PROJECT",
        cwd=root,
        project_root=root,
        user_home=tmp_path,
        profile=PermissionProfile.WORKSPACE,
        sandbox=FilesystemSandbox(read_root=root, write_roots=(root,)),
        limits=ProcessLimits(timeout_s=30, max_output_bytes=65536),
        artifact_root=tmp_path / "artifacts",
        clock=clock,
        cancellation=CancellationToken(),
        exposure=ToolExposure(key={"engine": "t", "model": "fake-model", "compaction": 0}),
    )
    ctx = AgentContext(
        session_id="s2",
        model_ref="fake-model",
        tool_ctx=tool_ctx,
        assembler_base=AssemblerContext(
            session_kind="PROJECT",
            constitution="You are RINARI.",
            environment={"cwd": str(root)},
        ),
        # The MCP servers' state of each request (C6): connecting, then connected.
        connected_index=lambda: connected_index(registry, services.mcp),
    )

    class ConnectsMidTurn(FakeModel):
        def invoke(self, request):
            if len(self.requests) == 1:
                gate.set()  # the server finishes connecting during the turn
                assert connected.wait(5)
            return super().invoke(request)

    model = ConnectsMidTurn(
        scripted=[
            ModelResponse(content="", tool_calls=(ToolCall("read", "fs.read", {"path": "a.txt"}),)),
            ModelResponse(content="It says one."),
            ModelResponse(content="Yes."),
        ]
    )
    loop = AgentLoop(model, runtime, PromptAssembler())
    try:
        loop.turn(ctx, "what does a.txt say?")
        loop.turn(ctx, "sure?")
    finally:
        gate.set()
        services.mcp.close()
    assert len(model.requests) == 3
    return model.requests, "mcp.tracker.lookup"


@pytest.mark.parametrize(
    "wire", [_chat, _responses, _anthropic], ids=["chat", "responses", "anthropic"]
)
def test_a_server_that_connects_late_changes_neither_the_list_nor_the_prefix(
    tmp_path, app_ctx, wire
) -> None:
    requests, late_tool = _late_server_conversation(tmp_path, app_ctx)
    for before, after in pairwise(requests):
        tools_before, messages_before = wire(before)
        tools_after, messages_after = wire(after)
        assert tools_after == tools_before
        kept = _strip(messages_before[:-1])
        assert _strip(messages_after[: len(kept)]) == kept
    # The late server reached the turn note, never the tool list: first as
    # still connecting (C6), then with its tools.
    assert late_tool not in str(wire(requests[0]))
    assert "mcp.tracker (connecting)" in str(wire(requests[0])[1])
    assert late_tool in str(wire(requests[-1])[1])
    assert late_tool not in str(wire(requests[-1])[0])
