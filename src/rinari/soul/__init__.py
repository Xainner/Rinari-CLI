"""Soul: versioned, customizable persona definitions (bundled default 4.0)."""

from rinari.soul.intensity import (
    DEFAULT_INTENSITY,
    INTENSITIES,
    intensity_instructions,
)
from rinari.soul.store import (
    DEFAULT_SOUL_ID,
    SOURCE_BUNDLED,
    SOURCE_CUSTOM,
    SoulDefinition,
    SoulStore,
)

__all__ = [
    "DEFAULT_INTENSITY",
    "DEFAULT_SOUL_ID",
    "INTENSITIES",
    "SOURCE_BUNDLED",
    "SOURCE_CUSTOM",
    "SoulDefinition",
    "SoulStore",
    "intensity_instructions",
]
