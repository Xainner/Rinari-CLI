"""`rinari setup` interactive wizard (first-run onboarding).

Covers the terminal wizard path (via the hidden --interactive seam), the
one-shot flags path, and the non-terminal error path. CliRunner stdin is never
a TTY, so the wizard is always exercised with --interactive. Every prompt gets
an explicit answer line: EOF aborts the wizard even when a default exists.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from rinari.cli.main import app
from rinari.shared.paths import ENV_HOME, ensure_layout

runner = CliRunner()

# type=custom, alias, bad endpoint (reprompt), endpoint, auth=api-key (default),
# hidden key, model id, model alias=default
WIZARD_INPUT = "custom\nwiz\nttps://\nhttps://api.example.org/v1\n\nsk-wiz-123\nqwen-wiz-9\n\n"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_HOME, str(tmp_path / "rinari-home"))
    ensure_layout(tmp_path / "rinari-home")
    return tmp_path / "rinari-home"


def _json(res) -> dict:
    assert res.exit_code == 0, res.output
    return json.loads(res.output)


def test_wizard_custom_provider_key_model(home) -> None:
    res = runner.invoke(app, ["setup", "--interactive"], input=WIZARD_INPUT)
    assert res.exit_code == 0, res.output
    assert "Provider 'wiz' saved and active." in res.output
    assert "Model 'qwen-wiz-9' saved and active." in res.output
    assert "Setup complete." in res.output

    data = _json(runner.invoke(app, ["--json", "status"]))["data"]
    assert data["provider"] == {"alias": "wiz", "type": "custom"}
    assert data["model"]["alias"] == "qwen-wiz-9"
    assert data["model"]["provider_model_id"] == "qwen-wiz-9"


def test_wizard_env_auth_source_warns_when_unset(home) -> None:
    res = runner.invoke(
        app,
        ["setup", "--interactive"],
        input="custom\nnet\nhttps://api.example.net/v1\nenv\nWIZ_NET_KEY\nqw-net\n\n",
    )
    assert res.exit_code == 0, res.output
    assert "Provider 'net' saved and active." in res.output
    assert "Model 'qw-net' saved and active." in res.output
    assert "WIZ_NET_KEY is not set in this shell" in res.output


def test_flags_path_still_works(home) -> None:
    res = runner.invoke(
        app,
        [
            "setup",
            "--provider",
            "custom",
            "--name",
            "flagged",
            "--endpoint",
            "https://api.example.org/v1",
            "--api-key",
            "sk-flag",
            "--model",
            "qw-42",
            "--model-name",
            "qw42",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "Provider 'flagged' saved and active." in res.output
    assert "Model 'qw42' saved and active." in res.output


def test_wizard_requires_model_id_and_aborts_cleanly(home) -> None:
    # all answers, then model id left empty until EOF: aborts, nothing is saved
    res = runner.invoke(
        app,
        ["setup", "--interactive"],
        input="custom\nwiz\nhttps://api.example.org/v1\n\ntest-key\n\n",
    )
    assert res.exit_code != 0
    listing = runner.invoke(app, ["--json", "providers", "list"])
    assert listing.exit_code == 0, listing.output
    assert _json(listing)["data"] == []


def test_non_terminal_without_flags_fails_with_hint(home) -> None:
    res = runner.invoke(app, ["setup"])
    assert res.exit_code != 0
    assert "Onboarding flags are required" in res.output
    assert "rinari providers add custom" in res.output


def test_non_interactive_flag_keeps_flags_error(home) -> None:
    res = runner.invoke(app, ["setup", "--non-interactive"])
    assert res.exit_code != 0
    assert "Onboarding flags are required" in res.output


def test_existing_configuration_skips_wizard(home) -> None:
    pre = runner.invoke(
        app, ["providers", "add", "openai", "--name", "openai-personal", "--api-key-env", "FAKE"]
    )
    assert pre.exit_code == 0, pre.output
    res = runner.invoke(app, ["setup", "--interactive"])
    assert res.exit_code == 0, res.output
    assert "Existing Rinari configuration found - nothing was changed." in res.output
    listing = runner.invoke(app, ["--json", "providers", "list"])
    assert len(_json(listing)["data"]) == 1
