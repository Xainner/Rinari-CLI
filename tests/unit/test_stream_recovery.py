"""Terminal integrity and bounded streaming, with no network or external actions."""

import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from rinari.models.types import ChatMessage, ModelRequest, StopReason, ToolCall
from rinari.providers.adapters.http import iter_model_lines, open_model_stream
from rinari.providers.adapters.responses import _ResponsesStreamAccumulator
from rinari.runtime.cancellation import CancellationToken
from rinari.runtime.durable_history import DurableHistory, complete_tool_pairs, recover_legacy_turns
from rinari.shared.errors import CancelledError, NetworkError, ProviderModelError
from rinari.tools.definition import ToolResult


@pytest.mark.parametrize("status", ["failed", "cancelled", "incomplete"])
def test_responses_failure_retains_partial_and_never_returns_tools(status):
    acc = _ResponsesStreamAccumulator()
    acc.update({"type": "response.output_text.delta", "delta": "avance ñ"}, lambda _: None)
    acc.update(
        {
            "type": f"response.{status}",
            "response": {
                "id": "r1",
                "output": [
                    {
                        "type": "function_call",
                        "name": "shell.exec",
                        "call_id": "t1",
                        "arguments": "{}",
                    },
                ],
            },
        },
        lambda _: None,
    )
    with pytest.raises(ProviderModelError) as err:
        acc.finalize("https://fake.test")
    assert err.value.details["partial_text"] == "avance ñ"
    assert err.value.details["response_id"] == "r1"


def test_responses_length_is_truncated_and_eof_is_not_completion():
    acc = _ResponsesStreamAccumulator()
    acc.update(
        {
            "type": "response.incomplete",
            "response": {
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [{"type": "function_call", "name": "shell.exec", "arguments": "{}"}],
            },
        },
        lambda _: None,
    )
    result = acc.finalize("fake")
    assert result.stop_reason == StopReason.MAX_TOKENS and not result.tool_calls
    with pytest.raises(NetworkError, match="terminal"):
        _ResponsesStreamAccumulator().finalize("fake")


@pytest.mark.parametrize("family", ["chat", "anthropic"])
def test_nonstream_length_also_discards_executable_calls(family):
    from rinari.providers.adapters.anthropic import _response_from_anthropic
    from rinari.providers.adapters.openai_compatible import _response_from_openai

    if family == "chat":
        result = _response_from_openai(
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {
                            "content": "partial",
                            "tool_calls": [
                                {
                                    "id": "c",
                                    "function": {
                                        "name": "shell.exec",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        },
                    }
                ]
            },
            "fake",
        )
    else:
        result = _response_from_anthropic(
            {
                "stop_reason": "max_tokens",
                "content": [
                    {"type": "tool_use", "id": "c", "name": "shell.exec", "input": {}},
                ],
            },
            "fake",
        )
    assert result.stop_reason == StopReason.MAX_TOKENS
    assert not result.tool_calls


def test_duplicate_event_is_not_duplicate_text():
    acc = _ResponsesStreamAccumulator()
    seen = []
    event = {"type": "response.output_text.delta", "sequence_number": 2, "delta": "hola"}
    acc.update(event, seen.append)
    acc.update(event, seen.append)
    assert seen == ["hola"]


class TimedStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = threading.Event()

    def __iter__(self):
        for delay, chunk in self.chunks:
            if self.closed.wait(delay):
                return
            yield chunk

    def close(self):
        self.closed.set()


def request(**timeouts):
    return ModelRequest(
        model="test",
        messages=(),
        stream_timeouts={
            "connect": 1,
            "first_byte": 1,
            "idle": 1,
            "total": 2,
            **timeouts,
        },
    )


@pytest.mark.parametrize(
    "phase,chunks,limits",
    [
        ("first_byte", [(0.3, b"data\n")], {"first_byte": 0.08}),
        ("between_chunks", [(0, b"a\n"), (0.3, b"b\n")], {"idle": 0.08}),
        ("total", [(0.02, b": heartbeat\n")] * 30, {"total": 0.12, "idle": 0.08}),
    ],
)
def test_distinct_wait_phases_close_transport(phase, chunks, limits):
    stream = TimedStream(chunks)
    response = httpx.Response(200, stream=stream)
    with pytest.raises(NetworkError) as err:
        list(iter_model_lines(response, request(**limits), time.monotonic()))
    assert err.value.details["phase"] == phase
    assert stream.closed.is_set()


def test_utf8_fragments_and_buffered_lines_survive_waits():
    response = httpx.Response(
        200,
        stream=TimedStream(
            [
                (0, b"data: espa\xc3"),
                (0.01, b"\xb1ol\r\n\n"),
                (0, b"tail"),
            ]
        ),
    )
    assert list(iter_model_lines(response, request(), time.monotonic())) == [
        "data: español",
        "",
        "tail",
    ]


def test_cancel_during_silence_closes_stream():
    token = CancellationToken()
    stream = TimedStream([(5, b"never")])
    timer = threading.Timer(0.05, token.cancel)
    timer.start()
    try:
        with pytest.raises(CancelledError):
            list(
                iter_model_lines(
                    httpx.Response(200, stream=stream),
                    replace(request(), cancellation=token),
                    time.monotonic(),
                )
            )
    finally:
        timer.join()
    assert stream.closed.is_set()


def test_abandoned_headers_close_eventual_response_without_retry():
    stream = TimedStream([])
    calls = []

    def handler(req):
        calls.append(req)
        time.sleep(0.2)
        return httpx.Response(200, stream=stream)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with (
            pytest.raises(NetworkError) as err,
            open_model_stream(
                client, request(first_byte=0.06), time.monotonic(), "POST", "https://fake.test"
            ),
        ):
            pytest.fail("late response accepted")
        assert err.value.details["phase"] == "response_headers"
        assert stream.closed.wait(1)
    assert len(calls) == 1


def test_missing_tool_result_is_unknown_projection_without_replay():
    original = [
        ChatMessage.assistant("", (ToolCall("c1", "shell.exec", {}),)),
        ChatMessage.user("continua"),
    ]
    projected = complete_tool_pairs(original)
    assert len(original) == 2 and len(projected) == 3
    assert json.loads(projected[1].content)["outcome"] == "unknown"
    assert complete_tool_pairs(projected) == projected


def test_compaction_does_not_persist_recovery_projections_as_new_turn_messages():
    projection = complete_tool_pairs(
        [
            ChatMessage.assistant("", (ToolCall("c1", "shell.exec", {}),)),
        ]
    )
    saved = []

    def save(message):
        saved.append(message)
        return message

    history = DurableHistory(projection, save)
    history.append(ChatMessage.user("continue"))
    kept = list(history)
    history.clear()
    history.extend(kept)
    assert len(saved) == 1 and saved[0].role == "user"


def test_legacy_recovery_uses_evidence_only_and_stable_ids():
    records = [SimpleNamespace(role="user", turn_id="t")]
    pairs = [
        ("turn.started", {"turn_id": "t"}),
        (
            "ToolRequested",
            {"tool_call_id": "c", "tool": "shell.exec", "arguments": {"argv": ["x"]}},
        ),
        ("tool.completed", {"tool_call_id": "c", "presentation": {"exit_code": 127}}),
        ("turn.failed", {}),
    ]
    events = [{"type": kind, "payload_json": json.dumps(data)} for kind, data in pairs]
    recovered = recover_legacy_turns(records, events)["t"]
    assert json.loads(recovered[1].content)["evidence"]["legacy_presentation"]["exit_code"] == 127
    assert recovered[0].message_id == recover_legacy_turns(records, events)["t"][0].message_id
    assert len(records) == 1 and len(events) == 4


@pytest.mark.parametrize("code,status", [(0, "exited_zero"), (127, "failed"), (255, "failed")])
def test_large_process_observation_preserves_exit_status(code, status):
    text = ToolResult(ok=True, data={"stdout": "x" * 9000, "exit_code": code}).to_model_text(
        "shell.exec"
    )
    data = json.loads(text)
    assert data["process_status"] == status and data["data"]["exit_code"] == code
    assert data["task_verified"] is False


def test_cli_stream_timeouts_persist_and_inherit(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from rinari.cli.main import app
    from rinari.models.execution import policy

    monkeypatch.setenv("RINARI_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(app, ["vision", "execution", "--first-byte", "240", "--idle", "90"])
    assert result.exit_code == 0, result.output
    assert policy(tmp_path)["timeouts"] == {"first_byte": 240, "idle": 90}
    result = runner.invoke(app, ["vision", "execution", "--inherit-timeouts"])
    assert result.exit_code == 0 and policy(tmp_path)["timeouts"] == {}


@pytest.mark.parametrize(
    "value",
    [
        {"timeouts": {"idle": 0}},
        {"provider_timeouts": []},
        {"timeouts": {"total": float("inf")}},
        {"timeouts": {"bad": 2}},
    ],
)
def test_timeout_settings_reject_invalid_values(value):
    from rinari.models.execution import validate

    with pytest.raises(ValueError):
        validate(value)


def test_crash_after_completion_uses_durable_observation_scoped_to_turn():
    from types import SimpleNamespace

    from rinari.runtime.durable_history import complete_tool_pairs, completed_observations

    first = ChatMessage.assistant("", (ToolCall(id="same", name="fs.read", arguments={}),))
    second = ChatMessage.assistant("", (ToolCall(id="same", name="fs.read", arguments={}),))
    records = [
        SimpleNamespace(id=first.message_id, turn_id="t1"),
        SimpleNamespace(id=second.message_id, turn_id="t2"),
    ]
    events = [
        {
            "type": "tool.completed",
            "payload_json": json.dumps(
                {
                    "turn_id": turn,
                    "tool_call_id": "same",
                    "observation": json.dumps({"data": value}),
                }
            ),
        }
        for turn, value in [("t1", "FIRST"), ("t2", "SECOND")]
    ]
    restored = complete_tool_pairs([first, second], completed_observations(records, events))
    assert json.loads(restored[1].content)["data"] == "FIRST"
    assert json.loads(restored[3].content)["data"] == "SECOND"
