"""Tests para el rendering de streaming con rich."""

from rinari.render import DeltaAccumulator, strip_ansi


def test_accumulator_builds_text():
    acc = DeltaAccumulator()
    acc.add("Hola")
    acc.add(" mundo")
    assert acc.text == "Hola mundo"


def test_accumulator_handles_empty():
    acc = DeltaAccumulator()
    acc.add("")
    assert acc.text == ""


def test_strip_ansi_removes_codes():
    assert strip_ansi("\x1b[32mverde\x1b[0m") == "verde"


def test_accumulator_reset():
    acc = DeltaAccumulator()
    acc.add("a")
    acc.reset()
    assert acc.text == ""
    acc.add("b")
    assert acc.text == "b"
