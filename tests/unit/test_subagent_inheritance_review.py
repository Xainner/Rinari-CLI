"""Adversarial checks for effective subagent permission inheritance."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from rinari.agents.definition import AgentDefinition
from rinari.agents.orchestrator import SubagentRunSpec
from rinari.agents.runtime import SubagentRuntimeConfig, make_subagent_runner
from rinari.policy.engine import PermissionProfile, PolicyEngine
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.runtime.cancellation import CancellationToken
from rinari.shared.clock import FakeClock
from rinari.tools.definition import ToolContext, ToolDefinition
from rinari.tools.native.process import ProcessRegistry
from rinari.tools.registry import ToolRegistry


def _tool(name: str) -> ToolDefinition:
    return ToolDefinition(name, name, {"type": "object"})


def _parent_ctx(root, clock) -> ToolContext:
    return ToolContext(
        session_id="parent",
        kind="PROJECT",
        cwd=root,
        project_root=root,
        user_home=root,
        profile=PermissionProfile.WORKSPACE,
        sandbox=FilesystemSandbox(root, (root,)),
        limits=ProcessLimits(),
        artifact_root=root / "artifacts",
        clock=clock,
        cancellation=CancellationToken(),
        processes=ProcessRegistry(),
    )


def test_inherited_child_keeps_ceiling_but_loses_owner_channel_context(tmp_path) -> None:
    parent_registry = ToolRegistry()
    for name in (
        "fs.read",
        "memory.remember",
        "channel.send",
        "artifact.import",
        "capability.search",
    ):
        parent_registry.register(_tool(name))
    parent_runtime = SimpleNamespace(registry=parent_registry)
    parent_ctx = replace(
        _parent_ctx(tmp_path, FakeClock()),
        profile=PermissionProfile.WORKSPACE,
        sandbox=FilesystemSandbox(tmp_path, (tmp_path,)),
        channel_host=object(),
        memory_source={"session_id": "owner", "message_id": "owner-message"},
    )
    runner = make_subagent_runner(
        SubagentRuntimeConfig(
            None,
            None,
            CancellationToken(),
            PolicyEngine(),
            None,
            parent_ctx,
            parent_runtime=lambda: parent_runtime,
        )
    )
    spec = SubagentRunSpec(
        agent_id="child",
        definition=AgentDefinition("custom", "", "", profile="inherit"),
        objective="inspect",
        project_root=tmp_path,
        session_id="parent",
    )

    ctx = runner._build_tool_ctx(spec)
    registry = runner._build_registry(spec)
    assert ctx.profile is PermissionProfile.WORKSPACE
    assert ctx.sandbox.read_root == parent_ctx.sandbox.read_root
    assert ctx.sandbox.write_roots == parent_ctx.sandbox.write_roots
    assert ctx.channel_host is None
    assert ctx.memory_source is None
    assert "fs.read" in registry.names()
    assert "memory.remember" in registry.names()
    assert "channel.send" not in registry.names()
    assert "artifact.import" not in registry.names()
    # Discovery is rebuilt over the restricted child catalog.
    assert "capability.search" in registry.names()
    ctx.browser.close()


def test_explicit_child_read_only_narrows_inherited_workspace(tmp_path) -> None:
    parent_ctx = replace(
        _parent_ctx(tmp_path, FakeClock()),
        profile=PermissionProfile.WORKSPACE,
        sandbox=FilesystemSandbox(tmp_path, (tmp_path,)),
    )
    runner = make_subagent_runner(
        SubagentRuntimeConfig(
            None,
            ToolRegistry(),
            CancellationToken(),
            PolicyEngine(),
            None,
            parent_ctx,
        )
    )
    spec = SubagentRunSpec(
        agent_id="child",
        definition=AgentDefinition("custom", "", "", profile="read-only"),
        objective="inspect",
        project_root=tmp_path,
        session_id="parent",
    )

    ctx = runner._build_tool_ctx(spec)
    assert ctx.profile is PermissionProfile.READ_ONLY
    assert ctx.sandbox.read_root == parent_ctx.sandbox.read_root
    assert ctx.sandbox.write_roots == ()
    assert ctx.sandbox.unrestricted is False
    ctx.browser.close()
