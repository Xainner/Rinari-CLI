"""Ajustes → Avanzado (plan 0.4.0 #96): model execution and the turn's limits."""

from __future__ import annotations

import json

from . import test_engine_turns as base
from .test_engine_turns import _req

services = base.services
server = base.server


_ids = iter(range(1, 10_000))


def _call(server, method, params=None):
    return server.handle_line(_req(f"a{next(_ids)}", method, params))


def _limit(result, key):
    return next(row for row in result["limits"] if row["key"] == key)


def test_execution_has_its_own_pair_with_defaults(server) -> None:
    got = _call(server, "execution.settings.get")
    assert got["ok"], got
    assert got["result"]["defaults"]["timeouts"]["first_byte"] == 120.0
    assert got["result"]["settings"]["max_concurrency"] == 8
    saved = _call(
        server,
        "execution.settings.set",
        {"settings": {"max_concurrency": 2, "providers": {}, "models": {"m": 4096}}},
    )
    assert saved["ok"], saved
    assert saved["result"]["settings"]["max_concurrency"] == 2
    home = server._services.ctx.home
    stored = json.loads((home / "model-execution.json").read_text(encoding="utf-8"))
    assert stored["models"] == {"m": 4096}
    # The vision settings are not touched.
    assert not (home / "vision.json").exists()
    bad = _call(server, "execution.settings.set", {"settings": {"max_concurrency": 0}})
    assert not bad["ok"]
    assert (
        json.loads((home / "model-execution.json").read_text(encoding="utf-8"))["max_concurrency"]
        == 2
    )


def test_limits_show_value_default_and_range(server) -> None:
    got = _call(server, "advanced.settings.get")
    assert got["ok"], got
    row = _limit(got["result"], "runtime.emergency.max_model_calls")
    assert row == {
        "key": "runtime.emergency.max_model_calls",
        "type": "int",
        "value": 500,
        "default": 500,
        "min": 1,
        "max": 100_000,
    }
    assert _limit(got["result"], "agents.enabled")["value"] is True


def test_a_limit_changes_for_the_next_turn_and_null_puts_it_back(server) -> None:
    changed = _call(
        server,
        "advanced.settings.set",
        {"values": {"agents.max_total": 20, "runtime.safeguards.loop_detection": False}},
    )
    assert changed["ok"], changed
    assert _limit(changed["result"], "agents.max_total")["value"] == 20
    config = server._services.ctx.config.config
    assert config.agents.max_total == 20 and config.runtime.safeguards.loop_detection is False
    back = _call(server, "advanced.settings.set", {"values": {"agents.max_total": None}})
    assert _limit(back["result"], "agents.max_total")["value"] == 12
    assert server._services.ctx.config.config.agents.max_total == 12


def test_a_bad_limit_is_refused_and_nothing_is_written(server) -> None:
    out_of_range = _call(server, "advanced.settings.set", {"values": {"agents.max_depth": 99}})
    assert not out_of_range["ok"] and "max_depth" in out_of_range["error"]["message"]
    other = _call(server, "advanced.settings.set", {"values": {"model": "x"}})
    assert not other["ok"] and "Not a limit setting" in other["error"]["message"]
    assert server._services.ctx.config.config.agents.max_depth == 2


def test_storage_usage_measures_each_part_of_the_home(server) -> None:
    home = server._services.ctx.home
    (home / "workspaces" / "chat_1").mkdir(parents=True, exist_ok=True)
    (home / "workspaces" / "chat_1" / "a.txt").write_bytes(b"x" * 1000)
    (home / "logs").mkdir(exist_ok=True)
    (home / "logs" / "engine.log").write_bytes(b"y" * 300)
    (home / "context-window-abc.json").write_text("{}", encoding="utf-8")
    got = _call(server, "storage.usage")
    assert got["ok"], got
    sections = {row["id"]: row for row in got["result"]["sections"]}
    assert sections["workspaces"]["bytes"] >= 1000 and sections["workspaces"]["files"] >= 1
    assert sections["logs"]["bytes"] >= 300
    assert sections["cache"]["files"] >= 1
    assert got["result"]["total"] == sum(row["bytes"] for row in got["result"]["sections"])
