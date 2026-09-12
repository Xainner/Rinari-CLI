"""Installation-owned media limits; no model-supplied overrides."""

import os


def limit(name, default):
    value = int(os.environ.get("RINARI_MEDIA_" + name.upper(), default))
    if value <= 0:
        raise ValueError("Media limits must be positive")
    return value
