"""Read-only capability prerequisites. Permissions remain a per-call decision."""

from __future__ import annotations

import os
import shutil

HOSTS = {
    "browser": "browser",
    "memory": "memory",
    "context": "context_retrieval",
    "verify": "validation",
    "lsp": "lsp",
    "process": "processes",
    "pty": "pty",
    "channel": "channel_host",
}


def availability(name: str, ctx=None) -> dict:
    family = name.split(".")[0]
    if family == "pty" and os.name == "nt":
        return {
            "available": False,
            "reason": "POSIX PTY unavailable on Windows; use process.start.",
            "requires": ["POSIX"],
        }
    binary = {"git": "git", "ssh": "ssh"}.get(family)
    if binary and not shutil.which(binary):
        return {
            "available": False,
            "reason": f"Install {binary} and add it to PATH.",
            "requires": [binary],
        }
    host = HOSTS.get(family)
    if ctx is not None and host and getattr(ctx, host, None) is None:
        return {
            "available": False,
            "reason": f"This session has no {family} service.",
            "requires": [host],
        }
    return {
        "available": True if ctx is not None else None,
        "reason": "Permissions and resource availability are checked at execution.",
        "requires": [host] if host else [],
    }
