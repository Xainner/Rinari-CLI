"""Tests para la lógica del REPL (parseo de comandos, flujo de sesión)."""

import pytest

from qwencli.repl import SYSTEM_PROMPT, ChatSession, parse_command, run_command


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
    assert session.messages == [{"role": "system", "content": SYSTEM_PROMPT}]


def test_run_model_switches_profile():
    session = ChatSession(history=None, profile="casa")
    run_command("model", "sat", session)
    assert session.profile == "sat"


def test_run_model_without_name_raises():
    session = ChatSession(history=None, profile="casa")
    with pytest.raises(ValueError, match="nombre"):
        run_command("model", "", session)


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
