"""Prepare groups in order, execute authorized readers, consolidate by call ID."""

from rinari.shared.errors import CancelledError
from rinari.tools.definition import ToolErrorCode, ToolErrorInfo, ToolResult
from rinari.tools.executor import map_reads
from rinari.tools.scheduler import is_parallelizable


def execute_round(calls, registry, prepare, *, cancellation, max_concurrency, on_completed):
    groups = []
    readers = []
    for call in calls:
        if is_parallelizable(registry.get(call.name), call.arguments):
            readers.append(call)
        else:
            if readers:
                groups.append(readers)
            readers = []
            groups.append([call])
    if readers:
        groups.append(readers)
    for group in groups:
        cancellation.throw_if_cancelled()
        pending = []
        for index, call in enumerate(group):
            try:
                cancellation.throw_if_cancelled()
                executed, work = prepare(call)
                pending.append((call, executed, work))
            except CancelledError:
                cancelled = ToolResult(
                    ok=False,
                    error=ToolErrorInfo(
                        ToolErrorCode.CANCELLED,
                        "Tool was not dispatched because the turn was cancelled",
                    ),
                )
                pending = [(item, executed, cancelled) for item, executed, _ in pending]
                pending.extend((item, False, cancelled) for item in group[index:])
                break

        def run(entry):
            call, executed, work = entry
            try:
                cancellation.throw_if_cancelled()
                result = work() if callable(work) else work
            except CancelledError:
                result = ToolResult(
                    ok=False,
                    error=ToolErrorInfo(
                        ToolErrorCode.CANCELLED, "Tool cancelled before completion"
                    ),
                )
            except Exception as exc:
                result = ToolResult(
                    ok=False,
                    error=ToolErrorInfo(
                        ToolErrorCode.UNKNOWN,
                        f"Tool result unavailable ({type(exc).__name__}); "
                        "verify state before repeating",
                    ),
                )
            return call, executed, result

        def completed(entry):
            call, executed, result = entry
            if executed:
                on_completed(call, result)

        if len(group) > 1:
            results = map_reads(run, pending, limit=max_concurrency, on_completed=completed)
        else:
            results = [run(pending[0])]
            completed(results[0])
        for index, entry in enumerate(results):
            yield (*entry, index == len(results) - 1)
        cancellation.throw_if_cancelled()
