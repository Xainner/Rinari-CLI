"""Native LSP tools: structural code intelligence via the session's LSP manager.

All operations are read-only: `lsp.rename` only plans the WorkspaceEdit
(applying it is a separate explicit fs operation), so every tool classifies
as `fs.read` on its target path under the normal policy. When no language
server is available for the file, the result says to fall back to search.*
tools (grep is the universal fallback, stack.md 69).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rinari.lsp import LspError
from rinari.tools.definition import (
    RISK_LOW,
    SIDE_EFFECT_NONE,
    ClassifiedAction,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)
from rinari.tools.native.fs import _ok, _resolve_read

NO_SERVER_MESSAGE = (
    "no LSP server is available for this project; use search.* tools as the universal fallback."
)

# Tool-level op name -> LspManager method name.
_POSITION_OPS = {
    "definition": "definition",
    "references": "references",
    "hover": "hover",
    "signature": "signature_help",
    "rename": "rename",
}
_FILE_OPS = {"symbols": "symbols", "diagnostics": "diagnostics"}


def _base(ctx: ToolContext, path: Any) -> tuple[Path | None, ToolResult | None]:
    return _resolve_read(ctx, path)


def _manager(ctx: ToolContext):
    return getattr(ctx, "lsp", None)


def _err(code: ToolErrorCode, message: str, retryable: bool = False) -> ToolResult:
    return ToolResult(
        ok=False, error=ToolErrorInfo(code=code, message=message, retryable=retryable)
    )


def _pos(input: dict) -> tuple[int, int]:
    line = input.get("line")
    column = input.get("column", 1)
    for label, value in (("line", line), ("column", column)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"{label} must be a positive integer (1-based)")
    return line, column


def _handle(ctx: ToolContext, input: dict, op: str) -> ToolResult:
    base, error = _base(ctx, input.get("path"))
    if error:
        return error
    manager = _manager(ctx)
    if manager is None:
        return _err(ToolErrorCode.DEPENDENCY_ERROR, NO_SERVER_MESSAGE)
    method_name = _POSITION_OPS.get(op) or _FILE_OPS.get(op)
    if method_name is None:
        return _err(ToolErrorCode.UNKNOWN, f"unknown LSP operation: {op}")
    method = getattr(manager, method_name, None)
    if method is None:
        return _err(ToolErrorCode.UNKNOWN, f"unknown LSP operation: {op}")
    try:
        if op in _POSITION_OPS or op == "rename":
            line, column = _pos(input)
            if op == "rename":
                new_name = input.get("new_name")
                if not isinstance(new_name, str) or not new_name:
                    return _err(
                        ToolErrorCode.INVALID_ARGUMENT, "new_name must be a non-empty string"
                    )
                return _ok(method(base, line, column, new_name))
            return _ok(method(base, line, column))
        return _ok(method(base))
    except (ValueError, LspError) as exc:
        return _err(
            ToolErrorCode.INVALID_ARGUMENT
            if isinstance(exc, ValueError)
            else ToolErrorCode.DEPENDENCY_ERROR,
            str(exc),
        )
    except Exception as exc:
        return _err(ToolErrorCode.UNKNOWN, f"{exc.__class__.__name__}: {exc}")


def _handler(op: str):
    def handler(input: dict, ctx: ToolContext) -> ToolResult:
        return _handle(ctx, input, op)

    handler.__name__ = f"lsp_{op}"
    return handler


lsp_definition = _handler("definition")
lsp_references = _handler("references")
lsp_symbols = _handler("symbols")
lsp_diagnostics = _handler("diagnostics")
lsp_hover = _handler("hover")
lsp_signature = _handler("signature")
lsp_rename = _handler("rename")


def _as_fs_read(path: Any):
    return ClassifiedAction("fs.read", str(path) if path else "")


def lsp_tools() -> list[ToolDefinition]:
    positional_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "line": {"type": "integer", "minimum": 1},
            "column": {"type": "integer", "minimum": 1},
        },
        "required": ["path", "line"],
    }
    file_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    rename_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "line": {"type": "integer", "minimum": 1},
            "column": {"type": "integer", "minimum": 1},
            "new_name": {"type": "string"},
        },
        "required": ["path", "line", "new_name"],
    }
    entries = [
        (
            "lsp.definition",
            "Go to definition at a 1-based position via the language server.",
            positional_schema,
            "definition",
        ),
        (
            "lsp.references",
            "Find all references to the symbol at a 1-based position.",
            positional_schema,
            "references",
        ),
        (
            "lsp.symbols",
            "Document symbols (structure tree) for a file.",
            file_schema,
            "symbols",
        ),
        (
            "lsp.diagnostics",
            "Latest diagnostics (errors/warnings) published for a file.",
            file_schema,
            "diagnostics",
        ),
        (
            "lsp.hover",
            "Hover/type information at a 1-based position.",
            positional_schema,
            "hover",
        ),
        (
            "lsp.signature",
            "Signature help at a 1-based position (call parameters).",
            positional_schema,
            "signature",
        ),
        (
            "lsp.rename",
            (
                "Plan a rename via the language server. Returns the WorkspaceEdit "
                "(file edits) without applying it; apply with fs.patch when safe."
            ),
            rename_schema,
            "rename",
        ),
    ]
    return [
        ToolDefinition(
            name=name,
            description=description,
            input_schema=schema,
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=_handler(op),
            classify=_as_fs_read,
            namespace="lsp",
        )
        for name, description, schema, op in entries
    ]
