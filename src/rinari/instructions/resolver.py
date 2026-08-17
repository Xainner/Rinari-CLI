"""Project instruction resolution: RINARI.md chains (phase 3).

Resolution rules (docs/commands.md; AGENTS.md precedence):

- ``RINARI.override.md`` in a directory replaces ``RINARI.md`` at that
  level (it never stacks with it).
- The chain runs root -> ... -> cwd; later (deeper) entries are more
  specific and take precedence over earlier ones on conflict.
- The user-global file ``~/.rinari/RINARI.md`` always resolves first
  (it is user-owned and trusted).
- Project chain files are trusted only when the project itself is
  trusted; otherwise they are withheld entirely.
- README files are data, never instructions.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

INSTRUCTION_FILE = "RINARI.md"
OVERRIDE_FILE = "RINARI.override.md"
GLOBAL_FILE_NAME = "RINARI.md"

SCOPE_GLOBAL = "global"

MAX_INSTRUCTION_BYTES = 32 * 1024


@dataclass(frozen=True, slots=True)
class InstructionFile:
    path: Path
    # "" for the project root; "sub/dir" below it; "global" for the user file.
    relative: str
    kind: str  # "standard" | "override"
    scope: str  # "global" | "root" | "dir:<relative>"
    trust: str  # "trusted" | "untrusted"
    content: str
    sha256: str
    size_bytes: int


def read_instruction_file(path: Path) -> tuple[str | None, int]:
    """Read an instruction file (bounded). Returns (text, on-disk size)."""
    if not path.is_file():
        return None, 0
    try:
        raw = path.read_bytes()
    except OSError:
        return None, 0
    text = raw[:MAX_INSTRUCTION_BYTES].decode("utf-8", errors="replace").strip()
    return (text or None), len(raw)


def chain_dirs(root: Path, cwd: Path) -> list[Path]:
    """Directories from the project root down to cwd (inclusive)."""
    root = root.resolve()
    cwd = cwd.resolve()
    if cwd != root and root not in cwd.parents:
        return [root]
    levels: list[Path] = []
    current = cwd
    while True:
        levels.append(current)
        if current == root:
            break
        current = current.parent
    levels.reverse()
    return levels


def resolve_project_instructions(
    root: Path | None,
    cwd: Path | None = None,
    *,
    global_path: Path | None = None,
    trusted: bool = True,
) -> list[InstructionFile]:
    files: list[InstructionFile] = []
    if global_path is not None:
        text, size = read_instruction_file(global_path)
        if text is not None:
            files.append(
                InstructionFile(
                    path=global_path,
                    relative="global",
                    kind="standard",
                    scope=SCOPE_GLOBAL,
                    trust="trusted",
                    content=text,
                    sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    size_bytes=size,
                )
            )
    if root is None or not trusted:
        return files

    root = root.resolve()
    base = cwd.resolve() if cwd is not None else root
    for directory in chain_dirs(root, base):
        override = directory / OVERRIDE_FILE
        standard = directory / INSTRUCTION_FILE
        path, kind = (override, "override") if override.is_file() else (standard, "standard")
        text, size = read_instruction_file(path)
        if text is None:
            continue
        relative = "" if directory == root else directory.relative_to(root).as_posix()
        files.append(
            InstructionFile(
                path=path,
                relative=relative,
                kind=kind,
                scope="root" if relative == "" else f"dir:{relative}",
                trust="trusted",
                content=text,
                sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                size_bytes=size,
            )
        )
    return files


def provenance_for(entry: InstructionFile) -> str:
    if entry.relative == "global":
        return "global/RINARI.md"
    rel = entry.relative or "."
    name = OVERRIDE_FILE if entry.kind == "override" else INSTRUCTION_FILE
    return f"{rel}/{name}"
