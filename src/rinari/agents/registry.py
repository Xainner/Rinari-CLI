"""AgentRegistry: built-ins + trust-gated project agent definitions (phase 6).

Project agents are markdown files with the same frontmatter mechanics as
skills: `<root>/.rinari/agents/<name>.md`. They only load for a trusted
project (same rule as project skills/hooks). Omitted tools/profile inherit
the parent session; explicit restrictions narrow that scope at execution.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from rinari.agents.definition import (
    INHERIT,
    READ_ONLY,
    VALID_PROFILES,
    AgentBudget,
    AgentDefinition,
    builtin_agents,
)
from rinari.skills.manifest import SkillError, parse_frontmatter_lists

AGENT_SUFFIX = ".md"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class AgentRegistry:
    def __init__(self, trust=None) -> None:
        self._trust = trust

    def project_dir(self, root: Path | None) -> Path | None:
        if root is None:
            return None
        d = root / ".rinari" / "agents"
        return d if d.is_dir() else None

    def list(self, project: Path | None = None) -> dict[str, AgentDefinition]:
        agents = builtin_agents()
        base = self.project_dir(project)
        trusted = base is not None and self._trust is not None and self._trust.is_trusted(project)
        if base is None or not trusted:
            return agents
        for entry in sorted(base.iterdir(), key=lambda p: p.name):
            if not entry.is_file() or not entry.name.endswith(AGENT_SUFFIX):
                continue
            try:
                m = self._parse_project_agent(entry)
            except SkillError:
                continue  # invalid project agents never break the registry
            agents[m.name] = m
        return agents

    def get(self, name: str, project: Path | None = None) -> AgentDefinition | None:
        return self.list(project).get(name)

    def validate(self, name: str | None = None, project: Path | None = None) -> list[dict]:
        issues: list[dict] = []
        agents = self.list(project)
        for key, agent in sorted(agents.items()):
            if name and key != name:
                continue
            problems = self._check(agent)
            issues.append({"name": key, "ok": not problems, "issues": problems})
        return issues

    def _check(self, agent: AgentDefinition) -> list[dict]:
        problems: list[dict] = []
        if agent.profile not in VALID_PROFILES:
            problems.append(
                {"code": "PROFILE_INVALID", "message": f"{agent.name}: profile {agent.profile!r}"}
            )
        if agent.profile == READ_ONLY and any(
            t.startswith("fs.write") or t.startswith("fs.patch") for t in agent.tool_allowlist
        ):
            problems.append(
                {
                    "code": "WRITE_TOOL_IN_READ_ONLY",
                    "message": f"{agent.name}: read-only agent lists write tools",
                }
            )
        return problems

    def _parse_project_agent(self, path: Path) -> AgentDefinition:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillError("LOAD_FAILED", f"cannot read {path}: {exc}") from exc
        fields = parse_frontmatter_lists(text)
        name = str(fields.get("name") or path.stem)
        if not _NAME_RE.match(name):
            raise SkillError("NAME_INVALID", f"agent name invalid: {name!r}")
        tools: tuple[str, ...] = ()
        raw_tools = fields.get("tools")
        if isinstance(raw_tools, list):
            tools = tuple(str(t) for t in raw_tools if t)
        elif isinstance(raw_tools, str):
            tools = tuple(t.strip() for t in raw_tools.split(",") if t.strip())
        profile = str(fields.get("profile") or INHERIT)
        if profile not in VALID_PROFILES:
            raise SkillError(
                "PROFILE_INVALID", f"agent profile invalid in {path.name}: {profile!r}"
            )
        budget = AgentBudget()
        try:
            if str(fields.get("max_model_calls") or "").strip():
                budget = replace(budget, max_model_calls=int(fields["max_model_calls"]))
            if str(fields.get("max_tool_calls") or "").strip():
                budget = replace(budget, max_tool_calls=int(fields["max_tool_calls"]))
            if str(fields.get("max_wall_time_s") or "").strip():
                budget = replace(budget, max_wall_time_s=float(fields["max_wall_time_s"]))
        except ValueError as exc:
            raise SkillError("NAME_INVALID", f"agent budget invalid in {path.name}") from exc
        context_scope = str(fields.get("context") or "project")
        if context_scope not in ("project", "none"):
            raise SkillError("NAME_INVALID", f"agent context scope invalid in {path.name}")
        can_delegate = str(fields.get("can_delegate") or "false").lower() in ("true", "yes", "1")
        return AgentDefinition(
            name=name,
            description=str(fields.get("description") or ""),
            objective=str(fields.get("objective") or ""),
            tool_allowlist=tools,
            profile=profile,
            context_scope=context_scope,
            budget=budget,
            can_delegate=can_delegate,
            provenance="project",
        )


__all__ = ["AGENT_SUFFIX", "AgentRegistry"]
