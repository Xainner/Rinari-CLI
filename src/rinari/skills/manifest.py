"""Skill manifest: schema, parsing and validation (phase 6).

A skill is a directory `<root>/<name>/SKILL.md`. The YAML frontmatter
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

Frontmatter uses the shared YAML reader with legacy fallback. A `required_tools` entry is
a *capability request*, never a grant: the runtime still evaluates every
tool call against the policy engine (harness.md 51).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from rinari.skills.catalog import skill_version
from rinari.skills.frontmatter import as_list, as_str_map, as_text, read_frontmatter

SKILL_FILE = "SKILL.md"
VALID_RISK = ("low", "medium", "high")
VALID_SOURCES = ("packaged", "user", "project")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
# Standard skills put a free-form version in `metadata.version` ("1.0").
_LOOSE_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,31}$")
# Frontmatter keys only Rinari's own format uses: any of them (or a
# `# Procedure` section) marks a skill as Rinari-format.
_RINARI_KEYS = frozenset({"required_tools", "optional_tools", "risk", "triggers", "can_delegate"})
SKILL_FORMATS = ("rinari", "standard")
# The standard caps the description; the catalog shows it on every turn.
DESCRIPTION_MAX_CHARS = 1024

# Headings a model or a person writes for the same sections. A learned skill
# failed validation for writing "## Procedimiento" instead of "# Procedure".
_SECTION_ALIASES = {
    "procedure": "procedure",
    "procedimiento": "procedure",
    "pasos": "procedure",
    "steps": "procedure",
    "instructions": "procedure",
    "instrucciones": "procedure",
    "verification": "verification",
    "verificacion": "verification",
    "verify": "verification",
    "failure handling": "failure_policy",
    "manejo de fallos": "failure_policy",
    "manejo de errores": "failure_policy",
    "troubleshooting": "failure_policy",
    "success criteria": "success_criteria",
    "criterios de exito": "success_criteria",
}
# These are also ordinary subheadings. Only peer/parent headings (or a
# heading outside an active section) may use them as section boundaries.
_GENERIC_SECTION_TITLES = frozenset(
    {"steps", "instructions", "pasos", "instrucciones", "verify", "troubleshooting"}
)


def _section_title(title: str) -> str:
    """Normalize accents, parenthetical hints and trailing punctuation."""
    import unicodedata

    plain = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    plain = re.sub(r"\(.*?\)", "", plain).strip().rstrip(":.").strip().lower()
    return plain


class SkillError(Exception):
    """Structured skill failure (code + message)."""

    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        # SKILL_INVALID | NAME_INVALID | VERSION_INVALID | RISK_INVALID
        # | SKILL_NOT_FOUND | ALREADY_EXISTS | REMOVE_FAILED
        # | TOOL_NOT_FOUND | TRUST_REQUIRED | LOAD_FAILED
        # | SOURCE_INVALID | SOURCE_TOO_LARGE | DOWNLOAD_FAILED | SKILL_AMBIGUOUS
        # | REVIEW_REQUIRED | NOT_UPDATABLE | LOCALLY_MODIFIED | NOT_EDITABLE
        self.code = code
        self.message = message
        # Structured context for clients: review findings, candidate names…
        self.details = details or {}


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
    # "rinari" (required_tools/risk/# Procedure) or "standard" (Agent Skills:
    # Claude, Codex…). A standard skill's whole body is its procedure.
    format: str = "rinari"
    license: str = ""
    compatibility: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    # Standard `allowed-tools`: a hint of what the skill expects to use. Never
    # a grant (harness.md 51); tool names are the original product's.
    allowed_tools: tuple[str, ...] = ()
    procedure_present: bool = False

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
    """Extract named sections without losing their nested Markdown.

    Canonical section names are boundaries at any level (legacy skills mix
    levels). Nested aliases of the active section, and generic titles such as
    Steps/Verify, remain content without changing its level. Other headings
    only end a section at its own level or above. Fenced and indented code is literal.
    Absent keys distinguish a missing section from an explicitly empty one.
    """
    chunks: dict[str, list[str]] = {}
    active: str | None = None
    level = 0
    fence: str | None = None
    for line in body.splitlines():
        if fence is not None:
            if active is not None:
                chunks[active].append(line)
            if re.fullmatch(
                r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line
            ):
                fence = None
            continue
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if opening and not (opening[1][0] == "`" and "`" in opening[2]):
            fence = opening[1]
            if active is not None:
                chunks[active].append(line)
            continue
        header = re.match(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?)|[ \t]*)$", line)
        if header:
            title = _section_title(re.sub(r"[ \t]+#+[ \t]*$", "", header[2] or ""))
            key = _SECTION_ALIASES.get(title)
            if (
                active is not None
                and len(header[1]) > level
                and (key == active or title in _GENERIC_SECTION_TITLES)
            ):
                key = None
            if key is not None or len(header[1]) <= level:
                active, level = key, len(header[1])
                if active is not None:
                    chunks.setdefault(active, [])
                continue
        if active is not None:
            chunks[active].append(line)
    return {key: "\n".join(lines_).strip("\r\n") for key, lines_ in chunks.items()}


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

    fields, body = read_frontmatter(text)
    sections = _split_sections(body)
    skill_format = "rinari" if _RINARI_KEYS & set(fields) or "procedure" in sections else "standard"
    metadata = as_str_map(fields.get("metadata"))

    name = as_text(fields.get("name")) or skill_md.parent.name
    if not _NAME_RE.match(name):
        raise SkillError("NAME_INVALID", f"skill name invalid: {name!r}")
    version = as_text(fields.get("version"))
    if not version and skill_format == "standard":
        declared = metadata.get("version", "")
        version = declared if _LOOSE_VERSION_RE.match(declared) else ""
    if not version:
        version = skill_version(text)
    elif (
        skill_format == "rinari"
        and not _VERSION_RE.match(version)
        and not version.startswith("sha:")
    ):
        raise SkillError("VERSION_INVALID", f"skill version invalid: {version!r}")
    elif skill_format == "standard" and not _LOOSE_VERSION_RE.match(version):
        version = skill_version(text)
    risk = as_text(fields.get("risk")) or "low"
    if risk not in VALID_RISK:
        raise SkillError("RISK_INVALID", f"skill risk invalid: {risk!r}")

    can_delegate_raw = as_text(fields.get("can_delegate")).lower() or "false"
    return SkillManifest(
        name=name,
        description=as_text(fields.get("description")),
        version=version,
        source=source,
        triggers=as_list(fields.get("triggers")),
        required_tools=as_list(fields.get("required_tools")),
        optional_tools=as_list(fields.get("optional_tools")),
        risk=risk,
        can_delegate=can_delegate_raw in ("true", "yes", "1"),
        # A standard skill has no sections: the whole body is the procedure.
        procedure=sections.get("procedure", "")
        or (body.strip() if skill_format == "standard" else ""),
        verification=sections.get("verification", ""),
        failure_policy=sections.get("failure_policy", ""),
        success_criteria=sections.get("success_criteria", ""),
        body=body.strip(),
        path=str(skill_md),
        format=skill_format,
        license=as_text(fields.get("license")),
        compatibility=as_text(fields.get("compatibility")),
        metadata=metadata,
        allowed_tools=as_list(fields.get("allowed-tools")),
        procedure_present="procedure" in sections,
    )


def validate_skill(m: SkillManifest, known_tools: set[str]) -> list[dict]:
    """Static validation: schema + referenced tools exist (harness.md 52)."""
    issues: list[dict] = []
    if not m.description:
        issues.append(
            {
                "code": "MISSING_DESCRIPTION",
                "field": "description",
                "message": f"{m.name} has no description",
            }
        )
    elif len(m.description) > DESCRIPTION_MAX_CHARS:
        issues.append(
            {
                "code": "DESCRIPTION_TOO_LONG",
                "field": "description",
                "message": f"{m.name} description exceeds {DESCRIPTION_MAX_CHARS} characters",
            }
        )
    if m.path and Path(m.path).parent.name != m.name:
        issues.append(
            {
                "code": "NAME_MISMATCH",
                "field": "name",
                "message": f"{m.name} lives in folder {Path(m.path).parent.name!r}",
            }
        )
    if not m.procedure.strip():
        # Rinari format names the section; a standard skill just needs a body.
        issues.append(
            {
                "code": "EMPTY_PROCEDURE" if m.procedure_present else "MISSING_PROCEDURE",
                "field": "procedure",
                "message": (
                    f"{m.name} has an empty Procedure section; add the steps under that heading"
                    if m.procedure_present
                    else f"{m.name} has no procedure: after the frontmatter, add a line "
                    "'# Procedure' followed by the steps"
                ),
            }
            if m.format == "rinari"
            else {
                "code": "MISSING_BODY",
                "field": "body",
                "message": f"{m.name} has no instructions",
            }
        )
    for tool in m.required_tools:
        # MCP, plugin and OpenAPI tools exist only once their source connects.
        dynamic = tool.startswith(("mcp.", "mcp_", "plugin.", "openapi."))
        if known_tools and tool not in known_tools and not dynamic:
            # Provider transports may spell dots as underscores. Suggest only
            # an unambiguous registered name; never silently grant/alias a tool.
            matches = sorted(name for name in known_tools if name.replace(".", "_") == tool)
            issues.append(
                {
                    "code": "TOOL_NOT_FOUND",
                    "field": "required_tools",
                    "tool": tool,
                    "message": f"{m.name} requires unknown tool {tool!r}",
                    **({"suggestion": matches[0]} if len(matches) == 1 else {}),
                }
            )
    return issues


__all__ = [
    "DESCRIPTION_MAX_CHARS",
    "SKILL_FILE",
    "SKILL_FORMATS",
    "VALID_RISK",
    "VALID_SOURCES",
    "SkillError",
    "SkillManifest",
    "load_skill_manifest",
    "parse_frontmatter_lists",
    "validate_skill",
]
