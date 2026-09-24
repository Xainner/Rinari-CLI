"""Learned skills: what Rinari distills from a conversation (`/learn`) or
proposes on its own after a verified task.

Who decides the state is the Engine, never the model:

    /learn (the owner asked)   saved active at once; the chat offers undo
    anything else              kept aside as a proposal until the owner approves

A proposal with dangerous review findings waits for approval even under
/learn. Secrets are refused, not silently masked: the model must rewrite the
skill without them. An update keeps the previous version in `.history/`, so
«Deshacer» restores it; a brand-new learned skill is simply removed.

Layout under ~/.rinari/skills (dot folders are never discovered as skills):

    <name>/                     active skill
    .pending/<name>/            a proposal waiting for the owner
    .pending/<name>.json        who proposed it, when, update or new
    .history/<name>/<stamp>/    previous versions of learned skills
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from rinari.shared.redaction import redact_text
from rinari.skills.manifest import SKILL_FILE, SkillError, load_skill_manifest, validate_skill
from rinari.skills.review import content_hash, review_skill

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_REFERENCE_ROOTS = ("references", "scripts", "assets")
_REFERENCE_MAX_BYTES = 256 * 1024
_REFERENCES_MAX = 20
_BLOCKING_ISSUES = frozenset(
    {"MISSING_DESCRIPTION", "MISSING_PROCEDURE", "MISSING_BODY", "NAME_MISMATCH"}
)
AUTO_LEARN_KEY = "skills.auto_learn"
AUTO_LEARN_MODES = ("propose", "never")


class SkillLearning:
    """Proposals, approvals and undo for learned skills (used by SkillService)."""

    def __init__(self, service) -> None:
        self._service = service

    @property
    def _root(self) -> Path:
        return self._service.user_skills_dir()

    @property
    def _pending(self) -> Path:
        return self._root / ".pending"

    @property
    def _history(self) -> Path:
        return self._root / ".history"

    # -- propose -----------------------------------------------------------------

    def propose(
        self,
        name: str,
        skill_md: str,
        references: dict[str, str] | None = None,
        *,
        session_id: str = "",
        update_of: str | None = None,
        owner_asked: bool = False,
        known: set[str] | None = None,
    ) -> dict:
        if not _NAME_RE.match(name or ""):
            raise SkillError("NAME_INVALID", f"skill name invalid: {name!r}")
        if update_of and update_of != name:
            raise SkillError("SKILL_INVALID", "an update keeps the skill's name")
        if (self._service.packaged_dir() / name).is_dir():
            raise SkillError(
                "NAME_TAKEN", f"{name} is one of Rinari's own skills; pick another name"
            )
        exists = (self._root / name).is_dir()
        if exists and not update_of:
            raise SkillError(
                "ALREADY_EXISTS",
                f"a skill named {name} exists; pass update_of to propose a new version",
            )
        if update_of and not exists:
            raise SkillError("SKILL_NOT_FOUND", f"no installed skill {name} to update")
        files = {SKILL_FILE: skill_md, **_checked_references(references or {})}
        leaks = sum(1 for text in files.values() if redact_text(text) != text)
        if leaks:
            raise SkillError(
                "SENSITIVE_CONTENT",
                f"{leaks} file(s) contain what looks like a secret (token, key, password). "
                "Rewrite the skill without it and propose again.",
            )
        with tempfile.TemporaryDirectory(
            prefix="rinari-learn-", ignore_cleanup_errors=True
        ) as work:
            draft = Path(work) / name
            for relative, text in files.items():
                target = draft / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            manifest = load_skill_manifest(draft, "user")
            if manifest.name != name:
                raise SkillError("SKILL_INVALID", f"the SKILL.md name must be {name!r}")
            issues = [
                issue
                for issue in validate_skill(manifest, known or set())
                if issue["code"] in _BLOCKING_ISSUES
            ]
            if issues:
                raise SkillError(
                    "SKILL_INVALID",
                    "; ".join(issue["message"] for issue in issues),
                    details={"issues": issues},
                )
            review = review_skill(draft)
            active = owner_asked and review.verdict != "danger"
            if active:
                self._activate(draft, name, session_id=session_id)
            else:
                self._stage(draft, name, session_id=session_id, update=bool(update_of))
        result = {
            "name": name,
            "status": "active" if active else "pending",
            "version": manifest.version,
            "update": bool(update_of),
            "review": review.verdict,
        }
        self._service.notify_learned({**result, "session_id": session_id})
        return result

    def _activate(self, draft: Path, name: str, *, session_id: str) -> None:
        current = self._root / name
        if current.is_dir():
            snapshot = self._history / name / self._service.stamp()
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(current, snapshot)
        self._service.place(draft, name, replace=True)
        previous = self._service.record(name) or {}
        now = self._service.now()
        self._service.upsert_record(
            name,
            origin="learned",
            source_kind="learned",
            source=f"session:{session_id}" if session_id else "learned",
            content_hash=content_hash(self._root / name),
            status="active",
            learned_from=session_id or None,
            enabled=previous.get("enabled", 1),
            installed_at=previous.get("installed_at") or now,
            updated_at=now,
        )

    def _stage(self, draft: Path, name: str, *, session_id: str, update: bool) -> None:
        self._pending.mkdir(parents=True, exist_ok=True)
        target = self._pending / name
        if target.exists():
            shutil.rmtree(target)  # the latest proposal replaces an older one
        shutil.copytree(draft, target)
        meta = {"session_id": session_id, "update": update, "proposed_at": self._service.now()}
        (self._pending / f"{name}.json").write_text(json.dumps(meta), encoding="utf-8")

    # -- the owner's decision ----------------------------------------------------------

    def pending(self) -> list[dict]:
        if not self._pending.is_dir():
            return []
        out = []
        for folder in sorted(p for p in self._pending.iterdir() if p.is_dir()):
            meta_path = self._pending / f"{folder.name}.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
            try:
                manifest = load_skill_manifest(folder, "user")
                description, version = manifest.description, manifest.version
            except SkillError:
                description, version = "", None
            current = self._root / folder.name / SKILL_FILE
            out.append(
                {
                    "name": folder.name,
                    "description": description,
                    "version": version,
                    "learned_from": meta.get("session_id") or None,
                    "proposed_at": meta.get("proposed_at"),
                    "update": bool(meta.get("update")),
                    "review": review_skill(folder).to_dict(),
                    "skill_md": (folder / SKILL_FILE).read_text(encoding="utf-8"),
                    "current_skill_md": (
                        current.read_text(encoding="utf-8") if current.is_file() else None
                    ),
                }
            )
        return out

    def approve(self, name: str) -> dict:
        folder = self._pending / name
        if not folder.is_dir():
            raise SkillError("SKILL_NOT_FOUND", f"no proposal named {name}")
        meta_path = self._pending / f"{name}.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
        self._activate(folder, name, session_id=meta.get("session_id") or "")
        self._discard(name)
        return {"name": name, "status": "active"}

    def reject(self, name: str) -> bool:
        if not (self._pending / name).is_dir():
            return False
        self._discard(name)
        return True

    def revert(self, name: str) -> dict:
        """Undo a learned skill: back to its previous version, or gone if new."""
        record = self._service.record(name) or {}
        if record.get("origin") != "learned":
            raise SkillError("NOT_EDITABLE", f"{name} is not a learned skill")
        snapshots = (
            sorted((self._history / name).iterdir()) if (self._history / name).is_dir() else []
        )
        if snapshots:
            latest = snapshots[-1]
            self._service.place(latest, name, replace=True)
            shutil.rmtree(latest)
            self._service.upsert_record(
                name,
                content_hash=content_hash(self._root / name),
                updated_at=self._service.now(),
            )
            restored = load_skill_manifest(self._root / name, "user").version
            return {"name": name, "restored": restored, "removed": False}
        self._service.remove(name)
        return {"name": name, "restored": None, "removed": True}

    def _discard(self, name: str) -> None:
        shutil.rmtree(self._pending / name, ignore_errors=True)
        (self._pending / f"{name}.json").unlink(missing_ok=True)


def _checked_references(references: dict[str, str]) -> dict[str, str]:
    if not isinstance(references, dict):
        raise SkillError("SKILL_INVALID", "references must map relative paths to text")
    if len(references) > _REFERENCES_MAX:
        raise SkillError("SKILL_INVALID", f"at most {_REFERENCES_MAX} reference files")
    from rinari.skills.service import _REFERENCE_SUFFIXES

    checked: dict[str, str] = {}
    for raw_path, text in references.items():
        path = PurePosixPath(str(raw_path).replace("\\", "/"))
        if (
            path.is_absolute()
            or ".." in path.parts
            or len(path.parts) < 2
            or path.parts[0] not in _REFERENCE_ROOTS
            or path.suffix.lower() not in _REFERENCE_SUFFIXES
        ):
            raise SkillError(
                "SKILL_INVALID",
                f"reference {raw_path!r} must live under references/, scripts/ or assets/ "
                "as a text file",
            )
        if not isinstance(text, str) or len(text.encode("utf-8")) > _REFERENCE_MAX_BYTES:
            raise SkillError("SKILL_INVALID", f"reference {raw_path!r} is not text or too large")
        checked[path.as_posix()] = text
    return checked


__all__ = ["AUTO_LEARN_KEY", "AUTO_LEARN_MODES", "SkillLearning"]
