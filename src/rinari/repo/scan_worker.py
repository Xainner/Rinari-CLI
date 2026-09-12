"""Disposable bounded search worker: expensive regex cannot strand the engine."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


def scan(config):
    pattern = config["pattern"]
    regex = re.compile(
        re.escape(pattern) if config["literal"] else pattern,
        re.IGNORECASE if config["literal"] else 0,
    )
    matches = []
    backend = (
        "ripgrep" if shutil.which("rg") and not config.get("force_python") else "python-worker"
    )
    paths = config["paths"]
    searched = 0
    if backend == "ripgrep":
        for start in range(0, len(paths), 32):
            batch = paths[start : start + 32]
            argv = [
                "rg",
                "--no-config",
                "--json",
                "--max-count",
                "50",
                "--max-columns",
                "500",
                "--max-filesize",
                "2M",
            ]
            if config["literal"]:
                argv += ["--fixed-strings", "--ignore-case"]
            result = subprocess.run(
                [*argv, "--", pattern, *batch],
                capture_output=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode > 1:
                return scan({**config, "force_python": True})
            grouped = {}
            for raw in result.stdout.splitlines():
                row = json.loads(raw)
                if row.get("type") == "match":
                    data = row["data"]
                    name = data["path"].get("text")
                    if name:
                        grouped.setdefault(name, []).append(
                            {
                                "line": data["line_number"],
                                "text": data["lines"].get("text", "").rstrip()[:500],
                            }
                        )
            matches.extend({"file": name, "lines": hits} for name, hits in grouped.items())
            searched += len(batch)
            if len(matches) >= config["limit"]:
                config["result_limit_reached"] = True
                matches = matches[: config["limit"]]
                break
    else:
        for name in paths:
            path = Path(name)
            searched += 1
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                continue
            try:
                raw = path.read_bytes()
                if b"\0" in raw:
                    continue
                lines = raw.decode("utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            hits = []
            for index, line in enumerate(lines):
                if regex.search(line):
                    hits.append({"line": index + 1, "text": line[:500]})
                    if len(hits) == 50:
                        break
            if hits:
                matches.append({"file": name, "lines": hits})
            if len(matches) >= config["limit"]:
                break
    data = {
        "root": config["root"],
        "pattern": pattern,
        "matches": matches,
        "files_searched": searched,
        "truncated": searched < len(paths) or config.get("result_limit_reached", False),
        "backend": backend,
    }
    while len(json.dumps(data)) > 50000 and matches:
        matches.pop()
        data["truncated"] = True
    return data


if __name__ == "__main__":
    try:
        result = scan(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
        print(json.dumps(result, ensure_ascii=True))
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"error": type(exc).__name__}))
        sys.exit(2)
