"""Native tool packs."""

from __future__ import annotations

from rinari.tools.definition import ToolDefinition
from rinari.tools.native.fs import filesystem_tools
from rinari.tools.native.git import git_tools
from rinari.tools.native.process import process_tools
from rinari.tools.native.shell import shell_tools

__all__ = ["all_native_tools"]


def all_native_tools() -> list[ToolDefinition]:
    return [*filesystem_tools(), *shell_tools(), *git_tools(), *process_tools()]
