"""AgentDefinition + the six built-in specialist agents (phase 6).

Per harness.md 111-115: the main agent is the coordinator; each subagent
gets a bounded objective, a tool allowlist, a permission profile, a budget,
a context scope, and a structured output contract. Read-only specialists
(Explore, Reviewer, Verifier) default to the read-only profile; Researcher
gets no local writes by default either.
"""

from __future__ import annotations

from dataclasses import dataclass, field

READ_ONLY = "read-only"
WORKSPACE = "workspace"
VALID_PROFILES = (READ_ONLY, WORKSPACE)

# Hard multi-agent defaults (harness.md 115): no recursive agent explosion.
MAX_CONCURRENT = 4
MAX_DEPTH = 2
MAX_TOTAL = 12


@dataclass(frozen=True, slots=True)
class AgentBudget:
    max_model_calls: int = 12
    max_tool_calls: int = 48
    max_wall_time_s: float = 600.0


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    name: str
    description: str
    objective: str  # what this agent is for; the spawn goal is bounded into it
    tool_allowlist: tuple[str, ...] = ()  # empty = all available tools
    profile: str = READ_ONLY  # "read-only" | "workspace"
    context_scope: str = "project"  # "project" | "none" (CHAT subagents)
    budget: AgentBudget = field(default_factory=AgentBudget)
    can_delegate: bool = False  # may itself spawn subagents (depth+1)
    provenance: str = "builtin"  # "builtin" | "project"

    def allows(self, tool_name: str) -> bool:
        if not self.tool_allowlist:
            return True
        for allowed in self.tool_allowlist:
            if allowed.endswith(".*"):
                if tool_name.startswith(allowed[:-1]):
                    return True
            elif tool_name == allowed:
                return True
        return False


BUILTIN_AGENTS: dict[str, AgentDefinition] = {
    "explore": AgentDefinition(
        name="explore",
        description="Read-only repository exploration: find file, symbol, and convention answers.",
        objective=(
            "Answer the scoped question using read-only repository tools. "
            "Return findings with file:line references; do not modify anything."
        ),
        tool_allowlist=(
            "fs.read",
            "fs.read_lines",
            "fs.list",
            "fs.glob",
            "fs.stat",
            "fs.diff",
            "search.*",
            "git.status",
            "git.log",
            "git.show",
            "git.branch",
            "context.*",
            "skills.list",
            "skills.show",
        ),
        profile=READ_ONLY,
    ),
    "reviewer": AgentDefinition(
        name="reviewer",
        description="Read-only code review of a defined diff range; severity-ranked findings.",
        objective=(
            "Review the scoped change for defects, security issues, and contract "
            "violations. Return severity-ranked findings with file:line evidence. "
            "Read-only: never modify code."
        ),
        tool_allowlist=(
            "git.status",
            "git.diff",
            "git.log",
            "git.show",
            "fs.read",
            "fs.read_lines",
            "fs.search_text",
            "fs.stat",
            "search.*",
            "skills.list",
            "skills.show",
        ),
        profile=READ_ONLY,
    ),
    # The debugger is NOT on the harness 112 read-only list: reproducing a defect
    # requires running commands, so it maps to the workspace profile. Its
    # isolation comes from the allowlist (no structured fs.write/fs.patch and no
    # worktree by default), not from prompt text.
    "debugger": AgentDefinition(
        name="debugger",
        description="Reproduce and root-cause a defect; may run commands but not edit code.",
        objective=(
            "Reproduce the described failure, isolate the boundary, and confirm the "
            "root cause with a minimal experiment. Report mechanism + repro. "
            "You may run read-only commands and scripts; do not edit project files."
        ),
        tool_allowlist=(
            "fs.read",
            "fs.read_lines",
            "fs.list",
            "fs.glob",
            "fs.stat",
            "fs.search_text",
            "search.*",
            "shell.exec",
            "process.*",
            "git.status",
            "git.log",
            "git.show",
        ),
        profile=WORKSPACE,
    ),
    "researcher": AgentDefinition(
        name="researcher",
        description=(
            "External/local technical research with cited evidence (no local writes by default)."
        ),
        objective=(
            "Answer the research question with cited evidence (web + local docs). "
            "Distinguish cited facts from inference. No local file writes."
        ),
        tool_allowlist=(
            "web.*",
            "http.request",
            "fs.read",
            "fs.read_lines",
            "fs.list",
            "fs.glob",
            "fs.search_text",
            "search.*",
        ),
        profile=READ_ONLY,
    ),
    "implementer": AgentDefinition(
        name="implementer",
        description="Scoped writer: implements one bounded task inside its isolated worktree.",
        objective=(
            "Implement the bounded objective following existing conventions, add "
            "tests, and leave the workspace with a verified, self-contained change. "
            "Stay inside the scope; report exactly what changed and how it was verified."
        ),
        tool_allowlist=(
            "fs.read",
            "fs.read_lines",
            "fs.write",
            "fs.patch",
            "fs.list",
            "fs.glob",
            "fs.stat",
            "fs.search_text",
            "fs.diff",
            "search.*",
            "shell.exec",
            "process.*",
            "git.status",
            "git.diff",
            "skills.list",
            "skills.show",
            "skills.activate",
            "skills.deactivate",
        ),
        profile=WORKSPACE,
    ),
    "verifier": AgentDefinition(
        name="verifier",
        description=(
            "Independent verification of a claimed result: re-runs tests/lints, inspects the diff."
        ),
        objective=(
            "Independently verify the claimed result: re-run the named validations, "
            "inspect the final diff, and decide DONE / IMPLEMENTED_UNVERIFIED / "
            "PARTIAL / BLOCKED / FAILED with the evidence table. Read-only."
        ),
        tool_allowlist=(
            "fs.read",
            "fs.read_lines",
            "fs.list",
            "fs.glob",
            "fs.stat",
            "fs.search_text",
            "fs.diff",
            "search.*",
            "shell.exec",
            "git.status",
            "git.diff",
            "git.log",
            # verify.evaluate only: plan/record are state.write, which the
            # read-only profile denies. The verdict contract is the summary
            # + parseable validation block (harness 112: Verifier = no writes).
            "verify.evaluate",
        ),
        profile=READ_ONLY,
    ),
}


def builtin_agents() -> dict[str, AgentDefinition]:
    return dict(BUILTIN_AGENTS)


__all__ = [
    "BUILTIN_AGENTS",
    "MAX_CONCURRENT",
    "MAX_DEPTH",
    "MAX_TOTAL",
    "READ_ONLY",
    "VALID_PROFILES",
    "WORKSPACE",
    "AgentBudget",
    "AgentDefinition",
    "builtin_agents",
]
