"""Launch long work, end the turn, be woken when it ends (Ideas-2, 23-oct).

Processes started with notify=wake, and connector jobs left to job.watch,
start a new turn in their conversation when they finish after the turn.
"""

from __future__ import annotations

import sys
import time

import pytest

from rinari.application.job_watches import JobWatchService, WatchError
from rinari.engine_protocol.errors import EngineProtocolError
from rinari.engine_protocol.turns import MAX_WAKES_PER_HOUR
from rinari.tools.native.process import ProcessRegistry
from tests.unit.test_engine_turns import server, services  # noqa: F401  (fixtures)


def test_a_process_with_notify_wake_is_pending_once_it_ends() -> None:
    registry = ProcessRegistry()
    quiet = registry.start(f'"{sys.executable}" -c "raise SystemExit(0)"', notify="failure")
    loud = registry.start(f'"{sys.executable}" -c "raise SystemExit(3)"', notify="wake")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not registry.pending_wakes():
        registry.get(quiet)
        registry.get(loud)
        time.sleep(0.1)
    assert [handle.id for handle in registry.pending_wakes()] == [loud]
    assert registry.pending_wakes()[0].exit_code == 3


def _record_starts(server, monkeypatch, *, fail: Exception | None = None):  # noqa: F811
    started: list[dict] = []

    def start_turn(session_id, message, **kwargs):
        if fail is not None:
            raise fail
        started.append({"session_id": session_id, "message": message, **kwargs})
        return {"turn_id": f"turn_{len(started)}"}

    monkeypatch.setattr(server._turns, "start_turn", start_turn)
    return started


class _Ended:
    def __init__(self, handles):
        self._handles = handles

    def pending_wakes(self):
        return list(self._handles)


class _Handle:
    def __init__(self, name, code):
        self.id, self.name, self.exit_code = "proc_001", name, code


def test_a_process_that_ends_after_its_turn_wakes_the_conversation(
    server,  # noqa: F811
    monkeypatch,
) -> None:
    started = _record_starts(server, monkeypatch)
    server._turns._wake_after_exit("ses_w", _Ended([_Handle("render", 0)]))
    (turn,) = started
    assert turn["session_id"] == "ses_w"
    assert "render" in turn["message"] and "exit code 0" in turn["message"]
    assert turn["origin"] == {
        "kind": "automation",
        "source": "process_exit",
        "source_label": "render",
    }
    assert turn["memory_origin"] == "automation"
    # Nothing waiting: nothing starts.
    server._turns._wake_after_exit("ses_w", _Ended([]))
    assert len(started) == 1


def test_wakes_are_capped_per_hour_and_a_busy_conversation_is_left_for_later(
    server,  # noqa: F811
    monkeypatch,
) -> None:
    started = _record_starts(server, monkeypatch)
    origin = {"kind": "automation", "source": "job_done"}
    results = [server._turns.wake("ses_c", "m", origin, "m") for _ in range(MAX_WAKES_PER_HOUR + 1)]
    assert results[:MAX_WAKES_PER_HOUR] == ["started"] * MAX_WAKES_PER_HOUR
    assert results[-1] == "refused" and len(started) == MAX_WAKES_PER_HOUR
    monkeypatch.setattr(server._turns, "has_active_turn", lambda _sid: True)
    assert server._turns.wake("ses_b", "m", origin, "m") == "busy"
    from rinari.engine_protocol import errors

    _record_starts(server, monkeypatch, fail=EngineProtocolError(errors.SESSION_CLOSED, "closed"))
    monkeypatch.setattr(server._turns, "has_active_turn", lambda _sid: False)
    assert server._turns.wake("ses_x", "m", origin, "m") == "refused"


class _Client:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def call_tool(self, name, arguments=None):
        self.calls += 1
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


class _Mcp:
    def __init__(self, client, *, read_only=True, decision=None):
        self._client = client
        self.read_only = read_only
        self.decision = decision

    def show(self, name, scope="global"):
        return {"name": name, "enabled": True} if name == "lab" else None

    def reads_only(self, name, tool):
        return self.read_only

    def decision_for(self, name, tool):
        return self.decision

    def client(self, name, project=None):
        return self._client


def _text(value):
    return {"content": [{"type": "text", "text": value}]}


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _service(answers, **mcp):
    clock = _Clock()
    client = _Client(answers)
    service = JobWatchService(_Mcp(client, **mcp), clock=clock)
    service._ensure_thread = lambda: None  # ticks are driven by the test
    woken: list[tuple] = []
    service.wake = lambda *args: woken.append(args) or "started"
    return service, client, clock, woken


def test_a_watched_job_wakes_its_conversation_with_the_last_answer() -> None:
    service, client, clock, woken = _service(
        [_text('{"status": "running"}'), _text('{"status": "completed", "url": "u"}')]
    )
    watch = service.add(
        "ses_j",
        server="lab",
        tool="get_job",
        arguments={"job_id": "j1"},
        until={"path": "status", "in": ["completed", "failed"]},
        note="Send the song to the owner",
    )
    (live,) = service._watches.values()
    service.tick(live)
    assert not woken and client.calls == 1
    clock.now += 30
    service.tick(live)
    (session_id, message, origin, _display) = woken[0]
    assert session_id == "ses_j" and '"completed"' in message
    assert "Send the song to the owner" in message
    assert origin["source"] == "job_done" and origin["source_label"].startswith("Send the song")
    assert origin["external_content"] is True
    assert service.list("ses_j") == [] and watch["watch_id"] == live.id


def test_a_job_watch_only_polls_read_only_tools_the_owner_allows() -> None:
    service, *_ = _service([_text("x")], read_only=False)
    with pytest.raises(WatchError, match="read-only"):
        service.add("s", server="lab", tool="start_job", until={"text": "done"})
    service, *_ = _service([_text("x")], decision="ask")
    with pytest.raises(WatchError, match="ask or block"):
        service.add("s", server="lab", tool="get_job", until={"text": "done"})
    service, *_ = _service([_text("x")])
    with pytest.raises(WatchError, match="connector"):
        service.add("s", server="nadie", tool="get_job", until={"text": "done"})
    with pytest.raises(WatchError, match="until"):
        service.add("s", server="lab", tool="get_job", until={"path": "status"})


def test_a_job_watch_waits_for_a_busy_turn_and_gives_up_after_errors() -> None:
    service, _client, clock, woken = _service([_text("Completed!")])
    service.add("s", server="lab", tool="get_job", until={"text": ["completed"]})
    (watch,) = service._watches.values()
    service.wake = lambda *args: woken.append(args) or "busy"
    service.tick(watch)
    assert len(woken) == 1 and service.list("s")  # kept: tried again later
    service.wake = lambda *args: woken.append(args) or "started"
    clock.now += 10
    service.tick(watch)
    assert len(woken) == 2 and service.list("s") == []

    service, _client, clock, woken = _service([RuntimeError("down")])
    service.add("s", server="lab", tool="get_job", until={"text": "done"})
    (watch,) = service._watches.values()
    for _ in range(5):
        service.tick(watch)
        clock.now += 30
    assert woken and woken[0][2]["source"] == "job_failed"
    assert "RuntimeError: down" in woken[0][1]
