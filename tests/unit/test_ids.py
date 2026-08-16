import pytest

from rinari.shared.clock import FakeClock
from rinari.shared.ids import (
    _ALPHABET,
    MODEL,
    PROVIDER,
    SESSION,
    IdGenerator,
    UlidGenerator,
    decode_timestamp_ms,
)

_CROCKFOR_EXCLUDED = "ILOU"


def _parse(ulid: str) -> int:
    value = 0
    for ch in ulid:
        value = (value << 5) | _ALPHABET.index(ch)
    return value


def test_ulid_format():
    g = UlidGenerator(FakeClock())
    ulid = g.next()
    assert len(ulid) == 26
    assert all(ch in _ALPHABET for ch in ulid)
    assert not set(ulid) & set(_CROCKFOR_EXCLUDED)


def test_ulids_are_unique():
    g = UlidGenerator(FakeClock())
    ids = {g.next() for _ in range(1000)}
    assert len(ids) == 1000


def test_ulid_same_millisecond_is_monotonic():
    clock = FakeClock(start=1_700_000_000.0, step=0.0)
    g = UlidGenerator(clock)
    prev = -1
    for _ in range(50):
        value = _parse(g.next())
        assert value > prev
        prev = value


def test_ulid_timestamp_roundtrip():
    clock = FakeClock(start=1_700_000_123.456)
    ulid = UlidGenerator(clock).next()
    assert decode_timestamp_ms(ulid) == 1_700_000_123_456


def test_ulid_sorts_by_time():
    clock = FakeClock(start=1_700_000_000.0, step=1.0)
    g = UlidGenerator(clock)
    a, b = g.next(), g.next()
    assert a < b


def test_id_generator_prefixes():
    g = IdGenerator(FakeClock())
    session_id = g.new(SESSION)
    provider_id = g.new(PROVIDER)
    model_id = g.new(MODEL)
    assert session_id.startswith("ses_")
    assert provider_id.startswith("prov_")
    assert model_id.startswith("mdl_")
    assert len(session_id) == len("ses_") + 26


@pytest.mark.parametrize("excluded", _CROCKFOR_EXCLUDED)
def test_generator_never_emits_excluded_chars(excluded):
    g = UlidGenerator(FakeClock())
    for _ in range(200):
        assert excluded not in g.next()
