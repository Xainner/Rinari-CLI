"""Common AST summary model and query grammar (phase 3 Tree-sitter/AST).

Adapters (tree-sitter, regex) normalize into the same record types. Lines
are 1-based. ``AstSummary.source`` records which engine produced the data:
``tree-sitter``, ``regex``, or ``unavailable``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SYMBOL_KINDS = ("function", "method", "class")


@dataclass(frozen=True, slots=True)
class AstSymbol:
    name: str
    kind: str
    qualified_name: str
    line: int
    end_line: int = 0


@dataclass(frozen=True, slots=True)
class AstImport:
    module: str
    names: tuple[tuple[str, str | None], ...]
    line: int


@dataclass(frozen=True, slots=True)
class AstCall:
    callee: str
    line: int


@dataclass(slots=True)
class AstSummary:
    language: str = "unknown"
    source: str = "unavailable"
    symbols: list[AstSymbol] = field(default_factory=list)
    imports: list[AstImport] = field(default_factory=list)
    calls: list[AstCall] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "language": self.language,
            "source": self.source,
            "symbols": [
                {
                    "name": s.name,
                    "kind": s.kind,
                    "qualified_name": s.qualified_name,
                    "line": s.line,
                    "end_line": s.end_line,
                }
                for s in self.symbols
            ],
            "imports": [
                {
                    "module": i.module,
                    "names": [list(n) for n in i.names],
                    "line": i.line,
                }
                for i in self.imports
            ],
            "calls": [{"callee": c.callee, "line": c.line} for c in self.calls],
        }


def query_summary(summary: AstSummary, query: str) -> list:
    """Evaluate the unified query grammar over a summary.

    Supported queries:
      symbols        -> all symbols
      functions      -> function + method symbols
      methods        -> method symbols only
      classes        -> class symbols
      imports        -> all imports
      calls          -> all call sites
      calls:NAME     -> calls whose callee is NAME or ends with ".NAME"
    """

    q = query.strip()
    if q == "symbols":
        return list(summary.symbols)
    if q == "functions":
        return [s for s in summary.symbols if s.kind in ("function", "method")]
    if q == "methods":
        return [s for s in summary.symbols if s.kind == "method"]
    if q == "classes":
        return [s for s in summary.symbols if s.kind == "class"]
    if q == "imports":
        return list(summary.imports)
    if q == "calls":
        return list(summary.calls)
    if q.startswith("calls:"):
        name = q[len("calls:") :].strip()
        if not name:
            raise ValueError(f"invalid ast query: {query!r}")
        return [c for c in summary.calls if c.callee == name or c.callee.endswith(f".{name}")]
    raise ValueError(f"invalid ast query: {query!r}")
