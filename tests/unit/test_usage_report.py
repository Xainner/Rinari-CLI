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
    # Input is everything the model read: 10 new, 5 from the cache, 7 written to it.
    assert usage == Usage(
        input_tokens=22, output_tokens=2, cached_input_tokens=5, cache_write_tokens=7
    )


def test_the_snapshot_keeps_who_the_context_and_every_providers_limits(server, tmp_path):
    """#78: client, context and plan limits are taken when the report is made."""
    created = _call(server, "session.create", {"cwd": str(tmp_path), "chat": True})
    sid = created["result"]["session"]["id"]
    services = server._services
    services.providers.add(
        AddProviderInput(
            alias="otro",
            provider_type="openai",
            endpoint="http://127.0.0.1:9/v2",
            secret="dummy-secret-not-real",
        )
    )
    other = services.providers.get("otro")
    asked: list[str] = []

    def limits_of(provider_id):
        asked.append(provider_id)
        if provider_id == other.id:
            return {
                "status": "available",
                "product_id": "opencode-go",
                "windows": [{"id": "rolling", "used_percent": 40.0}],
                "balances": [],
            }
        return {"status": "unsupported", "detail": "not exposed", "windows": [], "balances": []}

    from rinari.engine_protocol.usage_report import usage_report

    report = usage_report(
        services,
        {"scope": "session", "ref": sid, "app_version": "0.4.0-test"},
        limits_of=limits_of,
        context_of=lambda session_id: {
            "window_tokens": 200_000,
            "last_request": {"input_tokens": 50_000},
            "compact_at_tokens": 160_000,
        },
    )["report"]
    assert report["client"]["app_version"] == "0.4.0-test"
    assert report["client"]["engine_version"]
    assert (report["client"]["provider"], report["client"]["model"]) == ("fake", "fake-one")
    assert report["context"] == {
        "window_tokens": 200_000,
        "used_tokens": 50_000,
        "compact_at_tokens": 160_000,
        "last_compaction_at": None,
    }
    # The conversation's provider always (with why it says nothing), then the
    # others that report something.
    assert [(item["provider_alias"], item["conversation"]) for item in report["limits"]] == [
        ("fake", True),
        ("otro", False),
    ]
    assert report["limits"][0]["detail"] == "not exposed"
    assert len(asked) == len(set(asked))

    from rinari.engine_protocol.usage_report import render_text

    text = render_text(report)
    assert "Rinari Agent 0.4.0-test · Engine" in text
    assert "  fake: not reported (not exposed)" in text
    assert "  otro · rolling: 40% used" in text
    assert "  50,000 of 200,000 tokens (25%) · 0 compactions" in text


def test_cost_uses_the_model_price_or_the_catalogs_with_the_cache_rates(monkeypatch):
    from rinari.engine_protocol import usage_report as module
    from rinari.providers import metadata

    entry = {
        "input_tokens": 1_000_000,
        "cache_read_tokens": 600_000,
        "cache_write_tokens": 100_000,
        "output_tokens": 100_000,
        "_settings": {"input_price_per_mtok": 3, "output_price_per_mtok": 15},
    }
    # Own prices without cache rates: the cache at the input rate (an upper bound).
    assert module._cost(None, dict(entry)) == 3 + 1.5

    monkeypatch.setattr(
        metadata,
        "catalog_prices",
        lambda provider, model_id: {
            "input": 3,
            "output": 15,
            "cache_read": 0.3,
            "cache_write": 3.75,
        },
    )
    from_catalog = {**entry, "_settings": {}, "_provider": object(), "_provider_model_id": "m"}
    assert module._cost(None, from_catalog) == round(0.9 + 0.18 + 0.375 + 1.5, 4)
    assert module._cost(None, {**entry, "_settings": {}}) is None


def test_the_catalog_gives_prices_for_a_known_model() -> None:
    from types import SimpleNamespace

    from rinari.providers.metadata import catalog_prices

    anthropic = SimpleNamespace(type="anthropic", endpoint=None, settings={}, auth_method="api-key")
    prices = catalog_prices(anthropic, "claude-sonnet-4-5")
    assert prices is not None and prices["input"] > 0 and prices["output"] > prices["input"]
    assert catalog_prices(anthropic, "no-such-model") is None


def test_large_outputs_still_in_the_context_are_a_warning(server, tmp_path) -> None:
    created = _call(server, "session.create", {"cwd": str(tmp_path), "chat": True})
    sid = created["result"]["session"]["id"]
    big = "x" * 30_000
    _event(server, sid, "tool.completed", {"tool": "shell.exec", "observation": big})
    _event(server, sid, "governor.compact", {})
    # Before the compaction: gone from the context. After it: still there.
    _event(server, sid, "tool.completed", {"tool": "fs.read", "observation": big + "yy"})
    _event(server, sid, "tool.completed", {"tool": "fs.read", "observation": "small"})
    report = _call(server, "usage.report", {"scope": "session", "ref": sid})["result"]["report"]
    assert {"kind": "large_outputs", "count": 1, "max": 30_002} in report["warnings"]
