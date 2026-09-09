"""Config schema: typed views, validation, and dotted-key access.

Validation is strict by design: `config set` validates before persistence
(docs/commands.md section 25), and unknown keys are errors, never silently
dropped, so typos surface immediately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rinari.shared.errors import ConfigurationError

PROFILE_NAMES = ("safe", "read-only", "workspace", "full-access")
NETWORK_MODES = ("off", "ask", "allow")
SESSION_MODES = ("ask", "plan", "agent", "review", "full-access")


@dataclass(frozen=True, slots=True)
class AgentSettings:
    max_turns: int = 200
    max_tool_calls: int = 500
    max_runtime_minutes: int = 120


@dataclass(frozen=True, slots=True)
class RuntimeSafeguardsSettings:
    loop_detection: bool = True
    progress_detection: bool = True
    context_compaction: bool = True


@dataclass(frozen=True, slots=True)
class RuntimeEmergencySettings:
    max_runtime_minutes: int = 120
    max_model_calls: int = 500
    max_tool_calls: int = 5000
    max_subagent_calls: int = 100


@dataclass(frozen=True, slots=True)
class RuntimeCostSettings:
    max_turn_cost: float = 0.0


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    execution: str = "automatic"
    safeguards: RuntimeSafeguardsSettings = field(default_factory=RuntimeSafeguardsSettings)
    emergency: RuntimeEmergencySettings = field(default_factory=RuntimeEmergencySettings)
    cost: RuntimeCostSettings = field(default_factory=RuntimeCostSettings)


@dataclass(frozen=True, slots=True)
class ContextSettings:
    compact_at_percent: int = 80
    artifact_output_threshold_kb: int = 64


@dataclass(frozen=True, slots=True)
class AgentsSettings:
    enabled: bool = True
    max_concurrent: int = 4
    max_depth: int = 2
    max_total: int = 12


@dataclass(frozen=True, slots=True)
class PermissionsSettings:
    profile: str = "workspace"
    approval_policy: str = "on-request"


@dataclass(frozen=True, slots=True)
class NetworkSettings:
    mode: str = "ask"


@dataclass(frozen=True, slots=True)
class WorkspaceSettings:
    project_root_markers: tuple[str, ...] = (
        ".git",
        "package.json",
        "pyproject.toml",
        "go.mod",
    )


@dataclass(frozen=True, slots=True)
class InstructionsSettings:
    global_file: str = "~/.rinari/RINARI.md"
    project_names: tuple[str, ...] = ("RINARI.override.md", "RINARI.md")


@dataclass(frozen=True, slots=True)
class TelemetrySettings:
    local_traces: bool = True
    redact_secrets: bool = True


@dataclass(frozen=True, slots=True)
class Config:
    user_name: str = ""
    language: str = ""
    model: str = ""
    profile: str = "workspace"
    agent: AgentSettings = field(default_factory=AgentSettings)
    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)
    context: ContextSettings = field(default_factory=ContextSettings)
    agents: AgentsSettings = field(default_factory=AgentsSettings)
    permissions: PermissionsSettings = field(default_factory=PermissionsSettings)
    network: NetworkSettings = field(default_factory=NetworkSettings)
    workspace: WorkspaceSettings = field(default_factory=WorkspaceSettings)
    instructions: InstructionsSettings = field(default_factory=InstructionsSettings)
    telemetry: TelemetrySettings = field(default_factory=TelemetrySettings)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        _validate(data)
        runtime = _section(data, "runtime", "runtime")
        safeguards = _nested_table(runtime, "safeguards", "runtime.safeguards")
        emergency = _nested_table(runtime, "emergency", "runtime.emergency")
        cost = _nested_table(runtime, "cost", "runtime.cost")
        return cls(
            user_name=_str(data, "user_name", "str"),
            language=_str(data, "language", "str"),
            model=_str(data, "model", "str"),
            # The selected profile may be a user-defined profile file
            # (~/.rinari/profiles/<name>.toml); existence is validated during
            # profile resolution, not here.
            profile=_str(data, "profile", "str", minimum=1),
            agent=AgentSettings(
                max_turns=_int(data, "agent", "max_turns", 1, 10_000),
                max_tool_calls=_int(data, "agent", "max_tool_calls", 1, 100_000),
                max_runtime_minutes=_int(data, "agent", "max_runtime_minutes", 1, 1440),
            ),
            runtime=RuntimeSettings(
                execution=_plain_enum(runtime, "execution", "runtime.execution", ("automatic",)),
                safeguards=RuntimeSafeguardsSettings(
                    loop_detection=_plain_bool(
                        safeguards, "loop_detection", "runtime.safeguards.loop_detection"
                    ),
                    progress_detection=_plain_bool(
                        safeguards,
                        "progress_detection",
                        "runtime.safeguards.progress_detection",
                    ),
                    context_compaction=_plain_bool(
                        safeguards,
                        "context_compaction",
                        "runtime.safeguards.context_compaction",
                    ),
                ),
                emergency=RuntimeEmergencySettings(
                    max_runtime_minutes=_plain_int(
                        emergency,
                        "max_runtime_minutes",
                        "runtime.emergency.max_runtime_minutes",
                        1,
                        1440,
                    ),
                    max_model_calls=_plain_int(
                        emergency,
                        "max_model_calls",
                        "runtime.emergency.max_model_calls",
                        1,
                        100_000,
                    ),
                    max_tool_calls=_plain_int(
                        emergency,
                        "max_tool_calls",
                        "runtime.emergency.max_tool_calls",
                        1,
                        1_000_000,
                    ),
                    max_subagent_calls=_plain_int(
                        emergency,
                        "max_subagent_calls",
                        "runtime.emergency.max_subagent_calls",
                        1,
                        10_000,
                    ),
                ),
                cost=RuntimeCostSettings(
                    max_turn_cost=_plain_number(
                        cost, "max_turn_cost", "runtime.cost.max_turn_cost", 0.0
                    )
                ),
            ),
            context=ContextSettings(
                compact_at_percent=_int(data, "context", "compact_at_percent", 1, 100),
                artifact_output_threshold_kb=_int(
                    data, "context", "artifact_output_threshold_kb", 1, 1024 * 1024
                ),
            ),
            agents=AgentsSettings(
                enabled=_bool(data, "agents", "enabled"),
                max_concurrent=_int(data, "agents", "max_concurrent", 1, 64),
                max_depth=_int(data, "agents", "max_depth", 0, 8),
                max_total=_int(data, "agents", "max_total", 1, 128),
            ),
            permissions=PermissionsSettings(
                profile=_enum(data, "permissions.profile", PROFILE_NAMES, "str"),
                approval_policy=_str(data, "permissions.approval_policy", "str", minimum=1),
            ),
            network=NetworkSettings(mode=_enum(data, "network.mode", NETWORK_MODES, "str")),
            workspace=WorkspaceSettings(
                project_root_markers=_str_list(data, "workspace", "project_root_markers"),
            ),
            instructions=InstructionsSettings(
                global_file=_str(data, "instructions.global_file", "str"),
                project_names=_str_list(data, "instructions", "project_names"),
            ),
            telemetry=TelemetrySettings(
                local_traces=_bool(data, "telemetry", "local_traces"),
                redact_secrets=_bool(data, "telemetry", "redact_secrets"),
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_name": self.user_name,
            "language": self.language,
            "model": self.model,
            "profile": self.profile,
            "agent": {
                "max_turns": self.agent.max_turns,
                "max_tool_calls": self.agent.max_tool_calls,
                "max_runtime_minutes": self.agent.max_runtime_minutes,
            },
            "runtime": {
                "execution": self.runtime.execution,
                "safeguards": {
                    "loop_detection": self.runtime.safeguards.loop_detection,
                    "progress_detection": self.runtime.safeguards.progress_detection,
                    "context_compaction": self.runtime.safeguards.context_compaction,
                },
                "emergency": {
                    "max_runtime_minutes": self.runtime.emergency.max_runtime_minutes,
                    "max_model_calls": self.runtime.emergency.max_model_calls,
                    "max_tool_calls": self.runtime.emergency.max_tool_calls,
                    "max_subagent_calls": self.runtime.emergency.max_subagent_calls,
                },
                "cost": {"max_turn_cost": self.runtime.cost.max_turn_cost},
            },
            "context": {
                "compact_at_percent": self.context.compact_at_percent,
                "artifact_output_threshold_kb": self.context.artifact_output_threshold_kb,
            },
            "agents": {
                "enabled": self.agents.enabled,
                "max_concurrent": self.agents.max_concurrent,
                "max_depth": self.agents.max_depth,
                "max_total": self.agents.max_total,
            },
            "permissions": {
                "profile": self.permissions.profile,
                "approval_policy": self.permissions.approval_policy,
            },
            "network": {"mode": self.network.mode},
            "workspace": {"project_root_markers": list(self.workspace.project_root_markers)},
            "instructions": {
                "global_file": self.instructions.global_file,
                "project_names": list(self.instructions.project_names),
            },
            "telemetry": {
                "local_traces": self.telemetry.local_traces,
                "redact_secrets": self.telemetry.redact_secrets,
            },
        }

    def value(self, dotted: str) -> Any | None:
        return _walk(self.to_dict(), dotted)

    def sections(self) -> tuple[str, ...]:
        return (
            "agent",
            "runtime",
            "context",
            "agents",
            "permissions",
            "network",
            "workspace",
            "instructions",
            "telemetry",
        )


def _section_keys(section: str) -> list[str]:
    return {
        "agent": ["max_turns", "max_tool_calls", "max_runtime_minutes"],
        "runtime": ["execution", "safeguards", "emergency", "cost"],
        "context": ["compact_at_percent", "artifact_output_threshold_kb"],
        "agents": ["enabled", "max_concurrent", "max_depth", "max_total"],
        "permissions": ["profile", "approval_policy"],
        "network": ["mode"],
        "workspace": ["project_root_markers"],
        "instructions": ["global_file", "project_names"],
        "telemetry": ["local_traces", "redact_secrets"],
    }[section]


def leaf_keys() -> list[str]:
    keys = ["user_name", "language", "model", "profile"]
    for section in Config().sections():
        if section == "runtime":
            keys.extend(
                [
                    "runtime.execution",
                    "runtime.safeguards.loop_detection",
                    "runtime.safeguards.progress_detection",
                    "runtime.safeguards.context_compaction",
                    "runtime.emergency.max_runtime_minutes",
                    "runtime.emergency.max_model_calls",
                    "runtime.emergency.max_tool_calls",
                    "runtime.emergency.max_subagent_calls",
                    "runtime.cost.max_turn_cost",
                ]
            )
            continue
        keys.extend(f"{section}.{k}" for k in _section_keys(section))
    return keys


def key_type(dotted: str) -> str:
    """Declared type of a dotted key for `config set` value parsing."""
    parts = dotted.split(".")
    if len(parts) == 1:
        if parts[0] in ("user_name", "language", "model"):
            return "str"
        if parts[0] == "profile":
            return "str"
        if parts[0] in Config().sections():
            raise ConfigurationError(
                f"'{dotted}' is a section, not a value", hint="Use a dotted key."
            )
        raise ConfigurationError(f"Unknown config key: {dotted}")
    if len(parts) == 3 and parts[0] == "runtime":
        nested_types = {
            "runtime.safeguards.loop_detection": "bool",
            "runtime.safeguards.progress_detection": "bool",
            "runtime.safeguards.context_compaction": "bool",
            "runtime.emergency.max_runtime_minutes": "int",
            "runtime.emergency.max_model_calls": "int",
            "runtime.emergency.max_tool_calls": "int",
            "runtime.emergency.max_subagent_calls": "int",
            "runtime.cost.max_turn_cost": "float",
        }
        if dotted not in nested_types:
            raise ConfigurationError(f"Unknown config key: {dotted}")
        return nested_types[dotted]
    if len(parts) != 2:
        raise ConfigurationError(f"Invalid config key: {dotted}")
    section, key = parts
    if section not in Config().sections():
        raise ConfigurationError(f"Unknown config section: {section}")
    if key not in _section_keys(section):
        raise ConfigurationError(f"Unknown config key: {dotted}")
    types = {
        ("agent", "max_turns"): "int",
        ("agent", "max_tool_calls"): "int",
        ("agent", "max_runtime_minutes"): "int",
        ("runtime", "execution"): "str",
        ("context", "compact_at_percent"): "int",
        ("context", "artifact_output_threshold_kb"): "int",
        ("agents", "enabled"): "bool",
        ("agents", "max_concurrent"): "int",
        ("agents", "max_depth"): "int",
        ("agents", "max_total"): "int",
        ("permissions", "profile"): "str",
        ("permissions", "approval_policy"): "str",
        ("network", "mode"): "str",
        ("workspace", "project_root_markers"): "list[str]",
        ("instructions", "global_file"): "str",
        ("instructions", "project_names"): "list[str]",
        ("telemetry", "local_traces"): "bool",
        ("telemetry", "redact_secrets"): "bool",
    }
    return types[(section, key)]


def _walk(data: dict[str, Any], dotted: str) -> Any | None:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _section(data: dict[str, Any], name: str, dotted: str) -> dict[str, Any]:
    value = data.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        _fail(dotted, f"must be a table, got {type(value).__name__}")
    return value


def _nested_table(data: dict[str, Any], name: str, dotted: str) -> dict[str, Any]:
    value = data.get(name)
    if not isinstance(value, dict):
        _fail(dotted, "must be a table")
    return value


def _plain_bool(data: dict[str, Any], key: str, dotted: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        _fail(dotted, "must be a boolean")
    return value


def _plain_int(data: dict[str, Any], key: str, dotted: str, minimum: int, maximum: int) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        _fail(dotted, "must be an integer")
    if not minimum <= value <= maximum:
        _fail(dotted, f"must be between {minimum} and {maximum}, got {value}")
    return value


def _plain_number(data: dict[str, Any], key: str, dotted: str, minimum: float) -> float:
    value = data.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        _fail(dotted, "must be a number")
    if float(value) < minimum:
        _fail(dotted, f"must be at least {minimum}, got {value}")
    return float(value)


def _plain_enum(data: dict[str, Any], key: str, dotted: str, allowed: tuple[str, ...]) -> str:
    value = data.get(key)
    if not isinstance(value, str) or value not in allowed:
        _fail(dotted, f"must be one of {', '.join(allowed)}")
    return value


def _str(data: dict, dotted: str, expected: str, minimum: int = 0) -> str:
    parts = dotted.split(".")
    section = _section(data, parts[0], parts[0]) if len(parts) > 1 else data
    name = parts[-1]
    if name not in section:
        _fail(dotted, "is required")
    value = section[name]
    if not isinstance(value, str) or isinstance(value, bool):
        _fail(dotted, f"must be a string, got {type(value).__name__}")
    if len(value) < minimum:
        _fail(dotted, "must not be empty")
    return value


def _int(data: dict, section: str, key: str, minimum: int, maximum: int) -> int:
    dotted = f"{section}.{key}"
    table = _section(data, section, dotted)
    if key not in table:
        _fail(dotted, "is required")
    value = table[key]
    if not isinstance(value, int) or isinstance(value, bool):
        _fail(dotted, f"must be an integer, got {type(value).__name__}")
    if not minimum <= value <= maximum:
        _fail(dotted, f"must be between {minimum} and {maximum}, got {value}")
    return value


def _bool(data: dict, section: str, key: str) -> bool:
    dotted = f"{section}.{key}"
    table = _section(data, section, dotted)
    if key not in table:
        _fail(dotted, "is required")
    value = table[key]
    if not isinstance(value, bool):
        _fail(dotted, f"must be a boolean, got {type(value).__name__}")
    return value


def _enum(data: dict, dotted: str, allowed: tuple[str, ...], expected: str) -> str:
    value = _str(data, dotted, expected)
    if value not in allowed:
        _fail(dotted, f"must be one of {', '.join(allowed)}, got {value!r}")
    return value


def _str_list(data: dict, section: str, key: str) -> tuple[str, ...]:
    dotted = f"{section}.{key}"
    table = _section(data, section, dotted)
    if key not in table:
        _fail(dotted, "is required")
    value = table[key]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value) or not value:
        _fail(dotted, "must be a non-empty list of non-empty strings")
    return tuple(value)


def _validate(data: dict[str, Any]) -> None:
    unknown = set(data) - {
        "user_name",
        "language",
        "model",
        "profile",
        *Config().sections(),
    }
    if unknown:
        _fail(", ".join(sorted(unknown)), "unknown key(s)")
    for section in Config().sections():
        if section in data and isinstance(data[section], dict):
            allowed = set(_section_keys(section))
            stray = set(data[section]) - allowed
            if stray:
                _fail(f"{section}.", f"unknown key(s): {', '.join(sorted(stray))}")
    runtime = data.get("runtime")
    if isinstance(runtime, dict):
        nested_allowed = {
            "safeguards": {"loop_detection", "progress_detection", "context_compaction"},
            "emergency": {
                "max_runtime_minutes",
                "max_model_calls",
                "max_tool_calls",
                "max_subagent_calls",
            },
            "cost": {"max_turn_cost"},
        }
        for table, allowed in nested_allowed.items():
            value = runtime.get(table)
            if isinstance(value, dict):
                stray = set(value) - allowed
                if stray:
                    _fail(
                        f"runtime.{table}.",
                        f"unknown key(s): {', '.join(sorted(stray))}",
                    )


def _fail(where: str, problem: str) -> None:
    raise ConfigurationError(f"Config key {where} {problem}")
