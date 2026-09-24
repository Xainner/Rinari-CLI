"""SKILL.md frontmatter: real YAML, so standard skills load as written.

Agent Skills (agentskills.io, used by Claude, Codex and others) write the
frontmatter as YAML: multi-line `description: >`, a nested `metadata:` map,
`allowed-tools` as a string. The old line parser turned a folded description
into the literal ">" and dropped `metadata`. `yaml.safe_load` reads them as
intended; a block that is not valid YAML (hand-written Rinari skills tolerate
`key: value: more`) falls back to that line parser, so nothing that loaded
before stops loading.
"""

from __future__ import annotations

import yaml

# A frontmatter is metadata, not content: a huge block is a mistake or abuse.
FRONTMATTER_MAX_CHARS = 64 * 1024


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """(raw frontmatter block or None, body after the closing `---`)."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None, text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "".join(lines[1:index]), "".join(lines[index + 1 :]).lstrip("\n")
    return None, text


def read_frontmatter(text: str) -> tuple[dict, str]:
    """Fields (YAML, or the legacy line parser as fallback) and the body."""
    from rinari.skills.manifest import parse_frontmatter_lists

    block, body = split_frontmatter(text)
    if block is None:
        return {}, body
    if len(block) > FRONTMATTER_MAX_CHARS:
        return {}, body
    try:
        loaded = yaml.safe_load(block)
    except yaml.YAMLError:
        loaded = None
    if isinstance(loaded, dict):
        return {str(key): value for key, value in loaded.items()}, body
    return dict(parse_frontmatter_lists(text)), body


def as_text(value) -> str:
    """A scalar field as one clean string (folded YAML keeps its newlines)."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(as_text(item) for item in value)
    return " ".join(str(value).split())


def as_list(value) -> tuple[str, ...]:
    """A list field: YAML list, comma string, or space string (allowed-tools)."""
    if value is None or value == "":
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    text = str(value)
    separator = "," if "," in text else None
    return tuple(part.strip() for part in text.split(separator) if part.strip())


def as_str_map(value) -> dict[str, str]:
    """`metadata:` as the standard defines it: string keys to string values."""
    if not isinstance(value, dict):
        return {}
    return {str(key): as_text(item) for key, item in value.items() if item is not None}


__all__ = [
    "FRONTMATTER_MAX_CHARS",
    "as_list",
    "as_str_map",
    "as_text",
    "read_frontmatter",
    "split_frontmatter",
]
