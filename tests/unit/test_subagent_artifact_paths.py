"""Subagent output spills must use portable, isolated filesystem namespaces."""

from pathlib import Path

from rinari.agents.definition import AgentDefinition
from rinari.agents.orchestrator import SubagentRunSpec
from rinari.agents.runtime import SubagentRuntimeConfig, make_subagent_runner
from rinari.policy.approvals import ApprovalEngine
from rinari.policy.engine import PermissionProfile, PolicyEngine
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.runtime.cancellation import CancellationToken
from rinari.shared.clock import FakeClock
from rinari.shared.redaction import Redactor
from rinari.tools.definition import ToolContext
from rinari.tools.registry import ToolRegistry
from rinari.tools.runtime import ToolRuntime


def test_child_spills_large_output_to_portable_isolated_path(tmp_path: Path):
    clock = FakeClock()
    parent = ToolContext(
        session_id="ses_parent",
        kind="PROJECT",
        cwd=tmp_path,
        project_root=tmp_path,
        user_home=tmp_path,
        clock=clock,
        artifact_root=tmp_path / "artifacts",
        cancellation=CancellationToken(),
        profile=PermissionProfile.WORKSPACE,
        limits=ProcessLimits(),
        sandbox=FilesystemSandbox(tmp_path, (tmp_path,)),
    )
    policy = PolicyEngine()
    runner = make_subagent_runner(
        SubagentRuntimeConfig(
            None,
            None,
            CancellationToken(),
            policy,
            None,
            parent,
        )
    )
    runtime = ToolRuntime(
        ToolRegistry(), policy, ApprovalEngine(prompt=None), clock=clock, redactor=Redactor()
    )
    refs = []
    payload = "Repository documentation 日本語\n" * 5000
    for agent_id in ("agt_001", "agt_002"):
        spec = SubagentRunSpec(
            agent_id=agent_id,
            definition=AgentDefinition("explore", "", ""),
            objective="read documentation",
            project_root=tmp_path,
            session_id="ses_parent",
        )
        child = runner._build_tool_ctx(spec)
        try:
            assert child.session_id == f"ses_parent-{agent_id}"
            assert not any(char in child.session_id for char in '<>:"/\\|?*')
            ref = runtime._spill("read-docs", payload, child)
            target = parent.artifact_root / child.session_id / "runtime" / ref.name
            assert target.read_text(encoding="utf-8") == payload
            assert ref.uri == f"artifact://{child.session_id}/runtime/{ref.name}"
            refs.append(ref.uri)
        finally:
            child.browser.close()
    assert refs[0] != refs[1]
