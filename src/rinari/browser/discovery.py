"""Resolve one browser executable without a shell or personal browser profile."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

CANDIDATES = (
    "chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
    "chrome", "headless_shell", "msedge",
)


def windows_candidates() -> list[str]:
    paths: list[str] = []
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(variable)
        if root:
            for relative in ("Microsoft/Edge/Application/msedge.exe",
                             "Google/Chrome/Application/chrome.exe"):
                paths.append(str(Path(root) / relative))
    try:
        import winreg
    except ImportError:
        return paths
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            for name in ("msedge.exe", "chrome.exe"):
                key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{name}"
                try:
                    with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as handle:
                        value, _ = winreg.QueryValueEx(handle, "")
                        if isinstance(value, str):
                            paths.append(os.path.expandvars(value).strip('"'))
                except OSError:
                    continue
    return paths


def find_browser(explicit: str | None = None) -> str | None:
    # An invalid explicit choice is an error, never an implicit fallback.
    if explicit:
        return shutil.which(explicit)
    for candidate in CANDIDATES:
        if resolved := shutil.which(candidate):
            return resolved
    if os.name == "nt":
        for candidate in windows_candidates():
            if resolved := shutil.which(candidate):
                return resolved
    return None
