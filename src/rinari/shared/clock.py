"""Deterministic clocks for IDs, timestamps, and tests."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...


class SystemClock:
    def now(self) -> float:
        return time.time()


class FakeClock:
    """Advance by a fixed step on every read; explicit advance for tests."""

    def __init__(self, start: float = 1_700_000_000.0, step: float = 0.0) -> None:
        self._t = start
        self._step = step

    def now(self) -> float:
        t = self._t
        self._t += self._step
        return t

    def advance(self, seconds: float) -> None:
        self._t += seconds

    @property
    def current(self) -> float:
        return self._t


def iso_utc(ts: float) -> str:
    """RFC 3339 UTC with millisecond precision."""
    dt = datetime.fromtimestamp(ts, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{dt.microsecond // 1000:03d}Z"


def now_iso(clock: Clock) -> str:
    return iso_utc(clock.now())
