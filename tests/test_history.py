"""Tests para el historial de conversaciones en SQLite."""

import json

import pytest

from qwencli.history import History, HistoryError


@pytest.fixture
def history(tmp_path):
    return History(db_path=tmp_path / "hist.sqlite")


def test_create_session_and_append(history):
    sid = history.create_session(profile="casa")
    assert sid is not None
    history.append_message(sid, {"role": "user", "content": "hola"})
    history.append_message(sid, {"role": "assistant", "content": "qué tal"})
    messages = history.get_messages(sid)
    assert len(messages) == 2
    assert messages[0]["content"] == "hola"
    assert messages[1]["role"] == "assistant"


def test_messages_stored_as_json_roundtrip(history):
    sid = history.create_session(profile="casa")
    msg = {"role": "assistant", "content": "texto", "extra": {"a": 1}}
    history.append_message(sid, msg)
    loaded = history.get_messages(sid)
    assert loaded[0]["extra"] == {"a": 1}


def test_list_sessions_recent_first(history):
    s1 = history.create_session(profile="casa")
    s2 = history.create_session(profile="sat")
    history.append_message(s1, {"role": "user", "content": "vieja"})
    sessions = history.list_sessions()
    assert len(sessions) == 2
    assert sessions[0]["id"] == s2  # más reciente primero
    assert sessions[0]["profile"] == "sat"
    assert sessions[1]["id"] == s1


def test_resume_existing_session(history):
    sid = history.create_session(profile="casa")
    history.append_message(sid, {"role": "user", "content": "a"})
    history.append_message(sid, {"role": "assistant", "content": "b"})
    resumed = history.get_messages(sid)
    assert [m["content"] for m in resumed] == ["a", "b"]


def test_delete_session(history):
    sid = history.create_session(profile="casa")
    history.append_message(sid, {"role": "user", "content": "x"})
    history.delete_session(sid)
    assert history.get_messages(sid) == []
    assert history.list_sessions() == []


def test_append_to_unknown_session_raises(history):
    with pytest.raises(HistoryError):
        history.append_message(999, {"role": "user", "content": "x"})


def test_persists_across_instances(tmp_path):
    db = tmp_path / "hist.sqlite"
    h1 = History(db_path=db)
    sid = h1.create_session(profile="casa")
    h1.append_message(sid, {"role": "user", "content": "persistente"})

    h2 = History(db_path=db)
    messages = h2.get_messages(sid)
    assert messages[0]["content"] == "persistente"


def test_message_ordering_preserved(history):
    sid = history.create_session(profile="casa")
    for i in range(10):
        history.append_message(sid, {"role": "user", "content": f"msg {i}"})
    messages = history.get_messages(sid)
    assert [m["content"] for m in messages] == [f"msg {i}" for i in range(10)]


def test_created_at_set(history):
    sid = history.create_session(profile="casa")
    sessions = history.list_sessions()
    assert sessions[0]["created_at"] is not None
