"""Search core: regex, symbols, references, hybrid ranking (phase 3 Search).

Pure functions over a directory tree (no ToolContext, no policy): the tool
wrappers resolve the base path through the sandbox and pass it here. All
scans are bounded (max_files) and skip generated/vendored directories.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    "dist",
    "build",
    "out",
    "target",
    "vendor",
    ".mypy_cache",
    ".ruff_cache",
    ".next",
    ".nuxt",
    ".cache",
    ".idea",
    ".vscode",
}

DEFAULT_MAX_FILES = 5000
LINE_SNIPPET_CHARS = 200

_BINARY_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".woff",
    ".woff2",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".pyc",
    ".so",
    ".dll",
    ".exe",
    ".bin",
    ".dat",
    ".mp4",
    ".webm",
}


@dataclass(frozen=True, slots=True)
class Symbol:
    name: str
    kind: str
    line: int
    context: str = ""


def walk_files(root: Path, include: str | None = None, max_files: int = DEFAULT_MAX_FILES):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")
        )
        for filename in sorted(filenames):
            if include and not fnmatch.fnmatch(filename, include):
                continue
            yield Path(dirpath) / filename
            count += 1
            if count >= max_files:
                return


def is_text_file(path: Path) -> bool:
    if path.suffix.lower() in _BINARY_SUFFIXES:
        return False
    try:
        with open(path, "rb") as handle:
            chunk = handle.read(8192)
    except OSError:
        return False
    return b"\x00" not in chunk


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _read_lines(path: Path) -> list[str] | None:
    if not is_text_file(path):
        return None
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError):
        return None


# -- regex search ----------------------------------------------------------------


def regex_search(
    root: Path,
    pattern: str,
    include: str | None = None,
    max_results: int = 100,
    max_files: int = DEFAULT_MAX_FILES,
) -> dict:
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"invalid regular expression: {exc}") from exc
    matches: list[dict] = []
    searched = 0
    for file in walk_files(root, include, max_files):
        lines = _read_lines(file)
        if lines is None:
            continue
        searched += 1
        file_hits = [
            {"line": i + 1, "text": line[:LINE_SNIPPET_CHARS]}
            for i, line in enumerate(lines)
            if regex.search(line)
        ]
        if file_hits:
            matches.append({"file": _rel(root, file), "lines": file_hits[:50]})
        if len(matches) >= max_results:
            break
    return {
        "root": str(root),
        "pattern": pattern,
        "files_searched": searched,
        "matches": matches,
        "truncated": len(matches) >= max_results,
    }


# -- symbol extraction -------------------------------------------------------------

_PY_CLASS = re.compile(r"^class\s+([A-Za-z_]\w*)")
_PY_DEF = re.compile(r"^(?:async\s+)?def\s+([A-Za-z_]\w*)")
_JS_FUNCTION = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)")
_JS_CLASS = re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)")
_JS_CONST = re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=")
_RS_FN = re.compile(r"^\s*(?:pub(?:\s+\w+)*\s+)?fn\s+([a-z_]\w*)")
_RS_TYPE = re.compile(r"^\s*(?:pub\s+)?(?:struct|enum|trait|type)\s+([A-Z]\w*)")
_GO_FUNC = re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)")
_GO_TYPE = re.compile(r"^\s*type\s+([A-Z]\w*)")


def symbols_for_file(path: Path) -> list[Symbol]:
    suffix = path.suffix.lower()
    if suffix not in {".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".rs", ".go"}:
        return []
    lines = _read_lines(path)
    if lines is None:
        return []
    symbols: list[Symbol] = []
    if suffix == ".py":
        class_scope: str | None = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            m_class = _PY_CLASS.match(stripped)
            m_def = _PY_DEF.match(stripped)
            if not line.startswith((" ", "\t")):
                if m_class:
                    class_scope = m_class.group(1)
                    symbols.append(Symbol(m_class.group(1), "class", i + 1, stripped))
                elif m_def:
                    class_scope = None
                    symbols.append(Symbol(m_def.group(1), "function", i + 1, stripped))
            else:
                if m_def and class_scope:
                    symbols.append(
                        Symbol(m_def.group(1), "method", i + 1, f"{class_scope}.{m_def.group(1)}")
                    )
    elif suffix in {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"}:
        for i, line in enumerate(lines):
            if m := _JS_CLASS.match(line):
                symbols.append(Symbol(m.group(1), "class", i + 1, line.strip()))
            elif m := _JS_FUNCTION.match(line):
                symbols.append(Symbol(m.group(1), "function", i + 1, line.strip()))
            elif m := _JS_CONST.match(line):
                symbols.append(Symbol(m.group(1), "const", i + 1, line.strip()))
    elif suffix == ".rs":
        for i, line in enumerate(lines):
            if m := _RS_TYPE.match(line):
                symbols.append(Symbol(m.group(1), "type", i + 1, line.strip()))
            elif m := _RS_FN.match(line):
                symbols.append(Symbol(m.group(1), "function", i + 1, line.strip()))
    elif suffix == ".go":
        for i, line in enumerate(lines):
            if m := _GO_TYPE.match(line):
                symbols.append(Symbol(m.group(1), "type", i + 1, line.strip()))
            elif m := _GO_FUNC.match(line):
                symbols.append(Symbol(m.group(1), "function", i + 1, line.strip()))
    return symbols


# -- symbol search -----------------------------------------------------------------


def find_symbols(
    root: Path,
    query: str,
    kind: str | None = None,
    include: str | None = None,
    max_results: int = 50,
    max_files: int = DEFAULT_MAX_FILES,
) -> dict:
    wanted = query.lower()
    qualified = "." in query
    results: list[dict] = []
    scanned = 0
    for file in walk_files(root, include, max_files):
        scanned += 1
        for symbol in symbols_for_file(file):
            if kind is not None and symbol.kind != kind:
                continue
            name_matches = symbol.name.lower() == wanted
            # Structural search: a qualified query (Class.method) matches the
            # class-qualified context of a symbol.
            context_matches = qualified and symbol.context.lower().endswith(wanted)
            if name_matches or context_matches:
                results.append(
                    {
                        "name": symbol.name,
                        "kind": symbol.kind,
                        "file": _rel(root, file),
                        "line": symbol.line,
                        "context": symbol.context,
                    }
                )
                if len(results) >= max_results:
                    break
        if len(results) >= max_results:
            break
    return {
        "root": str(root),
        "query": query,
        "files_scanned": scanned,
        "results": results,
        "truncated": len(results) >= max_results,
    }


# -- references ---------------------------------------------------------------------

_DEFINITION_PATTERNS = (
    r"^\s*(?:async\s+)?def\s+{name}\s*[(\[]",
    r"^\s*class\s+{name}\b",
    r"^\s*(?:export\s+)?function\s*\*?\s*{name}\s*[(\[]",
    r"^\s*(?:export\s+)?class\s+{name}\b",
    r"^\s*(?:export\s+)?(?:const|let|var)\s+{name}\s*=",
    r"^\s*(?:pub(?:\s+\w+)*\s+)?fn\s+{name}\s*[(<\[]",
    r"^\s*(?:pub\s+)?(?:struct|enum|trait|type)\s+{name}\b",
    r"^\s*func\s+(?:\([^)]*\)\s*)?{name}\s*[(\[]",
    r"^\s*type\s+{name}\b",
)


def _defines_name(line: str, name: str) -> bool:
    escaped = re.escape(name)
    return any(re.match(p.format(name=escaped), line) for p in _DEFINITION_PATTERNS)


def find_references(
    root: Path,
    name: str,
    include: str | None = None,
    max_results: int = 100,
    max_files: int = DEFAULT_MAX_FILES,
    include_definitions: bool = False,
) -> dict:
    regex = re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")
    results: list[dict] = []
    scanned = 0
    for file in walk_files(root, include, max_files):
        lines = _read_lines(file)
        if lines is None:
            continue
        scanned += 1
        file_refs: list[dict] = []
        for i, line in enumerate(lines):
            if not regex.search(line):
                continue
            definition = _defines_name(line, name)
            if definition and not include_definitions:
                continue
            file_refs.append(
                {
                    "line": i + 1,
                    "text": line.strip()[:LINE_SNIPPET_CHARS],
                    "definition": definition,
                }
            )
            if len(file_refs) >= 25:
                break
        if file_refs:
            results.append({"file": _rel(root, file), "references": file_refs})
        if len(results) >= max_results:
            break
    return {
        "root": str(root),
        "name": name,
        "files_scanned": scanned,
        "results": results,
        "truncated": len(results) >= max_results,
    }


# -- hybrid search --------------------------------------------------------------------


def hybrid_search(
    root: Path,
    query: str,
    include: str | None = None,
    max_results: int = 20,
    max_files: int = 3000,
) -> dict:
    """Combine identity, references, and text into explainable ranked hits.

    Scores per hit:
      5  symbol definition with the exact name
      4  symbol/reference in a context ending in the (qualified) query
      2  any other reference occurrence
      1  plain text occurrence only
      +2 name appears in the file name
    """
    wanted = query.lower()
    ref_regex = re.compile(rf"(?<![\w$]){re.escape(query)}(?![\w$])")
    hits: dict[tuple[str, int], dict] = {}

    def add(file: str, line: int, score: int, reason: str) -> None:
        key = (file, line)
        entry = hits.get(key)
        if entry is None:
            hits[key] = {"file": file, "line": line, "score": score, "reasons": [reason]}
        else:
            entry["score"] += score
            if reason not in entry["reasons"]:
                entry["reasons"].append(reason)

    for file in walk_files(root, include, max_files):
        rel = _rel(root, file)
        name_hit = wanted in Path(rel).name.lower()
        lines = _read_lines(file)
        if lines is None:
            continue
        file_has_hits = False
        for i, line in enumerate(lines):
            lower = line.lower()
            if wanted not in lower and not ref_regex.search(line):
                continue
            file_has_hits = True
            definition = _defines_name(line, query)
            qualified_hit = "." in query and lower.rstrip().endswith(wanted)
            if definition:
                add(rel, i + 1, 5, "definition")
            elif qualified_hit:
                add(rel, i + 1, 4, "qualified-context")
            elif ref_regex.search(line):
                add(rel, i + 1, 2, "reference")
            else:
                add(rel, i + 1, 1, "text")
            if name_hit:
                add(rel, i + 1, 2, "filename")
        if name_hit and not file_has_hits:
            add(rel, 0, 2, "filename")

    ranked = sorted(hits.values(), key=lambda e: (-e["score"], e["file"], e["line"]))
    selected = ranked[:max_results]
    return {
        "root": str(root),
        "query": query,
        "results": selected,
        "total_hits": len(hits),
        "truncated": len(hits) > max_results,
    }
