"""Project detection: walk cwd toward the home boundary looking for markers.

Rules (harness.md section 7, AGENTS.md section 13):
- Strong markers make a directory an automatic PROJECT root:
  `.rinari/project.toml` or `.git`.
- Secondary markers (package.json, pyproject.toml, ...) never promote a
  directory on their own in phase 1; they are reported as candidates.
- $HOME itself is never a project root, and the upward walk stops at the
  home boundary so user-profile state can never become a project.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

STRONG_MARKER_RINARI = ".rinari/project.toml"
STRONG_MARKER_GIT = ".git"

SECONDARY_MARKERS = (
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Gemfile",
    "composer.json",
)


@dataclass(frozen=True, slots=True)
class ProjectDetection:
    project_root: Path | None
    marker: str | None
    secondary_markers: tuple[str, ...]

    @property
    def kind(self) -> str:
        return "PROJECT" if self.project_root is not None else "CHAT"


def _secondary_at(path: Path) -> tuple[str, ...]:
    found = [name for name in SECONDARY_MARKERS if (path / name).is_file()]
    for pattern in ("*.sln", "*.csproj"):
        if any(path.glob(pattern)):
            found.append(pattern)
    return tuple(found)


def detect_project(cwd: Path, home: Path) -> ProjectDetection:
    cwd = Path(cwd).expanduser().resolve()
    home = Path(home).expanduser().resolve()

    if cwd == home:
        return ProjectDetection(None, None, ())

    current = cwd
    while True:
        if (current / STRONG_MARKER_RINARI).is_file():
            return ProjectDetection(current, STRONG_MARKER_RINARI, _secondary_at(current))
        if (current / STRONG_MARKER_GIT).exists():
            return ProjectDetection(current, STRONG_MARKER_GIT, _secondary_at(current))
        parent = current.parent
        if parent == current:
            break
        if parent == home:
            break
        current = parent

    return ProjectDetection(None, None, _secondary_at(cwd))


def is_home_root(path: Path, home: Path) -> bool:
    return Path(path).expanduser().resolve() == Path(home).expanduser().resolve()
