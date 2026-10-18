"""Every registered tool has a policy rule (plan 0.4.0 v2, V2-5).

`audio.transcribe` had none: it fell into `unknown_capability`, denied in
read-only and asked in Proyecto although it only reads a local file. A new
tool without a rule now fails here, not in the owner's app.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from rinari.policy.engine import PermissionProfile, PolicyEngine, SessionScope
from rinari.tools.catalog import builtin_catalog

CATALOG = builtin_catalog()
GENERIC = (
    "not covered by a built-in rule",
    "has no stricter rule",
    "is not available in read-only",
)


def _host_tools():
    from rinari.agents.tools import AgentToolHost, agent_tools
    from rinari.schedule.tools import ScheduleToolHost, schedule_tools
    from rinari.skills.tools import SkillToolHost, skill_tools
    from rinari.tools.native.children import child_tools
    from rinari.tools.native.image_create import image_create_tools
    from rinari.tools.native.rinari_state import RinariStateHost, rinari_state_tools

    image_host = SimpleNamespace(service=SimpleNamespace(default_label=lambda: None))
    return [
        *skill_tools(SkillToolHost(service=None, project=None)),
        *schedule_tools(ScheduleToolHost(service=None, project_id=None)),
        *rinari_state_tools(RinariStateHost(None, None)),
        *image_create_tools(image_host),
        *agent_tools(AgentToolHost(orchestrator=None)),
        *child_tools(None),
    ]


TOOLS = [CATALOG.get(name) for name in CATALOG.names()] + _host_tools()


@pytest.mark.parametrize("tool", TOOLS, ids=lambda tool: tool.name)
def test_every_native_tool_has_a_policy_rule(tmp_path, tool) -> None:
    work = Path(tmp_path)
    scope = SessionScope(
        kind="PROJECT", root=work, cwd=work, profile=PermissionProfile.WORKSPACE, user_home=work
    )
    try:
        action = tool.classify_action({"path": str(work / "a.txt"), "prompt": "x", "name": "x"})
        capability, target = action.capability, action.target
    except Exception:
        capability = tool.capabilities[0] if tool.capabilities else tool.name
        target = None
    decision = PolicyEngine().decide(capability, scope, path=target or None, target=target or None)
    assert decision.rule_id != "unknown_capability", (tool.name, decision.reason)
    assert not any(text in decision.reason for text in GENERIC), (tool.name, decision.reason)


def test_listening_to_audio_is_a_read() -> None:
    tool = CATALOG.get("audio.transcribe")
    downloads = Path.home() / "Downloads" / "song.mp3"
    engine = PolicyEngine()
    for profile in (PermissionProfile.READ_ONLY, PermissionProfile.WORKSPACE):
        scope = SessionScope(
            kind="PROJECT",
            root=Path("/proj"),
            cwd=Path("/proj"),
            profile=profile,
            user_home=Path.home(),
        )
        action = tool.classify_action({"path": str(downloads)})
        decision = engine.decide(action.capability, scope, path=action.target)
        assert action.capability == "fs.read"
        assert decision.action.value == "allow", (profile, decision.reason)
