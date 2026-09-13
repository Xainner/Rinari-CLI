"""Write-through conversation boundaries and safe provider projection after interruption."""

from collections.abc import Callable, Iterable

from rinari.models.types import ChatMessage


class DurableHistory(list[ChatMessage]):
    def __init__(self, messages: Iterable[ChatMessage], save: Callable[[ChatMessage], ChatMessage]):
        super().__init__(messages)
        self.save = save
        self.owner_seen = False
        # Compaction clears/extends the same list. Initial entries may be
        # recovery projections, which must never become newly authored records.
        self.known_ids = {message.message_id for message in self}

    def append(self, message: ChatMessage) -> None:
        if message.message_id not in self.known_ids:
            message = self.save(message)
            self.known_ids.add(message.message_id)
        super().append(message)

    def extend(self, messages: Iterable[ChatMessage]) -> None:
        for message in messages:
            self.append(message)


def complete_tool_pairs(messages: Iterable[ChatMessage]) -> list[ChatMessage]:
    """Do not replay unresolved calls. Supply explicit unknown-outcome observations."""
    result = []
    pending = {}

    def finish():
        for call in pending.values():
            result.append(
                ChatMessage.tool_result(
                    call.id,
                    call.name,
                    '{"ok":false,"outcome":"unknown","message":"Previous turn was interrupted. '
                    "No durable result exists for this call. It may not have started, or may have "
                    "had external effects. Verify existing state before attempting it again; "
                    'do not automatically repeat this action."}',
                )
            )
        pending.clear()

    for message in messages:
        if message.role != "tool":
            finish()
        else:
            pending.pop(message.tool_call_id, None)
        result.append(message)
        for call in message.tool_calls:
            pending[call.id] = call
    finish()
    return result


def recover_legacy_turns(records, events):
    """Read-only projection of tool evidence for old failed turns missing messages.

    No inferred execution: unknown results remain unknown. Original events survive.
    """
    import json
    import uuid

    from rinari.models.types import ToolCall
    from rinari.tools.definition import ToolResult

    turns_with_assistant = {r.turn_id for r in records if r.role == "assistant"}
    grouped = {}
    current = None
    for row in events:
        try:
            data = json.loads(row["payload_json"])
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        if row["type"] == "turn.started":
            current = data.get("turn_id")
        if current and current not in turns_with_assistant:
            grouped.setdefault(current, []).append((row["type"], data))
    recovered = {}
    for turn, entries in grouped.items():
        if not any(kind in {"turn.failed", "turn.cancelled"} for kind, _ in entries):
            continue
        requests = {}
        results = {}
        for kind, data in entries:
            call = data.get("tool_call_id")
            if kind == "ToolRequested" and call and isinstance(data.get("arguments"), dict):
                requests[call] = data
            if kind in {"tool.completed", "tool.failed", "tool.cancelled"} and call:
                results[call] = data
        messages = []
        for call, data in requests.items():
            name = data.get("tool")
            if not name:
                continue

            def mid(suffix, turn=turn, call=call):
                return uuid.uuid5(uuid.NAMESPACE_URL, turn + call + suffix).hex

            messages.append(
                ChatMessage(
                    role="assistant",
                    content="",
                    message_id=mid("request"),
                    tool_calls=(ToolCall(id=call, name=name, arguments=data["arguments"]),),
                )
            )
            outcome = results.get(call, {})
            evidence = outcome.get("presentation")
            if evidence is None:
                evidence = {
                    "outcome": "unknown",
                    "message": "No recoverable result. Verify state before repeating this action.",
                }
            encoded = json.dumps(evidence, ensure_ascii=False)
            if len(encoded) > ToolResult.OBSERVATION_INLINE_BUDGET:
                evidence = {
                    "preview": encoded[: ToolResult.OBSERVATION_INLINE_BUDGET],
                    "truncated": True,
                    "exit_code": evidence.get("exit_code") if isinstance(evidence, dict) else None,
                    "notice": "Full recorded presentation remains in the session timeline.",
                }
            messages.append(
                ChatMessage(
                    role="tool",
                    name=name,
                    tool_call_id=call,
                    message_id=mid("result"),
                    content=json.dumps(
                        {
                            "recovered_from": "persisted activity event",
                            "evidence": evidence,
                            "note": (
                                "Historical evidence, not fresh verification. "
                                "No action was replayed."
                            ),
                        },
                        ensure_ascii=False,
                    ),
                )
            )
        if messages:
            recovered[turn] = messages
    return recovered
