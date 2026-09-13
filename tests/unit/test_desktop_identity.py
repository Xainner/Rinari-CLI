"""Desktop rename keeps explicit resolution and the legacy CLI entry point."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from rinari.cli.commands import code
from rinari.cli.main import app
from rinari.shared.errors import InvalidUsageError


@pytest.fixture(autouse=True)
def clear_overrides(monkeypatch):
    monkeypatch.delenv("RINARI_AGENT_BIN", raising=False)
    monkeypatch.delenv("RINARI_CODE_BIN", raising=False)


def test_prefers_new_binary(monkeypatch):
    monkeypatch.setattr(code.shutil, "which", lambda name: name)
    assert code._resolve_binary(None) == "rinari-agent"


def test_accepts_legacy_binary(monkeypatch):
    monkeypatch.setattr(code.shutil, "which", lambda name: name if name == "rinari-code" else None)
    assert code._resolve_binary(None) == "rinari-code"


def test_new_override_precedes_legacy(tmp_path, monkeypatch):
    binary = tmp_path / "rinari-agent.exe"
    binary.touch()
    monkeypatch.setenv("RINARI_AGENT_BIN", str(binary))
    monkeypatch.setenv("RINARI_CODE_BIN", "missing")
    assert code._resolve_binary(None) == str(binary)


def test_invalid_override_does_not_fall_back(monkeypatch):
    monkeypatch.setenv("RINARI_AGENT_BIN", "missing")
    monkeypatch.setattr(code.shutil, "which", lambda name: name)
    with pytest.raises(InvalidUsageError):
        code._resolve_binary(None)


def test_legacy_environment_still_works(tmp_path, monkeypatch):
    binary = tmp_path / "rinari-code.exe"
    binary.touch()
    monkeypatch.setenv("RINARI_CODE_BIN", str(binary))
    assert code._resolve_binary(None) == str(binary)


def test_explicit_binary_precedes_environment(tmp_path, monkeypatch):
    binary = tmp_path / "custom-desktop.exe"
    binary.touch()
    monkeypatch.setenv("RINARI_AGENT_BIN", "missing")
    assert code._resolve_binary(str(binary)) == str(binary)


@pytest.mark.parametrize("command", ["desktop", "code"])
def test_handoff_preserves_project_session(command, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(code, "_resolve_binary", lambda explicit: "rinari-agent")
    monkeypatch.setattr(code.subprocess, "Popen", lambda args, **kwargs: calls.append(args))
    result = CliRunner().invoke(app, [command, str(tmp_path), "--session", "session-123"])
    assert result.exit_code == 0, result.output
    assert calls == [
        ["rinari-agent", "--project", str(Path(tmp_path).resolve()), "--session", "session-123"]
    ]
    assert "Rinari Agent" in result.output


def test_agent_command_remains_autonomous():
    result = CliRunner().invoke(app, ["agent", "--help"])
    assert result.exit_code == 0
    assert "autonomous task" in result.output
