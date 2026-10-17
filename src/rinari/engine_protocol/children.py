"""Child sessions: what a mother conversation opens for a separate piece of work.

Plan: docs/planes/plan-sesiones-hijas.md, section 5 (Rinari-Agent).

- `session.start` opens a child when the owner asked; an accepted offer, a
  note or a plan step open one through `create` too.
- The child inherits the mother's permission profile and the approvals she
  had «in this session» when it is born, never more.
- In a git project a child works in its own worktree by default.
- When a child finishes a turn the owner did not drive, the mother gets a
  report; if Rinari opened the child and the mother is free, the mother runs
  one short turn with the new reports.
- Merging a child's branch is the owner's decision (`integrate`), never the
  model's.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
import threading
from pathlib import Path
from typing import Any

from rinari.children import ACTIVE_LIMIT, DEPTH_LIMIT, ChildRecord, ChildStore
from rinari.children.store import OPENED_BY_RINARI, SUMMARY_CHARS
from rinari.engine_protocol import errors
from rinari.engine_protocol.errors import EngineProtocolError
from rinari.engine_protocol.messages import event
from rinari.shared.errors import ConflictError, InvalidUsageError, NotFoundError

logger = logging.getLogger(__name__)

LINEAGE_KIND = {"start": "delegated", "offer": "offer", "note": "note", "plan_step": "plan_step"}
CONTEXTS = ("brief", "fork")
ISOLATIONS = ("auto", "worktree", "same")
# How a turn of a child came to be: these report to the mother.
REPORTING_SOURCES = ("child_brief", "parent_message")
PARENT_PREFIX = (
    "[A message from the conversation that opened you (your mother, Rinari on the "
    "user's behalf), not typed by the user. Take it into account in this task.]\n\n"
)
REPORT_TEXT_CHARS = 4000


def isolation_key(root: Path) -> str:
    return f"children.isolation.{Path(root).resolve().as_posix()}"


def _git(root: Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, proc.stdout.strip()


class ChildSessions:
    def __init__(self, turns: Any) -> None:
        self._turns = turns
        self._services = turns._services
        self._lock = threading.RLock()

    @property
    def store(self) -> ChildStore:
        return ChildStore(self._services.ctx.db)

    # -- creating ----------------------------------------------------------------

    def create(
        self,
        parent_session_id: str,
        *,
        title: str,
        prompt: str,
        origin: str,
        context: str = "brief",
        isolation: str = "auto",
        done_when: str = "",
        external_content: bool = False,
    ) -> dict[str, Any]:
        """Open a child of `parent_session_id` and start its brief."""
        title = " ".join(str(title or "").split())[:80]
        prompt = str(prompt or "").strip()
        if len(title) < 3 or len(prompt) < 10:
            raise InvalidUsageError("A child needs a title and a self-contained task.")
        if context not in CONTEXTS:
            raise InvalidUsageError(f"context must be one of {', '.join(CONTEXTS)}.")
        if isolation not in ISOLATIONS:
            raise InvalidUsageError(f"isolation must be one of {', '.join(ISOLATIONS)}.")
        sessions = self._services.sessions
        repo = self._services.ctx.session_repo
        with self._lock:
            parent = sessions.show(parent_session_id)
            store = self.store
            if store.depth(parent.id) + 1 > DEPTH_LIMIT:
                raise ConflictError(
                    "This conversation is already a child of a child: leave a note instead."
                )
            working = store.working(parent.id)
            if len(working) >= ACTIVE_LIMIT:
                raise ConflictError(
                    f"{ACTIVE_LIMIT} child sessions are already working: "
                    "wait for one or leave a note."
                )
            project = self._services.projects.get(parent.project_id) if parent.project_id else None
            if project is not None and project.archived:
                raise ConflictError("Restore the project before opening a child.")
            root = Path(project.canonical_root) if project is not None else None
            if context == "fork":
                record = sessions.fork(parent.id, title=title).session
            else:
                cwd = root if root is not None else Path(parent.current_cwd)
                record = sessions.new(
                    cwd=cwd,
                    title=title,
                    forced_chat=project is None,
                    mode=parent.mode if parent.mode in ("plan", "build", "review") else "build",
                    permission_profile=parent.permission_profile,
                    rinari_profile_id=None if project is not None else parent.rinari_profile_id,
                )
                if root is not None and record.kind != "PROJECT":
                    record = sessions.promote(record.id, root)
            worktree = self._worktree_for(root, isolation, title, working)
            if worktree is not None:
                record = sessions.show(record.id)
                record.current_cwd = str(worktree["path"])
                repo.update(record)
            self._inherit_grants(parent.id, record.id, root, worktree)
            brief = {"title": title, "request": prompt, "origin": origin}
            if done_when:
                brief["done_when"] = done_when
            repo.set_lineage(record.id, parent.id, LINEAGE_KIND[origin], brief)
            child = store.insert(
                ChildRecord(
                    child_session_id=record.id,
                    parent_session_id=parent.id,
                    origin=origin,
                    context=context,
                    worktree_path=str(worktree["path"]) if worktree else None,
                    branch=worktree["branch"] if worktree else None,
                    base_commit=worktree["base"] if worktree else None,
                    external_content=external_content,
                )
            )
        message = self._brief_message(parent, prompt, done_when, worktree)
        turn = self._turns.start_turn(
            record.id,
            message,
            display_message=prompt,
            origin={
                "kind": "user",
                "source": "child_brief",
                "parent_session_id": parent.id,
                "child_origin": origin,
                **({"external_content": True} if external_content else {}),
            },
        )
        self._emit(child, title)
        return {
            **child.as_dict(),
            "title": title,
            "turn_id": turn.get("turn_id") if isinstance(turn, dict) else None,
        }

    def _worktree_for(
        self, root: Path | None, isolation: str, title: str, working: list[ChildRecord]
    ) -> dict[str, Any] | None:
        """Its own worktree in a git project, unless asked otherwise (A-5)."""
        if root is None or isolation == "same":
            return None
        code, inside = _git(root, "rev-parse", "--is-inside-work-tree")
        if code != 0 or inside != "true":
            if isolation == "worktree":
                raise ConflictError("The project is not a git repository.")
            return None
        # The owner chose the same folder for this project; still never two
        # writers in it at once.
        if (
            isolation == "auto"
            and self._project_prefers_same(root)
            and not any(child.worktree_path is None for child in working)
        ):
            return None
        from rinari.agents.worktree_manager import WORKTREE_PARENT, WorktreeError, WorktreeManager

        try:
            info = WorktreeManager(root).create(f"child-{title}")
        except WorktreeError as exc:
            if isolation == "worktree":
                raise ConflictError(exc.message) from exc
            logger.warning("child worktree unavailable: %s", exc.message)
            return None
        self._exclude(root, f"{WORKTREE_PARENT}/")
        _code, base = _git(info.path, "rev-parse", "HEAD")
        return {"path": info.path, "branch": info.branch, "base": base or None}

    def _project_prefers_same(self, root: Path) -> bool:
        with contextlib.suppress(Exception):
            return self._services.ctx.config_repo.get(isolation_key(root)) == "same"
        return False

    def set_project_isolation(self, root: Path, isolation: str) -> None:
        """«Use the same folder in this project» from the child's card (A-5)."""
        from datetime import UTC, datetime

        from rinari.storage.records import ConfigValue

        if isolation not in ("same", "worktree"):
            raise InvalidUsageError("isolation must be same or worktree.")
        self._services.ctx.config_repo.set(
            ConfigValue(
                key=isolation_key(root),
                value=isolation,
                updated_at=datetime.now(UTC).isoformat(timespec="seconds"),
            )
        )

    @staticmethod
    def _exclude(root: Path, pattern: str) -> None:
        """Worktrees live inside the project: git must not list them as new files."""
        code, git_dir = _git(root, "rev-parse", "--git-common-dir")
        if code != 0 or not git_dir:
            return
        path = Path(git_dir)
        if not path.is_absolute():
            path = root / path
        exclude = path / "info" / "exclude"
        with contextlib.suppress(OSError):
            current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
            if pattern not in current.splitlines():
                exclude.parent.mkdir(parents=True, exist_ok=True)
                exclude.write_text(
                    current
                    + ("" if current.endswith("\n") or not current else "\n")
                    + pattern
                    + "\n",
                    encoding="utf-8",
                )

    def _inherit_grants(
        self, parent_id: str, child_id: str, root: Path | None, worktree: dict | None
    ) -> None:
        """The mother's «in this session» approvals as they are now (A-2)."""
        db = self._services.ctx.db
        rows = db.query(
            "SELECT capability, rule_id, target, project_id, granted_at "
            "FROM session_approval_grants WHERE session_id = ? AND capability != 'session.message'",
            (parent_id,),
        )
        for row in rows:
            target = row["target"]
            if worktree is not None and root is not None and target:
                # A folder of the mother is the same folder in the worktree.
                with contextlib.suppress(ValueError, OSError):
                    relative = Path(target).resolve().relative_to(root.resolve())
                    target = str(Path(worktree["path"]) / relative)
            db.execute(
                "INSERT INTO session_approval_grants "
                "(session_id, capability, rule_id, target, project_id, granted_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    child_id,
                    row["capability"],
                    row["rule_id"],
                    target,
                    row["project_id"],
                    row["granted_at"],
                ),
            )
        # A cached grant list of the child would miss these.
        with contextlib.suppress(Exception):
            self._turns.peers._grants.pop(child_id, None)

    @staticmethod
    def _brief_message(parent: Any, prompt: str, done_when: str, worktree: dict | None) -> str:
        parts = [
            f"[A task from the conversation «{parent.title or parent.id}», which opened this "
            "one for it. It is the user's request, delegated.]",
            "",
            prompt,
        ]
        if done_when:
            parts += ["", f"Done when: {done_when}"]
        if worktree is not None:
            parts += [
                "",
                f"You work in your own git worktree at {worktree['path']} on branch "
                f"{worktree['branch']}, made from the last commit: uncommitted changes of the "
                "main folder are not here. Work and run commands only inside it. Do not merge "
                "the branch: the user decides that.",
            ]
        parts += [
            "",
            "When you finish, end with a short report: what you did, what changed, how you "
            "checked it and what is left.",
        ]
        return "\n".join(parts)

    # -- the mother's tools --------------------------------------------------------

    def call(self, turn: Any, tool: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        """Host of session.start / session.children / session.message for one turn."""
        parent_id = turn.session_id
        if tool == "session.start":
            turn_state = getattr(ctx, "turn_state", None)
            return self.create(
                parent_id,
                title=str(args.get("title") or ""),
                prompt=str(args.get("prompt") or ""),
                origin="start",
                context=str(args.get("context") or "brief"),
                isolation=str(args.get("isolation") or "auto"),
                done_when=str(args.get("done_when") or "").strip()[:500],
                external_content=bool(getattr(turn_state, "external_content", False)),
            )
        if tool == "session.offer":
            return self.offer(turn, args, ctx)
        if tool == "session.children":
            return {"children": self.list(parent_id)}
        if tool == "session.message":
            return self.message(
                parent_id, str(args.get("child_session_id") or ""), str(args.get("message") or "")
            )
        raise InvalidUsageError(f"Unknown child tool: {tool}")

    def offer(self, turn: Any, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        """«New session or here?» for a request of the owner (A-6). Waits for the answer."""
        title = " ".join(str(args.get("title") or "").split())[:80]
        prompt = str(args.get("prompt") or "").strip()
        if len(title) < 3 or len(prompt) < 10:
            raise InvalidUsageError("An offer needs a title and a self-contained task.")
        context = str(args.get("context") or "brief")
        isolation = str(args.get("isolation") or "auto")
        done_when = str(args.get("done_when") or "").strip()[:500]
        answer = self._turns.questions.ask(
            turn,
            self._turns._activity_cb(turn),
            {
                "questions": [
                    {
                        "id": "offer",
                        "title": title,
                        "options": [{"label": "new_session"}, {"label": "here"}],
                    }
                ],
                "_host_offer": {
                    "title": title,
                    "prompt": prompt,
                    "reason": str(args.get("reason") or "").strip()[:300],
                    "context": context,
                    "isolation": isolation,
                    "done_when": done_when,
                },
            },
            getattr(ctx, "cancellation", None),
            getattr(ctx, "deadline_at", None),
        )
        choice = (
            (answer.get("answers") or {}).get("offer")
            if answer.get("status") == "answered"
            else None
        )
        if choice == "new_session":
            turn_state = getattr(ctx, "turn_state", None)
            child = self.create(
                turn.session_id,
                title=title,
                prompt=prompt,
                origin="offer",
                context=context,
                isolation=isolation,
                done_when=done_when,
                external_content=bool(getattr(turn_state, "external_content", False)),
            )
            return {"decision": "new_session", **child}
        if choice == "here":
            return {
                "decision": "here",
                "note": "The user wants it here: do it in this conversation.",
            }
        return {"decision": "none", "note": "The user did not answer: do not open a session."}

    def list(self, parent_session_id: str) -> list[dict[str, Any]]:
        out = []
        for child in self.store.of(parent_session_id):
            with contextlib.suppress(Exception):
                record = self._services.sessions.show(child.child_session_id)
                out.append(
                    {**child.as_dict(), "title": record.title, "busy": self._busy(record.id)}
                )
        return out

    def message(self, parent_session_id: str, child_session_id: str, text: str) -> dict[str, Any]:
        """The mother writes to one of her own children, never to anyone else (A-4)."""
        child = self.store.get(child_session_id)
        if child is None or child.parent_session_id != parent_session_id:
            raise NotFoundError("That is not a child session of this conversation.")
        text = text.strip()
        if not text:
            raise InvalidUsageError("The message is empty.")
        record = self._services.sessions.show(child_session_id)
        if self._busy(child_session_id):
            result = self._turns.steer(
                child_session_id, text, prefix=PARENT_PREFIX, source="parent"
            )
            delivery = result.get("delivery", "steer")
        else:
            self._turns.start_turn(
                child_session_id,
                PARENT_PREFIX + text,
                display_message=text,
                origin={
                    "kind": "user",
                    "source": "parent_message",
                    "parent_session_id": parent_session_id,
                },
            )
            delivery = "turn"
        updated = self.store.set_state(child_session_id, "working")
        if updated is not None:
            self._emit(updated, record.title)
        return {"child_session_id": child_session_id, "delivery": delivery}

    # -- after a turn ----------------------------------------------------------------

    def after_turn(self, turn: Any) -> None:
        """Report a child's turn to its mother, and let a free mother go on."""
        try:
            parent_id = self._report(turn)
            if parent_id is not None:
                self.wake(parent_id)
            # A mother whose own turn ended may have reports waiting; not after
            # the owner pressed Stop.
            if (turn.terminal_event or "") != "turn.cancelled":
                self.wake(turn.session_id)
        except Exception:
            logger.exception("child session report failed")

    def _report(self, turn: Any) -> str | None:
        """The child's report to its mother; returns the mother's id."""
        store = self.store
        child = store.get(turn.session_id)
        if child is None:
            return None
        origin = turn.origin or {}
        if origin.get("source") not in REPORTING_SOURCES:
            # The owner drives the child by hand: the mother is not told.
            return None
        terminal = (turn.terminal_event or "turn.completed").removeprefix("turn.")
        state = {"completed": "completed", "failed": "failed", "cancelled": "cancelled"}.get(
            terminal, "completed"
        )
        result = getattr(turn, "goal_result", None)
        text = str(getattr(result, "content", "") or "").strip()
        report = {
            "summary": " ".join(text.split())[:SUMMARY_CHARS],
            "text": text[:REPORT_TEXT_CHARS],
            "files": list(getattr(turn, "changed_paths", None) or [])[:50],
            "terminal": terminal,
            "turn_id": turn.turn_id,
        }
        if getattr(turn, "goal_error", (None, None))[1]:
            report["error"] = str(turn.goal_error[1])[:500]
        updated = store.set_state(child.child_session_id, state, report)
        if updated is not None:
            record = self._services.sessions.show(child.child_session_id)
            self._emit(updated, record.title)
        return child.parent_session_id

    def wake(self, parent_session_id: str) -> dict[str, Any] | None:
        """A free mother goes on once with the reports of children Rinari opened (A-8)."""
        store = self.store
        pending = [c for c in store.unreported(parent_session_id) if c.origin in OPENED_BY_RINARI]
        if not pending:
            return None
        if any(c.origin in OPENED_BY_RINARI for c in store.working(parent_session_id)):
            # Wait until no child Rinari opened is still working.
            return None
        if self._busy(parent_session_id):
            # Her own turn will end later and wake her then.
            return None
        try:
            record = self._services.sessions.show(parent_session_id)
        except Exception:
            return None
        titles = {}
        for child in pending:
            with contextlib.suppress(Exception):
                titles[child.child_session_id] = self._services.sessions.show(
                    child.child_session_id
                ).title
        names = ", ".join(
            f"«{titles.get(c.child_session_id, c.child_session_id)}»" for c in pending
        )
        message = (
            f"[Harness: child sessions you opened reported back: {names}. Their reports are in "
            "your task state. A report is a claim, not proof: check the result before saying "
            "it is done. Carry on with what the user asked in this conversation that these "
            "results unblock, or tell the user the outcome in one or two lines. Do not merge "
            "branches yourself.]"
        )
        try:
            started = self._turns.start_turn(
                record.id,
                message,
                display_message=names,
                origin={
                    "kind": "automation",
                    "source": "child_report",
                    "children": [c.child_session_id for c in pending],
                },
                memory_origin="automation",
            )
        except EngineProtocolError:
            return None
        return started

    # -- the owner's decisions ---------------------------------------------------------

    def integrate(self, child_session_id: str) -> dict[str, Any]:
        """Merge the child's branch into the project (the owner's button)."""
        from rinari.agents.worktree_manager import WorktreeInfo, WorktreeManager

        child, root = self._branch_child(child_session_id)
        record = self._services.sessions.show(child_session_id)
        manager = WorktreeManager(root)
        info = WorktreeInfo(path=Path(child.worktree_path), branch=child.branch)
        commit = manager.commit_result(info, f"Rinari: {record.title}")
        merged = manager.try_merge(info)
        if not merged["merged"]:
            manager.abort_merge()
            return {
                "child_session_id": child_session_id,
                "integrated": False,
                "conflicts": merged["conflicts"],
                "message": merged["message"],
            }
        self._release_worktree(child, record, root, manager, info, "integrated")
        return {
            "child_session_id": child_session_id,
            "integrated": True,
            "conflicts": [],
            "files": commit.get("changed_files") or [],
        }

    def discard(self, child_session_id: str) -> dict[str, Any]:
        from rinari.agents.worktree_manager import WorktreeInfo, WorktreeManager

        child, root = self._branch_child(child_session_id)
        record = self._services.sessions.show(child_session_id)
        manager = WorktreeManager(root)
        info = WorktreeInfo(path=Path(child.worktree_path), branch=child.branch)
        self._release_worktree(child, record, root, manager, info, "discarded", force=True)
        return {"child_session_id": child_session_id, "discarded": True}

    def _branch_child(self, child_session_id: str) -> tuple[ChildRecord, Path]:
        child = self.store.get(child_session_id)
        if child is None or not child.branch or not child.worktree_path:
            raise NotFoundError("That child session has no branch of its own.")
        if child.resolution:
            raise ConflictError(f"That branch was already {child.resolution}.")
        if self._busy(child_session_id):
            raise EngineProtocolError(errors.TURN_RUNNING, "The child session is still working.")
        record = self._services.sessions.show(child_session_id)
        project = self._services.projects.get(record.project_id) if record.project_id else None
        if project is None:
            raise NotFoundError("The child session has no project.")
        return child, Path(project.canonical_root)

    def _release_worktree(
        self, child, record, root, manager, info, resolution: str, *, force: bool = False
    ) -> None:
        with contextlib.suppress(Exception):
            manager.remove(info, force=force)
        # The conversation stays; it works in the project again.
        record.current_cwd = str(root)
        self._services.ctx.session_repo.update(record)
        updated = self.store.resolve(child.child_session_id, resolution)
        if updated is not None:
            self._emit(updated, record.title)

    # -- helpers ---------------------------------------------------------------------------

    def _busy(self, session_id: str) -> bool:
        with self._turns._lock:
            return any(
                not active.done.is_set() and active.session_id == session_id
                for active in self._turns._turns.values()
            )

    def _emit(self, child: ChildRecord, title: str) -> None:
        self._turns.emit_external(
            event(
                "session.child.updated",
                {
                    "session_id": child.parent_session_id,
                    "child": {**child.as_dict(), "title": title},
                },
            )
        )
