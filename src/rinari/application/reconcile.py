"""Resume reconciliation (phase 4).

Before a session is continued, re-verify the durable facts it assumes and,
where safe, re-establish them. Subsystems checked:

    identity       project identity row vs disk (re-registered when missing)
    git-branch     branch at session start vs currently checked out
    working-tree   session-start worktree baseline vs current dirty state
    permissions    active permission profile vs the session's recorded one
    provider       provider row still exists
    model          model row still exists
    trust          project trust / revalidation state
    skills         active (name, version) pairs vs the discovered skill catalog
    assumptions    recorded cwd still exists

The only automatic fix is re-registering a missing project identity row, or
re-linking the session to the row that exists for its recorded root. Nothing
here mutates the working tree, and no stale assumption is silently dropped:
anything but state "ok" surfaces as a warning the user can act on.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from rinari.application.context import AppContext
from rinari.application.project_service import ProjectService
from rinari.projects.git import git_state
from rinari.projects.worktree import snapshot_worktree
from rinari.skills.catalog import SkillInfo, discover_skills
from rinari.storage.records import SessionRecord
from rinari.trust import STATE_REVALIDATION, STATE_TRUSTED, TrustService

OK = "ok"
FIXED = "fixed"
CHANGED = "changed"
MISSING = "missing"

_KIND_PROJECT = "PROJECT"


@dataclass(frozen=True, slots=True)
class Finding:
    subsystem: str
    state: str
    detail: str
    action: str = "none"

    @property
    def warning(self) -> str | None:
        if self.state == OK:
            return None
        return f"[{self.subsystem}] {self.detail}"


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    findings: tuple[Finding, ...] = ()

    @property
    def issues(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.state != OK)

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(w for w in (f.warning for f in self.issues) if w is not None)


def trust_warning(trust: TrustService, root: Path) -> str | None:
    if not root.exists():
        return None
    status = trust.status(root)
    hint = f"`rinari trust add {root}`"
    if status.state == STATE_REVALIDATION:
        return (
            f"project trust needs revalidation (identity changed since the grant) — re-run {hint}"
        )
    if status.state != STATE_TRUSTED:
        return (
            "project is not trusted: project instructions are withheld until you "
            f"explicitly trust it — {hint}"
        )
    return None


class ResumeReconciler:
    def __init__(
        self,
        ctx: AppContext,
        projects: ProjectService,
        trust: TrustService | None = None,
    ) -> None:
        self._ctx = ctx
        self._projects = projects
        self._trust = trust

    def reconcile(self, record: SessionRecord) -> tuple[SessionRecord, ReconciliationReport]:
        findings: list[Finding] = []
        root = (
            Path(record.project_root_snapshot).resolve() if record.project_root_snapshot else None
        )
        root_exists = root is not None and root.exists()
        updated = record

        if record.kind == _KIND_PROJECT and root is not None:
            finding, updated = self._identity(record, root, root_exists)
        else:
            finding = Finding("identity", OK, "CHAT session")
        findings.append(finding)
        findings.append(self._git_branch(record, root, root_exists))
        findings.append(self._working_tree(record, root, root_exists))
        findings.append(self._permissions(record))
        findings.append(self._provider(record))
        findings.append(self._model(record))
        findings.append(self._trust_finding(updated, root, root_exists))
        findings.append(self._skills(record, root, root_exists))
        findings.append(self._assumptions(record))

        if updated is not record:
            self._ctx.session_repo.update(updated)
        return updated, ReconciliationReport(tuple(findings))

    # -- subsystems ----------------------------------------------------------

    def _identity(
        self, record: SessionRecord, root: Path, root_exists: bool
    ) -> tuple[Finding, SessionRecord]:
        if not root_exists:
            return (
                Finding(
                    "identity",
                    MISSING,
                    f"project root no longer exists: {root}",
                    "warned",
                ),
                record,
            )
        row = self._ctx.project_repo.get_by_root(str(root))
        if row is None:
            row = self._projects.upsert(root)
            return (
                Finding(
                    "identity",
                    FIXED,
                    f"project identity row was missing and was re-registered (root: {root})",
                    "fixed",
                ),
                replace(record, project_id=row.id),
            )
        if row.id != record.project_id:
            return (
                Finding(
                    "identity",
                    FIXED,
                    f"session was re-linked to the project identity row for {root} ({row.id})",
                    "fixed",
                ),
                replace(record, project_id=row.id),
            )
        return Finding("identity", OK, f"project identity row present ({row.id})"), record

    def _git_branch(self, record: SessionRecord, root: Path | None, root_exists: bool) -> Finding:
        if record.kind != _KIND_PROJECT or root is None or not root_exists:
            return Finding("git-branch", OK, "no project root to compare")
        if record.git_branch is None:
            return Finding("git-branch", OK, "no session-start branch recorded")
        state = git_state(root)
        if not state.available or state.branch is None:
            return Finding("git-branch", OK, "git state unavailable")
        if state.branch != record.git_branch:
            return Finding(
                "git-branch",
                CHANGED,
                f"branch changed since session start: {record.git_branch} -> {state.branch}",
                "warned",
            )
        return Finding("git-branch", OK, f"branch {state.branch}")

    def _working_tree(self, record: SessionRecord, root: Path | None, root_exists: bool) -> Finding:
        if (
            record.kind != _KIND_PROJECT
            or root is None
            or not root_exists
            or not (root / ".git").exists()
        ):
            return Finding("working-tree", OK, "no git repo to compare")
        baselines = self._ctx.worktree_repo.list(record.id)
        if not baselines:
            return Finding("working-tree", OK, "no session-start baseline recorded")
        baseline = {r.path: (r.git_status, r.blob_sha) for r in baselines}
        current = snapshot_worktree(root)
        changed: list[str] = []
        for path, current_entry in sorted(current.items()):
            if baseline.get(path) != current_entry:
                changed.append(path)
        changed.extend(sorted(set(baseline) - set(current)))
        if changed:
            shown = ", ".join(changed[:3])
            more = f" (+{len(changed) - 3} more)" if len(changed) > 3 else ""
            return Finding(
                "working-tree",
                CHANGED,
                f"{len(changed)} path(s) differ from the session-start baseline: {shown}{more} "
                "— verify assumptions before acting",
                "warned",
            )
        return Finding("working-tree", OK, "working tree matches the session-start baseline")

    def _permissions(self, record: SessionRecord) -> Finding:
        active = self._ctx.config.active_profile_name()
        if record.profile_id and record.profile_id != active:
            return Finding(
                "permissions",
                CHANGED,
                f"active permission profile changed since the session "
                f"(session: {record.profile_id}, active: {active})",
                "warned",
            )
        return Finding("permissions", OK, f"profile {active}")

    def _provider(self, record: SessionRecord) -> Finding:
        if self._ctx.provider_repo.get(record.provider_id) is None:
            return Finding(
                "provider",
                MISSING,
                f"provider no longer exists (id {record.provider_id})",
                "warned",
            )
        return Finding("provider", OK, "provider still exists")

    def _model(self, record: SessionRecord) -> Finding:
        if self._ctx.model_repo.get(record.model_id) is None:
            return Finding(
                "model",
                MISSING,
                f"model no longer exists (id {record.model_id})",
                "warned",
            )
        return Finding("model", OK, "model still exists")

    def _trust_finding(
        self, record: SessionRecord, root: Path | None, root_exists: bool
    ) -> Finding:
        if record.kind != _KIND_PROJECT or root is None or not root_exists or self._trust is None:
            return Finding("trust", OK, "no trust grant to check")
        warning = trust_warning(self._trust, root)
        if warning is None:
            return Finding("trust", OK, "project is trusted")
        if "revalidation" in warning:
            return Finding("trust", CHANGED, warning, "warned")
        return Finding("trust", MISSING, warning, "warned")

    def _skills(self, record: SessionRecord, root: Path | None, root_exists: bool) -> Finding:
        if not record.active_skills:
            return Finding("skills", OK, "no active skills recorded")
        catalog = self._skill_catalog(root, root_exists)
        missing = sorted(name for name, _ in record.active_skills if name not in catalog)
        changed = sorted(
            f"{name} ({version} -> {catalog[name].version})"
            for name, version in record.active_skills
            if name in catalog and catalog[name].version != version
        )
        if missing:
            return Finding(
                "skills",
                MISSING,
                f"skill(s) no longer installed: {', '.join(missing)} — re-verify any "
                "procedure that relied on them",
                "warned",
            )
        if changed:
            return Finding(
                "skills",
                CHANGED,
                f"skill version changed since the session: {', '.join(changed)} — re-read "
                "it before relying on its procedure",
                "warned",
            )
        return Finding("skills", OK, f"{len(record.active_skills)} active skill(s) unchanged")

    def _skill_catalog(self, root: Path | None, root_exists: bool) -> dict[str, SkillInfo]:
        project_dir = root / ".rinari" / "skills" if root is not None and root_exists else None
        return discover_skills(
            str(self._ctx.layout.dir("skills")), str(project_dir) if project_dir else None
        )

    def _assumptions(self, record: SessionRecord) -> Finding:
        if not record.current_cwd:
            return Finding("assumptions", OK, "no recorded cwd")
        cwd = Path(record.current_cwd)
        if not cwd.exists():
            return Finding(
                "assumptions",
                MISSING,
                f"session cwd no longer exists: {cwd}",
                "warned",
            )
        return Finding("assumptions", OK, "recorded cwd still exists")
