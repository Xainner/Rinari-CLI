"""Terminal encoding helpers for direct (non-Rich) output."""

from __future__ import annotations

import contextlib
import sys
from typing import TextIO


def supports_unicode(stream: TextIO | None = None) -> bool:
    target = stream or sys.stdout
    encoding = getattr(target, "encoding", None) or "utf-8"
    try:
        "✓ × • … — · │".encode(encoding)  # noqa: RUF001
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def choice(unicode_text: str, ascii_text: str, stream: TextIO | None = None) -> str:
    return unicode_text if supports_unicode(stream) else ascii_text


def configure_utf8_stdio() -> None:
    """Prefer UTF-8 for the installed CLI while tolerating captured streams."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8", errors="replace")


__all__ = ["choice", "configure_utf8_stdio", "supports_unicode"]
