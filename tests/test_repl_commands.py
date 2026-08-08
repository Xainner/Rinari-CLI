"""Tests de /compact, /todos y output json (fase 1 de pulido)."""

import json

import pytest

from rinari.repl import ChatSession, parse_command, run_command


class FakeClient:
    """Cliente que devuelve un resumen fijo."""

    def __init__(self, summary="[resumen] conversación anterior resumida"):
        self.summary = summary
        self.last_messages = None

    def chat(self, messages, temperature=0.7, max_tokens=None, tools=None):
        self.last_messages = list(messages)
        return self.summary


@pytest.fixture
def session():
    s = ChatSession(history=None, profile="default")
    s.add_user_message("primera pregunta")
    s.add_assistant_message("primera respuesta")
    s.add_user_message("segunda pregunta")
    s.add_assistant_message("segunda respuesta")
    return s


def test_parse_compact():
    cmd, args = parse_command("/compact")
    assert cmd == "compact"
    assert args == ""


def test_compact_resumes_and_keeps_context(session):
    """/compact resume la conversación y la reemplaza por system + resumen."""
    client = FakeClient()
    msg = run_command(
        "compact", "",
        session,
        config_dir=None,
        compact_client=client,
    )
    assert client.last_messages is not None  # llamó al modelo
    assert "resumen" in msg.lower()
    # la conversación quedó compactada: system + resumen
    assert len(session.messages) == 2
    assert session.messages[0]["role"] == "system"
    assert "resumen" in session.messages[1]["content"].lower()


def test_compact_short_conversation_noop(session):
    """Con pocos mensajes, /compact avisa y no toca nada."""
    short = ChatSession(history=None, profile="default")
    short.add_user_message("hola")
    msg = run_command(
        "compact", "",
        short,
        config_dir=None,
        compact_client=FakeClient(),
    )
    assert "corta" in msg.lower() or "no hace falta" in msg.lower()
    assert len(short.messages) == 2  # system + user sin cambios


def test_compact_no_client_raises(session):
    with pytest.raises(ValueError):
        run_command("compact", "", session, config_dir=None)


def test_todos_list_empty(session):
    msg = run_command("todos", "", session, config_dir=None)
    assert "sin tareas" in msg.lower() or "vacía" in msg.lower()


def test_todos_add_and_list(session):
    run_command("todos", "add arreglar el bug de auth", session)
    run_command("todos", "add escribir tests", session)
    msg = run_command("todos", "", session)
    assert "arreglar el bug de auth" in msg
    assert "escribir tests" in msg


def test_todos_done(session):
    run_command("todos", "add tarea uno", session)
    run_command("todos", "add tarea dos", session)
    msg = run_command("todos", "done 1", session)
    assert "✓" in msg or "completada" in msg.lower()
    final = run_command("todos", "", session)
    assert "[x]" in final or "✓" in final  # la primera está completada


def test_todos_help(session):
    msg = run_command("todos", "help", session)
    assert "add" in msg and "done" in msg


def test_compact_works_with_model_client_real_flow():
    """run_command integra compact_client=None por defecto (sin romper)."""
    s = ChatSession(history=None, profile="default")
    s.add_user_message("a")
    s.add_assistant_message("b")
    s.add_user_message("c")
    s.add_assistant_message("d")
    s.add_user_message("e")
    s.add_assistant_message("f")
    with pytest.raises(ValueError):
        run_command("compact", "", s)
