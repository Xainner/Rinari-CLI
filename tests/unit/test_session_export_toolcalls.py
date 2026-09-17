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


def _message(tool_calls, origin=None):
    return SimpleNamespace(
        role="assistant",
        content="hi",
        tool_call_id=None,
        name=None,
        tool_calls=tool_calls,
        created_at="2026-09-15T00:00:00.000Z",
        origin=origin,
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


def test_export_preserves_message_origin() -> None:
    origin = {"kind": "peer", "session_id": "ses_peer", "message_id": "pm_1"}
    document = export_session(_services([_message([], origin=origin), _message([])]), "ses_test")
    assert document["messages"][0]["origin"] == origin
    assert document["messages"][1]["origin"] is None


def test_export_preserves_runtime_session_state() -> None:
    document = export_session(_services([]), "ses_test")
    assert document["session"]["state"] == "interrupted"


def test_export_preserves_mixed_tool_calls_in_order() -> None:
    calls = [
        {"id": "c1", "name": "fs.read", "arguments": {"path": "a"}},
        SimpleNamespace(id="c2", name="shell.exec", arguments={"command": "ls"}),
        {"id": "c3", "name": "fs.write", "arguments": {"path": "b"}},
    ]
    document = export_session(_services([_message(calls)]), "ses_test")
    assert document["messages"][0]["tool_calls"] == [
        {"id": "c1", "name": "fs.read", "arguments": {"path": "a"}},
        {"id": "c2", "name": "shell.exec", "arguments": {"command": "ls"}},
        {"id": "c3", "name": "fs.write", "arguments": {"path": "b"}},
    ]


def test_export_empty_tool_calls() -> None:
    document = export_session(_services([_message(None), _message([])]), "ses_test")
    assert document["messages"][0]["tool_calls"] == []
    assert document["messages"][1]["tool_calls"] == []


def test_export_normalizes_missing_arguments() -> None:
    calls = [
        {"id": "c1", "name": "fs.read"},
        {"id": "c2", "name": "fs.read", "arguments": None},
    ]
    document = export_session(_services([_message(calls)]), "ses_test")
    assert document["messages"][0]["tool_calls"] == [
        {"id": "c1", "name": "fs.read", "arguments": {}},
        {"id": "c2", "name": "fs.read", "arguments": {}},
    ]


def _real_services(app_ctx, tmp_path):
    from rinari.application.services import build_services
    from rinari.storage.records import SessionMessageRecord, SessionRecord

    user_home = tmp_path / "home"
    user_home.mkdir()
    s = build_services(app_ctx, user_home=user_home)
    record = SessionRecord(
        id="ses_rt",
        kind="CHAT",
        title="roundtrip",
        project_id=None,
        project_root_snapshot=None,
        created_cwd=str(tmp_path),
        current_cwd=str(tmp_path),
        provider_id="prov",
        model_id="mdl",
        profile_id="workspace",
        mode="build",
        state="interrupted",
        compact_state=None,
        created_at="2026-09-15T00:00:00.000Z",
        updated_at="2026-09-15T00:00:00.000Z",
        last_active_at="2026-09-15T00:00:00.000Z",
    )
    s.ctx.session_repo.insert(record)
    s.ctx.message_repo.append_many(
        record.id,
        [
            SessionMessageRecord(
                id="m1",
                session_id=record.id,
                seq=0,
                role="assistant",
                content="hi",
                tool_calls=[{"id": "c1", "name": "shell.exec", "arguments": {"command": "ls"}}],
                created_at="2026-09-15T00:00:00.000Z",
            )
        ],
    )
    return s, record


def test_export_roundtrip_real_storage(app_ctx, tmp_path) -> None:
    s, record = _real_services(app_ctx, tmp_path)
    reloaded = s.ctx.message_repo.list(record.id)
    assert reloaded[0].tool_calls == [
        {"id": "c1", "name": "shell.exec", "arguments": {"command": "ls"}}
    ]
    document = export_session(s, record.id)
    assert document["session"]["id"] == record.id
    assert document["session"]["state"] == "interrupted"
    assert document["messages"][0]["tool_calls"] == [
        {"id": "c1", "name": "shell.exec", "arguments": {"command": "ls"}}
    ]


def test_export_runtime_states_do_not_mutate(app_ctx, tmp_path) -> None:
    import dataclasses

    from rinari.storage.records import SessionMessageRecord

    s, record = _real_services(app_ctx, tmp_path)
    stopped = dataclasses.replace(record, id="ses_rt_stopped", title="stopped", state="stopped")
    s.ctx.session_repo.insert(stopped)
    s.ctx.message_repo.append_many(
        stopped.id,
        [
            SessionMessageRecord(
                id="m2",
                session_id=stopped.id,
                seq=0,
                role="assistant",
                content="hi",
                tool_calls=[{"id": "c9", "name": "fs.read", "arguments": {"path": "x"}}],
                created_at="2026-09-15T00:00:00.000Z",
            )
        ],
    )
    for session_id, state in ((record.id, "interrupted"), (stopped.id, "stopped")):
        before = [(m.id, m.created_at) for m in s.ctx.message_repo.list(session_id)]
        document = export_session(s, session_id)
        assert document["session"]["state"] == state
        current = s.ctx.session_repo.get(session_id)
        assert current is not None and current.state == state
        assert [(m.id, m.created_at) for m in s.ctx.message_repo.list(session_id)] == before
