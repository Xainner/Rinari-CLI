"""Skill manifest: schema, parsing and validation (phase 6).

A skill is a directory `<root>/<name>/SKILL.md`. The YAML-ish frontmatter
carries the metadata; the markdown body carries the procedure. Per
harness.md 48:

    ---
    name: fix-ci
    description: Diagnose and repair failing CI checks.
    version: 1.0.0
    triggers:
      - ci failing
    required_tools:
      - git.status
      - shell.exec
    optional_tools:
      - github.checks
    risk: medium
    can_delegate: true
    ---
    # Procedure ...
    # Verification ...
    # Failure handling ...
    # Success criteria ...

Parsing is stdlib-only (the phase-4 catalog parser handled flat scalars;
this one additionally collects `- item` lists). A `required_tools` entry is
a *capability request*, never a grant: the runtime still evaluates every
tool call against the policy engine (harness.md 51).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from rinari.skills.catalog import skill_version

SKILL_FILE = "SKILL.md"
VALID_RISK = ("low", "medium", "high")
VALID_SOURCES = ("packaged", "user", "project")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# Body sections recognized by convention (harness.md 48). Order matters:
# the first `# <title>` line after each marker starts that section.
_SECTION_TITLES = ("procedure", "verification", "failure handling", "success criteria")

_BODY_KEYS = {
    "procedure": "procedure",
    "verification": "verification",
    "failure handling": "failure_policy",
    "success criteria": "success_criteria",
}


class SkillError(Exception):
    """Structured skill failure (code + message)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        # SKILL_INVALID | NAME_INVALID | VERSION_INVALID | RISK_INVALID
        # | SKILL_NOT_FOUND | ALREADY_EXISTS | REMOVE_FAILED
        # | TOOL_NOT_FOUND | TRUST_REQUIRED | LOAD_FAILED
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class SkillManifest:
    name: str
    description: str
    version: str
    source: str  # "packaged" | "user" | "project"
    triggers: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()
    optional_tools: tuple[str, ...] = ()
    risk: str = "low"
    can_delegate: bool = False
    procedure: str = ""
    verification: str = ""
    failure_policy: str = ""
    success_criteria: str = ""
    # Full markdown body (lazy-load payload).
    body: str = ""
    path: str = ""

    def requests(self) -> tuple[str, ...]:
        return self.required_tools + self.optional_tools

    def summary(self) -> str:
        return f"{self.name}: {self.description} ({self.risk}, v{self.version})"


def parse_frontmatter_lists(text: str) -> dict[str, list[str] | str]:
    """Parse the leading `---` block into scalars and `- item` lists.

    Superset of catalog.parse_frontmatter: nested `key:` followed by
    indented `- value` lines becomes a list.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, list[str] | str] = {}
    current_key: str | None = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") and current_key is not None:
            assert isinstance(fields[current_key], list)
            fields[current_key].append(stripped[2:].strip().strip("\"'"))
            continue
        if line.startswith((" ", "\t")):
            continue  # ignore non-list continuation lines
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip().strip("\"'")
        if value == "":
            fields[key] = []
            current_key = key
        else:
            fields[key] = value
            current_key = None
    return fields


def _split_sections(body: str) -> dict[str, str]:
    sections: dict[str, str | None] = {k: None for k in _BODY_KEYS.values()}
    chunks: dict[str, list[str]] = {k: [] for k in _BODY_KEYS.values()}
    active: str | None = None
    preamble: list[str] = []
    for line in body.splitlines():
        header = re.match(r"^#{1,2}\s+(.*)$", line.strip())
        if header:
            title = header.group(1).strip().lower()
            active = next((k for t, k in _BODY_KEYS.items() if title == t), None)
            continue
        if active is None:
            preamble.append(line)
        else:
            chunks[active].append(line)
    for key, lines_ in chunks.items():
        text = "\n".join(lines_).strip()
        if text:
            sections[key] = text
    return {k: (v or "") for k, v in sections.items()}


def load_skill_manifest(path: str | Path, source: str) -> SkillManifest:
    """Load + minimally validate one SKILL.md. Raises SkillError."""
    skill_md = Path(path)
    if skill_md.name != SKILL_FILE:
        skill_md = skill_md / SKILL_FILE
    if not skill_md.is_file():
        raise SkillError("SKILL_NOT_FOUND", f"no {SKILL_FILE} at {skill_md.parent}")
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillError("LOAD_FAILED", f"cannot read {skill_md}: {exc}") from exc

    fields = parse_frontmatter_lists(text)
    # Body = everything after the closing `---`.
    idx = text.find("\n---", 3)
    body = text[idx + 4 :].lstrip("\n") if idx != -1 else text
    sections = _split_sections(body)

    name = str(fields.get("name") or skill_md.parent.name)
    if not _NAME_RE.match(name):
        raise SkillError("NAME_INVALID", f"skill name invalid: {name!r}")
    version = str(fields.get("version") or "")
    if not version:
        version = skill_version(text)
    elif not _VERSION_RE.match(version) and not version.startswith("sha:"):
        raise SkillError("VERSION_INVALID", f"skill version invalid: {version!r}")
    risk = str(fields.get("risk") or "low")
    if risk not in VALID_RISK:
        raise SkillError("RISK_INVALID", f"skill risk invalid: {risk!r}")

    def _as_list(key: str) -> tuple[str, ...]:
        value = fields.get(key)
        if isinstance(value, list):
            return tuple(str(v) for v in value if v)
        if isinstance(value, str) and value:
            return tuple(v.strip() for v in value.split(",") if v.strip())
        return ()

    can_delegate_raw = str(fields.get("can_delegate") or "false").lower()
    return SkillManifest(
        name=name,
        description=str(fields.get("description") or ""),
        version=version,
        source=source,
        triggers=_as_list("triggers"),
        required_tools=_as_list("required_tools"),
        optional_tools=_as_list("optional_tools"),
        risk=risk,
        can_delegate=can_delegate_raw in ("true", "yes", "1"),
        procedure=sections["procedure"],
        verification=sections["verification"],
        failure_policy=sections["failure_policy"],
        success_criteria=sections["success_criteria"],
        body=body.strip(),
        path=str(skill_md),
    )


def validate_skill(m: SkillManifest, known_tools: set[str]) -> list[dict]:
    """Static validation: schema + referenced tools exist (harness.md 52)."""
    issues: list[dict] = []
    if not m.description:
        issues.append({"code": "MISSING_DESCRIPTION", "message": f"{m.name} has no description"})
    if not m.procedure:
        issues.append(
            {"code": "MISSING_PROCEDURE", "message": f"{m.name} has no # Procedure section"}
        )
    for tool in m.required_tools:
        if known_tools and tool not in known_tools:
            issues.append(
                {
                    "code": "TOOL_NOT_FOUND",
                    "message": f"{m.name} requires unknown tool {tool!r}",
                }
            )
    return issues


__all__ = [
    "SKILL_FILE",
    "VALID_RISK",
    "VALID_SOURCES",
    "SkillError",
    "SkillManifest",
    "load_skill_manifest",
    "parse_frontmatter_lists",
    "validate_skill",
]
