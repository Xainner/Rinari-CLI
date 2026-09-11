"""Shared literal/regex execution through the existing bounded process backend."""

import json
import sys
import tempfile
from pathlib import Path

from rinari.repo.search import walk_files
from rinari.tools.definition import ToolErrorCode, ToolErrorInfo, ToolResult
from rinari.tools.native.shell import shell_exec


def search_text(root, arguments, ctx, *, literal=False):
    paths = []
    for path in [root] if root.is_file() else walk_files(root, arguments.get("include"), 5001):
        if ctx.cancellation:
            ctx.cancellation.throw_if_cancelled()
        try:
            ctx.sandbox.assert_readable(path.resolve())
        except Exception:
            continue
        if not path.is_symlink():
            paths.append(str(path))
    config = {
        "root": str(root),
        "paths": paths[:5000],
        "pattern": arguments["pattern"],
        "literal": literal,
        "limit": min(200, max(1, int(arguments.get("max_results", 100)))),
    }
    with tempfile.TemporaryDirectory(prefix="rinari-search-") as folder:
        manifest = Path(folder) / "scan.json"
        manifest.write_text(json.dumps(config), encoding="utf-8")
        result = shell_exec(
            {
                "argv": [sys.executable, "-m", "rinari.repo.scan_worker", str(manifest)],
                "timeout_s": 15,
            },
            ctx,
        )
    if not result.ok:
        return result
    if result.data["exit_code"] != 0:
        return ToolResult(
            ok=False,
            error=ToolErrorInfo(
                ToolErrorCode.INVALID_ARGUMENT,
                "Search pattern failed or exceeded its per-file time limit",
            ),
        )
    data = json.loads(result.data["stdout"])
    data["truncated"] |= len(paths) > 5000
    if not literal:
        for row in data["matches"]:
            row["file"] = (
                Path(row["file"]).relative_to(root).as_posix() if root.is_dir() else root.name
            )
    return ToolResult(ok=True, data=data, truncated=data["truncated"])
