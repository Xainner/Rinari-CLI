import pytest
from typer.testing import CliRunner

from rinari.shared.paths import ENV_HOME

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "rinari-home"
    monkeypatch.setenv(ENV_HOME, str(home))
    return home


def test_config_path_pointing_at_user_config(isolated_home):
    from rinari.cli.main import app

    result = runner.invoke(app, ["config", "path"])
    assert result.exit_code == 0
    assert result.output.strip() == str(isolated_home / "config.toml")


def test_config_get_default_value(isolated_home):
    from rinari.cli.main import app

    result = runner.invoke(app, ["config", "get", "agent.max_turns"])
    assert result.exit_code == 0
    assert result.output.strip() == "200"


def test_config_get_unknown_key_exits_invalid_usage(isolated_home):
    from rinari.cli.main import app

    result = runner.invoke(app, ["config", "get", "agent.nope"])
    assert result.exit_code == 2
    assert "Unknown config key" in result.output


def test_config_set_roundtrips_through_user_file(isolated_home):
    from rinari.cli.main import app

    result = runner.invoke(app, ["config", "set", "agent.max_turns", "250"])
    assert result.exit_code == 0, result.output

    config_file = isolated_home / "config.toml"
    assert config_file.is_file()
    raw = config_file.read_text(encoding="utf-8")
    assert "max_turns = 250" in raw

    # The key must not drag unrelated defaults into the user file.
    assert "max_tool_calls" not in raw

    check = runner.invoke(app, ["config", "get", "agent.max_turns"])
    assert check.output.strip() == "250"


def test_config_set_validates_before_persisting(isolated_home):
    from rinari.cli.main import app

    result = runner.invoke(app, ["config", "set", "agent.max_turns", "999999"])
    assert result.exit_code == 3
    assert not (isolated_home / "config.toml").exists()


def test_config_set_bad_type_is_invalid_usage(isolated_home):
    from rinari.cli.main import app

    result = runner.invoke(app, ["config", "set", "agents.enabled", "maybe"])
    assert result.exit_code == 3  # declared-type validation failure = configuration error
    result2 = runner.invoke(app, ["config", "set", "agent.max_turns", "abc"])
    assert result2.exit_code == 3


def test_config_set_list_value(isolated_home):
    from rinari.cli.main import app

    result = runner.invoke(
        app, ["config", "set", "workspace.project_root_markers", ".git,Cargo.toml"]
    )
    assert result.exit_code == 0, result.output
    check = runner.invoke(app, ["config", "get", "workspace.project_root_markers"])
    assert check.output.strip() == '[".git", "Cargo.toml"]'


def test_config_unset_removes_override(isolated_home):
    from rinari.cli.main import app

    runner.invoke(app, ["config", "set", "model", "my-model"])
    result = runner.invoke(app, ["config", "unset", "model"])
    assert result.exit_code == 0
    check = runner.invoke(app, ["config", "get", "model"])
    assert check.output.strip() == ""


def test_config_unset_missing_key_not_found(isolated_home):
    from rinari.cli.main import app

    result = runner.invoke(app, ["config", "unset", "model"])
    assert result.exit_code == 7


def test_config_validate_ok_and_failing(isolated_home):
    from rinari.cli.main import app

    ok = runner.invoke(app, ["config", "validate"])
    assert ok.exit_code == 0
    assert "Config OK" in ok.output

    (isolated_home / "config.toml").write_text("[agent]\nmax_turns = -1\n", encoding="utf-8")
    bad = runner.invoke(app, ["config", "validate"])
    assert bad.exit_code == 3


def test_config_list_shows_all_keys(isolated_home):
    from rinari.cli.main import app

    result = runner.invoke(app, ["config", "list"])
    assert result.exit_code == 0
    assert "agent.max_turns = 200" in result.output
    assert "telemetry.redact_secrets = True" in result.output


def test_explicit_config_flag_replaces_user_file(isolated_home, tmp_path):
    from rinari.cli.main import app

    explicit = tmp_path / "explicit.toml"
    explicit.write_text("[agent]\nmax_turns = 44\n", encoding="utf-8")

    result = runner.invoke(app, ["--config", str(explicit), "config", "get", "agent.max_turns"])
    assert result.exit_code == 0
    assert result.output.strip() == "44"


def test_explicit_config_flags_missing(isolated_home, tmp_path):
    from rinari.cli.main import app

    result = runner.invoke(app, ["--config", str(tmp_path / "nope.toml"), "config", "validate"])
    assert result.exit_code == 3
