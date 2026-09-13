"""Explicit visual-send consent scoped to a session and provider/model identity."""

import json

from rinari.shared.clock import now_iso
from rinari.storage.records import SessionEventRecord

EVENT = "vision.send.confirmed"


def confirmed(services, record):
    rows = services.ctx.db.query(
        "SELECT payload_json FROM session_events WHERE session_id=? AND type=?",
        (record.id, EVENT),
    )
    return any(
        json.loads(row["payload_json"])
        == {
            "provider_id": record.provider_id,
            "model_id": record.model_id,
        }
        for row in rows
    )


def confirm(services, record):
    if confirmed(services, record):
        return
    services.ctx.event_repo.insert(
        SessionEventRecord(
            id=services.ctx.ids.new("evt"),
            session_id=record.id,
            seq=services.ctx.event_repo.next_seq(record.id),
            type=EVENT,
            payload={"provider_id": record.provider_id, "model_id": record.model_id},
            created_at=now_iso(services.ctx.clock),
        )
    )
