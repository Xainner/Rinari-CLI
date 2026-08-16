from rinari.shared.clock import FakeClock, SystemClock, iso_utc, now_iso


def test_fake_clock_advances_by_step():
    clock = FakeClock(start=100.0, step=1.0)
    values = [clock.now() for _ in range(3)]
    assert values == [100.0, 101.0, 102.0]


def test_fake_clock_explicit_advance():
    clock = FakeClock(start=0.0, step=0.0)
    clock.advance(5.5)
    assert clock.now() == 5.5
    assert clock.current == 5.5


def test_iso_utc_format():
    assert iso_utc(1_700_000_000.123456) == "2023-11-14T22:13:20.123Z"


def test_system_clock_is_monotonic_enough():
    clock = SystemClock()
    a, b = clock.now(), clock.now()
    assert b >= a


def test_now_iso_uses_injected_clock():
    assert now_iso(FakeClock(start=1_700_000_000.5)) == "2023-11-14T22:13:20.500Z"
