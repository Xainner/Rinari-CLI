"""Tests for the unified AST layer (tree-sitter + regex fallback)."""

from pathlib import Path

import pytest

from rinari.ast import (
    AstCall,
    AstImport,
    AstSummary,
    AstSymbol,
    analyze_file,
    get_parser,
    query_summary,
)
from rinari.ast.regex_adapter import RegexAstParser
from rinari.repo import search as core

SAMPLE = """import os
from x import y as z

class S:
    def m(self):
        f(1)
        self.g()

def top():
    pass
"""


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_parser_summary(tmp_path):
    path = _write(tmp_path, "sample.py", SAMPLE)
    parser = get_parser(path)
    assert parser is not None
    summary = parser.analyze(path)
    assert summary.source in {"tree-sitter", "regex"}
    assert summary.language == "python"
    names = {(s.name, s.kind, s.qualified_name) for s in summary.symbols}
    assert ("S", "class", "S") in names
    assert ("m", "method", "S.m") in names
    assert ("top", "function", "top") in names
    callees = {c.callee for c in summary.calls}
    assert "f" in callees
    assert "self.g" in callees
    modules = {i.module for i in summary.imports}
    assert modules == {"os", "x"}
    from_names = {n for i in summary.imports for n in i.names if i.module == "x"}
    assert ("y", "z") in from_names


def test_regex_fallback_summary(tmp_path):
    path = _write(tmp_path, "sample.py", SAMPLE)
    summary = RegexAstParser().analyze(path)
    assert summary.source == "regex"
    names = {(s.name, s.kind, s.qualified_name) for s in summary.symbols}
    assert ("S", "class", "S") in names
    assert ("m", "method", "S.m") in names
    assert ("top", "function", "top") in names
    callees = {c.callee for c in summary.calls}
    assert "f" in callees
    assert "self.g" in callees
    modules = {i.module for i in summary.imports}
    assert modules == {"os", "x"}
    plain = next(i for i in summary.imports if i.module == "os")
    assert plain.names == (("os", None),)


def test_analyze_file_dispatch(tmp_path):
    py = _write(tmp_path, "a.py", SAMPLE)
    summary = analyze_file(py)
    assert summary is not None
    assert summary.source in {"tree-sitter", "regex"}
    assert analyze_file(tmp_path / "notes.txt") is None
    assert get_parser(tmp_path / "notes.txt") is None


def test_analyze_file_source_argument(tmp_path):
    path = _write(tmp_path, "src.py", "def only():\n    pass\n")
    summary = analyze_file(path, source="def other():\n    pass\n")
    assert summary is not None
    names = {s.name for s in summary.symbols}
    assert names == {"other"}


def test_unparsable_python_falls_back(tmp_path):
    path = _write(tmp_path, "bad.py", "def broken(:\n")
    summary = analyze_file(path)
    assert summary is not None
    assert summary.source in {"tree-sitter", "regex", "unavailable"}


def test_query_summary_grammar():
    summary = AstSummary(
        symbols=[
            AstSymbol(name="S", kind="class", qualified_name="S", line=3),
            AstSymbol(name="m", kind="method", qualified_name="S.m", line=4),
            AstSymbol(name="top", kind="function", qualified_name="top", line=9),
        ],
        imports=[
            AstImport(module="os", names=(("os", None),), line=1),
        ],
        calls=[
            AstCall(callee="f", line=5),
            AstCall(callee="self.g", line=6),
        ],
    )
    assert [s.name for s in query_summary(summary, "symbols")] == ["S", "m", "top"]
    assert [s.name for s in query_summary(summary, "functions")] == ["m", "top"]
    assert [s.name for s in query_summary(summary, "methods")] == ["m"]
    assert [s.name for s in query_summary(summary, "classes")] == ["S"]
    assert [i.module for i in query_summary(summary, "imports")] == ["os"]
    assert [c.callee for c in query_summary(summary, "calls")] == ["f", "self.g"]
    assert [c.callee for c in query_summary(summary, "calls:g")] == ["self.g"]
    assert [c.callee for c in query_summary(summary, "calls:f")] == ["f"]
    with pytest.raises(ValueError):
        query_summary(summary, "bogus")
    with pytest.raises(ValueError):
        query_summary(summary, "calls:")


def test_summary_to_dict(tmp_path):
    path = _write(tmp_path, "sample.py", SAMPLE)
    payload = analyze_file(path).to_dict()
    assert payload["source"] in {"tree-sitter", "regex"}
    assert any(s["qualified_name"] == "S.m" for s in payload["symbols"])
    assert {i["module"] for i in payload["imports"]} == {"os", "x"}
    assert {c["callee"] for c in payload["calls"]} >= {"f", "self.g"}


def test_find_symbols_uses_ast_layer(tmp_path):
    _write(tmp_path, "mod.py", SAMPLE)
    result = core.find_symbols(tmp_path, "S.m")
    assert result["results"], "qualified query S.m should match the method"
    first = result["results"][0]
    assert first["name"] == "m"
    assert first["kind"] == "method"
    assert first["file"] == "mod.py"
