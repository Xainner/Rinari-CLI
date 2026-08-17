"""Skill catalog discovery (phase 4 foundation; the runtime is phase 6).

A skill is a directory with a `SKILL.md` whose YAML frontmatter carries
`name`, `description`, and `version` (harness.md 47-49). Layout:

    ~/.rinari/skills/<name>/SKILL.md        global (user)
    <project>/.rinari/skills/<name>/SKILL.md  project-local (needs trust)

Discovery is read-only and deterministic. When the frontmatter omits
`version`, the identity falls back to `sha:<12 hex>` of the file content,
so any on-disk change still moves the version — the resume reconciler only
needs stable, comparable version strings, never a YAML library.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

SKILL_FILE = "SKILL.md"
VERSION_SHA_PREFIX = "sha:"


@dataclass(frozen=True, slots=True)
class SkillInfo:
    name: str
    version: str
    path: str
    source: str  # "global" | "project"
    description: str = ""


def parse_frontmatter(text: str) -> dict[str, str]:
    """Parse the leading `---` frontmatter block into flat string fields."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line or line.startswith("#") or line.startswith((" ", "\t")):
            continue  # top-level `key: value` only; lists are out of scope here
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip("\"'")
    return fields


def skill_version(text: str) -> str:
    declared = parse_frontmatter(text).get("version")
    if declared:
        return declared
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{VERSION_SHA_PREFIX}{digest[:12]}"


def discover_skills(
    global_dir: str | None = None, project_dir: str | None = None
) -> dict[str, SkillInfo]:
    """Map skill name -> info; project skills shadow global ones by name."""
    from pathlib import Path

    found: dict[str, SkillInfo] = {}
    for source, base in (("global", global_dir), ("project", project_dir)):
        if base is None:
            continue
        root = Path(base)
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir(), key=lambda p: p.name):
            if not entry.is_dir():
                continue
            skill_md = entry / SKILL_FILE
            if not skill_md.is_file():
                continue
            try:
                text = skill_md.read_text(encoding="utf-8")
            except OSError:
                continue
            fields = parse_frontmatter(text)
            name = fields.get("name") or entry.name
            found[name] = SkillInfo(
                name=name,
                version=skill_version(text),
                path=str(skill_md),
                source=source,
                description=fields.get("description", ""),
            )
    return found


__all__ = [
    "SKILL_FILE",
    "VERSION_SHA_PREFIX",
    "SkillInfo",
    "discover_skills",
    "parse_frontmatter",
    "skill_version",
]
