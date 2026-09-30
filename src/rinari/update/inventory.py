"""What is installed: this CLI (and how) and the desktop app."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Literal

INSTALL_MARKER = ".rinari-install.json"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\rinari-code"
DESKTOP_EXECUTABLE = "rinari-agent.exe"

CliKind = Literal["bundled", "uv-git", "editable", "other"]


@dataclass(frozen=True, slots=True)
class CliInstall:
    kind: CliKind
    #: Commit the CLI was installed from (uv-git); the bundled one reports
    #: nothing here because the app's version is what identifies it.
    commit: str | None = None
    #: Where it comes from: the git URL, the editable checkout, or the app folder.
    source: str | None = None


@dataclass(frozen=True, slots=True)
class DesktopInstall:
    install_dir: Path
    version: str | None
    executable: Path
    running: bool

    @property
    def setup(self) -> Path:
        """The maintenance setup the installer keeps next to the app."""
        return self.install_dir / "Rinari-Setup.exe"


def _marker(folder: Path) -> dict | None:
    try:
        data = json.loads((folder / INSTALL_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def detect_cli(
    executable: str | os.PathLike[str] | None = None,
    direct_url: Callable[[], str | None] | None = None,
) -> CliInstall:
    """How this CLI is installed.

    - ``bundled``: it runs on the Python inside the desktop app
      (``<app>/resources/engine-dist``), so updating the app updates it.
    - ``uv-git``/``editable``: PEP 610 ``direct_url.json`` of the distribution.
    """
    python = Path(executable or sys.executable).resolve()
    for parent in python.parents:
        if parent.name == "engine-dist" and parent.parent.name == "resources":
            app = parent.parent.parent
            if _marker(app) is not None:
                return CliInstall("bundled", source=str(app))
    raw = (direct_url or _direct_url)()
    try:
        info = json.loads(raw) if raw else {}
    except ValueError:
        info = {}
    url = info.get("url") if isinstance(info.get("url"), str) else None
    vcs = info.get("vcs_info")
    if isinstance(vcs, dict) and vcs.get("vcs") == "git":
        commit = vcs.get("commit_id") if isinstance(vcs.get("commit_id"), str) else None
        return CliInstall("uv-git", commit=commit, source=url)
    folder = info.get("dir_info")
    if isinstance(folder, dict) and folder.get("editable"):
        return CliInstall("editable", source=url)
    return CliInstall("other", source=url)


def _direct_url() -> str | None:
    try:
        return metadata.distribution("rinari").read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None


def _registry_install_dirs() -> list[tuple[Path, str | None]]:
    if sys.platform != "win32":
        return []
    import winreg

    found: list[tuple[Path, str | None]] = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(
                hive, UNINSTALL_KEY, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY
            ) as key:
                location, _ = winreg.QueryValueEx(key, "InstallLocation")
                try:
                    version, _ = winreg.QueryValueEx(key, "DisplayVersion")
                except OSError:
                    version = None
        except OSError:
            continue
        if isinstance(location, str) and location:
            found.append((Path(location), version if isinstance(version, str) else None))
    return found


def _desktop_running() -> bool:
    if sys.platform != "win32":
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {DESKTOP_EXECUTABLE}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return DESKTOP_EXECUTABLE in result.stdout.lower()


def detect_desktop(
    registry: Callable[[], list[tuple[Path, str | None]]] = _registry_install_dirs,
    running: Callable[[], bool] = _desktop_running,
    environ: dict[str, str] | None = None,
) -> DesktopInstall | None:
    """The installed desktop app: from its uninstall entry, else RINARI_AGENT_BIN."""
    env = os.environ if environ is None else environ
    candidates = list(registry())
    override = env.get("RINARI_AGENT_BIN")
    if override:
        candidates.append((Path(override).parent, None))
    for folder, registered in candidates:
        executable = folder / DESKTOP_EXECUTABLE
        if not executable.is_file():
            continue
        marker = _marker(folder) or {}
        version = marker.get("version") if isinstance(marker.get("version"), str) else registered
        return DesktopInstall(folder, version, executable, running())
    return None
