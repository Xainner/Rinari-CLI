"""A provider stream that only sends heartbeats has stalled (audit 21-oct): a turn
hung on «Respondiendo…» because keep-alive bytes kept resetting the idle timer."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from rinari.providers.adapters import http
from rinari.shared.errors import NetworkError


class _Response:
    def __init__(self, chunks):
        self.headers = {}
        self._chunks = chunks

    def iter_bytes(self):
        for chunk in self._chunks:
            if isinstance(chunk, float):
                time.sleep(chunk)
                continue
            yield chunk

    def close(self):
        pass


def _request():
    return SimpleNamespace(
        stream_timeouts={"first_byte": 5.0, "idle": 0.2, "total": 30.0},
        stream_read_timeout_s=None,
        cancellation=None,
    )


def test_heartbeats_alone_do_not_keep_a_stalled_stream_alive(monkeypatch) -> None:
    monkeypatch.setattr(http, "STALLED_FLOOR_S", 0.5)
    beats = [b'data: {"x": 1}\n'] + [0.1, b": ping\n"] * 30
    lines = []
    with pytest.raises(NetworkError) as error:
        for line in http.iter_model_lines(_Response(beats), _request(), time.monotonic()):
            lines.append(line)
    assert error.value.details["phase"] == "stalled"
    assert lines[0].startswith("data:")


def test_a_stream_that_keeps_sending_content_is_never_stalled(monkeypatch) -> None:
    monkeypatch.setattr(http, "STALLED_FLOOR_S", 0.5)
    chunks = []
    for n in range(10):
        chunks += [0.1, f'data: {{"n": {n}}}\n'.encode()]
    lines = list(http.iter_model_lines(_Response(chunks), _request(), time.monotonic()))
    assert len([line for line in lines if line.startswith("data:")]) == 10


def test_pings_before_any_content_stall_at_the_first_byte_limit(monkeypatch) -> None:
    """An opencode stream sent only pings for eight minutes before the retry that
    answered in twelve seconds (studio run 4): pings never stretch the limit."""
    monkeypatch.setattr(http, "STALLED_FLOOR_S", 0.3)
    request = _request()
    request.stream_timeouts = {"first_byte": 0.3, "idle": 0.2, "total": 30.0}
    pings = [b": ping\n"] + [0.05, b": ping\n"] * 60
    started = time.monotonic()
    with pytest.raises(NetworkError) as error:
        list(http.iter_model_lines(_Response(pings), request, started))
    assert error.value.details["phase"] == "stalled"
    assert error.value.details["timeout_s"] == 0.3
    assert time.monotonic() - started < 0.8  # four idle limits would have been 0.8
