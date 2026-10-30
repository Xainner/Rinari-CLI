import copy

import pytest

from rinari.application.config.loader import load_defaults
from rinari.application.config.schema import Config, key_type, leaf_keys
from rinari.shared.errors import ConfigurationError


@pytest.fixture
def defaults() -> dict:
    return copy.deepcopy(load_defaults())


def test_defaults_parse():
    config = Config.from_dict(load_defaults())
    assert config.agents.max_total == 12
    assert config.profile == "workspace"
    assert config.runtime.emergency.max_model_calls == 500


def test_override_single_key(defaults):
    defaults["agents"] = {**defaults["agents"], "max_total": 42}
    assert Config.from_dict(defaults).agents.max_total == 42
    assert Config.from_dict(defaults).agents.max_depth == 2


def test_unknown_top_level_key_rejected(defaults):
    defaults["bogus"] = 1
    with pytest.raises(ConfigurationError, match="unknown key"):
        Config.from_dict(defaults)


def test_unknown_section_key_rejected(defaults):
    defaults["agents"]["bogus"] = 1
    with pytest.raises(ConfigurationError, match=r"agents.*unknown"):
        Config.from_dict(defaults)


def test_wrong_type_rejected(defaults):
    defaults["agents"]["max_total"] = "lots"
    with pytest.raises(ConfigurationError, match=r"agents\.max_total"):
        Config.from_dict(defaults)


def test_out_of_range_rejected(defaults):
    defaults["context"]["compact_at_percent"] = 120
    with pytest.raises(ConfigurationError, match="compact_at_percent"):
        Config.from_dict(defaults)


def test_bool_rejected_as_int(defaults):
    defaults["agents"]["max_total"] = True
    with pytest.raises(ConfigurationError, match="integer"):
        Config.from_dict(defaults)


def test_bad_enum_rejected(defaults):
    defaults["permissions"]["profile"] = "yolo"
    with pytest.raises(ConfigurationError, match="profile"):
        Config.from_dict(defaults)


def test_retired_settings_in_an_old_file_still_load(defaults):
    """Settings nothing read (#96) are dropped on the way in, not errors."""
    defaults["agent"] = {"max_turns": 200, "max_tool_calls": 500}
    defaults["workspace"] = {"project_root_markers": [".git"]}
    defaults["instructions"] = {"global_file": "~/.rinari/RINARI.md"}
    defaults["telemetry"] = {"local_traces": True, "redact_secrets": True}
    defaults["permissions"]["approval_policy"] = "on-request"
    defaults["runtime"]["execution"] = "automatic"
    defaults["runtime"]["safeguards"]["progress_detection"] = True
    config = Config.from_dict(defaults)
    assert "agent" not in config.to_dict()
    assert "approval_policy" not in config.to_dict()["permissions"]
    assert defaults["agent"] == {"max_turns": 200, "max_tool_calls": 500}, "input untouched"


def test_retired_settings_cannot_be_set():
    with pytest.raises(ConfigurationError, match="no longer a setting"):
        key_type("agent.max_turns")
    with pytest.raises(ConfigurationError, match="no longer a setting"):
        key_type("permissions.approval_policy")


def test_leaf_keys_cover_schema():
    keys = leaf_keys()
    assert keys[0] == "user_name"
    assert "agents.max_total" in keys
    assert "runtime.emergency.max_model_calls" in keys
    assert not any(key.startswith(("agent.", "telemetry.", "workspace.")) for key in keys)
    assert len(keys) == len(set(keys))


def test_key_type_mapping():
    assert key_type("agents.max_total") == "int"
    assert key_type("agents.enabled") == "bool"
    assert key_type("runtime.cost.max_turn_cost") == "float"
    assert key_type("model") == "str"


def test_key_type_section_is_error():
    with pytest.raises(ConfigurationError, match="section"):
        key_type("agents")


def test_key_type_unknown_is_error():
    with pytest.raises(ConfigurationError, match="Unknown"):
        key_type("agents.nope")
    with pytest.raises(ConfigurationError, match="Unknown"):
        key_type("nope.key")


def test_value_dotted_access(defaults):
    config = Config.from_dict(defaults)
    assert config.value("agents.max_total") == 12
    assert config.value("network.mode") == "auto"
    assert config.value("does.not.exist") is None
