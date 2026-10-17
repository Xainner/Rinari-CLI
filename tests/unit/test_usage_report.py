"""`/usage` (plan 0.4.0 #78): the report built from stored events, no model call."""

from __future__ import annotations

import itertools
import json

import pytest

from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.engine_protocol.server import EngineServer
from rinari.models.types import Usage
from rinari.storage.records import SessionEventRecord

_IDS = itertools.count()


@pytest.fixture
def server(app_ctx, tmp_path):
    user_home = tmp_path / "home"
    user_home.mkdir()
    services = build_services(app_ctx, user_home=user_home)
    services.providers.add(
        AddProviderInput(
            alias="fake",
            provider_type="openai",
            endpoint="http://127.0.0.1:9/v1",
            secret="dummy-secret-not-real",
        )
    )
    services.models.add("fake", "fake-model-1", "fake-one")
    services.providers.use("fake")
    engine = EngineServer(services)
    yield engine
    engine.close()


def _call(server, method, params=None):
    line = {"id": f"u-{next(_IDS)}", "method": method, "params": params or {}}
    return server.handle_line(json.dumps(line))


def _event(server, session_id, kind, payload, turn_id=None):
    repo = server._services.ctx.event_repo
    repo.insert(
        SessionEventRecord(
            id=f"evt-u-{next(_IDS)}",
            session_id=session_id,
            seq=repo.next_seq(session_id),
            type=kind,
            payload=payload,
            created_at="2026-10-17T10:00:00Z",
            turn_id=turn_id,
        )
    )


def _invoked(model, inp, out, cached=0, write=None, tools=None):
    usage = {"input_tokens": inp, "output_tokens": out, "cached_input_tokens": cached}
    if write is not None:
        usage["cache_write_tokens"] = write
    return {"context_anchor": {"model": model}, "usage": usage, "tool_calls": tools or []}


def test_report_groups_by_model_and_warns(server, tmp_path) -> None:
    created = _call(server, "session.create", {"cwd": str(tmp_path), "chat": True})
    sid = created["result"]["session"]["id"]
    _event(server, sid, "turn.started", {"occurred_at": "2026-10-17T10:00:00Z"}, "t1")
    _event(server, sid, "ModelInvoked", _invoked("m-main", 30_000, 500, cached=20_000, write=900))
    _event(server, sid, "ModelInvoked", _invoked("m-main", 1_000, 0))
    _event(server, sid, "ModelInvoked", _invoked("m-sub", 2_000, 100, tools=[{"id": "x"}]))
    _event(server, sid, "ToolCompleted", {"tool": "fs.read", "ok": True})
    _event(server, sid, "ToolCompleted", {"tool": "fs.read", "ok": True})
    _event(server, sid, "ToolFailed", {"tool": "shell.exec", "ok": False})
    for _ in range(3):
        _event(server, sid, "VisualPixelsDelivered", {"sources": [{"images": [{"sha256": "abc"}]}]})
    _event(server, sid, "governor.compact", {})
    _event(server, sid, "turn.completed", {"occurred_at": "2026-10-17T10:00:42Z"}, "t1")

    got = _call(server, "usage.report", {"scope": "session", "ref": sid})
    assert got["ok"], got
    report = got["result"]["report"]
    main = next(row for row in report["models"] if row["model_id"] == "m-main")
    assert main["calls"] == 2 and main["input_tokens"] == 31_000
    assert main["cache_read_tokens"] == 20_000 and main["cache_write_tokens"] == 900
    assert main["cache_percent"] == 65 and main["cost"] is None
    assert report["totals"]["calls"] == 3
    assert report["tools"] == {
        "total": 3,
        "ok": 2,
        "failed": 1,
        "top": [{"tool": "fs.read", "count": 2}, {"tool": "shell.exec", "count": 1}],
    }
    assert report["compactions"] == 1
    kinds = {warning["kind"] for warning in report["warnings"]}
    assert {"image_resent", "empty_answers"} <= kinds
    assert report["time"]["active_ms"] == 42_000

    saved = _call(server, "usage.report", {"scope": "today", "save": True, "session_id": sid})
    assert saved["ok"], saved
    listed = _call(server, "usage.report.list", {"session_id": sid})["result"]["reports"]
    assert [item["scope"] for item in listed] == ["today"]
    bad = _call(server, "usage.report", {"scope": "week"})
    assert bad["error"]["code"] == "INVALID_PARAMS"


def test_anthropic_reports_the_cache_it_writes() -> None:
    from rinari.providers.adapters.anthropic import _usage_from_anthropic

    usage = _usage_from_anthropic(
        {
            "input_tokens": 10,
            "output_tokens": 2,
            "cache_read_input_tokens": 5,
            "cache_creation_input_tokens": 7,
        }
    )
    assert usage == Usage(
        input_tokens=10, output_tokens=2, cached_input_tokens=5, cache_write_tokens=7
    )
