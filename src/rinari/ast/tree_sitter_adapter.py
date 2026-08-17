"""Tree-sitter AST adapter (Python grammar; phase 3 Tree-sitter/AST).

The adapter degrades gracefully: if the grammar packages are not
importable, :data:`available` is False and the factory keeps using the
regex adapter.

tree-sitter >= 0.26 API notes:
  - the language capsule must be wrapped: ``ts.Language(py_capsule)``
  - every S-expression pattern needs parentheses, even bare node types
  - ``QueryCursor.captures(node)`` returns ``dict[str, list[Node]]``
"""

from __future__ import annotations

from pathlib import Path

from .base import AstCall, AstImport, AstSummary, AstSymbol

_PY_SUFFIX = ".py"

# Query key -> capture name used inside the S-expression.
_CAPTURE_NAME = {
    "class": "name",
    "function": "name",
    "call_ident": "callee",
    "import_plain": "module",
    "import_from": "stmt",
}

_QUERIES = {
    "class": "(class_definition name: (identifier) @name)",
    "function": "(function_definition name: (identifier) @name)",
    "call_ident": "(call function: (identifier) @callee)",
    "call_attr": (
        "(call function: (attribute object: (identifier) @obj attribute: (identifier) @attr))"
    ),
    "import_plain": "(import_statement name: (dotted_name) @module)",
    "import_from": "(import_from_statement) @stmt",
}


def available() -> bool:
    try:
        import tree_sitter  # noqa: F401
        import tree_sitter_python  # noqa: F401

        return True
    except ImportError:
        return False


class TreeSitterPythonParser:
    def __init__(self) -> None:
        import tree_sitter
        import tree_sitter_python

        self._ts = tree_sitter
        self._language = tree_sitter.Language(tree_sitter_python.language())
        self._parser = tree_sitter.Parser(self._language)
        self._cursors = {
            key: tree_sitter.QueryCursor(tree_sitter.Query(self._language, pattern))
            for key, pattern in _QUERIES.items()
        }

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() == _PY_SUFFIX

    @property
    def language(self) -> str:
        return "python"

    def analyze(self, path: Path, source: bytes | str | None = None) -> AstSummary:
        root = self._parse(path, source)
        if root is None:
            return AstSummary(language="python", source="unavailable")
        summary = AstSummary(language="python", source="tree-sitter")
        self._collect_classes(root, summary)
        self._collect_functions(root, summary)
        self._collect_calls(root, summary)
        self._collect_imports(root, summary)
        return summary

    def _parse(self, path: Path, source: bytes | str | None):
        data = source
        if data is None:
            try:
                data = path.read_bytes()
            except OSError:
                return None
        if isinstance(data, str):
            data = data.encode("utf-8")
        return self._parser.parse(data).root_node

    def _capture_nodes(self, key: str, root) -> list:
        return self._cursors[key].captures(root).get(_CAPTURE_NAME[key], [])

    def _collect_classes(self, root, summary: AstSummary) -> None:
        for node in self._capture_nodes("class", root):
            name = node.text.decode()
            summary.symbols.append(
                AstSymbol(
                    name=name,
                    kind="class",
                    qualified_name=name,
                    line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                )
            )

    def _collect_functions(self, root, summary: AstSummary) -> None:
        for node in self._capture_nodes("function", root):
            name = node.text.decode()
            class_scope = self._enclosing_class(node)
            kind = "method" if class_scope else "function"
            qualified = f"{class_scope}.{name}" if class_scope else name
            summary.symbols.append(
                AstSymbol(
                    name=name,
                    kind=kind,
                    qualified_name=qualified,
                    line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                )
            )

    def _collect_calls(self, root, summary: AstSummary) -> None:
        for node in self._capture_nodes("call_ident", root):
            summary.calls.append(AstCall(callee=node.text.decode(), line=node.start_point[0] + 1))
        attr = self._cursors["call_attr"].captures(root)
        for obj, attribute in zip(attr.get("obj", []), attr.get("attr", []), strict=False):
            summary.calls.append(
                AstCall(
                    callee=f"{obj.text.decode()}.{attribute.text.decode()}",
                    line=attribute.start_point[0] + 1,
                )
            )

    def _collect_imports(self, root, summary: AstSummary) -> None:
        for node in self._capture_nodes("import_plain", root):
            module = node.text.decode()
            summary.imports.append(
                AstImport(module=module, names=((module, None),), line=node.start_point[0] + 1)
            )
        for node in self._capture_nodes("import_from", root):
            module_node = node.child_by_field_name("module_name")
            module = module_node.text.decode() if module_node is not None else ""
            names: list[tuple[str, str | None]] = []
            for index, child in enumerate(node.children):
                if not child.is_named or node.field_name_for_child(index) != "name":
                    continue
                if child.type == "aliased_import":
                    inner = child.child_by_field_name("name")
                    alias = child.child_by_field_name("alias")
                    names.append(
                        (
                            inner.text.decode() if inner is not None else child.text.decode(),
                            alias.text.decode() if alias is not None else None,
                        )
                    )
                else:
                    names.append((child.text.decode(), None))
            summary.imports.append(
                AstImport(module=module, names=tuple(names), line=node.start_point[0] + 1)
            )

    def _enclosing_class(self, node) -> str | None:
        current = node.parent
        while current is not None:
            if current.type == "class_definition":
                name = current.child_by_field_name("name")
                if name is not None and name.type == "identifier":
                    return name.text.decode()
            current = current.parent
        return None
