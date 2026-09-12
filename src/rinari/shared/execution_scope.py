"""Cooperative limits inherited by synchronous protocol adapters in one call."""
from contextvars import ContextVar

execution_context = ContextVar("rinari_execution_context", default=None)

# Bound by ToolRuntime for the lifetime of a handler invocation.  Process
# output readers run in their own threads, so this is set by the per-call
# output-sink wrapper rather than relying on context propagation.
tool_output_context = ContextVar("rinari_tool_output_context", default=None)
