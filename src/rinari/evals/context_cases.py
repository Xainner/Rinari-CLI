"""Context continuity suite (review plan §E, deterministic half).

Long, multi-turn sessions against the real harness push the context through
three or more compactions. The summarizer and the conversation model are
scripted, so what these cases check is what the harness guarantees no matter
how good the summary is: what the model sees when it continues (goal,
constraints, open work, complete tool blocks), what the projection keeps,
and that the summary is checked against the records.

How well a real model continues from that view is the other half: see
`tests/manual/context_continuity_live.py`, which needs a provider and costs
money, and is never run in CI.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from rinari.evals.scripted import answer, calls
from rinari.evals.spec import AssertionOutcome, AssertionResult, EvalCase

# The eval harness's base request (instructions and ~75 tool schemas) is about
# 11k tokens. A window of 40k leaves the compaction target (60 %) above that
# fixed part with room for the tail and the summary.
WINDOW = 40_000
GOAL = "implementar la exportación CSV de informes"
RULE = "Nunca cambies el esquema de la base de datos."
NEW_GOAL = "migrar los informes a PDF"
GOOD_SUMMARY = (
    "Objetivo vigente y restricciones en el estado estructurado. Se revisaron los "
    "registros de ventas y el código de pasos; queda escribir el exportador."
)
CONTRADICTION = "Todo está terminado; no queda nada pendiente."
TASK = "escribir el exportador CSV"


def _ok(name: str, ok: bool, detail: object = "") -> AssertionResult:
    return AssertionResult(
        name, AssertionOutcome.PASSED if ok else AssertionOutcome.FAILED, str(detail)
    )


def bulk(i: int) -> str:
    """~5k tokens of Spanish prose, Python and JSON: a realistic heavy turn."""
    rows = [{"fila": n, "informe": f"ventas-{i}-{n}", "total": n * 17} for n in range(160)]
    code = "\n".join(f"def paso_{i}_{n}(x):\n    return x * {n}  # registro {n}" for n in range(40))
    return (
        f"Registro del turno {i}. "
        + "El servidor devolvió datos del informe mensual. " * 200
        + "\n```python\n"
        + code
        + "\n```\n"
        + json.dumps(rows, ensure_ascii=False)
    )


def route(request) -> str:
    first = request.messages[0].content if request.messages else ""
    if (first or "").startswith("Summarize conversation evidence"):
        return "repair" if "was rejected" in (request.messages[-1].content or "") else "summary"
    return "main"


def _main_requests(run):
    return [r for r in run.fixture.model.requests if route(r) == "main"]


def _system(request) -> str:
    return "\n".join(m.content or "" for m in request.messages if m.role == "system")


def _compactions(run) -> list[dict]:
    return [
        p
        for p in run.event_payloads("governor.compact")
        if p.get("status") in ("completed", "failed", "cancelled")
    ]


def _state(run) -> dict:
    record = run.fixture.services.ctx.session_repo.get(run.fixture.record.id)
    return record.compact_state or {}


def _orphans(request) -> list[str]:
    """Tool results whose call is not in the same request, and calls without a result."""
    seen_calls: set[str] = set()
    results: set[str] = set()
    orphans = []
    for message in request.messages:
        for call in message.tool_calls or ():
            seen_calls.add(call.id)
        if message.role == "tool":
            results.add(message.tool_call_id)
            if message.tool_call_id not in seen_calls:
                orphans.append(f"result without call: {message.tool_call_id}")
    orphans.extend(f"call without result: {c}" for c in seen_calls - results)
    return orphans


def _continuity_checks(run, *, min_compactions: int, records: str = "passed"):
    done = [p for p in _compactions(run) if p.get("status") == "completed"]
    final = _main_requests(run)[-1]
    last_prompt = run.fixture.case.all_prompts()[-1]
    return [
        _ok(
            f"at_least_{min_compactions}_compactions",
            len(done) >= min_compactions,
            f"completed={len(done)} all={[p.get('status') for p in _compactions(run)]}",
        ),
        _ok(
            "each_compaction_reduced",
            all(p.get("after_tokens", 0) < p.get("used_tokens", 0) for p in done),
            [(p.get("used_tokens"), p.get("after_tokens")) for p in done],
        ),
        _ok(
            "checks_recorded",
            all(
                (p.get("checks") or {}).get("structure") == "passed"
                and (p.get("checks") or {}).get("reduction") == "passed"
                and (p.get("checks") or {}).get("records") in (records, "passed")
                for p in done
            ),
            [p.get("checks") for p in done],
        ),
        _ok(
            "latest_message_reaches_the_model",
            any(m.role == "user" and m.content == last_prompt for m in final.messages),
            "the final request lacks the latest user message",
        ),
        _ok(
            "tool_blocks_complete",
            not any(_orphans(r) for r in _main_requests(run)),
            [o for r in _main_requests(run) for o in _orphans(r)][:5],
        ),
    ]


def _sees(run, *fragments: str):
    final = _system(_main_requests(run)[-1])
    return [
        _ok(
            f"continuation_sees:{fragment[:40]}",
            fragment in final,
            "missing from the final request",
        )
        for fragment in fragments
    ]


def _long(first: str, turns: int, *, inserts: dict[int, str] | None = None) -> tuple[str, ...]:
    prompts = [f"{first} {bulk(0)}"]
    for i in range(1, turns):
        lead = (inserts or {}).get(i, "Continúa.")
        prompts.append(f"{lead} {bulk(i)}")
    return tuple(prompts)


def _lanes(turns: int, *, summary: str = GOOD_SUMMARY, repair: str | None = None, main=None):
    lanes = {
        "main": main or [answer("De acuerdo, sigo con el trabajo.")] * turns,
        "summary": [answer(summary)] * 40,
    }
    if repair is not None:
        lanes["repair"] = [answer(repair)] * 10
    return lambda _fixture: lanes


GOAL_AND_RULES = EvalCase(
    case_id="context.goal_and_rules_survive",
    suite="context",
    name="goal_and_rules_survive_repeated_compaction",
    description=(
        "Seventeen heavy turns (Spanish, code, JSON) force three or more compactions; "
        "the goal and a negative constraint from the first message still reach the model."
    ),
    prompts=_long(f"Objetivo: {GOAL}. {RULE}", 17),
    window=WINDOW,
    script_lanes=_lanes(17),
    script_route=route,
    expectations=lambda run: [
        *_continuity_checks(run, min_compactions=3),
        *_sees(run, GOAL, RULE),
        _ok(
            "projection_goal", GOAL in _state(run).get("goal", ""), _state(run).get("goal", "")[:80]
        ),
        _ok(
            "projection_rule",
            any(RULE in c for c in _state(run).get("constraints", [])),
            _state(run).get("constraints"),
        ),
    ],
)


GOAL_CHANGE = EvalCase(
    case_id="context.explicit_goal_change",
    suite="context",
    name="explicit_goal_change_replaces_the_goal",
    description=(
        "An explicit «Nuevo objetivo:» between compactions replaces the carried goal; the "
        "earlier constraint keeps reaching the model."
    ),
    prompts=_long(f"Objetivo: {GOAL}. {RULE}", 13, inserts={6: f"Nuevo objetivo: {NEW_GOAL}."}),
    window=WINDOW,
    script_lanes=_lanes(13),
    script_route=route,
    expectations=lambda run: [
        *_continuity_checks(run, min_compactions=2),
        *_sees(run, NEW_GOAL, RULE),
        _ok(
            "projection_goal_changed",
            _state(run).get("goal", "").startswith(NEW_GOAL),
            _state(run).get("goal", "")[:80],
        ),
    ],
)


def _preference_order(run):
    constraints = _state(run).get("constraints", [])
    first = next(
        (i for i, c in enumerate(constraints) if c.startswith("Prefiero PostgreSQL")), None
    )
    later = next((i for i, c in enumerate(constraints) if c.startswith("Prefiero SQLite")), None)
    final = _system(_main_requests(run)[-1])
    return [
        _ok("both_preferences_kept", first is not None and later is not None, constraints),
        _ok(
            "correction_comes_later",
            first is not None and later is not None and later > first,
            constraints,
        ),
        _ok(
            "precedence_is_stated",
            "later entries prevail" in final,
            "the constraints header does not say which one wins",
        ),
    ]


CORRECTED_DECISION = EvalCase(
    case_id="context.corrected_decision",
    suite="context",
    name="a_correction_supersedes_without_being_lost",
    description=(
        "A preference corrected after a compaction keeps both entries, in order, under a "
        "header that says the later one prevails."
    ),
    prompts=_long(
        "Prefiero PostgreSQL para los informes.",
        13,
        inserts={7: "Prefiero SQLite en lugar de PostgreSQL para los informes."},
    ),
    window=WINDOW,
    script_lanes=_lanes(13),
    script_route=route,
    expectations=lambda run: [*_continuity_checks(run, min_compactions=2), *_preference_order(run)],
)


def _open_task_setup(fixture):
    root = str(Path(fixture.work).resolve())
    now = "2026-09-23T00:00:00Z"
    fixture.app_ctx.task_repo.create(
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


_CONTRADICTION = re.compile(r"no queda nada pendiente|todo está terminado", re.IGNORECASE)

OPEN_TASK_REPAIR = EvalCase(
    case_id="context.open_task_repair",
    suite="context",
    name="a_summary_contradicting_open_work_is_repaired",
    description=(
        "The summarizer declares the work finished while a task is recorded in progress; "
        "one repair is requested and the published summary does not carry the claim."
    ),
    prompts=_long(f"Objetivo: {GOAL}.", 9),
    window=WINDOW,
    setup=_open_task_setup,
    script_lanes=_lanes(9, summary=CONTRADICTION, repair=GOOD_SUMMARY),
    script_route=route,
    expectations=lambda run: [
        *_continuity_checks(run, min_compactions=1, records="repaired"),
        _ok(
            "repaired",
            any((p.get("checks") or {}).get("records") == "repaired" for p in _compactions(run)),
            [p.get("checks") for p in _compactions(run)],
        ),
        _ok(
            "published_summary_is_clean",
            not _CONTRADICTION.search(_state(run).get("summary", "")),
            _state(run).get("summary", "")[:80],
        ),
        *_sees(run, f"active: {TASK}"),
    ],
)


def _tool_turns(turns: int):
    main = []
    for i in range(turns):
        main.append(calls(("fs.read", {"path": "notas.md"}), id_prefix=f"r{i}-"))
        main.append(answer("Notas leídas; sigo."))
    return main


TOOL_BLOCKS = EvalCase(
    case_id="context.tool_blocks_intact",
    suite="context",
    name="tool_call_result_blocks_survive_compaction",
    description=(
        "Every turn reads a file; across compactions no request carries a tool result "
        "without its call, or a call without its result."
    ),
    prompts=_long(f"Objetivo: {GOAL}.", 13),
    window=WINDOW,
    setup=lambda f: [f.file("notas.md", "Notas del exportador: columnas fecha, total.\n")],
    script_lanes=_lanes(13, main=_tool_turns(13)),
    script_route=route,
    expectations=lambda run: [
        *_continuity_checks(run, min_compactions=2),
        _ok(
            "reads_happened",
            len(run.fixture.tools()) >= 13,
            f"tool completions={len(run.fixture.tools())}",
        ),
    ],
)


RESTART = EvalCase(
    case_id="context.restart_keeps_projection",
    suite="context",
    name="rebuilding_the_session_keeps_the_projection",
    description=(
        "The session is rebuilt between every turn, as after an engine restart; the "
        "persisted projection keeps the goal and the constraint reaching the model."
    ),
    prompts=_long(f"Objetivo: {GOAL}. {RULE}", 13),
    window=WINDOW,
    resume_between_turns=True,
    script_lanes=_lanes(13),
    script_route=route,
    expectations=lambda run: [
        *_continuity_checks(run, min_compactions=2),
        *_sees(run, GOAL, RULE),
        _ok(
            "revision_matches_compactions",
            _state(run).get("revision")
            == len([p for p in _compactions(run) if p.get("status") == "completed"]),
            f"revision={_state(run).get('revision')}",
        ),
    ],
)


def cases() -> tuple[EvalCase, ...]:
    return (GOAL_AND_RULES, GOAL_CHANGE, CORRECTED_DECISION, OPEN_TASK_REPAIR, TOOL_BLOCKS, RESTART)
