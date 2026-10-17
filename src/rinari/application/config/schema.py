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
NETWORK_MODES = ("off", "auto", "ask", "allow")
SESSION_MODES = ("ask", "plan", "agent", "review", "full-access")


@dataclass(frozen=True, slots=True)
class RuntimeSafeguardsSettings:
    loop_detection: bool = True
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
class RuntimeToolsSettings:
    max_concurrency: int = 4
    observation_bytes: int = 65536
    round_observation_bytes: int = 262144


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    tools: RuntimeToolsSettings = field(default_factory=RuntimeToolsSettings)
    safeguards: RuntimeSafeguardsSettings = field(default_factory=RuntimeSafeguardsSettings)
    emergency: RuntimeEmergencySettings = field(default_factory=RuntimeEmergencySettings)
    cost: RuntimeCostSettings = field(default_factory=RuntimeCostSettings)


@dataclass(frozen=True, slots=True)
class ContextSettings:
    compact_at_percent: int = 80
    artifact_output_threshold_kb: int = 16


@dataclass(frozen=True, slots=True)
class AgentsSettings:
    enabled: bool = True
    max_concurrent: int = 4
    max_depth: int = 2
    max_total: int = 12


@dataclass(frozen=True, slots=True)
class PermissionsSettings:
    profile: str = "workspace"
    # Full access asks before sending data to a new destination after reading
    # external content (plan-permisos-v4, off by default).
    ask_new_destinations: bool = False


@dataclass(frozen=True, slots=True)
class NetworkSettings:
    mode: str = "auto"


@dataclass(frozen=True, slots=True)
class Config:
    user_name: str = ""
    language: str = ""
    model: str = ""
    profile: str = "workspace"
    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)
    context: ContextSettings = field(default_factory=ContextSettings)
    agents: AgentsSettings = field(default_factory=AgentsSettings)
    permissions: PermissionsSettings = field(default_factory=PermissionsSettings)
    network: NetworkSettings = field(default_factory=NetworkSettings)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        data = without_retired(data)
        _validate(data)
        runtime = _section(data, "runtime", "runtime")
        safeguards = _nested_table(runtime, "safeguards", "runtime.safeguards")
        emergency = _nested_table(runtime, "emergency", "runtime.emergency")
        cost = _nested_table(runtime, "cost", "runtime.cost")
        tools = {
            **Config().to_dict()["runtime"]["tools"],
            **(_nested_table(runtime, "tools", "runtime.tools") if "tools" in runtime else {}),
        }
        return cls(
            user_name=_str(data, "user_name", "str"),
            language=_str(data, "language", "str"),
            model=_str(data, "model", "str"),
            # The selected profile may be a user-defined profile file
            # (~/.rinari/profiles/<name>.toml); existence is validated during
            # profile resolution, not here.
            profile=_str(data, "profile", "str", minimum=1),
            runtime=RuntimeSettings(
                tools=RuntimeToolsSettings(
                    max_concurrency=_plain_int(
                        tools, "max_concurrency", "runtime.tools.max_concurrency", 1, 32
                    ),
                    observation_bytes=_plain_int(
                        tools,
                        "observation_bytes",
                        "runtime.tools.observation_bytes",
                        4096,
                        16777216,
                    ),
                    round_observation_bytes=_plain_int(
                        tools,
                        "round_observation_bytes",
                        "runtime.tools.round_observation_bytes",
                        4096,
                        67108864,
                    ),
                ),
                safeguards=RuntimeSafeguardsSettings(
                    loop_detection=_plain_bool(
                        safeguards, "loop_detection", "runtime.safeguards.loop_detection"
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
                ask_new_destinations=_bool(data, "permissions", "ask_new_destinations"),
            ),
            network=NetworkSettings(mode=_enum(data, "network.mode", NETWORK_MODES, "str")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_name": self.user_name,
            "language": self.language,
            "model": self.model,
            "profile": self.profile,
            "runtime": {
                "tools": {
                    "max_concurrency": self.runtime.tools.max_concurrency,
                    "observation_bytes": self.runtime.tools.observation_bytes,
                    "round_observation_bytes": self.runtime.tools.round_observation_bytes,
                },
                "safeguards": {
                    "loop_detection": self.runtime.safeguards.loop_detection,
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
                "ask_new_destinations": self.permissions.ask_new_destinations,
            },
            "network": {"mode": self.network.mode},
        }

    def value(self, dotted: str) -> Any | None:
        return _walk(self.to_dict(), dotted)

    def sections(self) -> tuple[str, ...]:
        return ("runtime", "context", "agents", "permissions", "network")


def _section_keys(section: str) -> list[str]:
    return {
        "runtime": ["safeguards", "emergency", "cost", "tools"],
        "context": ["compact_at_percent", "artifact_output_threshold_kb"],
        "agents": ["enabled", "max_concurrent", "max_depth", "max_total"],
        "permissions": ["profile", "ask_new_destinations"],
        "network": ["mode"],
    }[section]


def leaf_keys() -> list[str]:
    keys = ["user_name", "language", "model", "profile"]
    for section in Config().sections():
        if section == "runtime":
            keys.extend(
                [
                    "runtime.tools.max_concurrency",
                    "runtime.tools.observation_bytes",
                    "runtime.tools.round_observation_bytes",
                    "runtime.safeguards.loop_detection",
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
    if is_retired(dotted):
        raise ConfigurationError(
            f"'{dotted}' is no longer a setting: nothing in Rinari read it.",
            hint="An old config file that has it still loads; the value is ignored.",
        )
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
            "runtime.tools.max_concurrency": "int",
            "runtime.tools.observation_bytes": "int",
            "runtime.tools.round_observation_bytes": "int",
            "runtime.safeguards.loop_detection": "bool",
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
        ("context", "compact_at_percent"): "int",
        ("context", "artifact_output_threshold_kb"): "int",
        ("agents", "enabled"): "bool",
        ("agents", "max_concurrent"): "int",
        ("agents", "max_depth"): "int",
        ("agents", "max_total"): "int",
        ("permissions", "profile"): "str",
        ("permissions", "ask_new_destinations"): "bool",
        ("network", "mode"): "str",
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
            "tools": {"max_concurrency", "observation_bytes", "round_observation_bytes"},
            "safeguards": {"loop_detection", "context_compaction"},
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


# Settings nothing read (plan 0.4.0 #96): an old config file that has them still
# loads, and they are dropped on the way in.
RETIRED = (
    "agent",
    "workspace",
    "instructions",
    "telemetry",
    "runtime.execution",
    "runtime.safeguards.progress_detection",
    "permissions.approval_policy",
)


def is_retired(dotted: str) -> bool:
    return any(dotted == key or dotted.startswith(key + ".") for key in RETIRED)


def without_retired(data: dict[str, Any]) -> dict[str, Any]:
    """A copy of `data` without the retired settings."""
    clean = {key: value for key, value in data.items() if key not in RETIRED}
    for dotted in RETIRED:
        parts = dotted.split(".")
        if len(parts) < 2:
            continue
        parent = clean
        chain = []
        for part in parts[:-1]:
            node = parent.get(part) if isinstance(parent, dict) else None
            if not isinstance(node, dict):
                break
            node = dict(node)
            parent[part] = node
            chain.append(node)
            parent = node
        else:
            parent.pop(parts[-1], None)
    return clean


def _fail(where: str, problem: str) -> None:
    raise ConfigurationError(f"Config key {where} {problem}")
