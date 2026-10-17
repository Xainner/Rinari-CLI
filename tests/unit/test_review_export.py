# ruff: noqa: F811 (the server fixture comes from test_usage_report)
"""«Exportar para revisión» (plan 0.4.0 #79): no secret survives, text is optional."""

from __future__ import annotations

import io
import zipfile

from rinari.engine_protocol.review_export import review_files, zip_files
from rinari.storage.records import SessionMessageRecord
from tests.unit.test_usage_report import _call, _event, server  # noqa: F401

SECRET = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789ABCDEF"


def _seed(server, tmp_path):
    sid = _call(server, "session.create", {"cwd": str(tmp_path), "chat": True})["result"][
        "session"
    ]["id"]
    repo = server._services.ctx.message_repo
    repo.append_many(
        sid,
        [
            SessionMessageRecord(
                id=f"m-{index}",
                session_id=sid,
                seq=0,
                role=role,
                content=text,
                created_at=f"2026-10-17T10:0{index}:00Z",
            )
            for index, (role, text) in enumerate(
                [("user", "hola"), ("assistant", f"usa la clave {SECRET}"), ("user", "Entrega ya")]
            )
        ],
    )
    _event(
        server,
        sid,
        "tool.completed",
        {"tool": "shell.exec", "result": f"Authorization: Bearer {SECRET}"},
    )
    return sid


def test_review_package_has_no_secrets_and_can_leave_text_out(server, tmp_path) -> None:
    sid = _seed(server, tmp_path)
    files = review_files(server._services, {"session_id": sid})
    assert {"events.jsonl", "messages.jsonl", "usage.json", "entorno.json", "LEEME.md"} <= set(
        files
    )
    everything = "".join(files.values())
    assert SECRET not in everything
    assert "Entrega ya" in files["messages.jsonl"]

    quiet = review_files(server._services, {"session_id": sid, "include_messages": False})
    assert "messages.jsonl" not in quiet
    assert "[texto omitido]" in quiet["events.jsonl"]

    archive = zipfile.ZipFile(io.BytesIO(zip_files(files)))
    assert "LEEME.md" in archive.namelist()


def test_review_from_a_message_on(server, tmp_path) -> None:
    sid = _seed(server, tmp_path)
    got = _call(server, "session.review.export", {"session_id": sid, "from_seq": 3})
    assert got["ok"], got
    files = got["result"]["files"]
    assert "hola" not in files["messages.jsonl"] and "Entrega ya" in files["messages.jsonl"]


def test_review_from_the_turn_still_running(server, tmp_path) -> None:
    """#79: a message of the running turn has no stored number; its turn starts it."""
    from rinari.storage.records import SessionEventRecord

    sid = _seed(server, tmp_path)
    repo = server._services.ctx.event_repo
    repo.insert(
        SessionEventRecord(
            id="evt-live-start",
            session_id=sid,
            seq=repo.next_seq(sid),
            type="turn.started",
            payload={"message": "Entrega ya"},
            created_at="2026-10-17T10:02:00Z",
            turn_id="turn-live",
        )
    )
    got = _call(server, "session.review.export", {"session_id": sid, "from_turn_id": "turn-live"})
    assert got["ok"], got
    files = got["result"]["files"]
    assert "hola" not in files["messages.jsonl"] and "Entrega ya" in files["messages.jsonl"]
    bad = _call(server, "session.review.export", {"session_id": sid, "from_turn_id": 3})
    assert bad["error"]["code"] == "INVALID_PARAMS"
