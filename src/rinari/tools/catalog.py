"""Complete built-in inventory for inspection, without connecting integrations.

The executable session registry still owns availability and permissions. This
inventory must not be used as a replacement execution runtime.
"""

from dataclasses import replace

from rinari.agents.tools import AgentToolHost, agent_tools
from rinari.capability_search import capability_activation_tools, capability_search_tool
from rinari.skills.tools import SkillToolHost, skill_tools
from rinari.tools.definition import ToolErrorCode, ToolErrorInfo, ToolResult
from rinari.tools.native import all_native_tools
from rinari.tools.native.ssh import ssh_tools
from rinari.tools.registry import ToolRegistry


def _session_required(arguments, ctx):
    return ToolResult(
        ok=False,
        error=ToolErrorInfo(
            ToolErrorCode.DEPENDENCY_ERROR,
            "This tool requires an active session runtime; "
            "the inspection catalog cannot execute it.",
        ),
    )


def builtin_catalog() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_all(all_native_tools())
    registry.register_all(ssh_tools(None))
    registry.register_all(skill_tools(SkillToolHost(service=None, project=None)))
    registry.register_all(agent_tools(AgentToolHost(orchestrator=None)))
    registry.register(capability_search_tool(registry))
    registry.register_all(capability_activation_tools(registry))
    for name in registry.names():
        if name.startswith(("ssh.", "skills.", "agent.")):
            tool = registry.get(name)
            registry.register(replace(tool, handler=_session_required))
    return registry
