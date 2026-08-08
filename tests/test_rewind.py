"""Tests de /rewind: checkpoints de conversación (estilo codex /rewind)."""

import pytest

from rinari.repl import ChatSession, run_command


@pytest.fixture
def session():
    s = ChatSession(history=None, profile="test")
    s.messages = [
        {"role": "system", "content": "sistema"},
        {"role": "user", "content": "hola"},
    ]
    s.checkpoints = []
    return s


def test_rewind_no_checkpoints(session):
    with pytest.raises(ValueError):
        run_command("rewind", "", session)


def test_rewind_restores_last_checkpoint(session):
    # checkpoint: estado antes de la pregunta
    session.checkpoints.append(list(session.messages))
    session.messages.append({"role": "assistant", "content": "respuesta"})
    session.messages.append({"role": "user", "content": "otra pregunta"})

    result = run_command("rewind", "", session)
    assert "rebobina" in result.lower()
    assert len(session.messages) == 2  # volvió al checkpoint
    assert session.messages[-1]["content"] == "hola"


def test_rewind_two_steps(session):
    session.checkpoints.append(list(session.messages))  # checkpoint 0
    session.messages.append({"role": "assistant", "content": "r1"})
    session.checkpoints.append(list(session.messages))  # checkpoint 1
    session.messages.append({"role": "user", "content": "p2"})
    session.messages.append({"role": "assistant", "content": "r2"})

    result = run_command("rewind", "2", session)
    assert "2" in result
    assert len(session.messages) == 2  # volvió al checkpoint 0


def test_rewind_clears_used_checkpoint(session):
    session.checkpoints.append(list(session.messages))
    session.checkpoints.append(list(session.messages))
    run_command("rewind", "", session)
    assert len(session.checkpoints) == 1  # el usado se descarta
