"""Verification service: application wiring for evidence, plan, and gate.

The service owns project-root resolution and storage access; the planning
and gate math stay in the pure modules (`planner.py`, `gate.py`).
"""

from __future__ import annotations

from pathlib import Path

from rinari.application.context import AppContext
from rinari.shared.errors import InvalidUsageError
from rinari.verify.gate import GateDecision, evaluate_gate
from rinari.verify.planner import VerificationPlan, plan_verification
from rinari.verify.records import record_validation


class VerificationService:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    @property
    def repo(self):
        return self._ctx.validation_repo

    def _root(self, path: str | Path) -> Path:
        root = Path(path).expanduser()
        if not root.exists():
            raise InvalidUsageError(
                f"Path does not exist: {root}",
                hint="Point the verification target at an existing project directory.",
            )
        resolved = root.resolve()
        if not resolved.is_dir():
            raise InvalidUsageError(f"Not a directory: {resolved}")
        return resolved

    # -- evidence -------------------------------------------------------------

    def record(
        self,
        path: str | Path,
        *,
        kind: str,
        result: str,
        command: str = "",
        summary: str = "",
        detail: str = "",
        artifact_ref: str | None = None,
        session_ref: str | None = None,
    ) -> dict:
        project_root = str(self._root(path))
        return record_validation(
            self._ctx.validation_repo,
            project_root=project_root,
            kind=kind,
            result=result,
            command=command,
            summary=summary,
            detail=detail,
            artifact_ref=artifact_ref,
            session_ref=session_ref,
            clock=self._ctx.clock,
            ids=self._ctx.ids,
        )

    def latest(
        self,
        path: str | Path,
        *,
        kinds: tuple[str, ...] | None = None,
        limit: int = 50,
    ) -> list[dict]:
        return self._ctx.validation_repo.list(str(self._root(path)), kinds=kinds, limit=limit)

    # -- planning ---------------------------------------------------------------

    def _discovered_commands(self, root: Path) -> dict[str, list[str]]:
        from rinari.repo.state import analyze_repository  # local: keep import light

        summary = analyze_repository(root)
        return {
            "test": [hint.command for hint in summary.test],
            "lint": [hint.command for hint in summary.lint],
            "typecheck": [hint.command for hint in summary.typecheck],
            "build": [hint.command for hint in summary.build],
        }

    def _test_map(self, project_root: str) -> dict[str, list[str]]:
        rows = self._ctx.index_repo.test_map(project_root)
        if not rows:
            return {}
        mapping: dict[str, list[str]] = {}
        for row in rows:
            mapping.setdefault(row["target_file"], []).append(row["test_file"])
        return mapping

    def plan(
        self,
        path: str | Path,
        changed_files: list[str],
        *,
        user_constraints: list[str] | None = None,
        trusted: bool = True,
    ) -> VerificationPlan:
        root = self._root(path)
        project_root = str(root)
        constraints = (
            [str(c).strip() for c in user_constraints if c and str(c).strip()]
            if user_constraints
            else []
        )
        instructions = self.project_instructions(root) if trusted else []
        return plan_verification(
            changed_files,
            test_map=self._test_map(project_root),
            discovered=self._discovered_commands(root),
            user_constraints=constraints,
            project_instructions=instructions,
        )

    def project_instructions(self, root: Path) -> list[str]:
        from rinari.instructions.resolver import resolve_project_instructions

        entries = resolve_project_instructions(root, root)
        return [entry.content for entry in entries]

    # -- completion gate ----------------------------------------------------------

    def evaluate(
        self,
        path: str | Path,
        *,
        required_kinds: tuple[str, ...] = ("test",),
        unresolved: list[str] | None = None,
        blocked_reason: str | None = None,
        task_ids: list[str] | None = None,
        require_evidence: bool = True,
    ) -> GateDecision:
        project_root = str(self._root(path))
        tasks = None
        if task_ids:
            from rinari.tasks.core import done_when_report

            tasks = []
            for task_id in task_ids:
                task = self._ctx.task_repo.get(task_id)
                if task is None:
                    raise InvalidUsageError(f"Task not found: {task_id}")
                report = done_when_report(task)
                report["title"] = task.get("title") or task_id
                report["id"] = task["id"]
                tasks.append(report)
        records = self._ctx.validation_repo.list(project_root, limit=200)
        return evaluate_gate(
            records=records,
            required_kinds=tuple(required_kinds),
            unresolved=tuple(unresolved or ()),
            blocked_reason=blocked_reason,
            tasks=tasks,
            require_evidence=require_evidence,
        )
