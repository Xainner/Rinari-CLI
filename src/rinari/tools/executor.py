"""One bounded local-reader executor per process, with no nested worker waits."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from contextvars import copy_context
from threading import Condition, local

_pool = ThreadPoolExecutor(max_workers=32, thread_name_prefix="rinari-tools")
_gate = Condition()
_local = local()
_active = 0
_limits = {}


def map_reads(function, items, *, limit=4, on_completed=None):
    if getattr(_local, "worker", False):
        return [function(item) for item in items]
    ticket = object()
    with _gate:
        _limits[ticket] = max(1, min(32, limit))
        _gate.notify_all()

    def run(item):
        global _active
        with _gate:
            _gate.wait_for(lambda: _active < min(_limits.values()))
            _active += 1
        _local.worker = True
        try:
            return function(item)
        finally:
            _local.worker = False
            with _gate:
                _active -= 1
                _gate.notify_all()

    futures = []
    try:
        for item in items:
            futures.append(_pool.submit(copy_context().run, run, item))
        # Drain every started job before exposing a failed/cancelled group.
        outcomes = [None] * len(futures)
        indexes = {future: index for index, future in enumerate(futures)}
        failure = None
        for future in as_completed(futures):
            try:
                outcome = future.result()
                outcomes[indexes[future]] = outcome
                if on_completed is not None:
                    on_completed(outcome)
            except BaseException as exc:
                failure = failure or exc
        if failure is not None:
            raise failure
        return outcomes
    finally:
        for future in futures:
            with suppress(BaseException):
                future.result()
        with _gate:
            _limits.pop(ticket, None)
            _gate.notify_all()
