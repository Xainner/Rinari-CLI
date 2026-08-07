"""Tests para rinari doctor: diagnóstico de perfiles y endpoints."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from rinari import config as config_mod
from rinari.cli import app

runner = CliRunner()


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod.Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


def write_config(home: Path, text: str):
    d = home / ".rinari"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.toml").write_text(text, encoding="utf-8")


def test_diagnose_profile_ok():
    """Perfil válido con endpoint que responde → ok=True."""
    import httpx

    from rinari.client import LLMClient
    from rinari.cli import diagnose_profile

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "m1"}]})

    prof = {"base_url": "http://x/v1", "model": "m1", "api_key": None}
    ok, msg = diagnose_profile("casa", prof, make_client=LLMClient(
        base_url="http://x/v1", transport=httpx.MockTransport(handler)))
    assert ok is True
    assert "m1" in msg


def test_diagnose_profile_down():
    """Endpoint caído → ok=False con mensaje de error."""
    import httpx

    from rinari.client import LLMClient
    from rinari.cli import diagnose_profile

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    prof = {"base_url": "http://x/v1", "model": "m1", "api_key": None}
    ok, msg = diagnose_profile("casa", prof, make_client=LLMClient(
        base_url="http://x/v1", transport=httpx.MockTransport(handler)))
    assert ok is False
    assert "refused" in msg.lower() or "no se pudo" in msg.lower()


def test_diagnose_profile_bad_env():
    """Perfil con ${ENV} sin definir → ok=False (error de expansión)."""
    import os

    from rinari.cli import diagnose_profile

    os.environ.pop("VARIABLE_QUE_NO_EXISTE", None)
    prof = {"base_url": "http://x/v1", "model": "m1", "api_key": "${VARIABLE_QUE_NO_EXISTE}"}
    ok, msg = diagnose_profile("sat", prof, make_client=None)
    assert ok is False
    assert "variable" in msg.lower()


def test_diagnose_profile_alias_model():
    """1 modelo listado + activo distinto = alias del servidor → ok=True."""
    import httpx

    from rinari.client import LLMClient
    from rinari.cli import diagnose_profile

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "Huihui-...Q4_K.gguf"}]})

    prof = {"base_url": "http://x/v1", "model": "qwen3.6-27b", "api_key": None}
    ok, msg = diagnose_profile("casa", prof, make_client=LLMClient(
        base_url="http://x/v1", transport=httpx.MockTransport(handler)))
    assert ok is True
    assert "alias" in msg


def test_diagnose_profile_model_missing_multi():
    """Varios modelos listados y el activo no está → error real."""
    import httpx

    from rinari.client import LLMClient
    from rinari.cli import diagnose_profile

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}]})

    prof = {"base_url": "http://x/v1", "model": "no-existe", "api_key": None}
    ok, _ = diagnose_profile("casa", prof, make_client=LLMClient(
        base_url="http://x/v1", transport=httpx.MockTransport(handler)))
    assert ok is False


def test_doctor_command_reports_profiles(fake_home, monkeypatch):
    """`rinari doctor` reporta todos los perfiles con estado."""
    from rinari.cli import diagnose_profile

    write_config(fake_home, """
[default]
base_url = "http://x/v1"
model = "m1"

[profile.casa]
base_url = "http://x/v1"
model = "m1"
""")
    monkeypatch.setattr(
        "rinari.cli.diagnose_profile",
        lambda name, prof, make_client=None: (True, f"{name} OK"),
    )
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "default" in result.output
    assert "casa" in result.output


def test_doctor_command_detects_issues(fake_home, monkeypatch):
    """Reporta en rojo los perfiles con problemas y exit code != 0."""
    write_config(fake_home, """
[default]
base_url = "http://x/v1"
model = "m1"

[profile.malo]
base_url = "http://roto/v1"
model = "m1"
""")
    monkeypatch.setattr(
        "rinari.cli.diagnose_profile",
        lambda name, prof, make_client=None: (name != "malo", "msg"),
    )
    result = runner.invoke(app, ["doctor"])
    assert "malo" in result.output
    assert "✗" in result.output or "error" in result.output.lower() or "fall" in result.output.lower()
    assert result.exit_code != 0
