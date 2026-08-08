"""Tests para el CLI de historial: listar, ver, borrar, exportar."""

import pytest
from typer.testing import CliRunner

from rinari.history import History

runner = CliRunner()


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    """Redirige Path.home a tmp_path (config/historial aislados)."""
    from rinari import config as config_mod

    monkeypatch.setattr(config_mod.Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


@pytest.fixture
def hist(tmp_path):
    """Historial aislado en tmp_path."""
    h = History(tmp_path / "history.sqlite")
    sid = h.create_session("casa")
    h.append_message(sid, {"role": "user", "content": "hola"})
    h.append_message(sid, {"role": "assistant", "content": "¡hola! ¿en qué te ayudo?"})
    sid2 = h.create_session("casa")
    h.append_message(sid2, {"role": "user", "content": "segunda"})
    h.close()
    return tmp_path / "history.sqlite"


def test_export_session_markdown(hist):
    """export_session produce markdown legible con los mensajes."""
    h = History(hist)
    md = h.export_session(1)
    assert "casa" in md
    assert "hola" in md
    assert "¿en qué te ayudo?" in md
    # roles visibles
    assert "**Usuario:**" in md or "**usuario:**" in md
    assert "**Rinari:**" in md or "**assistant:**" in md
    h.close()


def test_export_session_unknown(hist):
    """Exportar sesión inexistente → HistoryError."""
    from rinari.history import HistoryError

    h = History(hist)
    with pytest.raises(HistoryError):
        h.export_session(999)
    h.close()


def test_export_includes_all_messages(hist):
    """El export incluye TODOS los mensajes de la sesión."""
    h = History(hist)
    md = h.export_session(2)
    assert "segunda" in md
    assert "hola" not in md  # solo la sesión 2
    h.close()


def test_history_command_lists_sessions(fake_home):
    """`rinari history` lista las sesiones con preview."""
    from pathlib import Path

    from rinari import config as config_mod
    from rinari.history import History

    # sembrar el historial en el home fake
    h = History(fake_home / ".rinari" / "history.sqlite")
    sid = h.create_session("casa")
    h.append_message(sid, {"role": "user", "content": "hola"})
    h.close()

    from rinari.cli import app

    result = runner.invoke(app, ["history"])
    assert result.exit_code == 0
    assert "casa" in result.output
    assert "hola" in result.output
