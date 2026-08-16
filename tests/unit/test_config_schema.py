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
    assert config.agent.max_turns == 200
    assert config.profile == "workspace"
    assert config.workspace.project_root_markers[0] == ".git"


def test_override_single_key(defaults):
    defaults["agent"] = {**defaults["agent"], "max_turns": 42}
    assert Config.from_dict(defaults).agent.max_turns == 42
    assert Config.from_dict(defaults).agent.max_tool_calls == 500


def test_unknown_top_level_key_rejected(defaults):
    defaults["bogus"] = 1
    with pytest.raises(ConfigurationError, match="unknown key"):
        Config.from_dict(defaults)


def test_unknown_section_key_rejected(defaults):
    defaults["agent"]["bogus"] = 1
    with pytest.raises(ConfigurationError, match=r"agent.*unknown"):
        Config.from_dict(defaults)


def test_wrong_type_rejected(defaults):
    defaults["agent"]["max_turns"] = "lots"
    with pytest.raises(ConfigurationError, match=r"agent\.max_turns"):
        Config.from_dict(defaults)


def test_out_of_range_rejected(defaults):
    defaults["context"]["compact_at_percent"] = 120
    with pytest.raises(ConfigurationError, match="compact_at_percent"):
        Config.from_dict(defaults)


def test_bool_rejected_as_int(defaults):
    defaults["agent"]["max_turns"] = True
    with pytest.raises(ConfigurationError, match="integer"):
        Config.from_dict(defaults)


def test_bad_enum_rejected(defaults):
    defaults["permissions"]["profile"] = "yolo"
    with pytest.raises(ConfigurationError, match="profile"):
        Config.from_dict(defaults)


def test_empty_list_rejected(defaults):
    defaults["workspace"]["project_root_markers"] = []
    with pytest.raises(ConfigurationError, match="project_root_markers"):
        Config.from_dict(defaults)


def test_empty_approval_policy_rejected(defaults):
    defaults["permissions"]["approval_policy"] = ""
    with pytest.raises(ConfigurationError, match="approval_policy"):
        Config.from_dict(defaults)


def test_leaf_keys_cover_schema():
    keys = leaf_keys()
    assert keys[0] == "user_name"
    assert "agent.max_turns" in keys
    assert "telemetry.redact_secrets" in keys
    assert len(keys) == len(set(keys))


def test_key_type_mapping():
    assert key_type("agent.max_turns") == "int"
    assert key_type("agents.enabled") == "bool"
    assert key_type("workspace.project_root_markers") == "list[str]"
    assert key_type("model") == "str"


def test_key_type_section_is_error():
    with pytest.raises(ConfigurationError, match="section"):
        key_type("agent")


def test_key_type_unknown_is_error():
    with pytest.raises(ConfigurationError, match="Unknown"):
        key_type("agent.nope")
    with pytest.raises(ConfigurationError, match="Unknown"):
        key_type("nope.key")


def test_value_dotted_access(defaults):
    config = Config.from_dict(defaults)
    assert config.value("agent.max_turns") == 200
    assert config.value("network.mode") == "ask"
    assert config.value("does.not.exist") is None
