"""Tests de /cost, /undo y skills (custom commands)."""

import json
from pathlib import Path

import pytest

from rinari.agent.tools import edit_file, write_file
from rinari.repl import ChatSession, parse_command, run_command


@pytest.fixture
def session():
    return ChatSession(history=None, profile="default")


# ------------------------------------------------------------------ /cost

def test_session_tracks_usage(session):
    """ChatSession acumula tokens usados por llamada."""
    session.add_usage(100, 50)
    session.add_usage(200, 75)
    assert session.total_prompt_tokens == 300
    assert session.total_completion_tokens == 125
    assert session.total_tokens == 425


def test_cost_command_shows_totals(session):
    session.add_usage(120, 30)
    msg = run_command("cost", "", session)
    assert "120" in msg or "150" in msg  # prompt o total
    assert "token" in msg.lower()


def test_cost_empty_session(session):
    msg = run_command("cost", "", session)
    assert "0" in msg


# ------------------------------------------------------------------ /undo

def test_edit_file_creates_backup(tmp_path):
    """edit_file guarda un backup antes de modificar."""
    f = tmp_path / "a.txt"
    f.write_text("linea1\n", encoding="utf-8")
    result = edit_file({"path": str(f), "old": "linea1", "new": "linea1\nlinea2"}, cwd=str(tmp_path))
    assert result["ok"] is True
    assert result.get("backup")  # hay backup
    undo_dir = tmp_path / ".rinari-undo"
    assert undo_dir.is_dir()
    backups = list(undo_dir.iterdir())
    assert backups  # al menos un backup


def test_write_file_creates_backup(tmp_path):
    """write_file guarda backup del archivo existente."""
    f = tmp_path / "b.txt"
    f.write_text("original\n", encoding="utf-8")
    result = write_file({"path": str(f), "content": "nuevo\n"}, cwd=str(tmp_path))
    assert result["ok"] is True
    assert result.get("backup")
    assert (tmp_path / ".rinari-undo").is_dir()


def test_undo_restores_last_edit(tmp_path):
    """undo_edit restaura el último backup del cwd."""
    from rinari.agent.tools import undo_edit

    f = tmp_path / "c.txt"
    f.write_text("v1\n", encoding="utf-8")
    edit_file({"path": str(f), "old": "v1", "new": "v1\nv2"}, cwd=str(tmp_path))
    assert f.read_text(encoding="utf-8") == "v1\nv2\n"
    result = undo_edit({}, cwd=str(tmp_path))
    assert result["ok"] is True
    assert f.read_text(encoding="utf-8") == "v1\n"


def test_undo_no_backups(tmp_path):
    from rinari.agent.tools import undo_edit

    result = undo_edit({}, cwd=str(tmp_path))
    assert result["ok"] is False


def test_undo_restores_file_in_subdirectory(tmp_path):
    """El backup conserva la ruta relativa (subdirectorios incluidos)."""
    from rinari.agent.tools import undo_edit

    sub = tmp_path / "src"
    sub.mkdir()
    f = sub / "mod.py"
    f.write_text("v1\n", encoding="utf-8")
    edit_file({"path": str(f), "old": "v1", "new": "v1\nv2"}, cwd=str(tmp_path))
    assert f.read_text(encoding="utf-8") == "v1\nv2\n"
    result = undo_edit({}, cwd=str(tmp_path))
    assert result["ok"] is True
    assert f.read_text(encoding="utf-8") == "v1\n"


# ------------------------------------------------------------------ skills

def test_load_skill_from_config_dir(tmp_path):
    """Un archivo commands/<nombre>.md es un skill cargable."""
    from rinari.repl import load_skill

    cmds = tmp_path / ".rinari" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "review.md").write_text("Revisa el código y encuentra bugs", encoding="utf-8")
    assert load_skill("review", config_dir=tmp_path / ".rinari") == "Revisa el código y encuentra bugs"


def test_load_skill_missing(tmp_path):
    from rinari.repl import load_skill

    assert load_skill("nope", config_dir=tmp_path / ".rinari") is None


def test_load_skill_from_home(tmp_path, monkeypatch):
    """También busca en ~/.rinari/commands/ (home)."""
    from rinari.repl import load_skill

    home = tmp_path
    monkeypatch.setattr(Path, "home", lambda: home)
    cmds = home / ".rinari" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "commit.md").write_text("Haz un commit con mensaje conventional", encoding="utf-8")
    assert load_skill("commit", config_dir=home / ".rinari") is not None


def test_run_command_unknown_still_raises(session):
    with pytest.raises(ValueError):
        run_command("zzz", "", session)


def test_parse_command_slash():
    cmd, args = parse_command("/review")
    assert cmd == "review"
    assert args == ""
