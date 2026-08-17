"""Unified AST layer: parser adapters over a common summary model.

The production target uses tree-sitter when the grammar is available
(currently Python) and falls back to the regex-based extractor from
``rinari.repo.search`` for all other languages. Both adapters produce the
same :class:`AstSummary` so consumers (repository index, future tools)
never care which engine produced it.
"""

from __future__ import annotations

from .base import AstCall, AstImport, AstSummary, AstSymbol, query_summary
from .registry import analyze_file, get_parser

__all__ = [
    "AstCall",
    "AstImport",
    "AstSummary",
    "AstSymbol",
    "analyze_file",
    "get_parser",
    "query_summary",
]
