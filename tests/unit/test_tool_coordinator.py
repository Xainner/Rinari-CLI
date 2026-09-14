from threading import Barrier, Event, get_ident

from rinari.models.types import ToolCall
from rinari.runtime.cancellation import CancellationToken
from rinari.runtime.tool_coordinator import execute_round
from rinari.tools.definition import ToolDefinition, ToolResult
from rinari.tools.executor import map_reads
from rinari.tools.registry import ToolRegistry
from rinari.tools.scheduler import is_parallelizable


def registry():
    reg = ToolRegistry()
    reg.register(
        ToolDefinition(name="read", description="", input_schema={}, concurrency="local-read")
    )
    reg.register(ToolDefinition(name="write", description="", input_schema={}))
    return reg


def test_readers_overlap_and_barrier_retains_call_order():
    sync = Barrier(2)
    events = []
    owner = get_ident()
    calls = [
        ToolCall(id=str(i), name=name, arguments={})
        for i, name in enumerate(["read", "read", "write", "read"])
    ]

    def prepare(call):
        assert get_ident() == owner
        events.append("authorize" + call.id)

        def work():
            if call.id in ("0", "1"):
                sync.wait(timeout=3)
                assert "authorize0" in events and "authorize1" in events
            if call.id == "2":
                assert "done0" in events and "done1" in events
            if call.id == "3":
                assert "done2" in events
            events.append("done" + call.id)
            return ToolResult(ok=True, data=call.id)

        return True, work

    rows = list(
        execute_round(
            calls,
            registry(),
            prepare,
            cancellation=CancellationToken(),
            max_concurrency=2,
            on_completed=lambda *args: None,
        )
    )
    assert [row[2].data for row in rows] == ["0", "1", "2", "3"]
    assert all(row[2].ok for row in rows)


def test_nested_reads_use_worker_inline_without_deadlock():
    sync = Barrier(2)

    def work(index):
        sync.wait(timeout=3)
        owner = get_ident()
        return map_reads(lambda _: get_ident() == owner, range(6), limit=2)

    assert map_reads(work, range(2), limit=2) == [[True] * 6, [True] * 6]


def test_default_contract_is_serial_even_if_idempotent():
    assert not is_parallelizable(ToolDefinition(name="mcp.read", description="", input_schema={}))


def test_completion_arrives_before_slow_sibling_but_results_stay_ordered():
    release = Event()
    calls = [ToolCall(id=str(i), name="read", arguments={}) for i in range(2)]
    completed = []

    def prepare(call):
        def work():
            if call.id == "0":
                assert release.wait(3)
            return ToolResult(ok=True, data=call.id)

        return True, work

    def complete(call, result):
        completed.append(call.id)
        if call.id == "1":
            release.set()

    rows = list(
        execute_round(
            calls,
            registry(),
            prepare,
            cancellation=CancellationToken(),
            max_concurrency=2,
            on_completed=complete,
        )
    )
    assert completed == ["1", "0"]
    assert [row[0].id for row in rows] == ["0", "1"]


def test_cancellation_during_authorization_dispatches_nothing():
    from rinari.shared.errors import CancelledError

    token = CancellationToken()
    ran = []
    calls = [ToolCall(id=str(i), name="read", arguments={}) for i in range(3)]

    def prepare(call):
        if call.id == "1":
            token.cancel()
            token.throw_if_cancelled()
        return True, lambda: ran.append(call.id)

    rows = []
    try:
        for row in execute_round(
            calls,
            registry(),
            prepare,
            cancellation=token,
            max_concurrency=2,
            on_completed=lambda *args: None,
        ):
            rows.append(row)
    except CancelledError:
        pass
    assert not ran
    assert len(rows) == 3
    assert all(row[2].error.code.value == "CANCELLED" for row in rows)
