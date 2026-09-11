"""Native search tools: exact files, regex, symbols, references, hybrid.

Everything is read-only; each tool classifies as `fs.read` on its base path
so the normal policy (session root, full-access, read-only) applies without
a special capability.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rinari.repo import search as core
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


def _base(ctx: ToolContext, path: Any) -> tuple[Path | None, ToolResult | None]:
    return _resolve_read(ctx, path or ".")


def _as_fs_read(arguments: dict):
    return ClassifiedAction("fs.read", str(arguments.get("path") or "."))


def _err(code: ToolErrorCode, message: str) -> ToolResult:
    return ToolResult(ok=False, error=ToolErrorInfo(code=code, message=message))


def search_files(input: dict, ctx: ToolContext) -> ToolResult:
    base, error = _base(ctx, input.get("path"))
    if error:
        return error
    pattern = input.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return _err(ToolErrorCode.INVALID_ARGUMENT, "pattern must be a non-empty string")
    from rinari.tools.file_search import file_matches

    try:
        return _ok(
            file_matches(
                base,
                pattern,
                ctx,
                limit=min(500, max(1, int(input.get("limit", 500)))),
                offset=max(0, int(input.get("offset", 0))),
            )
        )
    except ValueError as exc:
        return _err(ToolErrorCode.INVALID_ARGUMENT, str(exc))
    except OSError as exc:
        return _err(
            ToolErrorCode.PERMISSION_DENIED, f"Glob failed at {base}: {exc.__class__.__name__}"
        )


def search_regex(input: dict, ctx: ToolContext) -> ToolResult:
    base, error = _base(ctx, input.get("path"))
    if error:
        return error
    pattern = input.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return _err(ToolErrorCode.INVALID_ARGUMENT, "pattern must be a non-empty regex")
    from rinari.tools.text_search import search_text

    return search_text(base, input, ctx)


def search_symbols(input: dict, ctx: ToolContext) -> ToolResult:
    base, error = _base(ctx, input.get("path"))
    if error:
        return error
    query = input.get("query")
    if not isinstance(query, str) or not query:
        return _err(
            ToolErrorCode.INVALID_ARGUMENT,
            "query must be a non-empty symbol name (or Class.method)",
        )
    return _ok(
        core.find_symbols(
            base,
            query,
            kind=input.get("kind"),
            include=input.get("include"),
            max_results=int(input.get("max_results", 50)),
        )
    )


def search_references(input: dict, ctx: ToolContext) -> ToolResult:
    base, error = _base(ctx, input.get("path"))
    if error:
        return error
    name = input.get("name")
    if not isinstance(name, str) or not name:
        return _err(ToolErrorCode.INVALID_ARGUMENT, "name must be a non-empty identifier")
    return _ok(
        core.find_references(
            base,
            name,
            include=input.get("include"),
            max_results=int(input.get("max_results", 100)),
            include_definitions=bool(input.get("include_definitions", False)),
        )
    )


def search_hybrid(input: dict, ctx: ToolContext) -> ToolResult:
    base, error = _base(ctx, input.get("path"))
    if error:
        return error
    query = input.get("query")
    if not isinstance(query, str) or not query:
        return _err(ToolErrorCode.INVALID_ARGUMENT, "query must be a non-empty term")
    return _ok(
        core.hybrid_search(
            base, query, include=input.get("include"), max_results=int(input.get("max_results", 20))
        )
    )


def search_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="search.files",
            description="Exact file search by glob pattern (e.g. **/*.py, tests/test_*.py).",
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["pattern"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=search_files,
            classify=_as_fs_read,
            namespace="search",
        ),
        ToolDefinition(
            name="search.regex",
            description="Regular-expression search across text files (per-line matches).",
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "include": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 500},
                },
                "required": ["pattern"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=search_regex,
            classify=_as_fs_read,
            namespace="search",
        ),
        ToolDefinition(
            name="search.symbols",
            description=(
                "Symbol search (functions/classes/methods/constants) for a name; "
                "supports qualified structural queries like Class.method."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string"},
                    "kind": {"type": "string"},
                    "include": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 500},
                },
                "required": ["query"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=search_symbols,
            classify=_as_fs_read,
            namespace="search",
        ),
        ToolDefinition(
            name="search.references",
            description="Find references to an identifier (definition optional via flag).",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "path": {"type": "string"},
                    "include": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 500},
                    "include_definitions": {"type": "boolean"},
                },
                "required": ["name"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=search_references,
            classify=_as_fs_read,
            namespace="search",
        ),
        ToolDefinition(
            name="search.hybrid",
            description=(
                "Ranked hybrid search combining symbol identity, references, text, and "
                "filename signal; returns explainable scores per hit."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string"},
                    "include": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": ["query"],
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=search_hybrid,
            classify=_as_fs_read,
            namespace="search",
        ),
    ]
