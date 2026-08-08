"""Tests del selector de sesiones en `rinari chat` (estilo Hermes)."""

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
    yield h
    h.close()


def seed(hist: History, profile: str, n: int) -> list[int]:
    """Crea n sesiones con un mensaje cada una. Devuelve los ids."""
    ids = []
    for i in range(n):
        sid = hist.create_session(profile)
        hist.append_message(sid, {"role": "user", "content": f"mensaje {i}"})
        ids.append(sid)
    return ids


def test_last_session_returns_most_recent(hist):
    """last_session devuelve la sesión más reciente del perfil."""
    seed(hist, "casa", 3)
    last = hist.last_session("casa")
    assert last is not None
    assert last["id"] >= 1
    # la última creada tiene el id mayor
    ids = hist.list_sessions(limit=10)
    assert last["id"] == max(s["id"] for s in ids if s["profile"] == "casa")


def test_last_session_none_when_empty(hist):
    """Sin sesiones del perfil → None."""
    assert hist.last_session("casa") is None


def test_last_session_filters_by_profile(hist):
    """Filtra por perfil: no mezcla sesiones de otros perfiles."""
    seed(hist, "casa", 2)
    seed(hist, "net", 1)
    last = hist.last_session("net")
    assert last is not None
    assert last["profile"] == "net"


def test_pick_session_resume_last(fake_home, monkeypatch):
    """El selector permite continuar la última escribiendo su número."""
    from rinari import cli
    from rinari.config import set_profile_model

    set_profile_model(fake_home / ".rinari", "default", "m", base_url="http://x/v1")
    hist = History(fake_home / ".rinari" / "history.sqlite")
    ids = seed(hist, "default", 3)
    hist.close()

    monkeypatch.setattr("builtins.input", lambda prompt: str(ids[-1]))
    resolved = cli._resolve_resume("default", resume=None, force_new=False, home=fake_home)
    assert resolved == ids[-1]


def test_pick_session_force_new(fake_home):
    """--new fuerza sesión nueva (resume=None)."""
    from rinari import cli

    resolved = cli._resolve_resume("default", resume=None, force_new=True, home=fake_home)
    assert resolved is None


def test_pick_session_explicit_id(fake_home):
    """--resume 2 usa el id explícito."""
    from rinari import cli

    resolved = cli._resolve_resume("default", resume=2, force_new=False, home=fake_home)
    assert resolved == 2


def test_pick_session_prompt_selects(fake_home, monkeypatch):
    """El selector: escribir un número continúa esa sesión."""
    from rinari import cli
    from rinari.config import set_profile_model

    set_profile_model(fake_home / ".rinari", "default", "m", base_url="http://x/v1")
    hist = History(fake_home / ".rinari" / "history.sqlite")
    ids = seed(hist, "default", 3)
    hist.close()

    monkeypatch.setattr("builtins.input", lambda prompt: str(ids[1]))
    resolved = cli._resolve_resume("default", resume=None, force_new=False, home=fake_home)
    assert resolved == ids[1]


def test_pick_session_prompt_empty_is_new(fake_home, monkeypatch):
    """Enter en el selector → sesión nueva (None)."""
    from rinari import cli

    monkeypatch.setattr("builtins.input", lambda prompt: "")
    resolved = cli._resolve_resume("default", resume=None, force_new=False, home=fake_home)
    assert resolved is None
