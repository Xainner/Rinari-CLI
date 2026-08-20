"""`rinari config migrate`: deterministic v1-legacy -> modern config upgrade.

The legacy layout (inline [default]/[profile.*] endpoint tables + [user]) must
upgrade in place with a backup, without echoing or duplicating API keys, and
the result must load through the normal EffectiveConfig path.
"""

from __future__ import annotations

import json
import tomllib

import pytest
from typer.testing import CliRunner

from rinari.application.config.loader import load_effective_config
from rinari.application.config.migration import migrate_legacy_config
from rinari.cli.main import app
from rinari.shared.errors import ConfigurationError
from rinari.shared.paths import ENV_HOME, ensure_layout

LEGACY_CONFIG = """# rinari config - perfiles legacy v1

[user]
name = "Xainner"

[default]
base_url = "https://api.example.com/v1"
model = "qwen3.6-27b"
api_key = "sk-fake-legacy-default"
provider = "custom"
temperature = 0.7

[profile.casa]
base_url = "http://192.168.0.3:8020/v1"
model = "qwen3.6-27b"
api_key = "sk-fake-legacy-casa"
provider = "local"
temperature = 0.7

[profile.net]
base_url = "https://api.example.net/v1"
model = "qwen3.5-27b"
api_key = "${LEGACY_ENV_MASTER}"
provider = "local"
temperature = 0.7
"""

MODERN_EXTRA = """
[agent]
max_turns = 200
"""

runner = CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_HOME, str(tmp_path / "rinari-home"))
    return ensure_layout(tmp_path / "rinari-home")


@pytest.fixture
def legacy_home(home):
    home.config_file.write_text(LEGACY_CONFIG, encoding="utf-8")
    return home


def test_migrate_no_config_file(home):
    report = migrate_legacy_config(home, "20260820-000000")
    assert report.migrated is False
    assert "no user config file" in report.reason
    assert report.backup is None


def test_migrate_clean_config_is_noop(home):
    home.config_file.write_text(
        'user_name = "Xainner"\n\n[agent]\nmax_turns = 200\n', encoding="utf-8"
    )
    before = home.config_file.read_text(encoding="utf-8")
    report = migrate_legacy_config(home, "20260820-000000")
    assert report.migrated is False
    assert "no legacy" in report.reason
    assert home.config_file.read_text(encoding="utf-8") == before


def test_migrate_legacy_upgrades_in_place(home, tmp_path):
    home.config_file.write_text(LEGACY_CONFIG + MODERN_EXTRA, encoding="utf-8")
    report = migrate_legacy_config(home, "20260820-000000")

    assert report.migrated is True
    assert report.backup is not None
    assert report.backup.read_text(encoding="utf-8") == LEGACY_CONFIG + MODERN_EXTRA

    new_raw = tomllib.loads(home.config_file.read_text(encoding="utf-8"))
    assert "default" not in new_raw
    assert "profile" not in new_raw
    assert "user" not in new_raw
    assert new_raw["user_name"] == "Xainner"
    assert new_raw["agent"]["max_turns"] == 200

    # The migrated file loads through the normal validation path.
    effective = load_effective_config(home)
    assert effective.value("user_name") == "Xainner"

    names = [e.name for e in report.endpoints]
    assert names == ["default", "casa", "net"]
    casa = report.endpoints[1]
    net = report.endpoints[2]
    assert casa.has_secret is True and casa.secret_env is None
    assert net.secret_env == "LEGACY_ENV_MASTER"
    assert casa.model_alias == "casa-qwen3-6-27b"

    # Secret values are never echoed in the report.
    dumped = json.dumps(report.to_dict())
    assert "sk-fake-legacy-default" not in dumped
    assert "sk-fake-legacy-casa" not in dumped
    assert "--api-key <re-enter from backup>" in " ".join(casa.recreate_commands())
    assert "--api-key-env LEGACY_ENV_MASTER" in " ".join(net.recreate_commands())
    assert any("backup" in step for step in report.next_steps)


def test_migrate_dry_run_writes_nothing(legacy_home):
    before = legacy_home.config_file.read_text(encoding="utf-8")
    report = migrate_legacy_config(legacy_home, "20260820-000000", dry_run=True)
    assert report.migrated is True
    assert report.backup is None
    assert legacy_home.config_file.read_text(encoding="utf-8") == before
    backups = [p.name for p in legacy_home.root.glob("config.toml.bak-*")]
    assert backups == []


def test_migrate_invalid_toml_is_error(home):
    home.config_file.write_text("not [ valid toml", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        migrate_legacy_config(home, "20260820-000000")


def test_migrate_drops_unknown_profile_reference(home):
    home.config_file.write_text('profile = "ghost"\n\n[user]\nname = "X"\n', encoding="utf-8")
    report = migrate_legacy_config(home, "20260820-000000")
    assert report.migrated is True
    new_raw = tomllib.loads(home.config_file.read_text(encoding="utf-8"))
    assert "profile" not in new_raw
    assert any("ghost" in w for w in report.warnings)


def test_cli_migrate_dry_run_then_apply(legacy_home, monkeypatch):
    before = legacy_home.config_file.read_text(encoding="utf-8")
    monkeypatch.chdir(legacy_home.root)
    res = runner.invoke(app, ["config", "migrate", "--dry-run"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert "[dry-run]" in res.output
    assert legacy_home.config_file.read_text(encoding="utf-8") == before

    res = runner.invoke(app, ["config", "migrate"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert "Migrated legacy config" in res.output
    assert "Backup:" in res.output
    assert "rinari providers add custom --name casa" in res.output

    # After the migration the normal config path loads fine.
    effective = load_effective_config(legacy_home)
    assert effective.value("user_name") == "Xainner"


def test_cli_migrate_json_envelope(legacy_home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    res = runner.invoke(app, ["--json", "config", "migrate"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)["data"]
    assert data["migrated"] is True
    assert [e["name"] for e in data["endpoints"]] == ["default", "casa", "net"]
    assert data["backup"] is not None
