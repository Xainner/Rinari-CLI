"""Tests de /status: dashboard de la sesión (estilo codex /status)."""

import pytest

from rinari.repl import ChatSession, run_command


@pytest.fixture
def session():
    s = ChatSession(history=None, profile="casa")
    s.total_prompt_tokens = 1200
    s.total_completion_tokens = 300
    s.tool_calls = 7
    s.messages = [
        {"role": "system", "content": "sistema"},
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "¡hola!"},
    ]
    return s


def test_status_shows_profile(session):
    result = run_command("status", "", session)
    assert "casa" in result


def test_status_shows_token_usage(session):
    result = run_command("status", "", session)
    assert "1500" in result  # 1200 + 300
    assert "1200" in result


def test_status_shows_tool_calls(session):
    result = run_command("status", "", session)
    assert "7" in result


def test_status_shows_message_count(session):
    result = run_command("status", "", session)
    assert "3" in result  # mensajes en la sesión


def test_status_zero_defaults():
    s = ChatSession(history=None, profile="x")
    result = run_command("status", "", s)
    assert "0" in result  # sin uso todavía
