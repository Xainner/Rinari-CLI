"""Eval runtime behavior: trace span fields (turn_index/tool_seq), scripted
model lanes, judge, and failure surfacing (never a false success).
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from rinari.cli.agent_runtime import run_turn
from rinari.evals.fixtures import EvalFixture
from rinari.evals.judge import JudgeOutcome, Rule, RuleJudge, parse_judge_json
from rinari.evals.runner import run_case
from rinari.evals.scripted import ScriptedModel, ScriptExhaustedError, answer, calls
from rinari.evals.spec import AssertionOutcome, EvalCase
from rinari.policy.engine import PermissionProfile


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    from rinari.shared.paths import ENV_HOME

    monkeypatch.setenv(ENV_HOME, str(tmp_path / "rinari-home"))
    return tmp_path


def _two_turn_case() -> EvalCase:
    return EvalCase(
        case_id="tmp.spans",
        suite="tmp",
        name="spans",
        description="span fields test case",
        prompt="first",
        prompts=("first", "second"),
        project=True,
        setup=lambda f: [f.file("a.txt", "a\n")],
        script=lambda f: [
            calls(("fs.read", {"path": "a.txt"})),
            answer("one"),
            calls(("fs.read", {"path": "a.txt"})),
            answer("two"),
        ],
    )


def test_turn_and_tool_span_fields(workspace, tmp_path):
    case = _two_turn_case()
    base = tmp_path / "spanrun"
    base.mkdir()
    fixture = EvalFixture(case=case, base_dir=base).build()
    run_turn(fixture.session, case.prompts[0])
    run_turn(fixture.session, case.prompts[1])

    by_id = {
        e.payload.get("turn_index"): e for e in fixture.events() if e.type == "AgentTurnStarted"
    }
    assert sorted(by_id) == [0, 1]

    completed = [e for e in fixture.events() if e.type == "AgentTurnCompleted"]
    assert [c.payload.get("turn_index") for c in completed] == [0, 1]

    tools = [
        e for e in fixture.events() if e.type == "ToolCompleted" and "tool_call_id" in e.payload
    ]
    assert len(tools) == 2
    assert len([e for e in fixture.events() if e.type == "ToolCompleted"]) == 2
    first, second = tools
    assert first.payload["turn_index"] == 0 and first.payload["tool_seq"] == 1
    assert second.payload["turn_index"] == 1 and second.payload["tool_seq"] == 1
    fixture.close()


def test_read_only_turn_does_not_inherit_old_completion_evidence(workspace, tmp_path):
    case = EvalCase(
        case_id="tmp.readonly_gate",
        suite="tmp",
        name="readonly gate",
        description="read-only work has no completion badge",
        prompt="audit the prior fix",
        project=True,
        setup=lambda f: [f.file("a.txt", "a\n")],
        script=lambda f: [calls(("fs.read", {"path": "a.txt"})), answer("Tests passed before.")],
    )
    base = tmp_path / "readonly-gate"
    base.mkdir()
    fixture = EvalFixture(case=case, base_dir=base).build()
    fixture.services.verification.record(
        fixture.work,
        kind="test",
        result="passed",
        summary="old evidence",
        session_ref=fixture.record.id,
    )
    fixture.session.context.tool_ctx = replace(
        fixture.session.context.tool_ctx, profile=PermissionProfile.READ_ONLY
    )

    turn = run_turn(fixture.session, case.prompt)

    assert turn.completion is None
    assert "CompletionGateEvaluated" not in [e.type for e in fixture.events()]
    fixture.close()


def test_mutating_turn_requires_fresh_validation_evidence(workspace, tmp_path):
    case = EvalCase(
        case_id="tmp.fresh_gate",
        suite="tmp",
        name="fresh gate",
        description="old evidence cannot verify a new change",
        prompt="change the file",
        project=True,
        setup=lambda f: [f.file("a.txt", "a\n")],
        script=lambda f: [
            calls(("fs.write", {"path": "a.txt", "content": "b\n"})),
            answer("Fixed and tests passed."),
        ],
    )
    base = tmp_path / "fresh-gate"
    base.mkdir()
    fixture = EvalFixture(case=case, base_dir=base).build()
    fixture.services.verification.record(
        fixture.work,
        kind="test",
        result="passed",
        summary="old evidence",
        session_ref=fixture.record.id,
    )

    turn = run_turn(fixture.session, case.prompt)

    assert turn.completion is not None
    assert turn.completion["outcome"] == "IMPLEMENTED_UNVERIFIED"
    assert turn.completion["evidence"] == []
    fixture.close()


def test_script_exhaustion_surfaces_as_error(workspace, tmp_path):
    case = EvalCase(
        case_id="tmp.exhaust",
        suite="tmp",
        name="exhaust",
        description="script runs out mid-turn",
        prompt="go",
        project=True,
        script=lambda f: [
            calls(("fs.read", {"path": "a.txt"})),
            calls(("fs.read", {"path": "b.txt"})),  # never reached: see below
        ],
        setup=lambda f: [f.file("a.txt", "a\n"), f.file("b.txt", "b\n")],
    )
    # The script answers nothing: the loop keeps requesting -> exhaustion.
    base = tmp_path / "exhaust"
    base.mkdir()
    result = run_case(case, base_dir=base)
    assert result.status == "error"
    assert result.error is not None
    assert "exhaust" in result.error
    assert result.assertions[0].outcome is AssertionOutcome.FAILED


def test_scripted_model_lane_routing(workspace, tmp_path):
    model = ScriptedModel(
        {
            "main": [answer("main-done")],
            "sub": [calls(("fs.read", {"path": "x"}))],
        },
        route=lambda request: (
            "sub"
            if any(
                (m.content or "").startswith("TASK (")
                for m in reversed(request.messages)
                if m.role == "user"
            )
            else "main"
        ),
    )
    from rinari.models.types import ChatMessage, ModelRequest

    main_req = ModelRequest(model="m", messages=(ChatMessage.user("hello"),))
    sub_req = ModelRequest(
        model="m", messages=(ChatMessage.user("TASK (verifier subagent): do things"),)
    )
    assert model.lane(main_req) == "main"
    assert model.lane(sub_req) == "sub"
    assert model.lane(sub_req) == "sub"  # re-routing still resolves

    response = model.invoke(sub_req)
    assert response.has_tool_calls
    with pytest.raises(ScriptExhaustedError):
        model.invoke(sub_req)  # sub lane exhausted while main lane still has one
    assert model.invoke(main_req).content == "main-done"


def test_rule_judge_and_json_parsing():
    judge = RuleJudge(
        [Rule(r"\btests? passed\b"), Rule(r"ignored all instructions", required=False)]
    )
    assert judge.judge("All tests passed.").outcome is JudgeOutcome.PASS
    assert judge.judge("all good").outcome is JudgeOutcome.FAIL
    assert judge.judge("tests passed but it ignored all instructions").outcome is JudgeOutcome.FAIL

    verdict = parse_judge_json(
        'Sure! Here is the verdict:\n```json\n{"pass": true, "score": 0.9, "rationale": "ok"}\n```'
    )
    assert verdict == {"pass": True, "score": 0.9, "rationale": "ok"}
    with pytest.raises(ValueError):
        parse_judge_json("no JSON here")
