"""file.present: hand files, folders or pages to the owner (plan 0.4.0 #35).

A path written in the answer is only text: some models write a Markdown link,
others a path in backticks the owner cannot open. With this tool the desktop
draws a card for each item (a player for audio and video, a preview for
images and PDFs, the first rows of a sheet, a folder's first names, a page or
a local preview) and opens it in the side panel. Only what the conversation
may read can be presented: the project or workspace folders and this
conversation's saved outputs (artifact://), plus web and local URLs.
"""

from __future__ import annotations

import contextlib
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from rinari.tools.definition import (
    RISK_LOW,
    SIDE_EFFECT_NONE,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)

MAX_ITEMS = 20
MAX_ENTRIES = 8
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1", "0.0.0.0"}
_OFFICE = {".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".odt", ".ods", ".odp"}
_SHEET = {".csv", ".tsv"}
_MARKDOWN = {".md", ".markdown"}
_HTML = {".html", ".htm"}
_ARCHIVE = {".zip"}

DESCRIPTION = (
    "Hand files, folders or pages to the user so they can open them: deliverables you "
    "made (audio, video, images, documents, sheets, sites), not every file you touched. "
    "The app shows a card with a player or preview. items: [{path | url, caption?}]; "
    "paths inside the project or workspace, or artifact://; urls for a page or a local "
    "preview (http://localhost:5173). This is how the user sees an image, capture or "
    "download a tool made for you; each one is shown once."
)

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_ITEMS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string"},
                    "url": {"type": "string"},
                    "caption": {"type": "string", "maxLength": 200},
                },
            },
        },
    },
}


def _fail(code: ToolErrorCode, message: str) -> ToolResult:
    return ToolResult(ok=False, error=ToolErrorInfo(code=code, message=message, retryable=False))


def _kind(path: Path) -> tuple[str, str]:
    """Card kind and MIME: image, audio, video, pdf, office, sheet, html,
    markdown, text, archive or file."""
    from rinari.engine_protocol.desktop import _file_kind

    head = b""
    with contextlib.suppress(OSError), path.open("rb") as handle:
        head = handle.read(64)
    sniffed, mime = _file_kind(path, head)
    suffix = path.suffix.lower()
    if sniffed in ("image", "audio", "video", "pdf"):
        return sniffed, mime
    if suffix in _OFFICE:
        return "office", mime
    if suffix in _SHEET:
        return "sheet", mime
    if suffix in _HTML:
        return "html", "text/html"
    if suffix in _MARKDOWN:
        return "markdown", "text/markdown"
    if suffix in _ARCHIVE:
        return "archive", "application/zip"
    return ("text" if sniffed == "text" else "file"), mime


def _folder(path: Path) -> dict[str, Any]:
    names: list[str] = []
    count = 0
    with contextlib.suppress(OSError):
        for child in sorted(path.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
            count += 1
            if len(names) < MAX_ENTRIES:
                names.append(child.name + ("/" if child.is_dir() else ""))
    return {"kind": "folder", "entry_count": count, "entries": names}


def _archive(path: Path) -> dict[str, Any]:
    with contextlib.suppress(OSError, zipfile.BadZipFile), zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        return {"entry_count": len(names), "entries": names[:MAX_ENTRIES]}
    return {}


def _url_item(url: str, caption: str) -> tuple[dict[str, Any] | None, str | None]:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None, f"Only http(s) URLs can be presented: {url}"
    local = parts.hostname in _LOCAL_HOSTS
    return {
        "kind": "preview" if local else "web",
        "url": url,
        "name": parts.netloc if local else parts.hostname,
        "caption": caption,
    }, None


def _path_item(
    ctx: ToolContext, raw: str, caption: str
) -> tuple[dict[str, Any] | None, ToolResult | None]:
    from rinari.tools.native.fs import _resolve_read

    resolved, error = _resolve_read(ctx, raw)
    if error is not None or resolved is None:
        return None, error
    item: dict[str, Any] = {
        "path": str(resolved),
        "name": resolved.name,
        "caption": caption,
    }
    uri = raw if raw.startswith("artifact://") else _artifact_uri(ctx, resolved)
    if uri:
        # A saved output named by its file path (P3): the desktop opens it by
        # URI; the path itself is the Engine's private store.
        item["uri"] = uri
    if resolved.is_dir():
        item.update(_folder(resolved))
        return item, None
    kind, mime = _kind(resolved)
    item.update(kind=kind, mime=mime)
    with contextlib.suppress(OSError):
        item["size_bytes"] = resolved.stat().st_size
    if kind == "archive":
        item.update(_archive(resolved))
    return item, None


def _artifact_uri(ctx: ToolContext, resolved: Path) -> str | None:
    """`artifact://<session>/…` for a path inside this conversation's saved outputs."""
    root = getattr(ctx, "artifact_root", None)
    session_id = getattr(ctx, "session_id", None)
    if not root or not session_id:
        return None
    try:
        base = (Path(root) / session_id).resolve()
        relative = resolved.resolve().relative_to(base)
    except (OSError, ValueError):
        return None
    if not relative.parts:
        return None
    return f"artifact://{session_id}/{relative.as_posix()}"


def _delivery_key(item: dict[str, Any]) -> str | None:
    """What makes two cards the same picture: its URI, else its content."""
    if item.get("uri"):
        return str(item["uri"])
    if item.get("url"):
        return str(item["url"])
    path = item.get("path")
    if not path or item.get("kind") == "folder":
        return None
    import hashlib

    try:
        if Path(path).stat().st_size > 64 * 1024 * 1024:
            return f"path:{path}"
        return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def present_items(
    ctx: ToolContext, items: list[dict[str, Any]], present_id: str
) -> list[dict[str, Any]]:
    """Emit one card for the items not shown yet this turn; return those shown."""
    turn_state = getattr(ctx, "turn_state", None)
    seen = getattr(turn_state, "presented", None)
    fresh: list[dict[str, Any]] = []
    for item in items:
        key = _delivery_key(item)
        if seen is not None and key is not None:
            if key in seen:
                continue
            seen.add(key)
        fresh.append(item)
    sink = getattr(ctx, "activity_sink", None)
    if fresh and callable(sink):
        with contextlib.suppress(Exception):
            sink("file.presented", {"present_id": present_id, "items": fresh})
    return fresh


def _present(arguments: dict, ctx: ToolContext) -> ToolResult:
    raw_items = (arguments or {}).get("items")
    if not isinstance(raw_items, list) or not raw_items:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "items must be a non-empty list")
    if len(raw_items) > MAX_ITEMS:
        return _fail(ToolErrorCode.INVALID_ARGUMENT, f"at most {MAX_ITEMS} items")
    items: list[dict[str, Any]] = []
    for index, entry in enumerate(raw_items):
        if not isinstance(entry, dict):
            return _fail(ToolErrorCode.INVALID_ARGUMENT, f"item {index} must be an object")
        caption = entry.get("caption") if isinstance(entry.get("caption"), str) else ""
        caption = " ".join(caption.split())[:200]
        path, url = entry.get("path"), entry.get("url")
        if bool(path) == bool(url):
            return _fail(ToolErrorCode.INVALID_ARGUMENT, f"item {index}: give a path or a url")
        if url:
            item, message = _url_item(str(url), caption)
            if item is None:
                return _fail(ToolErrorCode.INVALID_ARGUMENT, message or "invalid url")
        else:
            item, error = _path_item(ctx, str(path), caption)
            if item is None:
                return error or _fail(ToolErrorCode.NOT_FOUND, f"cannot present {path}")
        items.append(item)
    shown = present_items(ctx, items, getattr(ctx, "tool_call_id", "") or "present")
    data: dict[str, Any] = {
        "presented": len(shown),
        "items": [{"name": i["name"], "kind": i["kind"]} for i in shown],
    }
    if len(shown) < len(items):
        data["already_shown"] = len(items) - len(shown)
        data["note"] = "Some items were already shown to the user this turn; not shown again."
    return ToolResult(ok=True, data=data)


def present_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="file.present",
            description=DESCRIPTION,
            input_schema=INPUT_SCHEMA,
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            idempotent=True,
            handler=_present,
            namespace="file",
            always_loaded=True,
        )
    ]
