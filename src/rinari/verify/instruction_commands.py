"""Commands the project's RINARI.md lists under «Cómo probar» (plan 0.4.0 #81).

`/init` writes that section with the real commands it found. Verification
reads them before guessing from manifests: what the project says is how it is
tested wins, and the discovered commands follow.
"""

from __future__ import annotations

import re

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*#*\s*$")
_TEST_TITLES = re.compile(
    r"^(c[oó]mo\s+probar|how\s+to\s+test|testing|tests|pruebas|verificaci[oó]n|verification)\b",
    re.IGNORECASE,
)
_INLINE = re.compile(r"`([^`\n]+)`")
_FENCE = re.compile(r"^\s*(```|~~~)")

_KINDS = (
    ("lint", re.compile(r"\b(lint|eslint|ruff\s+check|flake8|clippy|golangci)\b", re.IGNORECASE)),
    ("typecheck", re.compile(r"\b(typecheck|tsc|mypy|pyright|check-types)\b", re.IGNORECASE)),
    ("build", re.compile(r"\b(build|compile)\b", re.IGNORECASE)),
)


def _kind(command: str) -> str:
    for kind, pattern in _KINDS:
        if pattern.search(command):
            return kind
    return "test"


def _section_commands(text: str) -> list[str]:
    commands: list[str] = []
    inside = False
    level = 0
    fenced = False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        heading = None if fenced else _HEADING.match(line)
        if heading:
            depth = len(line.lstrip()) - len(line.lstrip().lstrip("#"))
            if _TEST_TITLES.match(heading.group("title")):
                inside, level = True, depth
            elif inside and depth <= level:
                inside = False
            continue
        if not inside:
            continue
        if fenced:
            command = line.strip()
            if command and not command.startswith("#"):
                commands.append(command.removeprefix("$ ").strip())
            continue
        commands.extend(match.strip() for match in _INLINE.findall(line))
    return [command for command in commands if command]


def instruction_commands(instructions: list[str]) -> dict[str, list[str]]:
    """Commands per kind (test, lint, typecheck, build) from the «Cómo probar» sections."""
    found: dict[str, list[str]] = {"test": [], "lint": [], "typecheck": [], "build": []}
    for text in instructions:
        for command in _section_commands(text):
            bucket = found[_kind(command)]
            if command not in bucket:
                bucket.append(command)
    return found


def merge_commands(first: dict[str, list[str]], then: dict[str, list[str]]) -> dict[str, list[str]]:
    """`first` (the project's own word) ahead of `then` (discovered), without repeats."""
    merged: dict[str, list[str]] = {}
    for kind in {*first, *then}:
        seen: list[str] = []
        for command in [*first.get(kind, []), *then.get(kind, [])]:
            if command not in seen:
                seen.append(command)
        merged[kind] = seen
    return merged
