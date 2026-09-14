from rinari.prompts.modes import mode_instructions


def test_plan_requires_grounded_implementation_ready_deliverable():
    text = mode_instructions("plan")
    for requirement in (
        "implementation-ready",
        "actually inspected",
        "acceptance criteria",
        "Assumptions",
        "non-goals",
        "do not implement",
        "brief answer",
        "private reasoning",
        "future BUILD",
    ):
        assert requirement in text


def test_other_modes_do_not_inherit_plan_contract():
    for mode in ("build", "review", None):
        assert mode_instructions(mode) == ""
