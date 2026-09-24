"""Slash commands: one catalog for the terminal and the desktop.

Kinds tell a client what to do with a command:

    ui     the client acts by itself (open a panel, new session…), no turn
    mode   switch PLAN/BUILD/REVIEW; with text (or a template) it starts a turn
    turn   a prepared prompt (the template), plus whatever the owner typed
    skill  pin a skill on the session, then start a turn with the text

Every enabled skill is also a command (`/pdf-tools extract page 3`), unless a
built-in already has its name: then `/skill <name>` reaches it. The Engine
expands `mode`, `turn` and `skill` commands itself (`expand_command`), so both
clients send the same thing and get the same behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

TEST_PROMPT = (
    "Run this project's test suite: detect the standard command from the project "
    "configuration (package.json, pyproject.toml, Makefile, ...), execute it, and "
    "report the outcome with failures summarized. Do not modify any code."
)
REVIEW_PROMPT = (
    "Review the uncommitted changes in this repository (git diff plus untracked "
    "files). Report concrete issues ordered by severity: bugs first, then "
    "security, then conventions. Reference file and line. Do not modify any code."
)
SKILL_PROMPT = "Use the {name} skill for this."

CLIENTS = ("cli", "desktop")


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    kind: str  # ui | mode | turn | skill
    description: str
    args: str = ""
    mode: str | None = None
    template: str | None = None
    clients: tuple[str, ...] = CLIENTS

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "description": self.description,
            "args": self.args,
            "mode": self.mode,
            "template": self.template,
            "source": "builtin",
        }


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec("help", "ui", "List the commands"),
    CommandSpec("new", "ui", "Start a new conversation"),
    CommandSpec("plan", "mode", "PLAN mode: read and plan, no changes", "[text]", mode="plan"),
    CommandSpec("build", "mode", "BUILD mode: implement and run", "[text]", mode="build"),
    CommandSpec(
        "review",
        "mode",
        "REVIEW mode; alone, it reviews the uncommitted changes",
        "[text]",
        mode="review",
        template=REVIEW_PROMPT,
    ),
    CommandSpec("test", "turn", "Run the project's test suite", "[text]", template=TEST_PROMPT),
    CommandSpec("skill", "skill", "Use a skill for this request", "<name> [text]"),
    CommandSpec("skills", "ui", "Open the skill library (terminal: list or search)", "[query]"),
    CommandSpec("compact", "ui", "Compact the context now"),
    CommandSpec("context", "ui", "Context usage and what fills it"),
    CommandSpec("model", "ui", "Change the model of this conversation", "[alias]"),
    CommandSpec("diff", "ui", "Changes in the workspace"),
    CommandSpec("tasks", "ui", "This project's task tree", "[id]"),
    CommandSpec("fork", "ui", "Continue this conversation in a copy", clients=("desktop",)),
    CommandSpec("rename", "ui", "Rename this conversation", "<title>", clients=("desktop",)),
    CommandSpec("usage", "ui", "Model and tool calls, tokens, cost", clients=("cli",)),
    CommandSpec("status", "ui", "Full runtime snapshot", clients=("cli",)),
    CommandSpec("provider", "ui", "List providers or switch", "[alias]", clients=("cli",)),
    CommandSpec("session", "ui", "Show this session", clients=("cli",)),
    CommandSpec("mode", "ui", "Session kind and mode", clients=("cli",)),
    CommandSpec("tokens", "ui", "Context usage estimate", clients=("cli",)),
    CommandSpec("tools", "ui", "Loaded tools with risk classes", clients=("cli",)),
    CommandSpec("agents", "ui", "Subagent state", clients=("cli",)),
    CommandSpec("permissions", "ui", "Policy profile and approval scopes", clients=("cli",)),
    CommandSpec("checkpoint", "ui", "Latest checkpoints for this project", clients=("cli",)),
    CommandSpec("undo", "ui", "Restore the latest checkpoint (asks)", clients=("cli",)),
    CommandSpec("trace", "ui", "Last session events", "[n]", clients=("cli",)),
    CommandSpec("resume", "ui", "Resume a session by id", "[id]", clients=("cli",)),
    CommandSpec(
        "attach",
        "ui",
        "Attach a file to the next turn (--ocr for image text)",
        "<path>",
        clients=("cli",),
    ),
    CommandSpec("exit", "ui", "Leave the session", clients=("cli",)),
)

_BY_NAME = {spec.name: spec for spec in COMMANDS}


class CommandError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code  # UNKNOWN_COMMAND | CLIENT_COMMAND | TEXT_REQUIRED | SKILL_NOT_FOUND
        self.message = message


@dataclass(frozen=True, slots=True)
class ExpandedCommand:
    message: str  # what the model receives
    mode: str | None = None  # switch the session to this mode first
    skill: str | None = None  # pin this skill first


def find_command(name: str) -> CommandSpec | None:
    return _BY_NAME.get(name)


def command_list(skills=None, project: Path | None = None, client: str = "desktop") -> list[dict]:
    """Built-ins for `client`, then one entry per enabled skill whose name is free."""
    rows = [spec.to_dict() for spec in COMMANDS if client in spec.clients]
    if skills is not None:
        for summary in skills.summaries(project):
            if summary["name"] in _BY_NAME:
                continue
            rows.append(
                {
                    "name": summary["name"],
                    "kind": "skill",
                    "description": summary["description"],
                    "args": "[text]",
                    "mode": None,
                    "template": None,
                    "source": "skill",
                }
            )
    return rows


def expand_command(name: str, text: str = "", skills=None, project: Path | None = None):
    """What a `mode`, `turn` or `skill` command sends to the model."""
    text = (text or "").strip()
    spec = _BY_NAME.get(name)
    if spec is None:
        if skills is None or name not in {row["name"] for row in skills.summaries(project)}:
            raise CommandError("UNKNOWN_COMMAND", f"unknown command /{name}")
        return ExpandedCommand(text or SKILL_PROMPT.format(name=name), skill=name)
    if spec.kind == "ui":
        raise CommandError("CLIENT_COMMAND", f"/{name} is handled by the client")
    if spec.kind == "mode":
        message = text or spec.template
        if not message:
            raise CommandError("TEXT_REQUIRED", f"/{name} needs text to start a turn")
        return ExpandedCommand(message, mode=spec.mode)
    if spec.kind == "turn":
        assert spec.template is not None
        return ExpandedCommand(f"{spec.template}\n\n{text}" if text else spec.template)
    # /skill <name> [text]
    skill, _, rest = text.partition(" ")
    if not skill:
        raise CommandError("TEXT_REQUIRED", "/skill needs a skill name")
    if skills is None or skill not in {row["name"] for row in skills.summaries(project)}:
        raise CommandError("SKILL_NOT_FOUND", f"no enabled skill named {skill!r}")
    rest = rest.strip()
    return ExpandedCommand(rest or SKILL_PROMPT.format(name=skill), skill=skill)


__all__ = [
    "COMMANDS",
    "REVIEW_PROMPT",
    "TEST_PROMPT",
    "CommandError",
    "CommandSpec",
    "ExpandedCommand",
    "command_list",
    "expand_command",
    "find_command",
]
