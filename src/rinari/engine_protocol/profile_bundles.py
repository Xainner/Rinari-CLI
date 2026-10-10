"""Compatibility re-export: the store lives in `rinari.profiles.bundles`."""

from rinari.profiles.bundles import (
    DEFAULT_PROFILE_ID,
    DEFAULT_PROFILE_NAME,
    MAX_DESCRIPTION_CHARS,
    PROFILE_ID_RE,
    ProfileBundle,
    ProfileBundleStore,
    default_bundle,
)

__all__ = [
    "DEFAULT_PROFILE_ID",
    "DEFAULT_PROFILE_NAME",
    "MAX_DESCRIPTION_CHARS",
    "PROFILE_ID_RE",
    "ProfileBundle",
    "ProfileBundleStore",
    "default_bundle",
]
