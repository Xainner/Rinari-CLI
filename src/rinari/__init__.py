"""Rinari CLI — production agent harness."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("rinari")
except PackageNotFoundError:
    # Source checkout without installed metadata.
    __version__ = "0.1.0"
