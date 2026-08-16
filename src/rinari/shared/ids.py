"""Deterministic, sortable IDs (ULID) with Rinari prefixes.

ULID: 48-bit millisecond timestamp + 80-bit randomness,
encoded as 26 Crockford base32 characters.
"""

from __future__ import annotations

import os
import threading

from rinari.shared.clock import Clock, SystemClock

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_RANDOM_BITS = 80
_VALUE_BITS = 128

PROVIDER = "prov"
MODEL = "mdl"
SESSION = "ses"
PROJECT = "prj"
EVENT = "evt"
CONFIG = "cfg"
CREDENTIAL = "cred"


class UlidGenerator:
    """Thread-safe ULID generator with per-millisecond monotonicity."""

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._lock = threading.Lock()
        self._last_ms = -1
        self._last_random = 0

    def next(self) -> str:
        with self._lock:
            now_ms = int(self._clock.now() * 1000)
            if now_ms == self._last_ms:
                random_part = (self._last_random + 1) & ((1 << _RANDOM_BITS) - 1)
            else:
                random_part = int.from_bytes(os.urandom(10), "big")
            self._last_ms = now_ms
            self._last_random = random_part
            return _encode((now_ms << _RANDOM_BITS) | random_part)


class IdGenerator:
    def __init__(self, clock: Clock | None = None) -> None:
        self._ulid = UlidGenerator(clock)

    def new(self, prefix: str) -> str:
        return f"{prefix}_{self._ulid.next()}"


def _encode(value: int) -> str:
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def decode_timestamp_ms(ulid: str) -> int:
    """Decode the 48-bit millisecond timestamp from a ULID string."""
    value = 0
    for ch in ulid.upper():
        value = (value << 5) | _ALPHABET.index(ch)
    return value >> (_VALUE_BITS - 48)
