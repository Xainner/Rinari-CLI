"""Splitting the canonical Soul file into always-injected and on-demand parts.

Harness.md section 37: only the Canonical Soul is injected on every turn.
The `Extended Identity Reference` and `Maintainer Notes` sections are loaded
on demand (identity/appearance tasks) and never as a stable segment.
"""

from __future__ import annotations

EXTENDED_MARKER = "# Extended Identity Reference"
MAINTAINER_MARKER = "# Maintainer Notes"


def split_soul(text: str) -> tuple[str, str]:
    """Return (canonical, extended_identity) from a full soul.md text."""
    if not text:
        return "", ""
    extended = ""
    idx_ext = text.find(EXTENDED_MARKER)
    if idx_ext >= 0:
        extended = text[idx_ext:]
    canonical = text if idx_ext < 0 else text[:idx_ext]

    idx_m = canonical.find(MAINTAINER_MARKER)
    if idx_m >= 0:
        canonical = canonical[:idx_m]
    idx_m = extended.find(MAINTAINER_MARKER)
    if idx_m >= 0:
        extended = extended[:idx_m]
    return canonical.strip(), extended.strip()
