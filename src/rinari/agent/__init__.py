"""Paquete del modo agente: tool registry, loop y prompts."""

from rinari.agent.loop import AgentError, run_agent
from rinari.agent.tools import ToolRegistry

__all__ = ["AgentError", "ToolRegistry", "run_agent"]
