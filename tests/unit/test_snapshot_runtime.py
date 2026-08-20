"""Phase 7: RuntimeSnapshot (single source of truth for the CLI UI)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli import agent_runtime
from rinari.cli.agent_runtime import build_agent_session, run_turn
from rinari.cli.snapshot import (
    SessionUsage,
    build_snapshot,
    estimate_context_used,
)
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
        return ProviderCapabilities(streaming=False, tool_calls=True, max_context_tokens=200_000)

    def invoke(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.scripted.pop(0)


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


def _fake_session(env, monkeypatch) -> tuple:
    tmp_path, user_home, s = env
    record = s.sessions.start(tmp_path, forced_chat=True).session
    fake = FakeModel(scripted=[ModelResponse(content="hi", stop_reason=StopReason.END_TURN)])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: fake)
    session = build_agent_session(s, record, interactive=False, user_home=user_home)
    return s, record, session


def test_snapshot_session_fields(env, monkeypatch) -> None:
    _s, record, session = _fake_session(env, monkeypatch)
    try:
        snap = build_snapshot(session)
        assert snap.session_id == record.id
        assert snap.session_kind == "CHAT"
        assert snap.provider_alias == "fake"
        assert snap.provider_type == "openai"
        assert snap.model_alias == "fake-one"
        assert snap.provider_model_id == "fake-model-1"
        assert snap.tools_loaded > 0
        assert snap.context_window_tokens == 200_000
        assert snap.version
        data = snap.to_dict()
        assert data["provider"]["alias"] == "fake"
        assert data["model"]["provider_model_id"] == "fake-model-1"
    finally:
        session.end()


def test_snapshot_unknown_metrics_stay_none(env, monkeypatch) -> None:
    _, record, session = _fake_session(env, monkeypatch)
    try:
        snap = build_snapshot(session)
        # No pricing configured on the model: cost must stay unknown.
        assert snap.usage.cost_usd is None
        # Window known, nothing used yet: percent is the real 0 (not guessed).
        assert snap.context_percent == 0.0
        # CHAT without a detected project: project fields None (not guessed).
        assert snap.project_name is None
        assert record.kind == "CHAT"
    finally:
        session.end()


def test_usage_merge_none_safety() -> None:
    usage = SessionUsage()
    assert usage.tokens_total is None
    usage.merge_turn(
        None,
        tool_calls=2,
        model_calls=1,
        elapsed_delta_s=0.5,
        input_price_per_mtok=None,
        output_price_per_mtok=None,
    )
    assert usage.tokens_total is None
    assert usage.cost_usd is None
    assert usage.model_calls == 1
    assert usage.tool_calls == 2
    assert usage.elapsed_s == 0.5

    usage.merge_turn(
        Usage(input_tokens=100, output_tokens=10),
        tool_calls=0,
        model_calls=1,
        elapsed_delta_s=0.25,
        input_price_per_mtok=3.0,
        output_price_per_mtok=15.0,
    )
    assert usage.tokens_total == 110
    # cost appears only once pricing is real: 100/1M*3 + 10/1M*15
    assert usage.cost_usd == pytest.approx((100 / 1e6) * 3.0 + (10 / 1e6) * 15.0)


def test_usage_merge_accumulates_across_turns() -> None:
    usage = SessionUsage()
    for _ in range(2):
        usage.merge_turn(
            Usage(input_tokens=100, output_tokens=10, cached_input_tokens=5, reasoning_tokens=2),
            tool_calls=1,
            model_calls=1,
            elapsed_delta_s=0.5,
            input_price_per_mtok=None,
            output_price_per_mtok=None,
        )
    assert usage.input_tokens == 200
    assert usage.output_tokens == 20
    assert usage.cached_input_tokens == 10
    assert usage.reasoning_tokens == 4
    assert usage.model_calls == 2
    assert usage.tool_calls == 2
    assert usage.cost_usd is None


def test_estimate_context_used() -> None:
    class _Msg:
        content = "x" * 400
        tool_calls = ()

    class _Ctx:
        def __init__(self) -> None:
            self.history = [_Msg()]

    assert estimate_context_used(_Ctx()) == 100

    class _Empty:
        def __init__(self) -> None:
            self.history = []

    assert estimate_context_used(_Empty()) == 0

    class _Broken:
        history = None

    assert estimate_context_used(_Broken()) is None


def test_account_turn_updates_session_state(env, monkeypatch) -> None:
    _s, _record, session = _fake_session(env, monkeypatch)
    try:
        assert session.usage is not None
        run_turn(session, "hello")
        assert session.usage.model_calls >= 1
        assert session.usage.elapsed_s >= 0.0
        snap = build_snapshot(session)
        assert snap.usage.model_calls >= 1
        assert snap.usage.tokens_total is not None or snap.usage.model_calls >= 1
    finally:
        session.end()
