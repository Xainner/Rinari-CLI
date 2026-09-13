"""Probe the real turn-to-tool owner-source wiring."""

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.cli.agent_runtime import build_agent_session, run_turn
from rinari.models.types import ModelResponse, StopReason, ToolCall
from tests.unit.test_conversation_persistence import FakeModel


def test_live_turn_attaches_current_owner_message_to_memory_tool(app_ctx, tmp_path):
    user_home = tmp_path / "home"
    user_home.mkdir()
    services = build_services(app_ctx, user_home=user_home)
    services.providers.add(
        AddProviderInput(
            alias="fake",
            provider_type="openai",
            endpoint="http://127.0.0.1:9/v1",
            secret="synthetic-secret",
        )
    )
    services.models.add("fake", "fake-model", "fake")
    services.providers.use("fake")
    cwd = tmp_path / "work"
    cwd.mkdir()
    record = services.sessions.start(cwd, forced_chat=True).session
    text = "Prefiero respuestas breves"
    caller = FakeModel(
        scripted=[
            ModelResponse(
                content="",
                stop_reason=StopReason.TOOL_CALLS,
                tool_calls=(
                    ToolCall(
                        id="remember-1",
                        name="memory.remember",
                        arguments={"scope": "user", "topic": "longitud", "text": text},
                    ),
                ),
            ),
            ModelResponse(content="Listo", stop_reason=StopReason.END_TURN),
        ]
    )
    session = build_agent_session(
        services,
        record,
        interactive=False,
        user_home=user_home,
        model_caller=caller,
    )

    run_turn(session, text, turn_id="owner-turn")

    tool_messages = [message for message in caller.requests[-1].messages if message.role == "tool"]
    assert len(tool_messages) == 1
    assert '"ok": true' in tool_messages[0].content
    assert "validated owner-message source" not in tool_messages[0].content
