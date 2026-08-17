"""Read-only local Git tools (tools.md section 9, local subset).

Remote Git mutations are intentionally absent from the tool catalog; they
remain reachable only through `shell.exec` behind the approval gate
(harness.md section 77: local operations allowed, remote mutation asks).
"""

from __future__ import annotations

import subprocess
from typing import Any

from rinari.policy.sandbox import SandboxViolationError
from rinari.tools.definition import (
    RISK_LOW,
    SIDE_EFFECT_NONE,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)

GIT_TIMEOUT_S = 30.0
MAX_OUTPUT = 100_000


def _ok(data: Any) -> ToolResult:
    return ToolResult(ok=True, data=data)


def _fail(code: ToolErrorCode, message: str) -> ToolResult:
    return ToolResult(ok=False, error=ToolErrorInfo(code=code, message=message))


def _bound(text: str) -> tuple[str, bool]:
    if len(text) > MAX_OUTPUT:
        return text[:MAX_OUTPUT], True
    return text, False


def _run_git(ctx: ToolContext, input: dict, *args: str) -> tuple[int, str, bool] | ToolResult:
    """Run a read-only git command; return (code, out, truncated) or an error result.

    Raises SandboxViolationError when the requested path leaves the roots.
    """
    if input.get("path"):
        try:
            if not isinstance(input["path"], str):
                raise TypeError()
            resolved = ctx.sandbox.resolve(input["path"], base=ctx.cwd)
            ctx.sandbox.assert_readable(resolved)
            root: str = str(resolved)
        except SandboxViolationError as exc:
            return _fail(ToolErrorCode.SANDBOX_VIOLATION, exc.message)
        except TypeError:
            return _fail(ToolErrorCode.INVALID_ARGUMENT, "path must be a string")
    else:
        root = str(ctx.project_root) if ctx.project_root is not None else str(ctx.cwd)
    try:
        process = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            timeout=GIT_TIMEOUT_S,
        )
    except FileNotFoundError:
        return _fail(ToolErrorCode.DEPENDENCY_ERROR, "git executable not found")
    except subprocess.TimeoutExpired:
        return _fail(ToolErrorCode.TIMEOUT, "git command timed out", retryable=True)
    out = process.stdout.decode("utf-8", errors="replace")
    if process.returncode != 0 and process.returncode != 1:
        detail = process.stderr.decode("utf-8", errors="replace").strip()
        if "not a git repository" in (out + detail).lower():
            return _fail(ToolErrorCode.NOT_FOUND, f"Not a git repository: {root}")
        return _fail(ToolErrorCode.UNKNOWN, f"git {args[0]} failed: {detail[:300]}")
    return process.returncode, out, False


def _truncated(out: str) -> bool:
    return len(out) > MAX_OUTPUT


def git_status(input: dict, ctx: ToolContext) -> ToolResult:
    result = _run_git(ctx, input, "status", "--porcelain", "--branch")
    if isinstance(result, ToolResult):
        return result
    _, out, _ = result
    branch: str | None = None
    files: list[dict[str, str]] = []
    for line in out.splitlines():
        if line.startswith("## "):
            branch = line[3:].split(" ")[0]
        elif line:
            files.append({"status": line[:2].strip(), "path": line[3:]})
    return _ok(
        {
            "branch": branch,
            "dirty": bool(files),
            "files": files[:200],
            "truncated": _truncated(out),
        }
    )


def git_diff(input: dict, ctx: ToolContext) -> ToolResult:
    args = ["diff", "--no-color", "--stat=200"]
    if input.get("cached", False):
        args.append("--cached")
    if input.get("path"):
        args += ["--", str(input["path"])]
    result = _run_git(ctx, input, *args)
    if isinstance(result, ToolResult):
        return result
    _, out, _ = result
    bound, truncated = _bound(out)
    return _ok({"diff": bound, "truncated": truncated})


def git_log(input: dict, ctx: ToolContext) -> ToolResult:
    limit = input.get("limit", 20)
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "limit must be an integer")
    result = _run_git(ctx, input, "log", f"-n{limit}", "--oneline", "--decorate", "--no-color")
    if isinstance(result, ToolResult):
        if "not have any commits yet" in result.error.message:
            return _ok({"entries": [], "truncated": False})
        return result
    _, out, _ = result
    bound, truncated = _bound(out)
    return _ok({"entries": [line for line in bound.splitlines() if line], "truncated": truncated})


def git_show(input: dict, ctx: ToolContext) -> ToolResult:
    ref = input.get("ref") or "HEAD"
    if not isinstance(ref, str) or not ref.strip():
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "ref must be a non-empty string")
    result = _run_git(ctx, input, "show", "--stat", "--no-color", ref)
    if isinstance(result, ToolResult):
        return result
    _, out, _ = result
    bound, truncated = _bound(out)
    return _ok({"ref": ref, "stat": bound, "truncated": truncated})


def git_branch(input: dict, ctx: ToolContext) -> ToolResult:
    result = _run_git(ctx, input, "branch", "--list", "--no-color")
    if isinstance(result, ToolResult):
        return result
    _, out, _ = result
    current = None
    current_result = _run_git(ctx, input, "rev-parse", "--abbrev-ref", "HEAD")
    if not isinstance(current_result, ToolResult) and current_result[1].strip():
        current = current_result[1].strip()
    if current is None:
        # unborn branch (fresh repo): the status header carries the name
        status_result = _run_git(ctx, input, "status", "--porcelain", "--branch")
        if not isinstance(status_result, ToolResult):
            for line in status_result[1].splitlines():
                if line.startswith("## "):
                    current = line[3:].split(" ")[0]
                    break
    branches = [line.lstrip("* ").strip() for line in out.splitlines() if line.strip()]
    return _ok({"current": current, "branches": branches})


def git_tools() -> list[ToolDefinition]:
    base_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
        },
    }
    return [
        ToolDefinition(
            name="git.status",
            description="Working tree status: branch, dirty flag, and changed files.",
            input_schema=dict(base_schema),
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=git_status,
            namespace="git",
        ),
        ToolDefinition(
            name="git.diff",
            description="Unstaged diff (pass cached=true for the index).",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "cached": {"type": "boolean"},
                },
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=git_diff,
            namespace="git",
        ),
        ToolDefinition(
            name="git.log",
            description="Recent commit history (oneline).",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=git_log,
            namespace="git",
        ),
        ToolDefinition(
            name="git.show",
            description="Show a commit's metadata and file stats.",
            input_schema={
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "path": {"type": "string"},
                },
            },
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=git_show,
            namespace="git",
        ),
        ToolDefinition(
            name="git.branch",
            description="Current branch and local branch list.",
            input_schema=dict(base_schema),
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            handler=git_branch,
            namespace="git",
        ),
    ]
