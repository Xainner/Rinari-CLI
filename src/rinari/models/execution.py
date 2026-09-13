"""Installation-local model execution policy, shared by all callers and routes."""

import json
import threading
from collections import deque
from contextlib import contextmanager
from pathlib import Path

DEFAULT_CONCURRENCY = 8  # Client scheduling policy, never a claim about server slots.
_guard = threading.RLock()
_gates = {}


def policy(home):
    path = Path(home) / "model-execution.json" if home else None
    return (
        validate(json.loads(path.read_text(encoding="utf-8")))
        if path and path.exists()
        else {"max_concurrency": DEFAULT_CONCURRENCY, "providers": {}, "models": {}}
    )


def validate(value):
    if not isinstance(value, dict) or any(
        not isinstance(value.get(k, {}), dict) for k in ("providers", "models")
    ):
        raise ValueError("Model execution settings must contain provider/model maps")
    result = {
        "max_concurrency": value.get("max_concurrency", DEFAULT_CONCURRENCY),
        "providers": value.get("providers", {}),
        "models": value.get("models", {}),
    }
    values = [result["max_concurrency"], *result["providers"].values(), *result["models"].values()]
    if any(type(v) is not int or v < 1 for v in values):
        raise ValueError(
            "Execution concurrency and configured output tokens must be positive integers"
        )
    return result


def destination_limit(home, provider_id):
    value = policy(home)
    return value.get("providers", {}).get(
        provider_id, value.get("max_concurrency", DEFAULT_CONCURRENCY)
    )


class Gate:
    def __init__(self):
        self.condition = threading.Condition()
        self.active = 0
        self.queue = deque()

    @contextmanager
    def acquire(self, limit, check):
        ticket = object()
        with self.condition:
            self.queue.append(ticket)
            try:
                while self.queue[0] is not ticket or self.active >= limit:
                    check()
                    self.condition.wait(0.05)
                check()
                self.queue.popleft()
                self.active += 1
                self.condition.notify_all()
            except BaseException:
                self.queue.remove(ticket)
                self.condition.notify_all()
                raise
        try:
            yield
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()


@contextmanager
def destination_slot(home, provider_id, check=lambda: None):
    key = (str(Path(home).resolve()) if home else "", provider_id)
    with _guard:
        gate = _gates.setdefault(key, Gate())
    with gate.acquire(destination_limit(home, provider_id), check):
        yield
