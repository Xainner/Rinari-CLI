"""One consumption base for preflight, status and CLI (review plan §C).

The desktop engine builds a fresh agent session for every turn, so the
provider-reported usage anchor that calibrates the estimate has to survive in
the session's events, tied to the model and the projection it measured.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli import agent_runtime
from rinari.cli.agent_runtime import build_agent_session, run_turn
from rinari.models.types import (
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    StopReason,
    Usage,
)


@dataclass
class FakeModel:
    scripted: list[ModelResponse]
    requests: list[ModelRequest] = field(default_factory=list)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=False, tool_calls=True, structured_output=True)

    def invoke(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.scripted.pop(0)


@pytest.fixture
def env(app_ctx, tmp_path, monkeypatch):
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
    cwd = tmp_path / "work"
    cwd.mkdir()
    record = s.sessions.start(cwd, forced_chat=True).session

    def session(*responses):
        fake = FakeModel(scripted=list(responses))
        monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)
        current = s.ctx.session_repo.get(record.id)
        return build_agent_session(s, current, interactive=False, user_home=user_home)

    return s, record, session


def answer(text="ok", input_tokens=None):
    usage = Usage(input_tokens=input_tokens, output_tokens=3) if input_tokens else None
    return ModelResponse(content=text, stop_reason=StopReason.END_TURN, usage=usage)


def test_the_reported_usage_anchor_survives_a_new_session_build(env):
    _s, _record, session = env
    first = session(answer(input_tokens=9000))
    run_turn(first, "hello")
    anchor = first.context.context_usage
    assert anchor["actual"] == 9000
    # The next turn builds a new session (the desktop engine does, every turn).
    second = session()
    assert second.context.context_usage == anchor


def test_a_projection_change_invalidates_the_anchor(env):
    s, record, session = env
    run_turn(session(answer(input_tokens=9000)), "hello")
    current = s.ctx.session_repo.get(record.id)
    s.ctx.session_repo.update(
        dataclasses.replace(
            current,
            compact_state={
                "projection_version": 1,
                "revision": 4,
                "summary": "earlier",
                "covered_message_ids": [],
            },
        )
    )
    assert session().context.context_usage == {}


def test_a_different_model_invalidates_the_anchor(env):
    s, record, session = env
    run_turn(session(answer(input_tokens=9000)), "hello")
    s.models.add("fake", "fake-model-2", "fake-two")
    current = s.ctx.session_repo.get(record.id)
    other = s.models.resolve("fake-two")
    s.ctx.session_repo.update(dataclasses.replace(current, model_id=other.id))
    assert session().context.context_usage == {}


def test_no_reported_usage_leaves_no_anchor(env):
    _s, _record, session = env
    run_turn(session(answer()), "hello")
    assert session().context.context_usage == {}


@pytest.fixture
def handlers(env, monkeypatch):
    """The engine's context handlers, with discovery offline (no network in tests)."""
    from types import SimpleNamespace

    from rinari.context import windows
    from rinari.engine_protocol.media import register_media

    monkeypatch.setattr(windows, "_discover", lambda ctx, main, provider: None)
    s, _record, _session = env
    registered = {}
    register_media(SimpleNamespace(register=registered.__setitem__), s)
    return registered


def test_session_status_is_rebuilt_from_what_the_engine_persisted(env, handlers):
    s, record, session = env
    run_turn(session(answer(input_tokens=9000)), "hello")
    status = handlers["context.status"]({"session_id": record.id})
    assert status["session_id"] == record.id
    assert status["last_request"]["input_tokens"] == 9000
    assert status["last_request"]["measurement"] == "reported"
    assert status["compact_at_tokens"] == int(status["usable_input_tokens"] * 0.80)
    assert status["target_tokens"] == int(status["usable_input_tokens"] * 0.60)
    assert status["projection_revision"] == 0
    assert status["last_compaction"] is None
    s.context._persist_event(
        record.id,
        "governor.compact",
        {
            "compaction_id": "c1",
            "reason": "automatic",
            "status": "completed",
            "used_tokens": 9100,
            "after_tokens": 4000,
            "duration_ms": 1200,
            "checks": {"structure": "passed", "records": "passed", "reduction": "passed"},
        },
    )
    last = handlers["context.status"]({"session_id": record.id})["last_compaction"]
    assert last["status"] == "completed"
    assert (last["used_tokens"], last["after_tokens"], last["duration_ms"]) == (9100, 4000, 1200)
    assert last["checks"]["records"] == "passed"


def test_the_cli_meter_and_the_engine_status_share_one_capacity(env, handlers):
    from rinari.cli.snapshot import build_snapshot

    _s, record, session = env
    current = session()
    try:
        snapshot = build_snapshot(current)
    finally:
        current.end()
    status = handlers["context.status"]({"session_id": record.id})
    assert snapshot.context_window_tokens == status["usable_input_tokens"]


def test_context_models_answers_for_every_model_even_if_one_fails(env, handlers, monkeypatch):
    from rinari.context import settings

    s, _record, _session = env
    broken = s.models.add("fake", "fake-model-2", "fake-two")
    resolve = settings.window

    def window(ctx, caller):
        if caller.model_id == broken.id:
            raise RuntimeError("metadata unreadable")
        return resolve(ctx, caller)

    monkeypatch.setattr(settings, "window", window)
    rows = {row["model_alias"]: row for row in handlers["context.models"]({})["models"]}
    assert rows["fake-one"]["window_tokens"] == 128_000
    assert rows["fake-two"]["error"] == "metadata unreadable"
