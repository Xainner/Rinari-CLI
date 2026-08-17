import pytest

from rinari.application.config.loader import (
    LAYER_PROJECT,
    LAYER_USER,
    load_defaults,
    load_effective_config,
)
from rinari.shared.errors import ConfigurationError
from rinari.shared.paths import ensure_layout


@pytest.fixture
def layout(tmp_path):
    return ensure_layout(tmp_path / "rinari-home")


def test_defaults_only_when_no_user_file(layout):
    effective = load_effective_config(layout)
    assert effective.config.agent.max_turns == 200
    assert [layer.name for layer in effective.layers] == ["defaults"]


def test_user_overrides_defaults(layout):
    user = {**load_defaults()}
    user["agent"] = {**user["agent"], "max_turns": 77}
    effective = load_effective_config(layout, user_data=user)
    assert effective.config.agent.max_turns == 77
    assert effective.config.agent.max_tool_calls == 500


def test_user_file_is_read_from_disk(layout):
    (layout.config_file).write_text(
        'user_name = "Xainner"\n\n[agent]\nmax_turns = 11\n', encoding="utf-8"
    )
    effective = load_effective_config(layout)
    assert effective.config.user_name == "Xainner"
    assert effective.config.agent.max_turns == 11
    assert LAYER_USER in [layer.name for layer in effective.layers]


def test_invalid_user_file_raises(layout):
    layout.config_file.write_text("not = valid = toml", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="not valid TOML"):
        load_effective_config(layout)


def test_user_file_with_unknown_key_raises(layout):
    layout.config_file.write_text("bogus_key = 1\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="unknown key"):
        load_effective_config(layout)


def test_profile_layer_sits_above_user(layout):
    (layout.config_file).write_text(
        'profile = "safe"\n\n[network]\nmode = "allow"\n', encoding="utf-8"
    )
    effective = load_effective_config(layout)
    names = [layer.name for layer in effective.layers]
    assert names == ["defaults", "user", "profile:safe"]
    # Profile defines these keys -> it overrides the user layer.
    assert effective.config.permissions.profile == "read-only"
    assert effective.config.network.mode == "ask"
    # Profile does not define these keys -> they inherit from lower layers.
    assert effective.config.agent.max_turns == 200
    assert effective.config.user_name == ""


def test_legacy_inline_profiles_rejected_with_repair_hint(layout):
    (layout.config_file).write_text(
        "[profile.casa]\nbase_url = \"http://192.168.0.3:8020/v1\"\n"
        "model = \"qwen3.6-27b\"\n"
        "[profile.net]\nbase_url = \"https://api.example.net/v1\"\n"
        "model = \"qwen3.6-27b\"\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="must be a string profile name") as excinfo:
        load_effective_config(layout)
    hint = excinfo.value.hint or ""
    assert "casa" in hint
    assert "~/.rinari/profiles/casa.toml" in hint
    assert "~/.rinari/profiles/net.toml" in hint


def test_unknown_profile_raises(layout, tmp_path):
    (layout.config_file).write_text('profile = "nope"\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Unknown profile"):
        load_effective_config(layout)


def test_user_profile_file_overrides_builtin_keys(layout):
    (layout.dir("profiles") / "custom.toml").write_text(
        '[network]\nmode = "off"\n', encoding="utf-8"
    )
    (layout.config_file).write_text('profile = "custom"\n', encoding="utf-8")
    effective = load_effective_config(layout)
    assert effective.config.network.mode == "off"
    assert effective.config.permissions.profile == "workspace"


def test_project_layer_sits_above_profile(layout, tmp_path):
    project_config = tmp_path / "project-config.toml"
    project_config.write_text('[network]\nmode = "allow"\n', encoding="utf-8")
    (layout.config_file).write_text('profile = "safe"\n', encoding="utf-8")
    effective = load_effective_config(layout, project_config=project_config)
    names = [layer.name for layer in effective.layers]
    assert names == ["defaults", "user", "profile:safe", LAYER_PROJECT]
    assert effective.config.network.mode == "allow"


def test_sources_for_reports_contributing_layers(layout, tmp_path):
    (layout.config_file).write_text('model = "user-model"\n', encoding="utf-8")
    effective = load_effective_config(layout)
    sources = effective.sources_for("model")
    assert sources == [("defaults", ""), ("user", "user-model")]
    assert effective.sources_for("agent.max_turns") == [("defaults", 200)]


def test_effective_value_follows_precedence(layout):
    user = {**load_defaults()}
    user["agent"] = {**user["agent"], "max_turns": 50}
    effective = load_effective_config(layout, user_data=user)
    assert effective.value("agent.max_turns") == 50
    assert effective.value("unknown.key") is None
