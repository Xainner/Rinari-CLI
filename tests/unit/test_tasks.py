"""Task graph + done-when contract (phase 3).

Regression contract:
- tasks are project-scoped;
- a task cannot start while its dependencies are not done;
- dependencies cannot form cycles;
- a task can be marked done only when its done-when contract is satisfied
  (acceptance + validation must exist and be satisfied, no unresolved items);
- retry resets blocked/cancelled tasks to pending and clears the blocker.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rinari.application.services import build_services
from rinari.shared.errors import (
    BlockedError,
    ConflictError,
    InvalidUsageError,
    NotFoundError,
    ValidationFailureError,
)

runner = CliRunner()

ACCEPTANCE = "- [x] callback patched\n- [x] root cause identified"
VALIDATION = "- [x] unit tests pass"


@pytest.fixture
def services(app_ctx, tmp_path):
    user_home = tmp_path / "home"
    user_home.mkdir()
    return build_services(app_ctx, user_home=user_home)


@pytest.fixture
def project(tmp_path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    return root


def test_add_and_list(services, project) -> None:
    task = services.tasks.add(
        project, "Patch auth callback", acceptance=ACCEPTANCE, validation=VALIDATION
    )
    assert task["status"] == "pending"
    assert task["id"].startswith("task_")

    listing = services.tasks.list(project)
    assert [t["id"] for t in listing] == [task["id"]]
    shown = services.tasks.show(project, task["id"])
    assert shown["done_when"]["can_complete"] is True
    assert shown["done_when"]["acceptance"]["total"] == 2


def test_add_is_project_scoped(services, project, tmp_path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    services.tasks.add(project, "Scoped task", acceptance=ACCEPTANCE)
    assert services.tasks.list(other) == []
    assert len(services.tasks.list(project)) == 1


def test_in_progress_requires_done_dependencies(services, project) -> None:
    dep = services.tasks.add(
        project, "Find root cause", acceptance=ACCEPTANCE, validation=VALIDATION
    )
    task = services.tasks.add(
        project, "Patch code", depends_on=dep["id"], acceptance=ACCEPTANCE, validation=VALIDATION
    )
    with pytest.raises(BlockedError):
        services.tasks.update(project, task["id"], status="in_progress")

    services.tasks.update(project, dep["id"], status="in_progress")
    services.tasks.update(project, dep["id"], status="done")

    started = services.tasks.update(project, task["id"], status="in_progress")
    assert started["status"] == "in_progress"
    blockers = services.tasks.blockers(project)
    assert blockers == []


def test_dependency_cycle_rejected(services, project) -> None:
    a = services.tasks.add(project, "A", acceptance=ACCEPTANCE)
    b = services.tasks.add(project, "B", depends_on=a["id"])
    with pytest.raises(ConflictError):
        services.tasks.update(project, a["id"], depends_on=f"{a['id']},{b['id']}")


def test_unknown_dependency_rejected(services, project) -> None:
    with pytest.raises(InvalidUsageError):
        services.tasks.add(project, "Ghost dep", depends_on="task_nope")


def test_done_requires_done_when(services, project) -> None:
    task = services.tasks.add(
        project,
        "Incomplete task",
        acceptance="- [x] did a\n- [ ] did b",
        validation="- [ ] tests pass",
    )
    with pytest.raises(ValidationFailureError) as excinfo:
        services.tasks.update(project, task["id"], status="done")
    assert "acceptance" in str(excinfo.value)

    missing_valid = services.tasks.add(project, "No validation", acceptance=ACCEPTANCE)
    with pytest.raises(ValidationFailureError):
        services.tasks.update(project, missing_valid["id"], status="done")


def test_done_accepts_satisfied_contract(services, project) -> None:
    task = services.tasks.add(project, "Ready task", acceptance=ACCEPTANCE, validation=VALIDATION)
    done = services.tasks.update(project, task["id"], status="done")
    assert done["status"] == "done"
    with pytest.raises(InvalidUsageError):
        services.tasks.cancel(project, task["id"])


def test_unresolved_criteria_block_done(services, project) -> None:
    task = services.tasks.add(
        project,
        "Has unresolved",
        acceptance=ACCEPTANCE,
        validation=VALIDATION,
        unresolved="- vendor does not ship the fix yet",
    )
    with pytest.raises(ValidationFailureError):
        services.tasks.update(project, task["id"], status="done")
    services.tasks.update(project, task["id"], unresolved="")
    done = services.tasks.update(project, task["id"], status="done")
    assert done["status"] == "done"


def test_block_cancel_retry_cycle(services, project) -> None:
    task = services.tasks.add(project, "Blocked task", acceptance=ACCEPTANCE, validation=VALIDATION)
    blocked = services.tasks.update(
        project, task["id"], status="blocked", blocker="waiting on vendor"
    )
    assert blocked["blockers"] == "waiting on vendor"

    items = services.tasks.blockers(project)
    assert items[0]["id"] == task["id"]
    assert items[0]["blocker"] == "waiting on vendor"

    retried = services.tasks.retry(project, task["id"])
    assert retried["status"] == "pending"
    assert retried["blockers"] == ""

    cancelled = services.tasks.cancel(project, task["id"])
    assert cancelled["status"] == "cancelled"
    retried_again = services.tasks.retry(project, task["id"])
    assert retried_again["status"] == "pending"

    with pytest.raises(InvalidUsageError):
        services.tasks.retry(project, task["id"])


def test_show_missing_task_raises(services, project) -> None:
    with pytest.raises(NotFoundError):
        services.tasks.show(project, "task_missing")


def test_tree_depths(services, project) -> None:
    a = services.tasks.add(project, "Root task", acceptance=ACCEPTANCE)
    b = services.tasks.add(project, "Child task", depends_on=a["id"])
    c = services.tasks.add(project, "Grandchild task", depends_on=b["id"])
    depths = services.tasks.tree(project)["depths"]
    assert depths[a["id"]] == 0
    assert depths[b["id"]] == 1
    assert depths[c["id"]] == 2


def test_cli_tasks_lifecycle(tmp_path, monkeypatch) -> None:
    import rinari.cli.deps as cli_deps
    from rinari.cli.main import app as cli_app
    from rinari.shared.paths import ENV_HOME

    home = tmp_path / "rinari-home"
    monkeypatch.setenv(ENV_HOME, str(home))
    monkeypatch.setattr(cli_deps, "_global_json", True)
    proj = tmp_path / "proj"
    proj.mkdir()

    def invoke_cli(sub: list[str]):
        return runner.invoke(cli_app, ["tasks", *sub, "--project", str(proj)])

    add = invoke_cli(
        [
            "add",
            "Patch auth callback",
            "--acceptance",
            "- [x] callback patched",
            "--validation",
            "- [ ] integration tests pass",
        ]
    )
    assert add.exit_code == 0, add.output
    payload = json.loads(add.output)
    task_id = payload["data"]["id"]

    with_deps = invoke_cli(
        [
            "add",
            "Follow-up",
            "--acceptance",
            "- [x] done",
            "--validation",
            "- [x] ok",
            "--depends-on",
            task_id,
        ]
    )
    assert with_deps.exit_code == 0, with_deps.output
    followup_id = json.loads(with_deps.output)["data"]["id"]

    # Cannot start the follow-up while its dependency is pending.
    blocked = invoke_cli(["update", followup_id, "--status", "in_progress"])
    assert blocked.exit_code != 0
    assert json.loads(blocked.output)["ok"] is False

    # Satisfy the first task's contract, then complete it.
    satisfy = invoke_cli(["update", task_id, "--validation", "- [x] integration tests pass"])
    assert satisfy.exit_code == 0, satisfy.output
    done = invoke_cli(["update", task_id, "--status", "done"])
    assert done.exit_code == 0, done.output
    followup = invoke_cli(["update", followup_id, "--status", "done"])
    assert followup.exit_code == 0, followup.output

    listing = invoke_cli(["list"])
    assert listing.exit_code == 0
    assert json.loads(listing.output)["data"][0]["status"] == "done"
