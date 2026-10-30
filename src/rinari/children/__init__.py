"""Child sessions: conversations a mother opens for a separate piece of work.

See docs/planes/plan-sesiones-hijas.md (section 5) in Rinari-Agent.
"""

from rinari.children.store import (
    ACTIVE_LIMIT,
    DEPTH_LIMIT,
    ORIGINS,
    ChildRecord,
    ChildStore,
)

__all__ = ["ACTIVE_LIMIT", "DEPTH_LIMIT", "ORIGINS", "ChildRecord", "ChildStore"]
