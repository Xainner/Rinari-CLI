"""Native tool packs."""

from __future__ import annotations

from rinari.tools.definition import ToolDefinition
from rinari.tools.native.context import context_tools
from rinari.tools.native.fs import filesystem_tools
from rinari.tools.native.git import git_tools
from rinari.tools.native.lsp import lsp_tools
from rinari.tools.native.memory import memory_tools
from rinari.tools.native.process import process_tools
from rinari.tools.native.ptytools import pty_tools
from rinari.tools.native.search import search_tools
from rinari.tools.native.shell import shell_tools
from rinari.tools.native.verify import verify_tools
from rinari.tools.native.web import web_tools

__all__ = ["all_native_tools"]


def all_native_tools() -> list[ToolDefinition]:
    return [
        *filesystem_tools(),
        *search_tools(),
        *shell_tools(),
        *git_tools(),
        *process_tools(),
        *lsp_tools(),
        *verify_tools(),
        *memory_tools(),
        *context_tools(),
        *pty_tools(),
        *web_tools(),
    ]
