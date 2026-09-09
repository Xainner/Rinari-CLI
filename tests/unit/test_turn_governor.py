from rinari.runtime.governor import GovernorAction, TurnGovernor
from rinari.runtime.progress import ProgressKind


def test_useful_cycles_do_not_consume_recovery_attempts() -> None:
    governor = TurnGovernor()
    for index in range(120):
        governor.after_tool(
            "shell.exec",
            {"command": f"check-{index}"},
            f"artifact://session/result-{index}",
            ok=True,
        )
        decision = governor.after_cycle()
        assert decision.action is GovernorAction.CONTINUE
        assert decision.progress is ProgressKind.HEALTHY
    assert governor.recovery_attempts == 0


def test_stagnation_recovers_three_times_then_stops() -> None:
    governor = TurnGovernor()
    assert governor.after_cycle().action is GovernorAction.CONTINUE
    assert governor.after_cycle().action is GovernorAction.CONSOLIDATE
    assert governor.after_cycle().action is GovernorAction.NUDGE
    assert governor.after_cycle().action is GovernorAction.FINALIZE
    stopped = governor.after_cycle()
    assert stopped.action is GovernorAction.STOP
    assert stopped.reason == "stagnation"


def test_persistent_loop_stops_at_recovery_limit() -> None:
    governor = TurnGovernor(max_recovery_attempts=3)
    assert governor.after_cycle(looping=True).action is GovernorAction.CONSOLIDATE
    assert governor.after_cycle(looping=True).action is GovernorAction.NUDGE
    stopped = governor.after_cycle(looping=True)
    assert stopped.action is GovernorAction.STOP
    assert stopped.reason == "persistent_loop"
