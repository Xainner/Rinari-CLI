"""Session export/import (commands.md 30/55/56, SESSION_EXPORT_VERSION = "1").

The document is a plain JSON file. It contains the session record, the full
message history, and the event trace. Secrets never appear: only references.
"""

from __future__ import annotations

import json
from pathlib import Path

from rinari.shared.errors import ConfigurationError, InvalidUsageError

EXPORT_VERSION = "1"


def export_session(s, ref: str | None) -> dict:
    if ref is None:
        records = s.ctx.session_repo.list(limit=1)
        if not records:
            raise InvalidUsageError("No sessions to export")
        record = records[0]
    else:
        record = s.sessions.show(ref)
    messages = s.ctx.message_repo.list(record.id)
    events = s.ctx.event_repo.list(record.id)
    return {
        "version": EXPORT_VERSION,
        "session": {
            "id": record.id,
            "kind": record.kind,
            "title": record.title,
            "project_root": record.project_root_snapshot,
            "created_cwd": record.created_cwd,
            "current_cwd": record.current_cwd,
            "provider_id": record.provider_id,
            "model_id": record.model_id,
            "profile_id": record.profile_id,
            "mode": record.mode,
            "state": record.state,
            "created_at": record.created_at,
            "forked_from": record.forked_from,
            "git_branch": record.git_branch,
            "active_skills": list(record.active_skills or ()),
        },
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                "tool_call_id": m.tool_call_id,
                "name": m.name,
                "tool_calls": [
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                    for tc in (m.tool_calls or ())
                ],
                "created_at": m.created_at,
            }
            for m in messages
        ],
        "events": [
            {"seq": e.seq, "type": e.type, "payload": e.payload, "created_at": e.created_at}
            for e in events
        ],
    }


def import_session(s, text: str):
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidUsageError(f"Not a valid JSON document: {exc}") from exc
    version = document.get("version")
    if version != EXPORT_VERSION:
        raise ConfigurationError(
            f"Unsupported export version: {version!r}",
            hint=f"Current import version: {EXPORT_VERSION}.",
        )
    data = document.get("session") or {}
    from rinari.models.types import ToolCall
    from rinari.shared.clock import now_iso
    from rinari.storage.records import SessionMessageRecord, SessionRecord

    now = now_iso(s.ctx.clock)
    record = SessionRecord(
        id=s.ctx.ids.new("ses"),
        kind=data.get("kind", "CHAT"),
        title=data.get("title") or "imported session",
        project_id=None,
        project_root_snapshot=data.get("project_root"),
        created_cwd=data.get("created_cwd") or data.get("current_cwd") or str(Path.cwd()),
        current_cwd=data.get("current_cwd") or data.get("created_cwd") or str(Path.cwd()),
        provider_id=data.get("provider_id"),
        model_id=data.get("model_id"),
        profile_id=data.get("profile_id") or "workspace",
        mode=data.get("mode") or "auto",
        state="idle",
        compact_state=None,
        created_at=data.get("created_at") or now,
        updated_at=now,
        last_active_at=now,
        git_branch=data.get("git_branch"),
        forked_from=data.get("forked_from"),
        active_skills=tuple(data.get("active_skills") or ()),
    )
    s.ctx.session_repo.insert(record)
    messages = []
    for i, m in enumerate(document.get("messages") or [], start=1):
        tool_calls = tuple(
            ToolCall(
                id=tc.get("id", ""), name=tc.get("name", ""), arguments=tc.get("arguments") or {}
            )
            for tc in (m.get("tool_calls") or [])
        )
        messages.append(
            SessionMessageRecord(
                id=s.ctx.ids.new("msg"),
                session_id=record.id,
                seq=i,
                role=m.get("role", "user"),
                content=m.get("content", ""),
                tool_calls=tool_calls or None,
                tool_call_id=m.get("tool_call_id"),
                name=m.get("name"),
                created_at=m.get("created_at") or now,
            )
        )
    if messages:
        s.ctx.message_repo.append_many(record.id, messages)
    return record


__all__ = ["EXPORT_VERSION", "export_session", "import_session"]
