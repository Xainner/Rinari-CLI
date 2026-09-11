"""Cooperative limits inherited by synchronous protocol adapters in one call."""
from contextvars import ContextVar

execution_context = ContextVar("rinari_execution_context", default=None)
