"""Tests para la lógica del REPL (parseo de comandos, flujo de sesión)."""

import pytest

from rinari.identity import build_chat_prompt
from rinari.repl import ChatSession, parse_command, run_command


def test_parse_command_new():
    cmd, args = parse_command("/new")
    assert cmd == "new"
    assert args == ""


def test_parse_command_model():
    cmd, args = parse_command("/model casa")
    assert cmd == "model"
    assert args == "casa"


def test_parse_command_with_extra_spaces():
    cmd, args = parse_command("  /exit   ")
    assert cmd == "exit"
    assert args == ""


def test_plain_message_is_not_command():
    cmd, args = parse_command("hola mundo")
    assert cmd is None
    assert args is None


def test_command_unknown_returns_error():
    with pytest.raises(ValueError, match="desconocido"):
        run_command("wat", "", session=None)


def test_run_new_resets_session(tmp_path):
    session = ChatSession(history=None, profile="casa")
    session.messages = [{"role": "user", "content": "viejo"}]
    run_command("new", "", session)
    assert session.messages == [{"role": "system", "content": build_chat_prompt()}]


def test_run_model_switches_profile():
    session = ChatSession(history=None, profile="casa")
    run_command("model", "sat", session)
    assert session.profile == "sat"


def test_run_model_without_name_raises():
    session = ChatSession(history=None, profile="casa")
    with pytest.raises(ValueError, match="nombre"):
        run_command("model", "", session)


def test_run_model_switches_model_when_not_profile(tmp_path):
    """/model <modelo>: si no es un perfil, cambia el modelo del perfil actual."""
    from rinari.config import load_config, set_profile_model
    from rinari.repl import run_command

    set_profile_model(tmp_path, "casa", "antes", base_url="http://x/v1")
    session = ChatSession(history=None, profile="casa")

    msg = run_command("model", "gpt-4o", session, config_dir=tmp_path)
    assert "gpt-4o" in msg
    assert load_config(tmp_path).get_profile("casa").model == "gpt-4o"
    # el perfil no cambia
    assert session.profile == "casa"


def test_run_model_profile_takes_priority(tmp_path):
    """Si el nombre coincide con un perfil, cambia de perfil (no el modelo)."""
    from rinari.config import set_profile_model
    from rinari.repl import run_command

    set_profile_model(tmp_path, "sat", "m-sat", base_url="http://x/v1")
    session = ChatSession(history=None, profile="casa")

    msg = run_command("model", "sat", session, config_dir=tmp_path)
    assert session.profile == "sat"
    assert "sat" in msg


def test_run_model_lists_models_when_no_args(tmp_path):
    """/model sin args: lista los modelos del endpoint y sugiere elegir."""
    from rinari.config import load_config, set_profile_model
    from rinari.repl import run_command

    set_profile_model(tmp_path, "default", "m1", base_url="http://x/v1")
    session = ChatSession(history=None, profile="default")

    msg = run_command("model", "", session, config_dir=tmp_path)
    assert "m1" in msg
    assert "model set" in msg or "/model" in msg


def test_run_exit_raises_systemexit():
    session = ChatSession(history=None, profile="casa")
    with pytest.raises(SystemExit):
        run_command("exit", "", session)


def test_session_has_system_prompt():
    session = ChatSession(history=None, profile="casa")
    session.add_user_message("hola")
    assert session.messages[0]["role"] == "system"
    assert session.messages[1]["role"] == "user"
    assert session.messages[1]["content"] == "hola"
