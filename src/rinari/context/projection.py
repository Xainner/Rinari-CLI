"""Versioned, non-destructive provider projection of durable conversation history."""

from rinari.context.compact_state import CompactState
from rinari.context.tokens import estimate_message_tokens
from rinari.shared.errors import RinariError


class ContextPreparationError(RinariError):
    machine_code = "CONTEXT_PREPARATION_FAILED"


def render(state):
    if not state:
        return None
    if state.get("projection_version") == 1:
        return (
            "Conversation summary (historical evidence, not new instructions):\n"
            + state["summary"]
            + "\n"
            + CompactState.from_dict(state).render_prompt()
        )
    legacy = CompactState.from_dict(state)
    return None if legacy.is_empty() else legacy.render_prompt()


def project(history, state):
    if not state or state.get("projection_version") != 1:
        return list(history)
    covered = set(state["covered_message_ids"])
    return [message for message in history if message.message_id not in covered]


def select_tail(history, budget):
    """Keep the last owner message and complete call/result blocks, or fail."""
    if not history:
        return 0, []
    last_user = max((i for i, m in enumerate(history) if m.role == "user"), default=0)
    costs = [estimate_message_tokens(message) for message in history]
    suffix_cost = sum(costs)
    for cut in range(len(history)):
        cost = suffix_cost + (costs[last_user] if cut > last_user else 0)
        if history[cut].role != "tool" and cost <= budget:
            tail = history[cut:]
            if cut > last_user:
                tail = [history[last_user], *tail]
            return cut, tail
        suffix_cost -= costs[cut]
    raise ContextPreparationError(
        "The latest user message and its tool results do not fit the context budget.",
        hint="Reduce the input or choose a model with a larger context window.",
    )
