"""Verification planner: decide what to verify for a set of changed files.

Pure and deterministic (harness.md: planning is data about the repository,
never a model judgment). Inputs are repository facts: which files changed,
the repo's test mapping (from the repository index), discovered commands
(from repository state detection) and declared constraints. Output is a
`VerificationPlan` the agent can execute through `shell.exec` and then
record through `verify.record`.

Escalation rules (AGENTS.md 28 verification order):
  1. targeted: tests mapped to the changed source files (+ changed test files)
  2. adjacent:  test files in the same directory as a changed source file
  3. broader:   whole suite when shared/core/config/build files changed
Low risk = the narrower level is enough; high risk forces the broader set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"

# Files that affect more than the code paths that import them.
_SHARED_DIR_NAMES = {"utils", "common", "core", "lib", "internal", "app", "cli"}
_CONFIG_FILES = {
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "tox.ini",
    "requirements.txt",
    "uv.lock",
    "poetry.lock",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "tsconfig.json",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "Makefile",
    "Dockerfile",
}
_BUILD_FILES = {
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "poetry.lock",
    "uv.lock",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "tsconfig.json",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "Makefile",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
}

_TEST_FILE = re.compile(r"(^|/)(test_[^/]+\.py|[^/]+_test\.py|[^/]+\.test\.(js|ts|jsx|tsx))$")
_TEST_DIR = re.compile(r"(^|/)tests?($|/)")


def _is_test_file(rel_path: str) -> bool:
    return bool(_TEST_FILE.search(rel_path)) or bool(_TEST_DIR.search(rel_path))


def _name(rel_path: str) -> str:
    return rel_path.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _stem(rel_path: str) -> str:
    stem = _name(rel_path).rsplit(".", 1)[0]
    prefix = re.sub(r"^test_", "", stem)
    suffix = re.sub(r"(_test|\.test)$", "", prefix)
    return suffix or stem


def _heuristic_test_paths(rel_path: str) -> list[str]:
    """Conventional test locations for a source file, without an index."""
    stem = _stem(rel_path)
    base_dir = rel_path.replace("\\", "/").rsplit("/", 1)[0]
    if base_dir:
        return [
            f"{base_dir}/test_{stem}.py",
            f"{base_dir}/{stem}_test.py",
        ]
    return [f"test_{stem}.py", f"{stem}_test.py"]


_INSTRUCTION_HINT = re.compile(
    r"(test|lint|type ?check|build|pytest|cargo|npm|yarn|pnpm|pip|uv run|verify|ci\b)",
    re.IGNORECASE,
)


def _instruction_lines(instructions: list[str]) -> list[str]:
    """Verification-relevant lines from project instructions (bounded)."""
    lines: list[str] = []
    for chunk in instructions:
        for line in str(chunk).splitlines():
            line = line.strip()
            if not line or not _INSTRUCTION_HINT.search(line):
                continue
            lines.append(line[:120])
            if len(lines) >= 3:
                return lines
    return lines


@dataclass(frozen=True, slots=True)
class VerificationPlan:
    changed: tuple[str, ...]
    sources: tuple[str, ...]
    tests: tuple[str, ...]
    targeted: tuple[str, ...]
    adjacent: tuple[str, ...]
    broader: tuple[str, ...]
    test_commands: tuple[str, ...]
    lint_commands: tuple[str, ...]
    typecheck_commands: tuple[str, ...]
    build_commands: tuple[str, ...]
    risk: str
    reasons: tuple[str, ...]
    user_constraints: tuple[str, ...]
    project_instructions: tuple[str, ...]
    shared_files: tuple[str, ...] = field(default_factory=tuple)

    @property
    def empty(self) -> bool:
        return not self.changed

    def to_dict(self) -> dict:
        return {
            "changed": list(self.changed),
            "sources": list(self.sources),
            "tests": list(self.tests),
            "targeted": list(self.targeted),
            "adjacent": list(self.adjacent),
            "broader": list(self.broader),
            "test_commands": list(self.test_commands),
            "lint_commands": list(self.lint_commands),
            "typecheck_commands": list(self.typecheck_commands),
            "build_commands": list(self.build_commands),
            "risk": self.risk,
            "reasons": list(self.reasons),
            "user_constraints": list(self.user_constraints),
            "project_instructions": list(self.project_instructions),
            "shared_files": list(self.shared_files),
        }


def plan_verification(
    changed_files: list[str],
    *,
    test_map: dict[str, list[str]] | None = None,
    discovered: dict[str, list[str]] | None = None,
    user_constraints: list[str] | None = None,
    project_instructions: list[str] | None = None,
) -> VerificationPlan:
    changed = _normalize_changed(changed_files)
    constraints = [str(c).strip() for c in (user_constraints or []) if str(c).strip()]
    instructions = [str(i).strip() for i in (project_instructions or []) if str(i).strip()]
    reasons: list[str] = []
    for line in _instruction_lines(instructions):
        reasons.append(f"project instruction: {line}")

    sources: list[str] = []
    tests: list[str] = []
    shared: list[str] = []
    for rel in changed:
        if _is_test_file(rel):
            tests.append(rel)
        else:
            sources.append(rel)
        if _is_shared_config(rel):
            shared.append(rel)

    test_map = test_map or {}
    targeted: list[str] = []
    for rel in sources:
        mapped = test_map.get(rel.replace("\\", "/")) or []
        for path in mapped:
            if path not in targeted:
                targeted.append(path)
        if not mapped:
            reasons.append(f"no test mapping for {rel}; using conventions")
            for path in _heuristic_test_paths(rel):
                if path not in targeted:
                    targeted.append(path)
    for rel in tests:
        if rel not in targeted:
            targeted.append(rel)

    adjacent = _adjacent_tests(sources, changed)
    if adjacent:
        reasons.append("adjacent tests: same-directory test files of changed sources")

    broader: list[str] = []
    if shared:
        broader.append("suite")
        reasons.append(f"broader suite: shared/config files changed ({', '.join(shared[:4])})")
    if not sources and not tests and not shared:
        reasons.append("no changed files: nothing to verify")

    risk = _risk(sources, tests, shared, len(changed))
    if risk == RISK_HIGH and "suite" not in broader:
        broader.append("suite")
        reasons.append("broader suite: high-risk change set")

    discovered = discovered or {}

    def _cmds(kind: str) -> tuple[str, ...]:
        return tuple(str(c).strip() for c in (discovered.get(kind) or []) if str(c).strip())

    lint_commands = _cmds("lint")
    typecheck_commands = _cmds("typecheck")
    test_commands = _cmds("test")
    if not test_commands and targeted:
        test_commands = tuple(f"pytest {path}" for path in targeted[:8])
        reasons.append("default test command from targeted selection (pytest)")
    if not test_commands and sources:
        default = _default_test_command()
        if default:
            test_commands = (default,)
            reasons.append("no discovered test command; using repository default")
    if shared:
        build_commands = _cmds("build")
        if build_commands:
            reasons.append("build: dependency/build configuration changed")
    else:
        build_commands = ()

    return VerificationPlan(
        changed=tuple(changed),
        sources=tuple(sources),
        tests=tuple(tests),
        targeted=tuple(targeted),
        adjacent=tuple(adjacent),
        broader=tuple(broader),
        test_commands=test_commands,
        lint_commands=lint_commands,
        typecheck_commands=typecheck_commands,
        build_commands=build_commands,
        risk=risk,
        reasons=tuple(reasons),
        user_constraints=tuple(constraints),
        project_instructions=tuple(instructions),
        shared_files=tuple(shared),
    )


def _normalize_changed(changed_files: list[str]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for raw in changed_files or []:
        rel = str(raw or "").strip().replace("\\", "/")
        rel = rel.lstrip("./")
        if rel and rel not in seen:
            seen.add(rel)
            normalized.append(rel)
    return normalized[:200]


def _is_shared_config(rel: str) -> bool:
    name = _name(rel)
    if name in _CONFIG_FILES:
        return True
    parts = rel.split("/")
    if name not in _SHARED_DIR_NAMES:
        return False
    return len(parts) > 1 and parts[0] not in _SHARED_DIR_NAMES


def _is_build_file(rel: str) -> bool:
    return _name(rel) in _BUILD_FILES


def _adjacent_tests(sources: list[str], all_changed: list[str]) -> list[str]:
    adjacent: list[str] = []
    for rel in sources:
        base = rel.rsplit("/", 1)[0] if "/" in rel else ""
        for other in all_changed:
            if other == rel or _is_test_file(other):
                continue
            other_base = other.rsplit("/", 1)[0] if "/" in other else ""
            if base and other_base == base:
                if f"{base}/{_stem(other)}_test.py" not in adjacent:
                    adjacent.append(f"{base}/{_stem(other)}_test.py")
                if f"{base}/test_{_stem(other)}.py" not in adjacent:
                    adjacent.append(f"{base}/test_{_stem(other)}.py")
    return adjacent


def _default_test_command() -> str | None:
    return "pytest"


def _risk(
    sources: list[str],
    tests: list[str],
    shared: list[str],
    total_changed: int,
) -> str:
    if shared or any(_is_build_file(p) for p in shared):
        return RISK_HIGH
    dirs = {p.rsplit("/", 1)[0] for p in sources if "/" in p}
    if len(sources) >= 4 or (len(dirs) >= 3):
        return RISK_HIGH
    if len(sources) >= 2 or (sources and tests):
        return RISK_MEDIUM
    if total_changed <= 0:
        return RISK_LOW
    return RISK_LOW
