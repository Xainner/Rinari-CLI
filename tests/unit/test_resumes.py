"""Continue by itself when a provider limit resets (plan 0.4.0 #24)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from rinari.cli import agent_runtime
from rinari.engine_protocol.resume import CONTINUE_MESSAGE
from rinari.models.types import ModelRequest, ModelResponse, ProviderCapabilities, StopReason
from rinari.providers.errors import ProviderError, ProviderErrorCode, classify_http_error
from rinari.providers.resets import from_cli_text, resets_at
from rinari.resumes.service import MAX_ATTEMPTS
from tests.unit import test_checklist as base
from tests.unit.test_checklist import _call

services = base.services
server = base.server

NOW = datetime(2026, 10, 16, 20, 0, tzinfo=UTC)


# -- when the limit resets ------------------------------------------------------------


def test_the_reset_time_comes_from_the_body_first() -> None:
    body = {"error": {"type": "usage_limit_reached", "resets_in_seconds": 600}}
    assert resets_at(payload=body, now=NOW) == (NOW + timedelta(seconds=600)).isoformat()
    epoch = {"error": {"resets_at": int((NOW + timedelta(hours=2)).timestamp())}}
    assert resets_at(payload=epoch, now=NOW) == (NOW + timedelta(hours=2)).isoformat()


def test_headers_give_the_latest_of_their_resets() -> None:
    headers = {
        "x-ratelimit-reset-requests": "1s",
        "x-ratelimit-reset-tokens": "6m0s",
        "Retry-After": "30",
    }
    assert resets_at(headers=headers, now=NOW) == (NOW + timedelta(minutes=6)).isoformat()
    anthropic = {"anthropic-ratelimit-tokens-reset": "2026-10-16T20:45:00Z"}
    assert resets_at(headers=anthropic, now=NOW) == "2026-10-16T20:45:00+00:00"


def test_the_claude_cli_text_gives_the_next_such_time() -> None:
    local = NOW.astimezone()
    moment = from_cli_text("You've hit your limit · resets 5pm", NOW)
    expected = local.replace(hour=17, minute=0, second=0, microsecond=0)
    if expected <= local:
        expected += timedelta(days=1)
    assert moment == expected.astimezone(UTC)
    zoned = from_cli_text("resets 5pm (America/Costa_Rica)", NOW)
    assert zoned is not None and zoned > NOW
    assert from_cli_text("no time here", NOW) is None


def test_a_429_keeps_its_reset_time() -> None:
    url = "https://api.openai.com/v1/chat/completions"
    response = httpx.Response(
        429,
        headers={"x-ratelimit-reset-requests": "12m0s"},
        json={"error": {"message": "Rate limit reached", "code": "rate_limit_exceeded"}},
        request=httpx.Request("POST", url),
    )
    error = classify_http_error(response, url)
    assert error.error_code is ProviderErrorCode.RATE_LIMIT
    moment = datetime.fromisoformat(error.details["resets_at"])
    assert timedelta(minutes=11) < moment - datetime.now(UTC) <= timedelta(minutes=12, seconds=5)


# -- the service ------------------------------------------------------------------------


@pytest.fixture
def resumes(services):
    return services.resumes


@pytest.fixture
def session_id(services, tmp_path):
    return services.sessions.start(tmp_path).session.id


def test_the_setting_asks_by_default(resumes):
    assert resumes.mode() == "ask"
    assert resumes.set_mode("always") == "always"
    assert resumes.mode() == "always"
    with pytest.raises(ValueError):
        resumes.set_mode("sometimes")


def test_plan_uses_the_time_or_polls_a_balance(resumes):
    now = NOW.timestamp()
    mode, at = resumes.plan({"resets_at": (NOW + timedelta(minutes=30)).isoformat()}, now)
    assert mode == "at" and at == pytest.approx(now + 30 * 60 + 10)
    mode, at = resumes.plan({"provider_error_code": "QUOTA_EXHAUSTED"}, now)
    assert mode == "poll" and at == now + 600
    mode, _ = resumes.plan({}, now, usage_reset=(NOW + timedelta(hours=1)).isoformat())
    assert mode == "at"


def test_rearming_stops_after_the_last_attempt(resumes, session_id):
    resumes.arm(session_id, provider_id=None, mode="at", resume_at=1.0)
    for _ in range(MAX_ATTEMPTS - 1):
        assert (
            resumes.rearm(session_id, mode="at", resume_at=2.0, reason="again").state == "waiting"
        )
    assert resumes.rearm(session_id, mode="at", resume_at=3.0, reason="again").state == "failed"


def test_a_resume_left_more_than_12_hours_behind_only_says_so(resumes, session_id):
    now = NOW.timestamp()
    resumes.arm(session_id, provider_id=None, mode="at", resume_at=now - 13 * 3600)
    assert [r.state for r in resumes.expire_stale(now)] == ["expired"]
    resumes.arm(session_id, provider_id=None, mode="at", resume_at=now - 3600)
    assert resumes.expire_stale(now) == []


# -- through the Engine --------------------------------------------------------------------


class _Limited:
    """First call: a rate limit that resets soon; then normal answers."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.requests: list[ModelRequest] = []
        self.calls = 0

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(streaming=False, tool_calls=True, structured_output=True)

    def invoke(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.requests.append(request)
        if self.calls == 1:
            error = ProviderError(
                "Provider returned HTTP 429: rate limited",
                code=ProviderErrorCode.RATE_LIMIT,
                retryable=False,
            )
            error.details["limit_kind"] = "rate"
            # Far enough that the real scheduler never fires before the test's
            # own tick on a slow machine (the Windows CI).
            error.details["resets_at"] = (datetime.now(UTC) + timedelta(seconds=10)).isoformat()
            raise error
        return ModelResponse(content=self.answers.pop(0), stop_reason=StopReason.END_TURN)


def _wait(server, until, timeout=20.0):
    deadline = time.time() + timeout
    seen = []
    while time.time() < deadline:
        seen.extend(server.drain_events())
        if until(seen):
            return seen
        time.sleep(0.05)
    raise AssertionError("did not happen")


def test_always_arms_holds_the_queue_and_continues_when_due(server, tmp_path, monkeypatch):
    server._services.resumes.set_mode("always")
    session_id = _call(server, "session.create", {"cwd": str(tmp_path), "chat": True})["result"][
        "session"
    ]["id"]
    model = _Limited(["Sigo donde quedé.", "Respuesta a la cola."])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: model)
    _call(server, "session.turn.start", {"session_id": session_id, "message": "haz algo"})
    _wait(server, lambda seen: any(e.get("event") == "turn.failed" for e in seen))
    # The resume is armed as the turn ends: the event can arrive first.
    deadline = time.time() + 10
    resume = None
    while resume is None and time.time() < deadline:
        resume = _call(server, "session.resume.get", {"session_id": session_id})["result"]["resume"]
        time.sleep(0.05)
    assert resume["state"] == "waiting" and resume["mode"] == "at"

    # A queued message waits with the resume instead of hitting the same limit.
    _call(server, "session.queue.add", {"session_id": session_id, "message": "y luego esto"})
    time.sleep(0.3)
    assert model.calls == 1

    later = time.time() + 30
    assert server._resume.tick(now=later) == [session_id]
    seen = _wait(
        server,
        lambda seen: sum(e.get("event") == "turn.completed" for e in seen) >= 1,
    )
    started = [e for e in seen if e.get("event") == "turn.started"]
    assert started[0]["payload"]["origin"]["source"] == "auto_resume"
    assert any(m.content == CONTINUE_MESSAGE for m in model.requests[1].messages)
    resume = _call(server, "session.resume.get", {"session_id": session_id})["result"]["resume"]
    assert resume["state"] == "done"


def test_ask_waits_for_the_owner_and_cancel_releases(server, tmp_path, monkeypatch):
    session_id = _call(server, "session.create", {"cwd": str(tmp_path), "chat": True})["result"][
        "session"
    ]["id"]
    model = _Limited(["Sigo."])
    monkeypatch.setattr(agent_runtime, "_caller_for", lambda services, rec: model)
    _call(server, "session.turn.start", {"session_id": session_id, "message": "haz algo"})
    _wait(server, lambda seen: any(e.get("event") == "turn.failed" for e in seen))
    assert (
        _call(server, "session.resume.get", {"session_id": session_id})["result"]["resume"] is None
    )
    armed = _call(server, "session.resume.arm", {"session_id": session_id})["result"]["resume"]
    assert armed["state"] == "waiting"
    cancelled = _call(server, "session.resume.cancel", {"session_id": session_id})["result"]
    assert cancelled["resume"]["state"] == "cancelled"
