"""export_session must tolerate tool calls stored as plain dicts.

Message rows round-trip through JSON storage, so ``tool_calls`` items can be
dicts instead of ToolCall objects (seen on ses_01M2K6497GMFJDD8BB2C5J5S4D,
whose export crashed with AttributeError: 'dict' object has no attribute 'id').
"""

from types import SimpleNamespace

from rinari.cli.session_export import export_session


def _session(**overrides):
    base = {
        "id": "ses_test",
        "kind": "CHAT",
        "title": "t",
        "project_root_snapshot": None,
        "created_cwd": "C:\\tmp",
        "current_cwd": "C:\\tmp",
        "provider_id": "prov",
        "model_id": "mdl",
        "profile_id": "workspace",
        "mode": "build",
        "state": "interrupted",
        "created_at": "2026-09-15T00:00:00.000Z",
        "forked_from": None,
        "git_branch": None,
        "active_skills": (),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _services(messages):
    record = _session()
    return SimpleNamespace(
        sessions=SimpleNamespace(show=lambda ref: record),
        ctx=SimpleNamespace(
            session_repo=SimpleNamespace(list=lambda limit=1: [record]),
            message_repo=SimpleNamespace(list=lambda sid: messages),
            event_repo=SimpleNamespace(list=lambda sid: []),
        ),
    )


def _message(tool_calls):
    return SimpleNamespace(
        role="assistant",
        content="hi",
        tool_call_id=None,
        name=None,
        tool_calls=tool_calls,
        created_at="2026-09-15T00:00:00.000Z",
    )


def test_export_accepts_dict_tool_calls() -> None:
    messages = [_message([{"id": "c1", "name": "shell.exec", "arguments": {"command": "ls"}}])]
    document = export_session(_services(messages), "ses_test")
    assert document["messages"][0]["tool_calls"] == [
        {"id": "c1", "name": "shell.exec", "arguments": {"command": "ls"}}
    ]


def test_export_accepts_object_tool_calls() -> None:
    tool_call = SimpleNamespace(id="c2", name="fs.write", arguments={"path": "a"})
    document = export_session(_services([_message([tool_call])]), "ses_test")
    assert document["messages"][0]["tool_calls"] == [
        {"id": "c2", "name": "fs.write", "arguments": {"path": "a"}}
    ]


def test_export_preserves_runtime_session_state() -> None:
    document = export_session(_services([]), "ses_test")
    assert document["session"]["state"] == "interrupted"
