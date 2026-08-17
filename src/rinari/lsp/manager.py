"""Session-scoped LSP manager (phase 3 LSP).

One manager per session (lives in ToolContext, like the process registry).
Language servers are spawned lazily on first use, cached, and shut down
together with the session. Spawn failures are remembered for the session
(the model is told to fall back to search.* tools instead of retrying).

Capability gating: an operation is only performed when the server
advertised the corresponding provider in its initialize result, so a
minimal language server never receives calls it cannot serve.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from urllib.parse import unquote, urlparse

from .client import LspClient, LspError, LspServerSpec

# LSP capability key per high-level operation.
CAPABILITY_KEYS = {
    "definition": "definitionProvider",
    "references": "referencesProvider",
    "symbols": "documentSymbolProvider",
    "hover": "hoverProvider",
    "signature": "signatureHelpProvider",
    "rename": "renameProvider",
    "diagnostics": None,  # client-side: publishDiagnostics notifications
}

LANGUAGE_IDS = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
}


def language_id_for_path(path: Path) -> str | None:
    return LANGUAGE_IDS.get(Path(path).suffix.lower())


def default_specs() -> list[LspServerSpec]:
    """Well-known servers, activated only when the binary is on PATH.

    The initial LSP choice is a pending TODO decision; keeping discovery
    environment-based means the client works out of the box where a server
    already exists and degrades to search.* tools elsewhere.
    """
    specs: list[LspServerSpec] = []
    if shutil.which("pyright-langserver"):
        specs.append(
            LspServerSpec(
                name="pyright", languages=("python",), command=("pyright-langserver", "--stdio")
            )
        )
    if shutil.which("typescript-language-server"):
        specs.append(
            LspServerSpec(
                name="tsserver",
                languages=("typescript", "javascript"),
                command=("typescript-language-server", "--stdio"),
            )
        )
    return specs


def _uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        path_str = unquote(parsed.path)
        if len(path_str) >= 3 and path_str.startswith("/") and path_str[2] == ":":
            path_str = path_str[1:]  # Windows /C:/... -> C:/...
        return Path(path_str)
    return Path(uri)


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


class LspManager:
    def __init__(self, root: Path, specs: list[LspServerSpec] | None = None) -> None:
        import atexit

        self.root = Path(root)
        self.specs: dict[str, LspServerSpec] = {}
        self._entries: dict[str, LspClient | Exception] = {}
        self._torn_down = False
        # Language servers are child processes; guarantee their shutdown when
        # the CLI process ends from any exit path (REPL, one-shot, crash).
        atexit.register(self.shutdown_all)
        for spec in specs if specs is not None else default_specs():
            self.register(spec)

    def register(self, spec: LspServerSpec) -> None:
        if spec.name in self.specs:
            self._entries.pop(spec.name, None)
        self.specs[spec.name] = spec

    # -- lifecycle ------------------------------------------------------

    def server_for(self, language: str) -> LspClient | None:
        if self._torn_down:
            return None
        spec = next((s for s in self.specs.values() if language in s.languages), None)
        if spec is None:
            return None
        if spec.name not in self._entries:
            try:
                client = LspClient(spec, self.root)
                client.start()
                self._entries[spec.name] = client
            except Exception as exc:  # remember spawn failure for the session
                self._entries[spec.name] = exc
                return None
        entry = self._entries[spec.name]
        return entry if isinstance(entry, LspClient) else None

    def available_languages(self) -> list[str]:
        languages: set[str] = set()
        for spec in self.specs.values():
            if spec.name in self._entries and isinstance(self._entries[spec.name], LspClient):
                languages.update(spec.languages)
        return sorted(languages)

    def shutdown_all(self) -> None:
        self._torn_down = True  # once torn down the manager never re-spawns
        for entry in list(self._entries.values()):
            if isinstance(entry, LspClient):
                entry.close()
        self._entries.clear()

    # -- capability gating ------------------------------------------------

    def supports(self, language: str, operation: str) -> bool:
        if operation not in CAPABILITY_KEYS:
            raise ValueError(f"unknown LSP operation: {operation!r}")
        client = self.server_for(language)
        if client is None:
            return False
        key = CAPABILITY_KEYS[operation]
        if key is None:
            return True
        value = client.capabilities.get("textDocument", {}).get(key) or client.capabilities.get(key)
        return bool(value)

    def _require(self, path: Path, operation: str) -> tuple[LspClient, str]:
        language = language_id_for_path(path)
        if language is None:
            raise LspError(f"no LSP language id for {Path(path).suffix or 'this file'}")
        client = self.server_for(language)
        if client is None:
            raise LspError(
                f"no LSP server available for language {language!r}; fall back to search.* tools"
            )
        if operation in CAPABILITY_KEYS and not self.supports(language, operation):
            raise LspError(f"server {client.spec.name} does not support {operation}")
        self._ensure_open(client, path, language)
        return client, language

    def _ensure_open(self, client: LspClient, path: Path, language: str) -> None:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise LspError(f"cannot read {path}: {exc.__class__.__name__}") from exc
        if client.is_document_open(client.uri_for(path)):
            client.change_document(path, text)
        else:
            client.open_document(path, text, language)

    @staticmethod
    def _position(line: int, column: int) -> dict:
        # User-facing positions are 1-based; LSP positions are 0-based.
        if line < 1 or column < 1:
            raise LspError("line and column are 1-based and must be >= 1")
        return {"line": line - 1, "character": column - 1}

    # -- operations ---------------------------------------------------------

    def definition(self, path: Path, line: int, column: int) -> list[dict]:
        client, _ = self._require(path, "definition")
        raw = client.request(
            "textDocument/definition",
            {
                "textDocument": {"uri": client.uri_for(path)},
                "position": self._position(line, column),
            },
        )
        if raw is None:
            return []
        locations = raw if isinstance(raw, list) else [raw]
        targets = []
        for location in locations:
            if not isinstance(location, dict):
                continue
            target = location.get("targetUri") or location.get("uri")
            rng = location.get("targetRange") or location.get("range") or {}
            if not target or "line" not in rng.get("start", {}):
                continue
            loc = _uri_to_path(str(target))
            targets.append(
                {
                    "file": str(loc),
                    "relative": _rel(self.root, loc),
                    "line": int(rng["start"]["line"]) + 1,
                    "column": int(rng["start"]["character"]) + 1,
                }
            )
        return targets

    def references(self, path: Path, line: int, column: int) -> list[dict]:
        client, _ = self._require(path, "references")
        raw = (
            client.request(
                "textDocument/references",
                {
                    "textDocument": {"uri": client.uri_for(path)},
                    "position": self._position(line, column),
                    "context": {"includeDeclaration": True},
                },
            )
            or []
        )
        out = []
        for entry in raw:
            if not isinstance(entry, dict) or "uri" not in entry or "range" not in entry:
                continue
            loc = _uri_to_path(str(entry["uri"]))
            out.append(
                {
                    "file": str(loc),
                    "relative": _rel(self.root, loc),
                    "line": int(entry["range"]["start"]["line"]) + 1,
                    "column": int(entry["range"]["start"]["character"]) + 1,
                }
            )
        return out

    def symbols(self, path: Path) -> list[dict]:
        client, _ = self._require(path, "symbols")
        raw = (
            client.request(
                "textDocument/documentSymbol",
                {"textDocument": {"uri": client.uri_for(path)}},
            )
            or []
        )

        def flatten(node: dict, prefix: str = "") -> list[dict]:
            name = str(node.get("name", ""))
            qualified = f"{prefix}.{name}" if prefix else name
            rng = node.get("selectionRange") or node.get("range") or {}
            items = [
                {
                    "name": name,
                    "qualified_name": qualified,
                    "kind": node.get("symbolKind"),
                    "line": int(rng.get("start", {}).get("line", 0)) + 1,
                    "end_line": int(rng.get("end", {}).get("line", 0)) + 1,
                }
            ]
            for child in node.get("children") or []:
                items.extend(flatten(child, qualified))
            return items

        out = []
        for entry in raw:
            if isinstance(entry, dict):
                out.extend(flatten(entry))
        return out

    def hover(self, path: Path, line: int, column: int) -> dict:
        client, _ = self._require(path, "hover")
        raw = client.request(
            "textDocument/hover",
            {
                "textDocument": {"uri": client.uri_for(path)},
                "position": self._position(line, column),
            },
        )
        if not raw or not isinstance(raw, dict):
            return {"contents": None}
        contents = raw.get("contents")
        if isinstance(contents, dict):
            contents = contents.get("value")
        if isinstance(contents, list):
            parts = []
            for item in contents:
                if isinstance(item, dict):
                    parts.append(item.get("value", ""))
                elif isinstance(item, str):
                    parts.append(item)
            contents = "\n".join(parts)
        return {"contents": contents if isinstance(contents, str) else None}

    def signature_help(self, path: Path, line: int, column: int) -> dict:
        client, _ = self._require(path, "signature")
        raw = client.request(
            "textDocument/signatureHelp",
            {
                "textDocument": {"uri": client.uri_for(path)},
                "position": self._position(line, column),
            },
        )
        if not raw or not isinstance(raw, dict):
            return {"signatures": []}
        signatures = []
        for sig in raw.get("signatures") or []:
            if not isinstance(sig, dict):
                continue
            params = [p.get("label") for p in (sig.get("parameters") or []) if isinstance(p, dict)]
            signatures.append({"label": sig.get("label"), "parameters": params})
        return {
            "signatures": signatures,
            "active_signature": raw.get("activeSignature"),
        }

    def rename(self, path: Path, line: int, column: int, new_name: str) -> dict:
        """Plan a rename; returns the WorkspaceEdit without applying it.

        Applying the edit is an explicit fs operation, so the rename tool
        itself stays read-only and safe behind normal policy.
        """
        if not isinstance(new_name, str) or not new_name:
            raise LspError("new_name must be a non-empty string")
        client, _ = self._require(path, "rename")
        raw = client.request(
            "textDocument/rename",
            {
                "textDocument": {"uri": client.uri_for(path)},
                "position": self._position(line, column),
                "newName": new_name,
            },
        )
        if not isinstance(raw, dict):
            return {"changes": {}, "document_changes": None}
        changes = raw.get("changes") or {}
        normalized: dict[str, list[dict]] = {}
        for uri, edits in changes.items():
            loc = _uri_to_path(str(uri))
            normalized[_rel(self.root, loc)] = [
                {
                    "start": (e["range"]["start"]["line"], e["range"]["start"]["character"]),
                    "end": (e["range"]["end"]["line"], e["range"]["end"]["character"]),
                    "new_text": e.get("newText", ""),
                }
                for e in (edits or [])
                if isinstance(e, dict) and "range" in e
            ]
        document_changes = raw.get("documentChanges")
        if document_changes and isinstance(document_changes, list):
            document_changes = [
                {
                    "file": _rel(
                        self.root, _uri_to_path(str(ch.get("textDocument", {}).get("uri", "")))
                    ),
                    "edits": ch.get("edits"),
                }
                for ch in document_changes
                if isinstance(ch, dict)
            ]
        else:
            document_changes = None
        return {"changes": normalized, "document_changes": document_changes}

    def diagnostics(self, path: Path) -> list[dict]:
        client, _ = self._require(path, "diagnostics")
        out = []
        for diag in client.diagnostics(path):
            if not isinstance(diag, dict):
                continue
            rng = diag.get("range") or {}
            out.append(
                {
                    "severity": diag.get("severity"),
                    "code": diag.get("code"),
                    "message": diag.get("message"),
                    "line": int(rng.get("start", {}).get("line", 0)) + 1,
                    "column": int(rng.get("start", {}).get("character", 0)) + 1,
                }
            )
        return out
