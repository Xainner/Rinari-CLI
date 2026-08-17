"""Session fork (phase 4).

Regression contract: `session fork` copies the full durable state (kind,
project identity, provider/model/profile, mode, compact state, stored branch)
and the conversation into a new, independent session; the forked row carries
durable provenance (`forked_from` + a `SessionForked` event); the source
session is never modified.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.shared.clock import now_iso
from rinari.shared.errors import NotFoundError
from rinari.storage.records import SessionMessageRecord


def _msg(
    services, session_id: str, seq: int, role: str, content: str, **extra
) -> SessionMessageRecord:
    return SessionMessageRecord(
        id=services.ctx.ids.new("msg"),
        session_id=session_id,
        seq=seq,
        role=role,
        content=content,
        tool_calls=extra.get("tool_calls"),
        tool_call_id=extra.get("tool_call_id"),
        name=extra.get("name"),
        created_at=now_iso(services.ctx.clock),
    )


@pytest.fixture
def env(app_ctx, tmp_path):
    user_home = tmp_path / "home"
    user_home.mkdir()
    s = build_services(app_ctx, user_home=user_home)
    s.providers.add(
        AddProviderInput(
            alias="fake",
            provider_type="openai",
            endpoint="http://127.0.0.1:9/v1",
            secret="dummy-secret-not-real",
        )
    )
    s.models.add("fake", "fake-model-1", "fake-one")
    s.providers.use("fake")
    return tmp_path, user_home, s


def _start_chat(s, cwd: Path):
    return s.sessions.start(cwd).session


def test_fork_copies_state(env) -> None:
    tmp_path, _user_home, s = env
    cwd = tmp_path / "chatdir"
    cwd.mkdir()
    source = _start_chat(s, cwd)
    source.git_branch = "main"
    source.compact_state = {"goal": "ship it", "decisions": ["use uv"]}
    s.ctx.session_repo.update(source)

    started = s.sessions.fork(source.id)
    forked = started.session
    assert started.created is True
    assert forked.id != source.id
    assert forked.kind == source.kind
    assert forked.project_id == source.project_id
    assert forked.current_cwd == source.current_cwd
    assert forked.provider_id == source.provider_id
    assert forked.model_id == source.model_id
    assert forked.profile_id == source.profile_id
    assert forked.mode == source.mode
    assert forked.state == "active"
    assert forked.git_branch == "main"
    assert forked.compact_state == {"goal": "ship it", "decisions": ["use uv"]}
    assert forked.forked_from == source.id


def test_fork_copies_conversation_independently(env) -> None:
    tmp_path, _user_home, s = env
    cwd = tmp_path / "chatdir"
    cwd.mkdir()
    source = _start_chat(s, cwd)
    messages = [
        _msg(s, source.id, 1, "user", "fix the bug"),
        _msg(
            s,
            source.id,
            2,
            "assistant",
            "",
            tool_calls=[{"id": "tc1", "name": "fs.read", "arguments": {"path": "a"}}],
        ),
        _msg(s, source.id, 3, "tool", "file contents", tool_call_id="tc1", name="fs.read"),
    ]
    s.ctx.message_repo.append_many(source.id, messages)

    forked = s.sessions.fork(source.id).session
    copied = s.ctx.message_repo.list(forked.id)
    assert [m.seq for m in copied] == [1, 2, 3]
    assert [m.role for m in copied] == ["user", "assistant", "tool"]
    assert copied[1].tool_calls == [{"id": "tc1", "name": "fs.read", "arguments": {"path": "a"}}]
    assert copied[2].tool_call_id == "tc1"
    assert all(m.id != original.id for m, original in zip(copied, messages, strict=True))

    # the source keeps exactly its own messages, byte for byte
    original = s.ctx.message_repo.list(source.id)
    assert [m.id for m in original] == [m.id for m in messages]


def test_fork_preserves_provenance(env) -> None:
    tmp_path, _user_home, s = env
    cwd = tmp_path / "chatdir"
    cwd.mkdir()
    source = _start_chat(s, cwd)
    forked = s.sessions.fork(source.id).session
    assert forked.forked_from == source.id
    assert s.ctx.session_repo.get(forked.id).forked_from == source.id
    events = s.ctx.event_repo.list(forked.id)
    forked_events = [e for e in events if e.type == "SessionForked"]
    assert len(forked_events) == 1
    assert forked_events[0].payload == {"from": source.id}
    # provenance is not implied on normal sessions
    assert s.ctx.session_repo.get(source.id).forked_from is None


def test_fork_does_not_modify_source(env) -> None:
    tmp_path, _user_home, s = env
    cwd = tmp_path / "chatdir"
    cwd.mkdir()
    source = _start_chat(s, cwd)
    s.ctx.message_repo.append_many(source.id, [_msg(s, source.id, 1, "user", "hello")])
    before = s.ctx.session_repo.get(source.id)
    before_messages = s.ctx.message_repo.list(source.id)

    s.sessions.fork(source.id)

    after = s.ctx.session_repo.get(source.id)
    assert after.updated_at == before.updated_at
    assert after.title == before.title
    assert after.state == before.state
    assert after.forked_from is None
    assert [m.id for m in s.ctx.message_repo.list(source.id)] == [m.id for m in before_messages]


def test_fork_title_handling(env) -> None:
    tmp_path, _user_home, s = env
    cwd = tmp_path / "chatdir"
    cwd.mkdir()
    source = _start_chat(s, cwd)
    explicit = s.sessions.fork(source.id, title="alternate-approach").session
    assert explicit.title == "alternate-approach"
    default = s.sessions.fork(source.id).session
    assert default.title == f"{source.title} (fork)"


def test_forked_session_continues_independently(env) -> None:
    tmp_path, _user_home, s = env
    cwd = tmp_path / "chatdir"
    cwd.mkdir()
    source = _start_chat(s, cwd)
    s.ctx.message_repo.append_many(source.id, [_msg(s, source.id, 1, "user", "hello")])
    forked = s.sessions.fork(source.id).session

    # appending to the fork never reaches the source
    s.ctx.message_repo.append_many(forked.id, [_msg(s, forked.id, 1, "user", "continue?")])
    assert len(s.ctx.message_repo.list(forked.id)) == 2
    assert len(s.ctx.message_repo.list(source.id)) == 1

    # the fork can be resumed like any session (reconciliation included)
    resumed = s.sessions.resume(forked.id)
    assert resumed.created is False
    assert resumed.session.id == forked.id
    assert resumed.findings
    assert resumed.session.forked_from == source.id


def test_fork_unknown_ref_raises(env) -> None:
    _tmp_path, _user_home, s = env
    with pytest.raises(NotFoundError):
        s.sessions.fork("ses_doesnotexist")
