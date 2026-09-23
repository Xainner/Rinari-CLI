"""A turn aggregate; callers serialize access with the turn activity lock."""

import time
from dataclasses import dataclass, field

from rinari.context.tokens import CHARS_PER_TOKEN


@dataclass
class TurnTokenTracker:
    clock: object = time.monotonic
    calls: dict = field(default_factory=dict)
    revision: int = 0
    last_emit: float = float("-inf")
    emitted_total: int | None = None
    last: dict | None = None
    settled: bool = False

    def observe(self, event, payload):
        if self.settled:
            return None
        key = payload["call_id"]
        call = self.calls.setdefault(
            key, {"input_tokens": 0, "output_tokens": 0, "source": "estimated"}
        )
        complete = event == "usage.call.completed"
        if event == "usage.call.started":
            # Same opaque key on retry or final router normalization replaces the estimate.
            if call["source"] == "estimated":
                call.update(input_tokens=payload["input_tokens"], output_tokens=0)
        else:
            call["output_tokens"] = max(0, payload.get("output_chars", 0) // CHARS_PER_TOKEN)
        if complete:
            usage = payload.get("usage") or {}
            reported = 0
            for name in ("input_tokens", "output_tokens"):
                value = usage.get(name)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    call[name] = value
                    reported += 1
            call["source"] = (
                "reported"
                if reported == 2 and usage.get("source") == "complete"
                else "mixed"
                if reported
                else "estimated"
            )
            for name in ("cached_input_tokens", "reasoning_tokens"):
                value = usage.get(name)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    call[name] = value
        phase = (
            "thinking"
            if event == "usage.call.started"
            else "streaming"
            if not complete
            else "thinking"
        )
        # Only visible deltas are throttled. A call boundary is at most two
        # updates per call, and throttling it lost the new call's input estimate:
        # a call starting right after the previous one completed stayed hidden
        # until its first delta, or until it finished if it did not stream.
        return self.snapshot(phase, force=event != "usage.call.delta")

    def snapshot(self, phase, *, force=False):
        if not self.calls:
            return None
        sources = {c["source"] for c in self.calls.values()}
        result = {
            name: sum(c.get(name, 0) for c in self.calls.values())
            for name in ("input_tokens", "output_tokens")
        }
        result.update(
            total_tokens=result["input_tokens"] + result["output_tokens"],
            model_calls=len(self.calls),
            phase=phase,
            source="reported"
            if sources == {"reported"}
            else "estimated"
            if sources == {"estimated"}
            else "mixed",
        )
        for name in ("cached_input_tokens", "reasoning_tokens"):
            known = [c[name] for c in self.calls.values() if name in c]
            if known:
                result[name] = sum(known)
        previous = {k: v for k, v in (self.last or {}).items() if k != "revision"}
        if result == previous:
            return None
        now = self.clock()
        self.revision += 1
        self.last = {**result, "revision": self.revision}
        if not force and (
            now - self.last_emit < 0.1 or result["total_tokens"] == self.emitted_total
        ):
            return None
        self.last_emit = now
        self.emitted_total = result["total_tokens"]
        return self.last

    def finish(self):
        self.settled = True
        return self.snapshot("settled", force=True)
