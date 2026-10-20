"""«Exportar para revisión» (plan 0.4.0 #79): a conversation, or from one
message on, as files to analyze — never its secrets.

The Engine returns the conversation's part (events, messages if asked, the
checklist, the usage report and the environment) as named text files; the
desktop adds the app's logs of those hours and writes the .zip. The CLI
writes the same files to a .zip by itself (``rinari session export --review``).
"""

from __future__ import annotations

import io
import json
import platform
import zipfile
from pathlib import Path
from typing import Any

from rinari import __version__
from rinari.engine_protocol.errors import INVALID_PARAMS, EngineProtocolError
from rinari.shared.clock import now_iso
from rinari.shared.redaction import redact_text

#: The user's folder in paths becomes this, so a package does not say who you are.
USER_PLACEHOLDER = "<usuario>"


def register_review_export(dispatcher: Any, services: Any) -> None:
    dispatcher.register(
        "session.review.export", lambda params: {"files": review_files(services, params or {})}
    )


def _clean(text: str, home: str | None) -> str:
    text = redact_text(text)
    if home:
        text = text.replace(home, str(Path(home).parent / USER_PLACEHOLDER))
        text = text.replace(
            home.replace("\\", "/"), str(Path(home).parent / USER_PLACEHOLDER).replace("\\", "/")
        )
        text = text.replace(
            home.replace("\\", "\\\\"),
            str(Path(home).parent / USER_PLACEHOLDER).replace("\\", "\\\\"),
        )
    return text


def review_files(services: Any, params: dict[str, Any]) -> dict[str, str]:
    session_id = params.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise EngineProtocolError(INVALID_PARAMS, "Param 'session_id' is required.")
    from_seq = params.get("from_seq")
    if from_seq is not None and (
        not isinstance(from_seq, int) or isinstance(from_seq, bool) or from_seq < 0
    ):
        raise EngineProtocolError(INVALID_PARAMS, "Param 'from_seq' must be an int >= 0.")
    # A message of the turn still running has no stored number yet: its turn
    # says where to start (#79).
    from_turn_id = params.get("from_turn_id")
    if from_turn_id is not None and (not isinstance(from_turn_id, str) or not from_turn_id):
        raise EngineProtocolError(INVALID_PARAMS, "Param 'from_turn_id' must be a string.")
    include_messages = params.get("include_messages", True)
    if not isinstance(include_messages, bool):
        raise EngineProtocolError(INVALID_PARAMS, "Param 'include_messages' must be a boolean.")
    anonymize = params.get("anonymize", True) is not False
    record = services.sessions.show(session_id)
    home = str(Path.home()) if anonymize else None

    # From one message on: its time is the start of everything else.
    since: str | None = None
    messages = services.ctx.message_repo.list(record.id)
    if from_seq is not None:
        start = next((message for message in messages if message.seq >= from_seq), None)
        since = start.created_at if start else None
        messages = [message for message in messages if message.seq >= from_seq]
    elif from_turn_id is not None:
        started = [
            str(event.created_at)
            for event in services.ctx.event_repo.list_for_turns(record.id, [from_turn_id])
        ]
        since = min(started) if started else None
        if since is not None:
            messages = [message for message in messages if str(message.created_at) >= since]
    events = [
        event
        for event in services.ctx.event_repo.list(record.id)
        if since is None or str(event.created_at) >= since
    ]

    def line(value: dict[str, Any]) -> str:
        return _clean(json.dumps(value, ensure_ascii=False, default=str), home)

    files: dict[str, str] = {}
    files["events.jsonl"] = "\n".join(
        line(
            {
                "seq": event.seq,
                "type": event.type,
                "turn_id": event.turn_id,
                "at": event.created_at,
                "payload": event.payload if include_messages else _without_text(event.payload),
            }
        )
        for event in events
    )
    if include_messages:
        files["messages.jsonl"] = "\n".join(
            line(
                {
                    "seq": message.seq,
                    "role": message.role,
                    "at": message.created_at,
                    "turn_id": message.turn_id,
                    "content": message.content,
                    "tool_calls": message.tool_calls,
                    "name": message.name,
                    # Tells the owner's words from runtime notes.
                    "origin": message.origin,
                }
            )
            for message in messages
        )
    checklist = getattr(services, "checklist", None)
    if checklist is not None:
        try:
            current = checklist.get(record.id)
            if current is not None:
                files["checklist.json"] = line(current.as_dict())
        except Exception:
            pass
    try:
        from rinari.engine_protocol.usage_report import build_report

        files["usage.json"] = line(build_report(services, "session", record.id))
    except Exception:
        pass
    first_at = events[0].created_at if events else None
    last_at = events[-1].created_at if events else None
    files["entorno.json"] = line(
        {
            "exported_at": now_iso(services.ctx.clock),
            "engine_version": __version__,
            "os": f"{platform.system()} {platform.release()}",
            "python": platform.python_version(),
            "session": {
                "id": record.id,
                "kind": record.kind,
                "mode": record.mode,
                "provider_id": record.provider_id,
                "model_id": record.model_id,
                "permission_profile": getattr(record, "permission_profile", None),
                "project": bool(record.project_id),
            },
            "from_seq": from_seq,
            "first_event_at": first_at,
            "last_event_at": last_at,
            "includes_messages": include_messages,
        }
    )
    files["LEEME.md"] = _readme(record.id, include_messages, from_seq, first_at, last_at)
    return files


_TEXT_KEYS = {
    "content",
    "message",
    "delta",
    "text",
    "final_response",
    "observation",
    "arguments",
    "result",
}


def _without_text(payload: Any) -> Any:
    """Without what was said: the events keep their shape, not the words."""
    if isinstance(payload, dict):
        return {
            key: (
                "[texto omitido]"
                if key in _TEXT_KEYS and isinstance(value, str)
                else _without_text(value)
            )
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [_without_text(item) for item in payload]
    return payload


def _readme(
    session_id: str, messages: bool, from_seq: int | None, first: str | None, last: str | None
) -> str:
    lines = [
        "# Paquete para revisión de Rinari",
        "",
        f"Conversación: {session_id}",
        f"Desde: {'el mensaje ' + str(from_seq) if from_seq is not None else 'el principio'}",
        f"Intervalo: {first or '—'} → {last or '—'}",
        "",
        "Incluye:",
        "- events.jsonl: los eventos de la conversación (herramientas, errores, uso).",
    ]
    if messages:
        lines.append("- messages.jsonl: el texto de los mensajes (la conversación completa).")
    else:
        lines.append("- Sin el texto de los mensajes: los eventos lo dicen como «[texto omitido]».")
    lines += [
        "- usage.json: el informe de uso.",
        "- checklist.json: la lista de pasos.",
        "- entorno.json: versiones, sistema, proveedor y modelo por id, permisos.",
        "- logs/: los registros de la app y del motor de esas horas (si se pidieron).",
        "",
        "Siempre se quitan las claves, los tokens y las cabeceras de autorización,",
        "y la carpeta de usuario se escribe como <usuario>.",
    ]
    return "\n".join(lines) + "\n"


def zip_files(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()
