"""Parser factory: pick the best adapter for a file path.

Priority: tree-sitter when the grammar is installed and the suffix is
supported; otherwise the regex adapter for known text languages; otherwise
no parser (consumers get ``None`` / ``unavailable``).
"""

from __future__ import annotations

import threading
from pathlib import Path

from .base import AstSummary
from .regex_adapter import RegexAstParser

_lock = threading.Lock()
_ts_parser = None
_ts_tried = False
_regex_parser = RegexAstParser()


def _get_ts_parser():
    global _ts_parser, _ts_tried
    with _lock:
        if _ts_tried:
            return _ts_parser
        _ts_tried = True
        try:
            from .tree_sitter_adapter import TreeSitterPythonParser, available

            if available():
                _ts_parser = TreeSitterPythonParser()
        except Exception:
            _ts_parser = None
        return _ts_parser


def get_parser(path: Path):
    """Return the best parser for ``path`` (or ``None`` if unsupported)."""
    if path.suffix.lower() == ".py":
        ts = _get_ts_parser()
        if ts is not None:
            return ts
    if _regex_parser.supports(path):
        return _regex_parser
    return None


def analyze_file(path: Path, source: bytes | str | None = None) -> AstSummary | None:
    """Analyze one file with the best available parser (None if unsupported)."""
    parser = get_parser(path)
    if parser is None:
        return None
    try:
        return parser.analyze(path, source)
    except Exception:
        if path.suffix.lower() == ".py" and source is None:
            return _regex_parser.analyze(path)
        return AstSummary(source="unavailable")
