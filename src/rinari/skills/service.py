"""SkillService: discovery, conflict resolution, lazy load, activation (phase 6).

Sources (harness.md 50 conflict order, highest wins):

    project   <root>/.rinari/skills/<name>/SKILL.md   (requires project trust)
    user      ~/.rinari/skills/<name>/SKILL.md
    packaged  <package>/assets/skills/<name>/SKILL.md

Discovery is read-only and deterministic; activation is per-session state
(persisted in `sessions.active_skills_json`) plus an activation-trace event —
never a global mutation. `validate_skill` reports (does not raise) so `rinari
skills validate/test` can render a report.
"""

from __future__ import annotations

import contextlib
import re
import shutil
from dataclasses import replace
from pathlib import Path

from rinari.application.context import AppContext
from rinari.shared.clock import now_iso
from rinari.skills.manifest import (
    SKILL_FILE,
    SkillError,
    SkillManifest,
    load_skill_manifest,
    validate_skill,
)
from rinari.storage.records import SessionEventRecord

_PACKAGE_SKILLS = Path(__file__).resolve().parent.parent / "assets" / "skills"


class SkillService:
    def __init__(self, ctx: AppContext, trust=None) -> None:
        self._ctx = ctx
        self._trust = trust

    # -- discovery -----------------------------------------------------------

    def user_skills_dir(self) -> Path:
        return self._ctx.layout.dir("skills")

    def project_skills_dir(self, root: Path | None) -> Path | None:
        if root is None:
            return None
        d = root / ".rinari" / "skills"
        return d if d.is_dir() else None

    def discover(self, project: Path | None = None) -> dict[str, SkillManifest]:
        """name -> manifest, project > user > packaged, project gated by trust."""
        found: dict[str, SkillManifest] = {}
        proj_dir = self.project_skills_dir(project)
        trusted = (
            proj_dir is not None and self._trust is not None and self._trust.is_trusted(project)
        )
        for source, base in (
            ("packaged", _PACKAGE_SKILLS if _PACKAGE_SKILLS.is_dir() else None),
            ("user", self.user_skills_dir() if self.user_skills_dir().is_dir() else None),
            ("project", proj_dir if trusted else None),
        ):
            if base is None or not base.is_dir():
                continue
            for entry in sorted(base.iterdir(), key=lambda p: p.name):
                if not entry.is_dir():
                    continue
                try:
                    found[entry.name] = load_skill_manifest(entry, source)
                except SkillError:
                    continue  # unreadable/invalid skills never break discovery
        return found

    def get(self, name: str, project: Path | None = None) -> SkillManifest:
        found = self.discover(project)
        if name not in found:
            raise SkillError("SKILL_NOT_FOUND", f"skill not found: {name}")
        return found[name]

    def summaries(
        self,
        project: Path | None = None,
        session=None,
    ) -> list[dict]:
        """Compact metadata for the prompt (harness.md 49: bodies stay out)."""
        found = self.discover(project)
        active = _session_active(session)
        out = []
        for name in sorted(found):
            m = found[name]
            out.append(
                {
                    "name": m.name,
                    "description": m.description,
                    "version": m.version,
                    "source": m.source,
                    "risk": m.risk,
                    "triggers": list(m.triggers),
                    "active": name in active,
                }
            )
        return out

    def search(self, query: str, project: Path | None = None) -> list[dict]:
        terms = [t for t in query.lower().split() if t]
        rows = self.summaries(project)
        scored = []
        for row in rows:
            haystack = f"{row['name']} {row['description']} {' '.join(row['triggers'])}".lower()
            score = sum(haystack.count(t) for t in terms)
            if score > 0:
                scored.append((score, row))
        scored.sort(key=lambda pair: (-pair[0], pair[1]["name"]))
        return [row for _, row in scored]

    def load(self, name: str, project: Path | None = None) -> str:
        """Lazy full-body load (the payload an activation injects)."""
        return self.get(name, project).body

    def show(self, name: str, project: Path | None = None) -> SkillManifest:
        return self.get(name, project)

    # -- session activation state ---------------------------------------------

    def active_versions_for(self, session) -> tuple[tuple[str, str], ...]:
        """The (name, version) pairs pinned on one session record."""
        if session is None:
            return ()
        return tuple(session.active_skills or ())

    def _session(self, session_id: str):
        rec = self._ctx.session_repo.get(session_id)
        if rec is None:
            raise SkillError("SKILL_NOT_FOUND", f"unknown session: {session_id}")
        return rec

    def activate(self, name: str, session_id: str, project: Path | None = None) -> SkillManifest:
        manifest = self.get(name, project)
        rec = self._session(session_id)
        current = tuple(rec.active_skills or ())
        current = tuple(pair for pair in current if pair[0] != name)
        updated = replace(rec, active_skills=(*current, (manifest.name, manifest.version)))
        self._ctx.session_repo.update(updated)
        self._trace(
            session_id,
            "SkillActivated",
            {
                "skill": manifest.name,
                "version": manifest.version,
                "source": manifest.source,
            },
        )
        return manifest

    def deactivate(self, name: str, session_id: str, project: Path | None = None) -> bool:
        rec = self._session(session_id)
        current = tuple(rec.active_skills or ())
        if name not in tuple(n for n, _ in current):
            return False
        updated = replace(
            rec,
            active_skills=tuple(pair for pair in current if pair[0] != name),
        )
        self._ctx.session_repo.update(updated)
        self._trace(session_id, "SkillDeactivated", {"skill": name})
        return True

    def validate(self, name: str | None = None, project: Path | None = None) -> list[dict]:
        known = set()
        try:
            from rinari.tools.native import all_native_tools

            known = {tool.name for tool in all_native_tools()}
        except Exception:
            pass
        found = self.discover(project)
        targets = [found[name]] if name else list(found.values())
        report: list[dict] = []
        for m in targets:
            issues = validate_skill(m, known)
            report.append(
                {
                    "name": m.name,
                    "source": m.source,
                    "version": m.version,
                    "ok": not issues,
                    "issues": issues,
                }
            )
        return report

    def test(self, name: str, project: Path | None = None) -> dict:
        """Deterministic dry-run: parse + validate (no model, no tools run)."""
        try:
            m = self.get(name, project)
        except SkillError as exc:
            return {"name": name, "ok": False, "code": exc.code, "message": exc.message}
        report = self.validate(name, project)
        entry = report[0]
        return {
            "name": m.name,
            "ok": entry["ok"],
            "source": m.source,
            "version": m.version,
            "issues": entry["issues"],
        }

    # -- user-skill lifecycle (install/remove/update/create) -------------------

    def install(self, source_dir: str | Path, name: str | None = None) -> SkillManifest:
        candidate = Path(source_dir)
        if not (candidate / SKILL_FILE).is_file():
            raise SkillError("SKILL_NOT_FOUND", f"no {SKILL_FILE} at {candidate}")
        name = name or candidate.name
        if not _name_like(name):
            raise SkillError("NAME_INVALID", f"skill name invalid: {name!r}")
        dest_root = self.user_skills_dir()
        dest_root.mkdir(parents=True, exist_ok=True)
        dest = dest_root / name
        if dest.exists():
            raise SkillError("ALREADY_EXISTS", f"skill already installed: {name}")
        shutil.copytree(candidate, dest)
        return self._load_user(name)

    def update(self, source_dir: str | Path, name: str | None = None) -> SkillManifest:
        src = Path(source_dir)
        if not (src.is_dir() and (src / SKILL_FILE).is_file()):
            raise SkillError("SKILL_NOT_FOUND", f"no {SKILL_FILE} at {src}")
        name = name or src.name
        dest = self.user_skills_dir() / name
        if not dest.is_dir():
            raise SkillError("SKILL_NOT_FOUND", f"skill not installed: {name}")
        shutil.rmtree(dest)
        shutil.copytree(src, dest)
        return self._load_user(name)

    def remove(self, name: str) -> bool:
        dest = self.user_skills_dir() / name
        if not dest.is_dir():
            return False
        shutil.rmtree(dest)
        return True

    def create(self, name: str, description: str = "") -> str:
        if not _name_like(name):
            raise SkillError("NAME_INVALID", f"skill name invalid: {name!r}")
        dest = self.user_skills_dir() / name
        if dest.exists():
            raise SkillError("ALREADY_EXISTS", f"skill already exists: {name!r}")
        dest.mkdir(parents=True)
        body = (
            f"---\n"
            f"name: {name}\n"
            f"description: {description or 'TODO: what does this skill do?'}\n"
            f"version: 0.1.0\n"
            f"risk: low\n"
            f"required_tools:\n  - fs.read\n"
            f"---\n\n"
            f"# Procedure\n"
            f"1. ...\n\n"
            f"# Verification\n"
            f"- ...\n\n"
            f"# Failure handling\n"
            f"- ...\n\n"
            f"# Success criteria\n"
            f"- ...\n"
        )
        (dest / SKILL_FILE).write_text(body, encoding="utf-8")
        return str(dest / SKILL_FILE)

    def _load_user(self, name: str) -> SkillManifest:
        return load_skill_manifest(self.user_skills_dir() / name, "user")

    def _trace(self, session_id: str, event: str, payload: dict) -> None:
        with contextlib.suppress(Exception):
            # trace is observability, never a functional dependency
            self._ctx.event_repo.insert(
                SessionEventRecord(
                    id=self._ctx.ids.new("evt"),
                    session_id=session_id,
                    seq=self._ctx.event_repo.next_seq(session_id),
                    type=event,
                    payload=payload,
                    created_at=now_iso(self._ctx.clock),
                )
            )


def _session_active(session) -> frozenset[str]:
    if session is None:
        return frozenset()
    return frozenset(name for name, _ in (session.active_skills or ()))


def _name_like(name: str) -> bool:
    return bool(re.match(r"^[a-z0-9][a-z0-9-]*$", name))


__all__ = ["SkillService"]
