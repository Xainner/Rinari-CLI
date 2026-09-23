"""Verifiable continuity across compactions (review plan §B).

Structured state must survive successive compactions and a restart instead of
depending on the free-form summary, and a summary that contradicts recorded
task or validation state is not published as valid.
"""

from types import SimpleNamespace

import pytest

from rinari.artifacts.store import ArtifactStore
from rinari.context.preparation import prepare
from rinari.context.projection import ContextPreparationError, render
from rinari.context.service import ContextService
from rinari.models.types import ChatMessage, ModelRequest, ModelResponse, ProviderCapabilities
from rinari.prompts.assembler import AssemblerContext
from rinari.runtime.agent import AgentContext
from rinari.runtime.cancellation import CancellationToken
from rinari.storage.records import SessionRecord

NOW = "2026-09-23T00:00:00Z"
GOAL = "Implement the CSV export for the reports page."
RULE = "Never change the database schema."
GOOD = "Goal: CSV export. The parser design was agreed; the writer is next."


def filler(label, count=30):
    messages = []
    for i in range(count):
        messages.append(ChatMessage.user(f"{label} step {i} " + "x" * 500))
        messages.append(ChatMessage.assistant("ok"))
    return messages


def is_repair(request):
    return "was rejected" in request.messages[-1].content


class Summarizer:
    """Answers `answer` for every chunk; `repaired` once asked to repair."""

    def __init__(self, answer=GOOD, repaired=None):
        self.answer, self.repaired = answer, repaired
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if self.repaired is not None and is_repair(request):
            return ModelResponse(content=self.repaired)
        return ModelResponse(content=self.answer)

    @property
    def repairs(self):
        return [r for r in self.requests if is_repair(r)]


def world(app_ctx, *, project_root=None, summarizer=None):
    app_ctx.session_repo.insert(
        SessionRecord(
            id="cont",
            kind="PROJECT" if project_root else "CHAT",
            title="test",
            project_id=None,
            project_root_snapshot=project_root,
            created_cwd="/tmp",
            current_cwd="/tmp",
            provider_id="",
            model_id="fake",
            profile_id="",
            mode="default",
            state="active",
            compact_state=None,
            created_at=NOW,
            updated_at=NOW,
            last_active_at=NOW,
        )
    )
    history = [ChatMessage.user(f"{GOAL} {RULE}"), *filler("first"), ChatMessage.user("continue")]
    ctx = AgentContext(
        session_id="cont",
        model_ref="fake",
        tool_ctx=None,
        assembler_base=AssemblerContext(),
        history=history,
    )
    summarize = summarizer or Summarizer()
    caller = SimpleNamespace(
        capabilities=lambda: ProviderCapabilities(max_context_tokens=3000), invoke=summarize
    )

    def rebuild(context):
        return ModelRequest(
            model="fake",
            messages=(ChatMessage.system(context.compact_state_text or "rules"), *context.history),
        )

    service = ContextService(app_ctx, ArtifactStore(app_ctx))
    events = []

    def compact():
        return prepare(
            service,
            ctx,
            rebuild(ctx),
            caller,
            rebuild,
            lambda name, payload: events.append((name, payload)),
            CancellationToken(),
        )

    return SimpleNamespace(
        ctx=ctx, compact=compact, summarize=summarize, events=events, app_ctx=app_ctx
    )


def state_of(w):
    return w.app_ctx.session_repo.get("cont").compact_state


def task(app_ctx, status, title="wire the parser", root="/repo"):
    app_ctx.task_repo.create(
        {
            "id": f"task_{status}_{title.replace(' ', '_')}",
            "project_root": root,
            "session_ref": "cont",
            "title": title,
            "description": "",
            "status": status,
            "acceptance": "",
            "implementation": "",
            "validation": "",
            "scope": "",
            "unresolved": "",
            "depends_on": "",
            "blockers": "",
            "evidence": "",
            "created_at": NOW,
            "updated_at": NOW,
        }
    )


def validation(app_ctx, result, root="/repo"):
    app_ctx.validation_repo.insert(
        {
            "id": f"val_{result}",
            "project_root": root,
            "session_ref": "cont",
            "kind": "test",
            "command": "pytest -q",
            "result": result,
            "summary": "",
            "detail": "",
            "artifact_ref": "",
            "created_at": NOW,
        }
    )


def more(w, label):
    w.ctx.history.extend(filler(label, 40))
    w.ctx.history.append(ChatMessage.user("continue"))


def test_goal_and_constraints_survive_three_compactions_and_a_restart(app_ctx):
    w = world(app_ctx)
    w.compact()
    for label in ("second", "third"):
        more(w, label)
        w.compact()
        state = state_of(w)
        assert state["goal"].startswith(GOAL), label
        assert any(RULE in c for c in state["constraints"]), label
    assert state["revision"] == 3
    # Restart: the session reopens from the persisted projection alone.
    reopened = render(state)
    assert GOAL in reopened and RULE in reopened
    w.ctx.compact_state_text = reopened
    more(w, "after-restart")
    w.compact()
    assert state_of(w)["goal"].startswith(GOAL)
    assert any(RULE in c for c in state_of(w)["constraints"])


def test_an_explicit_new_goal_replaces_the_old_one_and_keeps_the_rules(app_ctx):
    w = world(app_ctx)
    w.compact()
    w.ctx.history.append(ChatMessage.user("Nuevo objetivo: migrate the reports to PDF."))
    more(w, "second")
    w.compact()
    state = state_of(w)
    assert state["goal"] == "migrate the reports to PDF."
    assert any(RULE in c for c in state["constraints"])


def test_a_summary_contradicting_pending_tasks_is_not_published(app_ctx):
    task(app_ctx, "in_progress")
    summarize = Summarizer("Everything is complete. No pending work.")
    w = world(app_ctx, project_root="/repo", summarizer=summarize)
    original = list(w.ctx.history)
    with pytest.raises(ContextPreparationError, match="recorded"):
        w.compact()
    assert state_of(w) is None
    assert w.ctx.history == original
    # One bounded repair, never a loop.
    assert len(summarize.repairs) == 1
    failed = [p for name, p in w.events if name == "governor.compact" and p["status"] == "failed"]
    assert failed and failed[-1]["checks"]["records"] == "failed"


def test_one_repair_can_fix_a_contradiction(app_ctx):
    task(app_ctx, "in_progress")
    summarize = Summarizer("All tasks are done; nothing is pending.", repaired=GOOD)
    w = world(app_ctx, project_root="/repo", summarizer=summarize)
    w.compact()
    assert state_of(w)["summary"] == GOOD
    assert len(summarize.repairs) == 1
    assert "wire the parser" in summarize.repairs[0].messages[-1].content
    completed = [
        p for name, p in w.events if name == "governor.compact" and p["status"] == "completed"
    ]
    assert completed[-1]["checks"] == {
        "structure": "passed",
        "records": "repaired",
        "reduction": "passed",
    }


def test_completion_needs_recorded_evidence_even_in_a_chat(app_ctx):
    # The reproduction from the review: the user asked for an export and the
    # summary declares the work finished. Nothing records that it was done.
    summarize = Summarizer("Everything is complete. No pending work.")
    w = world(app_ctx, summarizer=summarize)
    with pytest.raises(ContextPreparationError):
        w.compact()
    assert state_of(w) is None


def test_a_summary_cannot_report_a_test_run_the_records_contradict(app_ctx):
    validation(app_ctx, "failed")
    summarize = Summarizer("The writer is done and all tests pass.")
    w = world(app_ctx, project_root="/repo", summarizer=summarize)
    with pytest.raises(ContextPreparationError, match="test"):
        w.compact()


def test_claims_backed_by_the_records_are_accepted(app_ctx):
    task(app_ctx, "done")
    validation(app_ctx, "passed")
    w = world(
        app_ctx,
        project_root="/repo",
        summarizer=Summarizer("All tasks are done and the tests pass."),
    )
    w.compact()
    completed = [
        p for name, p in w.events if name == "governor.compact" and p["status"] == "completed"
    ]
    assert completed[-1]["checks"]["records"] == "passed"


def test_the_instruction_keeps_operational_facts_out_of_the_summary(app_ctx):
    w = world(app_ctx)
    w.compact()
    instruction = w.summarize.requests[0].messages[0].content
    assert "nothing is pending" in instruction
    assert "tests passed" in instruction
