"""Session service: CHAT/PROJECT resolution, open/resume/promote (phase 1).

Phase-1 scope: detection, persistence, resume reconciliation (provider/model
availability), and the atomic CHAT -> PROJECT promotion. Conversational
state (turns, tool calls) joins in phase 2 without changing this contract.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from rinari.application.context import AppContext
from rinari.application.project_service import ProjectService
from rinari.application.provider_service import ProviderService
from rinari.application.reconcile import Finding, ResumeReconciler, trust_warning
from rinari.policy.engine import PermissionProfile, normalize_profile
from rinari.projects.detector import ProjectDetection, detect_project
from rinari.shared.clock import now_iso
from rinari.shared.errors import (
    AuthenticationRequiredError,
    ConflictError,
    InvalidUsageError,
    NotFoundError,
    PermissionDeniedError,
)
from rinari.storage.records import (
    SessionEventRecord,
    SessionMessageRecord,
    SessionRecord,
    WorktreeBaselineRecord,
)
from rinari.trust import TrustService

EVENT_SESSION_STARTED = "SessionStarted"
EVENT_SESSION_PROMOTED = "SessionPromotedToProject"
EVENT_SESSION_FORKED = "SessionForked"
EVENT_SESSION_CLOSED = "SessionClosed"
EVENT_SESSION_RENAMED = "SessionRenamed"
EVENT_SESSION_ARCHIVED = "SessionArchived"
EVENT_SESSION_RESTORED = "SessionRestored"
EVENT_USER_PROMPT = "UserPrompt"

SESSION_KIND_CHAT = "CHAT"
SESSION_KIND_PROJECT = "PROJECT"
SESSION_STATE_ACTIVE = "active"
SESSION_STATE_CLOSED = "closed"
SESSION_STATE_ARCHIVED = "archived"

SESSION_MODE_PLAN = "plan"
SESSION_MODE_BUILD = "build"
SESSION_MODE_REVIEW = "review"
SESSION_MODES = (SESSION_MODE_PLAN, SESSION_MODE_BUILD, SESSION_MODE_REVIEW)


def profile_for_mode(mode: str | None) -> PermissionProfile:
    """Mode → execution policy. Legacy/unknown modes keep full workspace rights."""
    if mode in (SESSION_MODE_PLAN, SESSION_MODE_REVIEW):
        return PermissionProfile.READ_ONLY
    return PermissionProfile.WORKSPACE


def profile_for_session(record: SessionRecord) -> PermissionProfile:
    """Effective profile: PLAN/REVIEW are immutable read-only boundaries."""
    if record.mode in (SESSION_MODE_PLAN, SESSION_MODE_REVIEW):
        return PermissionProfile.READ_ONLY
    return normalize_profile(record.permission_profile)


@dataclass(frozen=True, slots=True)
class StartedSession:
    session: SessionRecord
    created: bool
    warnings: tuple[str, ...] = ()
    findings: tuple[Finding, ...] = ()


class SessionService:
    def __init__(
        self,
        ctx: AppContext,
        providers: ProviderService,
        projects: ProjectService,
        trust: TrustService | None = None,
        user_home: Path | None = None,
    ) -> None:
        self._ctx = ctx
        self._providers = providers
        self._projects = projects
        self._trust = trust
        self._user_home = user_home

    def _now(self) -> str:
        return now_iso(self._ctx.clock)

    def _home(self) -> Path:
        return self._user_home if self._user_home is not None else Path.home()

    # -- detection --------------------------------------------------------

    def detect(self, cwd: Path) -> ProjectDetection:
        return detect_project(Path(cwd), self._home())

    # -- start / resume ----------------------------------------------------

    def start(
        self, cwd: Path, forced_chat: bool = False, prompt: str | None = None
    ) -> StartedSession:
        cwd = Path(cwd).expanduser().resolve()
        detection = self.detect(cwd)
        project_root = None if forced_chat else detection.project_root
        kind = SESSION_KIND_PROJECT if project_root is not None else SESSION_KIND_CHAT

        selection = self._providers.current()
        if selection is None:
            raise AuthenticationRequiredError(
                "CONFIG_REQUIRED: Rinari is not configured yet.",
                hint=(
                    "Run: rinari setup (interactive wizard in a terminal), or:\n"
                    "  rinari providers add custom --name A --endpoint URL --api-key <key>\n"
                    "  rinari models add --provider A --model <id>"
                ),
            )
        if selection.model is None:
            raise AuthenticationRequiredError(
                f"No usable model for provider {selection.provider.alias!r}.",
                hint=(
                    "Add a model with `rinari models add --provider "
                    f"{selection.provider.alias} --model <id> --name <alias>`."
                ),
            )

        now = self._now()
        warnings: list[str] = []
        if project_root is not None:
            root_str = str(project_root.resolve())
            match = next(
                (
                    s
                    for s in self._ctx.session_repo.list(kind=SESSION_KIND_PROJECT, limit=100)
                    if s.project_root_snapshot == root_str and s.state == SESSION_STATE_ACTIVE
                ),
                None,
            )
        else:
            match = next(
                (
                    s
                    for s in self._ctx.session_repo.list(kind=SESSION_KIND_CHAT, limit=100)
                    if s.state == SESSION_STATE_ACTIVE
                ),
                None,
            )

        findings: tuple[Finding, ...] = ()
        if match is not None:
            record, report = self._reconciler().reconcile(match)
            for warning in report.warnings:
                warnings.append(warning)
            findings = report.findings
            record.current_cwd = str(cwd)
            record.last_active_at = now
            record.updated_at = now
            self._ctx.session_repo.update(record)
            created = False
        else:
            project_id = None
            project_root_snapshot = None
            if kind == SESSION_KIND_PROJECT and project_root is not None:
                project = self._projects.upsert(project_root)
                project_id = project.id
                project_root_snapshot = str(project_root.resolve())
            record = SessionRecord(
                id=self._ctx.ids.new("ses"),
                kind=kind,
                title=self._default_title(cwd, kind),
                project_id=project_id,
                project_root_snapshot=project_root_snapshot,
                created_cwd=str(cwd),
                current_cwd=str(cwd),
                provider_id=selection.provider.id,
                model_id=selection.model.id,
                profile_id=self._ctx.config.active_profile_name(),
                mode="ask",
                state=SESSION_STATE_ACTIVE,
                compact_state=None,
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
            with self._ctx.db.transaction():
                self._ctx.session_repo.insert(record)
                self._append_event(
                    record.id,
                    EVENT_SESSION_STARTED,
                    {"kind": kind, "project_id": project_id, "marker": detection.marker},
                )
            created = True
            if project_root_snapshot is not None:
                warning = (
                    trust_warning(self._trust, Path(project_root_snapshot)) if self._trust else None
                )
                if warning is not None:
                    warnings.append(warning)

        if prompt:
            self._append_event(record.id, EVENT_USER_PROMPT, {"prompt": prompt})
        return StartedSession(
            session=record, created=created, warnings=tuple(warnings), findings=findings
        )

    def resume(self, ref: str | None = None, cwd: Path | None = None) -> StartedSession:
        record: SessionRecord | None = None
        if ref is not None:
            record = self._resolve(ref)
            if record.state != SESSION_STATE_ACTIVE:
                record.state = SESSION_STATE_ACTIVE
        else:
            return self.start(cwd or Path.cwd())

        record, report = self._reconciler().reconcile(record)
        if cwd is not None:
            record.current_cwd = str(Path(cwd).expanduser().resolve())
        now = self._now()
        record.updated_at = now
        record.last_active_at = now
        self._ctx.session_repo.update(record)
        return StartedSession(
            session=record, created=False, warnings=report.warnings, findings=report.findings
        )

    def new(
        self,
        cwd: Path,
        title: str | None = None,
        forced_chat: bool = False,
        *,
        mode: str = "ask",
        permission_profile: str = "workspace",
    ) -> SessionRecord:
        cwd = Path(cwd).expanduser().resolve()
        detection = self.detect(cwd)
        project_root = None if forced_chat else detection.project_root
        kind = SESSION_KIND_PROJECT if project_root is not None else SESSION_KIND_CHAT
        selection = self._providers.current()
        if selection is None or selection.model is None:
            raise AuthenticationRequiredError(
                "CONFIG_REQUIRED: configure a provider and model first.",
                hint=(
                    "Run: rinari setup (interactive wizard in a terminal), or:\n"
                    "  rinari providers add custom --name A --endpoint URL --api-key <key>\n"
                    "  rinari models add --provider A --model <id>"
                ),
            )
        now = self._now()
        project_id = None
        project_root_snapshot = None
        if project_root is not None:
            project = self._projects.upsert(project_root)
            project_id = project.id
            project_root_snapshot = str(project_root.resolve())
        normalized_mode = (mode or "").strip().lower()
        if normalized_mode not in (*SESSION_MODES, "ask"):
            raise InvalidUsageError(f"Unknown session mode: {mode!r}.")
        normalized_profile = normalize_profile(permission_profile).value
        record = SessionRecord(
            id=self._ctx.ids.new("ses"),
            kind=kind,
            title=title or self._default_title(cwd, kind),
            project_id=project_id,
            project_root_snapshot=project_root_snapshot,
            created_cwd=str(cwd),
            current_cwd=str(cwd),
            provider_id=selection.provider.id,
            model_id=selection.model.id,
            profile_id=self._ctx.config.active_profile_name(),
            mode=normalized_mode,
            state=SESSION_STATE_ACTIVE,
            compact_state=None,
            created_at=now,
            updated_at=now,
            last_active_at=now,
            permission_profile=normalized_profile,
        )
        with self._ctx.db.transaction():
            self._ctx.session_repo.insert(record)
            self._append_event(
                record.id,
                EVENT_SESSION_STARTED,
                {"kind": kind, "project_id": project_id, "marker": detection.marker},
            )
        return record

    # -- fork -------------------------------------------------------------------

    def fork(self, ref: str, title: str | None = None) -> StartedSession:
        """Create an independent session continuing from `ref`.

        Fork state: kind, project identity, cwd, provider/model/profile,
        mode, compact state, stored branch, and the full conversation are
        copied. Preserve provenance: the new row keeps `forked_from` and a
        `SessionForked` event. Independent continuation: the copy has its
        own id, message seq space, and active state; the source session is
        not modified in any way.
        """
        source = self._resolve(ref)
        now = self._now()
        record = SessionRecord(
            id=self._ctx.ids.new("ses"),
            kind=source.kind,
            title=title or f"{source.title or source.id} (fork)",
            project_id=source.project_id,
            project_root_snapshot=source.project_root_snapshot,
            created_cwd=source.current_cwd,
            current_cwd=source.current_cwd,
            provider_id=source.provider_id,
            model_id=source.model_id,
            profile_id=source.profile_id,
            mode=source.mode,
            state=SESSION_STATE_ACTIVE,
            compact_state=source.compact_state,
            created_at=now,
            updated_at=now,
            last_active_at=now,
            git_branch=source.git_branch,
            forked_from=source.id,
            active_skills=source.active_skills,
            permission_profile=source.permission_profile,
        )
        messages = [
            SessionMessageRecord(
                id=self._ctx.ids.new("msg"),
                session_id=record.id,
                seq=0,
                role=message.role,
                content=message.content,
                tool_calls=message.tool_calls,
                tool_call_id=message.tool_call_id,
                name=message.name,
                created_at=message.created_at,
            )
            for message in self._ctx.message_repo.list(source.id)
        ]
        with self._ctx.db.transaction():
            self._ctx.session_repo.insert(record)
            if messages:
                self._ctx.message_repo.append_many(record.id, messages)
            self._append_event(record.id, EVENT_SESSION_FORKED, {"from": source.id})
        return StartedSession(session=record, created=True)

    # -- promotion ----------------------------------------------------------

    def move(self, session_ref: str, project_id: str | None) -> SessionRecord:
        """Rebind future execution; historical files/checkpoints stay at origin."""
        from rinari.sessions.turn_lock import SessionTurnLock

        record = self._resolve(session_ref)
        lock_path = self._ctx.layout.dir("sessions") / f"{record.id}.turn.lock"
        with SessionTurnLock(lock_path, record.id):
            return self._move_workspace(record.id, project_id)

    def _move_workspace(self, session_ref: str, project_id: str | None) -> SessionRecord:
        record = self._resolve(session_ref)
        if record.state != SESSION_STATE_ACTIVE:
            raise ConflictError("Restore the session before moving it.")
        project = self._projects.get(project_id) if project_id else None
        if project is not None and project.archived:
            raise ConflictError("Restore the destination project first.")
        original = Path(record.created_cwd).resolve()
        general = (
            original
            if original.is_relative_to((self._ctx.home / "workspaces").resolve())
            else self._ctx.home / "workspaces" / record.id
        )
        root = (Path(project.canonical_root) if project else general).resolve()
        if project is None:
            root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir() or root == self._home():
            raise PermissionDeniedError("Destination must be an available workspace directory.")
        if root == Path(record.current_cwd).resolve() and record.project_id == project_id:
            return record
        previous = {"cwd": record.current_cwd, "project_id": record.project_id}
        baseline = [asdict(row) for row in self._ctx.worktree_repo.list(record.id)]
        restored_baseline = []
        for event in self._ctx.event_repo.list(record.id):
            if event.type == "session.moved" and event.payload.get("previous", {}).get(
                "cwd"
            ) == str(root):
                restored_baseline = event.payload.get("baseline", [])
        pins = [
            p
            for p in self._ctx.pin_repo.list(record.id)
            if p["source"] in ("file", "symbol", "term")
        ]
        record.current_cwd = str(root)
        record.kind = SESSION_KIND_PROJECT if project else SESSION_KIND_CHAT
        record.project_id = project.id if project else None
        record.project_root_snapshot = str(root) if project else None
        record.compact_state = None
        record.active_skills = None
        record.git_branch = None
        record.updated_at = self._now()
        with self._ctx.db.transaction():
            self._ctx.session_repo.update(record)
            self._ctx.worktree_repo.insert_many(
                record.id, [WorktreeBaselineRecord(**row) for row in restored_baseline]
            )
            for pin in pins:
                self._ctx.pin_repo.unpin(record.id, pin["source"], pin["pin_ref"])
            self._append_event(
                record.id,
                "session.moved",
                {
                    "previous": previous,
                    "cwd": str(root),
                    "project_id": record.project_id,
                    "baseline": baseline,
                    "previous_pins": pins,
                },
            )
        return record

    def promote(self, session_ref: str, project_root: Path) -> SessionRecord:
        root = Path(project_root).expanduser().resolve()
        if root == self._home():
            raise PermissionDeniedError(
                "$HOME is never an implicit project workspace",
                hint="Promote into a project subdirectory instead.",
            )
        record = self._resolve(session_ref)
        now = self._now()
        with self._ctx.db.transaction():
            if record.kind != SESSION_KIND_CHAT:
                raise ConflictError(f"Session {record.id} is already a {record.kind} session")
            project = self._projects.upsert(root)
            record.kind = SESSION_KIND_PROJECT
            record.project_id = project.id
            record.project_root_snapshot = str(root)
            record.updated_at = now
            record.last_active_at = now
            self._ctx.session_repo.update(record)
            self._append_event(
                record.id,
                EVENT_SESSION_PROMOTED,
                {
                    "session_id": record.id,
                    "previous_kind": SESSION_KIND_CHAT,
                    "project_id": project.id,
                    "project_root": str(root),
                },
            )
        return record

    def find_promotable_chat_session(self, candidate_root: Path) -> SessionRecord | None:
        """Most recent active CHAT session whose cwd lives under candidate_root."""
        root = Path(candidate_root).expanduser().resolve()
        for session in self._ctx.session_repo.list(kind=SESSION_KIND_CHAT, limit=100):
            if session.state != SESSION_STATE_ACTIVE:
                continue
            cwd = Path(session.created_cwd)
            if cwd == root or root in cwd.parents:
                return session
        return None

    # -- queries --------------------------------------------------------------

    def list(
        self,
        kind: str | None = None,
        *,
        project_id: str | None = None,
        state: str | None = None,
        limit: int = 50,
    ) -> list[SessionRecord]:
        records = self._ctx.session_repo.list(
            kind=kind,
            project_id=project_id,
            state=state,
            limit=limit,
        )
        # Older Windows transports decoded UTF-8 titles with the ANSI locale.
        # Repair only the known generated title; never guess at user-authored text.
        default_title = "Nueva conversación"
        broken_titles = {
            default_title.encode("utf-8").decode(codec) for codec in ("cp1252", "latin-1")
        }
        for record in records:
            if record.title in broken_titles:
                record.title = default_title
                self._ctx.session_repo.update(record)
        return records

    def show(self, ref: str) -> SessionRecord:
        return self._resolve(ref)

    def close(self, ref: str) -> SessionRecord:
        """Mark a session closed: hidden from default lists, restorable.

        Idempotent: closing a closed session returns it unchanged.
        `resume` re-activates a closed session (existing behavior).
        """
        record = self._resolve(ref)
        if record.state == SESSION_STATE_CLOSED:
            return record
        record.state = SESSION_STATE_CLOSED
        now = self._now()
        record.updated_at = now
        record.last_active_at = now
        with self._ctx.db.transaction():
            self._ctx.session_repo.update(record)
            self._append_event(record.id, EVENT_SESSION_CLOSED, {})
        return record

    def name_from_first_message(self, ref: str, message: str) -> SessionRecord:
        """Give an untouched session a bounded, Unicode-safe first-message title."""
        record = self._resolve(ref)
        defaults = {
            "Nueva conversación",
            "New conversation",
            "New chat",
            "",
            self._default_title(Path(record.current_cwd), record.kind),
        }
        if record.title not in defaults or self._ctx.message_repo.list(record.id):
            return record
        if any(e.type == EVENT_SESSION_RENAMED for e in self._ctx.event_repo.list(record.id)):
            return record
        clean = " ".join(message.split())
        if not clean:
            return record
        if len(clean) > 72:
            prefix = clean[:69]
            clean = (prefix.rsplit(" ", 1)[0] or prefix) + "…"
        return self.rename(ref, clean)

    def rename(self, ref: str, title: str) -> SessionRecord:
        record = self._resolve(ref)
        clean = (title or "").strip()
        if not clean:
            raise InvalidUsageError("Session title must not be empty.")
        if len(clean) > 160:
            raise InvalidUsageError("Session title must be at most 160 characters.")
        record.title = clean
        record.updated_at = self._now()
        with self._ctx.db.transaction():
            self._ctx.session_repo.update(record)
            self._append_event(record.id, EVENT_SESSION_RENAMED, {"title": clean})
        return record

    def archive(self, ref: str) -> SessionRecord:
        record = self._resolve(ref)
        if record.state == SESSION_STATE_ARCHIVED:
            return record
        record.state = SESSION_STATE_ARCHIVED
        now = self._now()
        record.updated_at = now
        record.last_active_at = now
        with self._ctx.db.transaction():
            self._ctx.session_repo.update(record)
            self._append_event(record.id, EVENT_SESSION_ARCHIVED, {})
        return record

    def restore(self, ref: str) -> SessionRecord:
        record = self._resolve(ref)
        if record.state == SESSION_STATE_ACTIVE:
            return record
        record.state = SESSION_STATE_ACTIVE
        now = self._now()
        record.updated_at = now
        record.last_active_at = now
        with self._ctx.db.transaction():
            self._ctx.session_repo.update(record)
            self._append_event(record.id, EVENT_SESSION_RESTORED, {})
        return record

    def delete(self, ref: str) -> str:
        """Delete a session and its messages/events (irreversible).

        Same record scope as the CLI `session delete`: checkpoints,
        artifacts, tasks and the turn queue are owned by their own
        surfaces (see the engine `session.delete` cascade accounting).
        """
        record = self._resolve(ref)
        with self._ctx.db.transaction():
            self._ctx.db.execute("DELETE FROM session_events WHERE session_id = ?", (record.id,))
            self._ctx.db.execute("DELETE FROM session_messages WHERE session_id = ?", (record.id,))
            self._ctx.db.execute("DELETE FROM sessions WHERE id = ?", (record.id,))
        return record.id

    def latest_for_root(self, root: str | Path) -> SessionRecord | None:
        """Latest active session bound to a project root (or None)."""
        canonical = str(Path(root).expanduser().resolve())
        for session in self._ctx.session_repo.list(limit=500):
            if session.project_root_snapshot == canonical and session.state == SESSION_STATE_ACTIVE:
                return session
        return None

    def touch(self, ref: str) -> SessionRecord:
        """Refresh activity timestamps (e.g. the root was opened again)."""
        record = self._resolve(ref)
        now = self._now()
        record.updated_at = now
        record.last_active_at = now
        self._ctx.session_repo.update(record)
        return record

    def set_mode(self, ref: str, mode: str) -> SessionRecord:
        """Switch PLAN/BUILD/REVIEW. Same session, task graph and context kept."""
        record = self._resolve(ref)
        normalized = (mode or "").strip().lower()
        if normalized not in SESSION_MODES:
            raise InvalidUsageError(
                f"Unknown session mode: {mode!r}. Valid modes: plan, build, review."
            )
        if record.mode != normalized:
            record.mode = normalized
            record.updated_at = self._now()
            self._ctx.session_repo.update(record)
        return record

    def set_permission(self, ref: str, profile: str) -> SessionRecord:
        record = self._resolve(ref)
        if profile not in {item.value for item in PermissionProfile}:
            raise InvalidUsageError(
                f"Unknown permission profile: {profile!r}. "
                "Valid profiles: read-only, workspace, full-access."
            )
        normalized = normalize_profile(profile).value
        if record.permission_profile != normalized:
            record.permission_profile = normalized
            record.updated_at = self._now()
            self._ctx.session_repo.update(record)
        return record

    def set_soul(self, ref: str, soul_id: str) -> SessionRecord:
        """Pin a session-scope Soul override. Unknown ids fail (NotFoundError)
        before anything is written; the SoulStore stays the validator."""
        from rinari.soul.store import SoulStore

        record = self._resolve(ref)
        SoulStore(self._ctx.home).get(soul_id)
        if record.soul_id != soul_id:
            record.soul_id = soul_id
            record.updated_at = self._now()
            self._ctx.session_repo.update(record)
        return record

    def clear_soul(self, ref: str) -> SessionRecord:
        """Drop the session override; the global Soul 3.0 chain applies again."""
        record = self._resolve(ref)
        if record.soul_id is not None:
            record.soul_id = None
            record.updated_at = self._now()
            self._ctx.session_repo.update(record)
        return record

    def set_model(self, ref: str, model_ref: str, provider_ref: str | None = None) -> SessionRecord:
        """Persist the provider/model used by subsequent turns of one session."""
        record = self._resolve(ref)
        model = self._ctx.model_repo.get(model_ref)
        if model is None:
            raise NotFoundError(f"Model not found: {model_ref}")
        provider = self._providers.get(provider_ref or model.provider_id)
        if model.provider_id != provider.id:
            raise InvalidUsageError(
                f"Model {model_ref!r} does not belong to provider {provider.alias!r}."
            )
        if record.provider_id != provider.id or record.model_id != model.id:
            record.provider_id = provider.id
            record.model_id = model.id
            record.updated_at = self._now()
            self._ctx.session_repo.update(record)
        return record

    def _resolve(self, ref: str) -> SessionRecord:
        record = self._ctx.session_repo.get(ref)
        if record is not None:
            return record
        prefixes = [s for s in self._ctx.session_repo.list(limit=500) if s.id.startswith(ref)]
        if len(prefixes) == 1:
            return prefixes[0]
        if len(prefixes) > 1:
            known = ", ".join(s.id for s in prefixes[:5])
            raise InvalidUsageError(
                f"Ambiguous session reference {ref!r}", hint=f"Matches: {known}"
            )
        raise NotFoundError(f"Session not found: {ref}", hint="Use `rinari session list`.")

    # -- helpers -----------------------------------------------------------------

    def _reconciler(self) -> ResumeReconciler:
        return ResumeReconciler(self._ctx, self._projects, self._trust)

    def _append_event(self, session_id: str, event_type: str, payload: dict) -> None:
        self._ctx.event_repo.insert(
            SessionEventRecord(
                id=self._ctx.ids.new("evt"),
                session_id=session_id,
                seq=self._ctx.event_repo.next_seq(session_id),
                type=event_type,
                payload=payload,
                created_at=self._now(),
            )
        )

    @staticmethod
    def _default_title(cwd: Path, kind: str) -> str:
        name = cwd.name or "root"
        return f"{kind.lower()} in {name}"
