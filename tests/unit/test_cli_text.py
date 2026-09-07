from __future__ import annotations

import io

from rinari.cli.text import choice, supports_unicode


def test_terminal_choice_falls_back_for_cp1252() -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")
    assert supports_unicode(stream) is False
    assert choice("✓", "OK", stream) == "OK"


def test_terminal_choice_keeps_unicode_for_utf8() -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="utf-8")
    assert supports_unicode(stream) is True
    assert choice("✓", "OK", stream) == "✓"
