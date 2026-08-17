"""Repository index core: build, incremental update, query (phase 3).

Project-scoped store (harness.md 98): every keyed row carries the canonical
project root. File-level state (hash, language, symbols, imports) is
incremental: unchanged files (same sha256) keep their cached symbols/imports
and are not reparsed (harness.md 99). Cross-file derived data (references,
test mapping) is recomputed each build from the cached per-file symbols plus
a bounded text scan -- reparsing is what the hash cache avoids. The semantic
layer (embeddings) is declared but intentionally 'none': optional add-on,
never the default (stack.md 68).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .search import DEFAULT_MAX_FILES, _defines_name, _read_lines, walk_files

SEMANTIC_LAYER_NONE = "none"

_FILE_LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".rs": "rust",
    ".go": "go",
}

_TEST_FILE = re.compile(r"(^|/)(test_[^/]+\.py|[^/]+_test\.py|[^/]+\.test\.(js|ts|jsx|tsx))$")
_TEST_DIR = re.compile(r"(^|/)tests?(/|$)")


@dataclass(slots=True)
class IndexBuildResult:
    project_root: str
    files_added: int = 0
    files_changed: int = 0
    files_removed: int = 0
    files_unchanged: int = 0
    total_files: int = 0
    total_symbols: int = 0
    total_references: int = 0
    duration_s: float = 0.0
    semantic_layer: str = SEMANTIC_LAYER_NONE

    def to_dict(self) -> dict:
        return {
            "project_root": self.project_root,
            "files_added": self.files_added,
            "files_changed": self.files_changed,
            "files_removed": self.files_removed,
            "files_unchanged": self.files_unchanged,
            "total_files": self.total_files,
            "total_symbols": self.total_symbols,
            "total_references": self.total_references,
            "duration_s": round(self.duration_s, 3),
            "semantic_layer": self.semantic_layer,
        }


def _sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _is_test_file(rel_path: str) -> bool:
    return bool(_TEST_FILE.search(rel_path)) or bool(_TEST_DIR.search(rel_path))


def _analyze(file: Path) -> tuple[list[dict], list[dict], str]:
    from rinari.ast import analyze_file

    language = _FILE_LANGUAGES.get(file.suffix.lower(), "unknown")
    summary = analyze_file(file)
    if summary is None:
        return [], [], language
    return (
        [
            {
                "name": s.name,
                "kind": s.kind,
                "qualified_name": s.qualified_name,
                "line": s.line,
                "end_line": s.end_line,
            }
            for s in summary.symbols
        ],
        [
            {"module": i.module, "names": [list(n) for n in i.names], "line": i.line}
            for i in summary.imports
        ],
        language,
    )


def build_index(
    repo, root: Path, *, built_at: str, updated_at: str, full: bool = False
) -> IndexBuildResult:
    """Build or incrementally update the index for ``root``."""
    started = time.monotonic()
    project_root = str(root.resolve())
    result = IndexBuildResult(project_root=project_root)

    existing = repo.index_files(project_root)  # rel_path -> row dict
    current: dict[str, Path] = {}
    for file in walk_files(root, max_files=DEFAULT_MAX_FILES):
        current[file.relative_to(root).as_posix()] = file

    file_rows: list[dict] = []
    all_symbols: list[tuple[str, list[dict]]] = []
    all_imports: list[tuple[str, list[dict]]] = []

    for rel in sorted(current):
        file = current[rel]
        digest = _sha256(file)
        if digest is None:
            continue
        try:
            size = file.stat().st_size
        except OSError:
            continue
        old = existing.get(rel)
        if old is not None and not full and old.get("hash") == digest:
            result.files_unchanged += 1
            symbols = json.loads(old.get("symbols_json") or "[]")
            imports = json.loads(old.get("imports_json") or "[]")
            language = old.get("language") or "unknown"
            indexed_at = old.get("indexed_at") or updated_at
        else:
            if old is None:
                result.files_added += 1
            else:
                result.files_changed += 1
            symbols, imports, language = _analyze(file)
            indexed_at = updated_at
        file_rows.append(
            {
                "rel_path": rel,
                "hash": digest,
                "language": language,
                "size_bytes": size,
                "symbols_json": json.dumps(symbols),
                "imports_json": json.dumps(imports),
                "indexed_at": indexed_at,
            }
        )
        all_symbols.append((rel, symbols))
        all_imports.append((rel, imports))

    result.files_removed += len(set(existing) - set(current))

    symbols_flat = [{**sym, "rel_path": rel} for rel, symbols in all_symbols for sym in symbols]
    references = _scan_references(root, all_symbols)
    test_map = _test_mapping(all_imports, current)

    with repo.transaction() as db:
        repo.replace_index_files(db, project_root, file_rows)
        repo.replace_symbols(db, project_root, symbols_flat)
        repo.replace_references(db, project_root, references)
        repo.replace_test_map(db, project_root, test_map)
        repo.set_meta(
            db,
            project_root,
            built_at,
            updated_at,
            {
                "files": len(file_rows),
                "symbols": len(symbols_flat),
                "references": len(references),
                "semantic_layer": SEMANTIC_LAYER_NONE,
            },
        )

    result.total_files = len(file_rows)
    result.total_symbols = len(symbols_flat)
    result.total_references = len(references)
    result.duration_s = time.monotonic() - started
    return result


def _scan_references(root: Path, indexed: list[tuple[str, list[dict]]]) -> list[dict]:
    """Map symbol name -> (rel_path, line) reference sites, definitions out."""
    names: set[str] = set()
    for _, symbols in indexed:
        for sym in symbols:
            names.add(sym["name"])
    if not names:
        return []
    pattern = re.compile(
        r"(?<![\w$])("
        + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
        + r")(?![\w$])"
    )
    seen: set[tuple[str, str, int]] = set()
    for rel, _symbols in indexed:
        lines = _read_lines(root / rel)
        if lines is None:
            continue
        for i, line in enumerate(lines):
            for match in pattern.finditer(line):
                name = match.group(1)
                if _defines_name(line, name):
                    continue
                seen.add((name, rel, i + 1))
    return [
        {"symbol": name, "rel_path": rel, "line": line_no} for name, rel, line_no in sorted(seen)
    ]


def _import_targets(imports: list[dict], current: dict[str, Path]) -> list[str]:
    """Best-effort resolution of import module heads to repo-relative files."""
    heads: dict[str, list[str]] = {}
    for rel in current:
        heads.setdefault(rel.split("/", 1)[0], []).append(rel)
    targets: list[str] = []
    for imp in imports:
        module = str(imp.get("module") or "").strip(".")
        if not module:
            continue
        head = module.split(".", 1)[0]
        for candidate in heads.get(head, []):
            if candidate not in targets:
                targets.append(candidate)
    return targets


def _test_mapping(
    all_imports: list[tuple[str, list[dict]]], current: dict[str, Path]
) -> list[dict]:
    rows: set[tuple[str, str]] = set()
    for rel, imports in all_imports:
        if not _is_test_file(rel):
            continue
        targets = set(_import_targets(imports, current))
        base = Path(rel).name
        if base.startswith("test_") and base.endswith(".py"):
            heuristic = base[len("test_") :]
            for candidate in current:
                if Path(candidate).name == heuristic and candidate != rel:
                    targets.add(candidate)
        rows.update((rel, target) for target in targets)
    return [{"test_file": rel, "target_file": target} for rel, target in sorted(rows)]


def query_index(repo, project_root: str, name: str, *, limit: int = 50) -> dict:
    """Symbols + references for a name, plus the tests that touch those files."""
    symbols = repo.symbols_by_name(project_root, name, limit=limit)
    references = repo.references(project_root, name, limit=limit)
    files = {row["rel_path"] for row in symbols}
    test_files = repo.tests_touching(project_root, files)
    return {"query": name, "symbols": symbols, "references": references, "test_files": test_files}


def doctor_index(repo, root: Path) -> dict:
    """Cheap consistency report (counts + drift, no reparsing)."""
    project_root = str(root.resolve())
    meta = repo.get_meta(project_root)
    stored = repo.index_files(project_root)
    present: dict[str, str] = {}
    for file in walk_files(root, max_files=DEFAULT_MAX_FILES):
        digest = _sha256(file)
        if digest:
            present[file.relative_to(root).as_posix()] = digest
    return {
        "project_root": project_root,
        "indexed": meta is not None,
        "meta": (
            {
                "files": meta["files"],
                "symbols": meta["symbols"],
                "references": meta["ref_count"],
                "semantic_layer": meta["semantic_layer"],
                "built_at": meta["built_at"],
                "updated_at": meta["updated_at"],
            }
            if meta
            else None
        ),
        "stored_files": len(stored),
        "disk_files": len(present),
        "missing_from_disk": sorted(set(stored) - set(present)),
        "new_on_disk": sorted(set(present) - set(stored)),
        "stale": sorted(
            rel for rel, digest in present.items() if stored.get(rel, {}).get("hash") != digest
        ),
        "consistent": not (
            meta is None
            or (set(stored) ^ set(present))
            or any(stored.get(rel, {}).get("hash") != digest for rel, digest in present.items())
        ),
    }
