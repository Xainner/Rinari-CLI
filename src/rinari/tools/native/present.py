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
    "preview (http://localhost:5173)."
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
    if raw.startswith("artifact://"):
        item["uri"] = raw
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
    sink = getattr(ctx, "activity_sink", None)
    if callable(sink):
        with contextlib.suppress(Exception):
            sink(
                "file.presented",
                {"present_id": getattr(ctx, "tool_call_id", "") or "present", "items": items},
            )
    return ToolResult(
        ok=True,
        data={
            "presented": len(items),
            "items": [{"name": i["name"], "kind": i["kind"]} for i in items],
        },
    )


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
