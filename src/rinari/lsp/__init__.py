"""LSP layer: stdio JSON-RPC client, session manager, capability gating."""

from __future__ import annotations

from .client import (
    LspClient,
    LspError,
    LspNotStarted,
    LspProtocolError,
    LspRequestTimeout,
    LspServerCrashed,
    LspServerError,
    LspServerSpec,
    encode_message,
    read_message,
)
from .manager import (
    CAPABILITY_KEYS,
    LspManager,
    default_specs,
    language_id_for_path,
)

__all__ = [
    "CAPABILITY_KEYS",
    "LspClient",
    "LspError",
    "LspManager",
    "LspNotStarted",
    "LspProtocolError",
    "LspRequestTimeout",
    "LspServerCrashed",
    "LspServerError",
    "LspServerSpec",
    "default_specs",
    "encode_message",
    "language_id_for_path",
    "read_message",
]
