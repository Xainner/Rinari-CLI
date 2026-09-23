"""The live continuity comparison, exercised without a network.

A scripted model that only answers correctly when the needed fact is in front
of it stands in for the provider, so the pipeline (full vs. compacted, judge,
report, call budget) is checked before anyone spends money on it.
"""

import dataclasses

import pytest

from rinari.application.services import build_services
from rinari.evals import continuity_live
from rinari.models.types import ModelResponse, ProviderCapabilities, StopReason, Usage
from rinari.storage.records import SessionRecord


class FactBound:
    """Answers from what the request actually shows, like a model that cannot recall."""

    def __init__(self):
        self.requests = []

    def capabilities(self):
        return ProviderCapabilities(max_context_tokens=32_000)

    def invoke(self, request):
        self.requests.append(request)
        text = "\n".join(m.content or "" for m in request.messages)
        question = request.messages[-1].content or ""
        if (request.messages[0].content or "").startswith("Summarize conversation evidence"):
            content = "Se revisaron registros y código de los pasos del informe."
        elif "restricción" in question:
            content = (
                "Seguiré con el exportador CSV sin tocar el esquema."
                if "CSV" in text and "esquema" in text
                else "Seguiré revisando."
            )
        elif "base de datos" in question:
            content = "SQLite" if "SQLite" in text else "PostgreSQL"
        else:
            open_work = "active:" in text or "failed" in text
            content = "No, hay trabajo abierto y pruebas fallidas." if open_work else "Sí."
        return ModelResponse(
            content=content,
            stop_reason=StopReason.END_TURN,
            usage=Usage(input_tokens=100, output_tokens=5),
        )


@pytest.fixture
def world(app_ctx, tmp_path):
    services = build_services(app_ctx, user_home=tmp_path)

    def session_for(scenario):
        root = str(tmp_path / scenario.key)
        record = SessionRecord(
            id=f"s-{scenario.key}-{len(app_ctx.session_repo.list())}",
            kind="PROJECT" if scenario.seed else "CHAT",
            title="t",
            project_id=None,
            project_root_snapshot=root if scenario.seed else None,
            created_cwd=root,
            current_cwd=root,
            provider_id="",
            model_id="fake",
            profile_id="",
            mode="plan",
            state="active",
            compact_state=None,
            created_at="2026-09-23",
            updated_at="2026-09-23",
            last_active_at="2026-09-23",
        )
        app_ctx.session_repo.insert(record)
        return record, root

    return services, session_for


def test_both_variants_are_asked_and_judged(world):
    services, session_for = world
    model = FactBound()
    result = continuity_live.run(
        services, session_for, continuity_live.CallBudget(model, 50), "fake"
    )
    assert result["total"] == 3
    assert result["full_passed"] == 3
    # The compacted history still carries the goal, the rule, the correction
    # and the open work (from the records), so nothing regresses.
    assert result["compacted_passed"] == 3, result["runs"]
    assert result["regressions"] == 0
    for row in result["runs"]:
        assert row["compaction"]["status"] == "completed", row
        assert row["compacted"]["input_tokens"] == 100


def test_the_call_budget_stops_before_overspending(world):
    services, session_for = world
    budget = continuity_live.CallBudget(FactBound(), 2)
    result = continuity_live.run(services, session_for, budget, "fake")
    assert budget.calls == 2
    # The paid full answer is kept; the scenario it stopped in is marked.
    assert result["stopped"] and result["total"] == 1
    assert result["runs"][0]["full"]["passed"]
    assert result["runs"][0]["error"].startswith("stopped:")
    assert result["regressions"] == 0


def test_repeated_runs_seed_their_own_records(world):
    services, session_for = world
    result = continuity_live.run(
        services, session_for, continuity_live.CallBudget(FactBound(), 50), "fake", runs=2
    )
    assert result["total"] == 6 and result["errors"] == 0, result["runs"]
    assert result["compacted_passed"] == 6


def test_a_provider_error_is_recorded_and_the_run_goes_on(world):
    services, session_for = world

    class Flaky(FactBound):
        def invoke(self, request):
            if not self.requests:
                self.requests.append(request)
                raise ConnectionError("provider unreachable")
            return super().invoke(request)

    result = continuity_live.run(
        services, session_for, continuity_live.CallBudget(Flaky(), 50), "fake"
    )
    assert result["errors"] == 1 and result["total"] == 3
    assert result["runs"][0]["error"].startswith("full: ConnectionError")
    assert result["regressions"] == 0 and result["compacted_passed"] == 2


def test_the_plan_needs_no_provider():
    estimate = continuity_live.plan(runs=2)
    assert estimate["min_calls"] == 18
    assert estimate["max_calls_with_repairs"] == 24
    assert all(row["full_input_tokens"] > 1000 for row in estimate["scenarios"])
    full = sum(row["full_input_tokens"] for row in estimate["scenarios"])
    assert estimate["input_tokens_upper_bound"] == 2 * 4 * full


def test_a_compaction_that_loses_the_state_is_a_regression(world, monkeypatch):
    services, session_for = world
    real = continuity_live._request

    def without_state(model_id, session_id, state):
        # Keep what compaction left in the history, drop the carried state.
        request = real(model_id, session_id, state)
        system = continuity_live.ChatMessage.system("Answer the user.")
        return dataclasses.replace(request, messages=(system, *request.messages[1:]))

    monkeypatch.setattr(continuity_live, "_request", without_state)
    result = continuity_live.run(
        services, session_for, continuity_live.CallBudget(FactBound(), 50), "fake"
    )
    assert result["full_passed"] == 3
    assert result["regressions"] == 3, result["runs"]
