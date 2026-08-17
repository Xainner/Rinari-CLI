"""Regex AST adapter: language fallback when no grammar is installed.

Python symbols come from ``rinari.repo.search.symbols_for_file`` (the same
extractor the search tools use); imports and call sites are line-based and
conservative (keyword-guarded) so the fallback stays deterministic and
fast on large repositories.
"""

from __future__ import annotations

import re
from pathlib import Path

from rinari.repo.search import _read_lines, symbols_for_file

from .base import AstCall, AstImport, AstSummary, AstSymbol

SUPPORTED_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".rs", ".go"}

_GUARD_KEYWORDS = {
    "if",
    "elif",
    "for",
    "while",
    "with",
    "return",
    "lambda",
    "assert",
    "yield",
    "raise",
    "def",
    "async",
    "not",
    "and",
    "or",
    "in",
    "is",
}

_CALL_PATTERN = re.compile(r"(?<![\w.])([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\(")
_IMPORT_PATTERN = re.compile(r"^\s*import\s+([\w.]+(?:\s*,\s*[\w.]+)*)")
_FROM_PATTERN = re.compile(r"^\s*from\s+(\.*[\w.]*)\s+import\s+(.+)$")
_DEFINITION_HEADER = re.compile(
    r"^\s*(?:@(?:[\w.]+|\()|(?:async\s+)?def\s|(?:export\s+)?(?:function|class)\s)"
)


class RegexAstParser:
    def __init__(self) -> None:
        self._name = "regex"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in SUPPORTED_SUFFIXES

    @property
    def language(self) -> str:
        return "regex"

    def analyze(self, path: Path, source: bytes | str | None = None) -> AstSummary:
        suffix = path.suffix.lower()
        language = "python" if suffix == ".py" else "unknown"
        lines = _read_lines(path)
        summary = AstSummary(language=language, source="regex")
        if lines is None:
            return summary
        for symbol in symbols_for_file(path):
            # Only method contexts are qualified names ("Class.method");
            # other contexts carry the raw definition line, not a name.
            qualified = symbol.context if symbol.kind == "method" else symbol.name
            summary.symbols.append(
                AstSymbol(
                    name=symbol.name,
                    kind=symbol.kind,
                    qualified_name=qualified,
                    line=symbol.line,
                )
            )
        for i, line in enumerate(lines):
            if suffix == ".py":
                self._scan_python_line(summary, line, i + 1)
        return summary

    def _scan_python_line(self, summary: AstSummary, line: str, line_no: int) -> None:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or _DEFINITION_HEADER.match(line):
            return
        im = _IMPORT_PATTERN.match(line)
        if im:
            modules = [m.strip() for m in im.group(1).split(",")]
            summary.imports.append(
                AstImport(module=modules[0], names=tuple((m, None) for m in modules), line=line_no)
            )
            return
        fm = _FROM_PATTERN.match(line)
        if fm:
            names = []
            for part in fm.group(2).split(","):
                part = part.strip()
                if not part:
                    continue
                if " as " in part:
                    name, alias = (p.strip() for p in part.split(" as ", 1))
                else:
                    name, alias = part, None
                names.append((name, alias))
            summary.imports.append(AstImport(module=fm.group(1), names=tuple(names), line=line_no))
            return
        for m in _CALL_PATTERN.finditer(line):
            callee = m.group(1)
            head = callee.split(".", 1)[0]
            if head in _GUARD_KEYWORDS:
                continue
            summary.calls.append(AstCall(callee=callee, line=line_no))
