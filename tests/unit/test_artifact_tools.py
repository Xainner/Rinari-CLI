from __future__ import annotations

from pathlib import Path

import pytest

from rinari.policy.engine import PermissionProfile
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.shared.clock import FakeClock
from rinari.tools.definition import ToolContext
from rinari.tools.native.artifact import artifact_tools


def _tool(name: str):
    return next(item for item in artifact_tools() if item.name == name)


@pytest.fixture
def tool_ctx(tmp_path: Path) -> ToolContext:
    root = tmp_path / "project"
    root.mkdir()
    return ToolContext(
        session_id="session-1",
        kind="PROJECT",
        cwd=root,
        project_root=root,
        user_home=tmp_path,
        profile=PermissionProfile.WORKSPACE,
        sandbox=FilesystemSandbox(root, (root,)),
        limits=ProcessLimits(),
        artifact_root=tmp_path / "artifacts",
        clock=FakeClock(),
    )


def test_artifact_read_is_bounded_and_paginated(tool_ctx: ToolContext) -> None:
    ctx = tool_ctx
    path = ctx.artifact_root / ctx.session_id / "runtime" / "large.txt"
    path.parent.mkdir(parents=True)
    path.write_text("0123456789", encoding="utf-8")

    result = _tool("artifact.read").handler(
        {
            "uri": f"artifact://{ctx.session_id}/runtime/large.txt",
            "start_byte": 2,
            "max_bytes": 4,
        },
        ctx,
    )

    assert result.ok is True
    assert result.data["text"] == "2345"
    assert result.data["next_start_byte"] == 6
    assert result.data["truncated"] is True


def test_artifact_read_rejects_another_session(tool_ctx: ToolContext) -> None:
    ctx = tool_ctx
    result = _tool("artifact.read").handler({"uri": "artifact://another/runtime/result.txt"}, ctx)

    assert result.ok is False
    assert result.error.code.value == "PERMISSION_DENIED"
