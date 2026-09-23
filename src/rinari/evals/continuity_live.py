"""Continuation with full history vs. compacted history (review plan §E).

For each scenario the same question is asked twice of the same model: once
with the whole synthetic history, once after compacting it. A rule judge
checks both answers for the expected facts, so the report shows whether the
compaction lost something the model needed. Tokens, latency and the checks
the compaction ran are recorded for a baseline.

This module never chooses a model or spends money by itself. The manual
script (`tests/manual/context_continuity_live.py`) binds it to a saved model
the owner selects; `plan()` estimates the calls first, and `CallBudget`
aborts before a call would exceed `max_calls`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rinari.context.accounting import request_size
from rinari.evals.judge import Rule, RuleJudge
from rinari.models.types import ChatMessage, ModelRequest
from rinari.prompts.assembler import AssemblerContext
from rinari.runtime.agent import AgentContext
from rinari.runtime.cancellation import CancellationToken

GOAL = "implementar la exportación CSV de informes"
RULE = "Nunca cambies el esquema de la base de datos."
TASK = "escribir el exportador CSV"


class BudgetExceededError(RuntimeError):
    """The next provider call would exceed the owner's `max_calls`."""


class CallBudget:
    """Counts every provider call (summaries included) and stops at the limit."""

    def __init__(self, caller, max_calls: int):
        self._caller, self.max_calls, self.calls = caller, max_calls, 0

    def __getattr__(self, name):
        return getattr(self._caller, name)

    def invoke(self, request):
        if self.calls >= self.max_calls:
            raise BudgetExceededError(
                f"stopped before call {self.calls + 1} (max_calls={self.max_calls})"
            )
        self.calls += 1
        return self._caller.invoke(request)


def _observations(topic: str, count: int) -> list[ChatMessage]:
    messages = []
    for i in range(count):
        messages.append(
            ChatMessage.assistant(
                f"Observación {i} sobre {topic}: revisé los registros de ventas del mes y el "
                "código de los pasos del informe. " + "Los totales cuadran con el origen. " * 40
            )
        )
        messages.append(ChatMessage.user(f"Sigue revisando ({i})."))
    return messages


@dataclass(frozen=True)
class Scenario:
    key: str
    description: str
    history: Callable[[], list[ChatMessage]]
    question: str
    rules: tuple[Rule, ...]
    seed: Callable[[Any, str], None] | None = None


def _seed_open_work(ctx, root: str) -> None:
    now = "2026-09-23T00:00:00Z"
    ctx.task_repo.create(
        {
            "id": "task_exporter",
            "project_root": root,
            "session_ref": "",
            "title": TASK,
            "description": "",
            "status": "in_progress",
            "acceptance": "",
            "implementation": "",
            "validation": "",
            "scope": "",
            "unresolved": "",
            "depends_on": "",
            "blockers": "",
            "evidence": "",
            "created_at": now,
            "updated_at": now,
        }
    )
    ctx.validation_repo.insert(
        {
            "id": "val_failed",
            "project_root": root,
            "session_ref": "",
            "kind": "test",
            "command": "pytest -q",
            "result": "failed",
            "summary": "3 failed",
            "detail": "",
            "artifact_ref": "",
            "created_at": now,
        }
    )


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "goal_and_rule",
        "The goal and a negative constraint set at the start.",
        lambda: [
            ChatMessage.user(f"Objetivo: {GOAL}. {RULE}"),
            *_observations("la exportación", 18),
        ],
        "En una frase: ¿qué harás a continuación y qué restricción debes respetar?",
        (Rule(r"csv|exportador|exportaci"), Rule(r"esquema")),
    ),
    Scenario(
        "corrected_decision",
        "A preference corrected halfway through.",
        lambda: [
            ChatMessage.user("Prefiero PostgreSQL para los informes."),
            *_observations("la base de datos", 9),
            ChatMessage.user("Prefiero SQLite en lugar de PostgreSQL para los informes."),
            *_observations("la migración", 9),
        ],
        "¿Qué base de datos usaremos para los informes? Responde solo con el nombre.",
        (Rule(r"sqlite"), Rule(r"^\W*postgres", required=False)),
    ),
    Scenario(
        "open_work",
        "An open task and a failing test run mentioned early, then buried under routine turns.",
        lambda: [
            ChatMessage.user(f"Objetivo: {GOAL}."),
            ChatMessage.assistant(
                f"Creé la tarea «{TASK}» y está en curso. Ejecuté pytest -q: 3 failed."
            ),
            *_observations("el exportador", 18),
        ],
        "¿El trabajo está terminado y las pruebas pasan? Responde sí o no y por qué, en una frase.",
        (Rule(r"\bno\b"), Rule(r"^\W*s[ií]\b", required=False)),
        seed=_seed_open_work,
    ),
)


def _request(model_id: str, session_id: str, state) -> ModelRequest:
    return ModelRequest(
        model=model_id,
        session_id=session_id,
        messages=(
            ChatMessage.system(state.compact_state_text or "Answer the user."),
            *state.history,
        ),
    )


def plan(scenarios=SCENARIOS, runs: int = 1) -> dict:
    """What a run would send, without calling anything."""
    rows = []
    for scenario in scenarios:
        history = [*scenario.history(), ChatMessage.user(scenario.question)]
        state = AgentContext("plan", "plan", None, AssemblerContext(), history=history)
        rows.append(
            {
                "scenario": scenario.key,
                "full_input_tokens": request_size(_request("plan", "plan", state)),
            }
        )
    per_run_calls = sum(3 for _ in scenarios)  # full answer, >=1 summary, compacted answer
    return {
        "scenarios": rows,
        "runs": runs,
        "min_calls": per_run_calls * runs,
        # One bounded repair per compaction can add a call.
        "max_calls_with_repairs": (per_run_calls + len(scenarios)) * runs,
        # Every call (answer, summary, repair, compacted answer) sends at most
        # the full history, so this is a ceiling rather than an estimate.
        "input_tokens_upper_bound": runs * sum(4 * r["full_input_tokens"] for r in rows),
    }


@dataclass
class Variant:
    passed: bool
    rationale: str
    answer: str
    input_tokens: int | None
    output_tokens: int | None
    seconds: float


@dataclass
class ScenarioResult:
    scenario: str
    run: int
    full: Variant
    compacted: Variant | None = None
    compaction: dict = field(default_factory=dict)
    error: str | None = None

    @property
    def regression(self) -> bool:
        return self.full.passed and not (self.compacted and self.compacted.passed)


def _ask(caller, request) -> Variant:
    started = time.monotonic()
    response = caller.invoke(request)
    usage = response.usage
    return Variant(
        passed=False,
        rationale="",
        answer=(response.content or "")[:400],
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        seconds=round(time.monotonic() - started, 2),
    )


def _judge(variant: Variant, rules) -> Variant:
    verdict = RuleJudge(list(rules)).judge(variant.answer)
    variant.passed, variant.rationale = verdict.outcome.value == "pass", verdict.rationale
    return variant


def run(
    services, session_factory, caller, model_id: str, *, scenarios=SCENARIOS, runs: int = 1
) -> dict:
    """Run every scenario `runs` times; `session_factory(scenario)` returns (record, root)."""
    results: list[ScenarioResult] = []
    for index in range(runs):
        for scenario in scenarios:
            record, root = session_factory(scenario)
            if scenario.seed is not None:
                scenario.seed(services.ctx, root)
            history = [*scenario.history(), ChatMessage.user(scenario.question)]
            context = AgentContext(
                record.id, model_id, None, AssemblerContext(), history=list(history)
            )
            request = lambda state, record=record: _request(model_id, record.id, state)  # noqa: E731
            result = ScenarioResult(
                scenario.key, index, _judge(_ask(caller, request(context)), scenario.rules)
            )
            events: list[dict] = []
            context.force_compaction = True
            try:
                reduced = services.context.prepare(
                    context,
                    request(context),
                    caller,
                    request,
                    lambda _name, payload, sink=events: sink.append(payload),
                    CancellationToken(),
                )
                result.compacted = _judge(_ask(caller, reduced), scenario.rules)
            except BudgetExceededError:
                raise
            except Exception as exc:
                result.error = f"{type(exc).__name__}: {exc}"
            finally:
                context.force_compaction = False
            done = [e for e in events if e.get("status") in ("completed", "failed")]
            if done:
                last = done[-1]
                result.compaction = {
                    k: last.get(k)
                    for k in (
                        "status",
                        "used_tokens",
                        "after_tokens",
                        "duration_ms",
                        "checks",
                        "error",
                    )
                    if k in last
                }
            results.append(result)
    return report(results)


def report(results: list[ScenarioResult]) -> dict:
    def variant(v: Variant | None):
        return (
            None
            if v is None
            else {
                k: getattr(v, k)
                for k in (
                    "passed",
                    "rationale",
                    "answer",
                    "input_tokens",
                    "output_tokens",
                    "seconds",
                )
            }
        )

    rows = [
        {
            "scenario": r.scenario,
            "run": r.run,
            "full": variant(r.full),
            "compacted": variant(r.compacted),
            "compaction": r.compaction,
            "regression": r.regression,
            "error": r.error,
        }
        for r in results
    ]
    return {
        "runs": rows,
        "full_passed": sum(r.full.passed for r in results),
        "compacted_passed": sum(bool(r.compacted and r.compacted.passed) for r in results),
        "regressions": sum(r.regression for r in results),
        "total": len(results),
    }
