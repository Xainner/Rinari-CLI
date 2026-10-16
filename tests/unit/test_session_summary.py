"""session.summary: what «Resumen» cannot gather by itself (plan 0.4.0 #82)."""

from __future__ import annotations

import json
import time

from rinari.engine_protocol.summary import kind_of
from tests.unit import test_checklist as base
from tests.unit.test_checklist import _call

services = base.services
server = base.server


def _session(server, cwd) -> str:
    return _call(server, "session.create", {"cwd": str(cwd), "chat": True})["result"]["session"][
        "id"
    ]


def _tool(server, session_id, call_id, tool, arguments, data, ok=True):
    turns = server._turns
    turns.emit_persisted_activity(
        "tool.requested",
        session_id=session_id,
        turn_id="turn_x",
        payload={"tool": tool, "tool_call_id": call_id, "arguments": json.dumps(arguments)},
    )
    turns.emit_persisted_activity(
        "tool.completed",
        session_id=session_id,
        turn_id="turn_x",
        payload={
            "tool": tool,
            "tool_call_id": call_id,
            "ok": ok,
            "observation": json.dumps({"ok": ok, "tool": tool, "data": data}),
        },
    )


def test_kinds_come_from_the_mime_or_the_extension():
    assert kind_of("a.PNG") == "image"
    assert kind_of("x", "video/mp4") == "video"
    assert kind_of("informe.docx") == "document"
    assert kind_of("main.py") == "file"


def test_sources_results_and_mcp_by_connector(server, tmp_path):
    session_id = _session(server, tmp_path)
    picture = tmp_path / "logo.png"
    picture.write_bytes(b"png")
    _tool(server, session_id, "c1", "web.search", {"queries": ["qwen 4 27b", "qwen 4"]}, {})
    _tool(
        server,
        session_id,
        "c2",
        "web.fetch",
        {"url": "https://qwen.ai/blog"},
        {"title": "Qwen 4", "final_url": "https://qwen.ai/blog/qwen4"},
    )
    _tool(server, session_id, "c3", "web.fetch", {"url": "https://x.dev"}, {}, ok=False)
    _tool(server, session_id, "c4", "mcp.Visual_Lab.generate_image", {"prompt": "kart"}, {})
    _tool(server, session_id, "c5", "mcp.Visual_Lab.get_job", {"id": "1"}, {})
    _tool(server, session_id, "c6", "fs.write", {"path": str(picture)}, {})
    _tool(server, session_id, "c7", "fs.write", {"path": str(tmp_path / "main.py")}, {})
    _tool(
        server,
        session_id,
        "c8",
        "web.download",
        {"url": "https://cdn/x"},
        {"path": str(tmp_path / "clip"), "content_type": "video/mp4"},
    )
    summary = _call(server, "session.summary", {"session_id": session_id})["result"]
    sources = summary["sources"]
    assert [p["url"] for p in sources["pages"]] == ["https://qwen.ai/blog/qwen4"]
    assert sources["pages"][0]["title"] == "Qwen 4"
    assert [s["query"] for s in sources["searches"]] == ["qwen 4", "qwen 4 27b"]
    assert sources["mcp"] == [
        {
            "connector": "Visual_Lab",
            "calls": 2,
            "tools": ["generate_image", "get_job"],
            "at": sources["mcp"][0]["at"],
        }
    ]
    results = summary["results"]
    assert [(r["name"], r["kind"], r["exists"]) for r in results] == [
        ("clip", "video", False),
        ("logo.png", "image", True),
    ]
    assert summary["connectors"] == {"mcp": [], "skills": []}
    assert summary["background"] == {"processes": [], "scheduled": []}


def test_the_project_scheduled_tasks_show_in_background(server, tmp_path):
    root = tmp_path / "tienda"
    root.mkdir()
    project, _ = server._services.projects.add(root)
    session_id = _call(server, "session.create", {"cwd": str(root)})["result"]["session"]["id"]
    assert server._services.sessions.show(session_id).project_id == project.id
    tasks = server._schedule.tasks
    tasks.create(
        {
            "name": "Revisar pedidos",
            "kind": "reminder",
            "schedule": {"kind": "daily", "time": "09:00"},
            "prompt": "revisa",
            "project_id": project.id,
        }
    )
    tasks.create(
        {
            "name": "Otra cosa",
            "kind": "reminder",
            "schedule": {"kind": "daily", "time": "10:00"},
            "prompt": "x",
        }
    )
    summary = _call(server, "session.summary", {"session_id": session_id})["result"]
    assert [t["name"] for t in summary["background"]["scheduled"]] == ["Revisar pedidos"]


def test_a_finished_turn_announces_the_change_once(server, tmp_path):
    session_id = _session(server, tmp_path)
    server.drain_events()
    for _ in range(3):
        _tool(server, session_id, f"m{_}", "mcp.Lab.get_job", {}, {})
    deadline = time.time() + 5
    changed = []
    while time.time() < deadline and not changed:
        changed = [e for e in server.drain_events() if e.get("event") == "session.summary.changed"]
        time.sleep(0.05)
    assert len(changed) == 1 and changed[0]["payload"] == {
        "session_id": session_id,
        "reason": "tool",
    }
