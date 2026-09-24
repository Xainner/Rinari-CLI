"""SkillService: discovery, conflict resolution, lazy load, activation (phase 6),
and the skill library (install, review, on/off, edit, import).

Sources (harness.md 50 conflict order, highest wins):

    project   <root>/.rinari/skills/<name>/SKILL.md   (requires project trust)
    user      ~/.rinari/skills/<name>/SKILL.md
    packaged  <package>/assets/skills/<name>/SKILL.md

The library labels them by origin: `rinari` (packaged), `installed` (user
folder: installed, imported or created), `learned` and `project`. What a
folder cannot say about itself (provenance, content hash, on/off, approval)
lives in `skill_records`. A disabled or pending skill is left out of the
catalog, activation and reconciliation; the library still shows it.

Discovery is read-only and deterministic; activation is per-session state
(persisted in `sessions.active_skills_json`) plus an activation-trace event —
never a global mutation. `validate_skill` reports (does not raise) so `rinari
skills validate/test` can render a report.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

from rinari.application.context import AppContext
from rinari.shared.clock import now_iso
from rinari.skills.install import (
    IMPORT_FOLDERS,
    SkillFetcher,
    find_candidates,
    import_kind,
    parse_source,
)
from rinari.skills.manifest import (
    SKILL_FILE,
    SkillError,
    SkillManifest,
    load_skill_manifest,
    validate_skill,
)
from rinari.skills.review import content_hash, review_skill
from rinari.storage.records import SessionEventRecord

_PACKAGE_SKILLS = Path(__file__).resolve().parent.parent / "assets" / "skills"
# A skill's other files are read on demand, a page at a time, so its SKILL.md
# (injected every turn while active) can stay an index. Standard skills ship
# scripts next to their docs: those are text too, and worth reading first.
_REFERENCE_SUFFIXES = (
    ".md",
    ".txt",
    ".json",
    ".py",
    ".sh",
    ".ps1",
    ".bat",
    ".cmd",
    ".js",
    ".mjs",
    ".ts",
    ".yaml",
    ".yml",
    ".toml",
    ".csv",
    ".html",
    ".xml",
)
_REFERENCE_LINES = 400
_REFERENCE_MAX_BYTES = 512 * 1024
_EDITABLE_ORIGINS = ("installed", "learned")

# Injected in front of a standard skill's body: it was written for another
# agent, so its tool names and "run this script" steps need Rinari's mapping.
_STANDARD_PREAMBLE = """\
[Rinari] This skill follows the Agent Skills standard (written for Claude, Codex or \
a similar agent). Its tool names map to Rinari's: Bash/shell -> shell.exec, Read -> \
fs.read, Write -> fs.write, Edit -> fs.patch, Grep -> search.regex, Glob -> \
search.files, WebFetch -> web.fetch, WebSearch -> web.search.
Its folder is {folder}. Read its other files with skills.read (name "{name}", path \
relative to that folder). Run its scripts with shell.exec using that folder as cwd; \
policy and approvals apply as usual."""


class SkillService:
    def __init__(
        self,
        ctx: AppContext,
        trust=None,
        *,
        user_home: Path | None = None,
        fetcher: SkillFetcher | None = None,
    ) -> None:
        self._ctx = ctx
        self._trust = trust
        self._user_home = Path(user_home) if user_home is not None else Path.home()
        self._fetcher = fetcher or SkillFetcher()
        # Set by the Engine server: a learned skill becomes a `skill.learned` event.
        self.on_learned = None
        from rinari.skills.learning import SkillLearning

        self.learning = SkillLearning(self)

    # -- discovery -----------------------------------------------------------

    def user_skills_dir(self) -> Path:
        return self._ctx.layout.dir("skills")

    def project_skills_dir(self, root: Path | None) -> Path | None:
        if root is None:
            return None
        d = root / ".rinari" / "skills"
        return d if d.is_dir() else None

    def _bases(self, project: Path | None) -> list[tuple[str, Path]]:
        proj_dir = self.project_skills_dir(project)
        trusted = (
            proj_dir is not None and self._trust is not None and self._trust.is_trusted(project)
        )
        bases: list[tuple[str, Path]] = []
        for source, base in (
            ("packaged", _PACKAGE_SKILLS),
            ("user", self.user_skills_dir()),
            ("project", proj_dir if trusted else None),
        ):
            if base is not None and base.is_dir():
                bases.append((source, base))
        return bases

    @staticmethod
    def _folders(base: Path) -> list[Path]:
        # Dot folders are the service's own staging/backup/history, never skills.
        return [
            entry
            for entry in sorted(base.iterdir(), key=lambda p: p.name)
            if entry.is_dir() and not entry.name.startswith(".")
        ]

    def _records(self) -> dict[str, dict]:
        repo = getattr(self._ctx, "skill_repo", None)
        if repo is None:
            return {}
        return {row["name"]: row for row in repo.list()}

    def discover(
        self, project: Path | None = None, *, include_disabled: bool = False
    ) -> dict[str, SkillManifest]:
        """name -> manifest, project > user > packaged, project gated by trust.
        Disabled and pending skills are left out unless asked for."""
        found: dict[str, SkillManifest] = {}
        for source, base in self._bases(project):
            for entry in self._folders(base):
                try:
                    found[entry.name] = load_skill_manifest(entry, source)
                except SkillError:
                    continue  # unreadable/invalid skills never break discovery
        if include_disabled:
            return found
        records = self._records()
        return {name: m for name, m in found.items() if _usable(records.get(name))}

    def get(
        self, name: str, project: Path | None = None, *, include_disabled: bool = False
    ) -> SkillManifest:
        found = self.discover(project, include_disabled=include_disabled)
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
        return self.prompt_body(name, project)

    def prompt_body(self, name: str, project: Path | None = None) -> str:
        """The body the model sees: a standard skill gets Rinari's mapping first."""
        manifest = self.get(name, project)
        if manifest.format != "standard":
            return manifest.body
        preamble = _STANDARD_PREAMBLE.format(folder=Path(manifest.path).parent, name=name)
        return f"{preamble}\n\n{manifest.body}"

    def show(self, name: str, project: Path | None = None) -> SkillManifest:
        return self.get(name, project)

    # -- reference files (progressive disclosure) --------------------------------

    def references(
        self, name: str, project: Path | None = None, *, include_disabled: bool = False
    ) -> list[str]:
        """Text files a skill ships besides SKILL.md, relative to its folder."""
        manifest = self.get(name, project, include_disabled=include_disabled)
        root = Path(manifest.path).parent
        return sorted(
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
            if p.is_file()
            and not p.is_symlink()
            and p.name != SKILL_FILE
            and p.suffix.lower() in _REFERENCE_SUFFIXES
        )

    def read_reference(
        self,
        name: str,
        path: str,
        project: Path | None = None,
        offset: int = 0,
        limit: int = _REFERENCE_LINES,
        *,
        include_disabled: bool = False,
    ) -> dict:
        """One page of a skill file; never a file outside the skill folder."""
        manifest = self.get(name, project, include_disabled=include_disabled)
        root = Path(manifest.path).parent.resolve()
        relative = Path(str(path or "").strip().replace("\\", "/"))
        if not relative.parts or relative.is_absolute() or ".." in relative.parts:
            raise SkillError("SKILL_INVALID", "path must be relative to the skill folder")
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            known = ", ".join(self.references(name, project, include_disabled=True)) or "none"
            raise SkillError("SKILL_NOT_FOUND", f"no reference {path!r} in {name}; has: {known}")
        if target.suffix.lower() not in _REFERENCE_SUFFIXES or target.name == SKILL_FILE:
            raise SkillError("SKILL_INVALID", "only the skill's text files can be read this way")
        raw = target.read_bytes()
        if len(raw) > _REFERENCE_MAX_BYTES or b"\0" in raw:
            raise SkillError("SKILL_INVALID", f"{path} is too large or not text")
        lines = raw.decode("utf-8", errors="replace").splitlines()
        start = max(0, int(offset or 0))
        size = max(1, min(_REFERENCE_LINES, int(limit or _REFERENCE_LINES)))
        end = start + size
        return {
            "name": manifest.name,
            "path": relative.as_posix(),
            "text": "\n".join(lines[start:end]),
            "offset": start,
            "total_lines": len(lines),
            "next_offset": end if end < len(lines) else None,
        }

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
        if (manifest.name, manifest.version) in current:
            return manifest
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

    @staticmethod
    def _known_tools() -> set[str]:
        try:
            from rinari.tools.native import all_native_tools

            return {tool.name for tool in all_native_tools()}
        except Exception:
            return set()

    def validate(self, name: str | None = None, project: Path | None = None) -> list[dict]:
        known = self._known_tools()
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

    # -- library ------------------------------------------------------------------

    def library(self, project: Path | None = None) -> list[dict]:
        """Every skill folder, effective one per name, including disabled,
        pending and invalid ones (with their error), for the settings page."""
        records = self._records()
        known = self._known_tools()
        entries: dict[str, dict] = {}
        for source, base in self._bases(project):
            for folder in self._folders(base):
                previous = entries.get(folder.name)
                entries[folder.name] = self._entry(
                    folder,
                    source,
                    records.get(folder.name),
                    known,
                    shadows=previous["origin"] if previous else None,
                )
        return [entries[name] for name in sorted(entries)]

    def _entry(
        self,
        folder: Path,
        source: str,
        record: dict | None,
        known: set[str],
        *,
        shadows: str | None = None,
    ) -> dict:
        try:
            manifest: SkillManifest | None = load_skill_manifest(folder, source)
            error = None
        except SkillError as exc:
            manifest, error = None, {"code": exc.code, "message": exc.message}
        origin = _origin(source, record)
        installed_hash = (record or {}).get("content_hash")
        return {
            "name": folder.name,
            "description": manifest.description if manifest else "",
            "version": manifest.version if manifest else None,
            "format": manifest.format if manifest else None,
            "risk": manifest.risk if manifest else None,
            "origin": origin,
            "enabled": _enabled(record),
            "status": (record or {}).get("status") or "active",
            "valid": manifest is not None,
            "error": error,
            "issues": validate_skill(manifest, known) if manifest else [],
            "shadows": shadows,
            "editable": origin in _EDITABLE_ORIGINS,
            "modified": (
                content_hash(folder) != installed_hash
                if origin in _EDITABLE_ORIGINS and installed_hash
                else None
            ),
            "provenance": {
                "source_kind": (record or {}).get("source_kind"),
                "source": (record or {}).get("source"),
                "installed_at": (record or {}).get("installed_at"),
                "updated_at": (record or {}).get("updated_at"),
                "learned_from": (record or {}).get("learned_from"),
            },
        }

    def detail(self, name: str, project: Path | None = None) -> dict:
        """One library entry plus what the settings page shows about it."""
        entry = next((row for row in self.library(project) if row["name"] == name), None)
        if entry is None:
            raise SkillError("SKILL_NOT_FOUND", f"skill not found: {name}")
        folder = self._folder_of(name, project)
        view = dict(entry)
        view["path"] = str(folder)
        view["review"] = review_skill(folder).to_dict()
        skill_md = folder / SKILL_FILE
        # The whole file, frontmatter included: what the editor starts from.
        view["skill_md"] = (
            skill_md.read_text(encoding="utf-8", errors="replace") if skill_md.is_file() else ""
        )
        if entry["valid"]:
            manifest = load_skill_manifest(folder, _source_of(entry["origin"]))
            view.update(
                body=manifest.body,
                references=self.references(name, project, include_disabled=True),
                triggers=list(manifest.triggers),
                required_tools=list(manifest.required_tools),
                optional_tools=list(manifest.optional_tools),
                allowed_tools=list(manifest.allowed_tools),
                license=manifest.license,
                compatibility=manifest.compatibility,
                metadata=dict(manifest.metadata),
            )
        else:
            view["body"] = view["skill_md"]
            view["references"] = []
        return view

    def _folder_of(self, name: str, project: Path | None = None) -> Path:
        folder = None
        for _source, base in self._bases(project):
            candidate = base / name
            if candidate.is_dir():
                folder = candidate  # later bases win, as in discover()
        if folder is None:
            raise SkillError("SKILL_NOT_FOUND", f"skill not found: {name}")
        return folder

    def set_enabled(self, name: str, enabled: bool, project: Path | None = None) -> dict:
        entry = next((row for row in self.library(project) if row["name"] == name), None)
        if entry is None:
            raise SkillError("SKILL_NOT_FOUND", f"skill not found: {name}")
        repo = self._ctx.skill_repo
        existing = repo.get(name)
        repo.upsert(
            name,
            origin=(existing or {}).get("origin") or entry["origin"],
            enabled=bool(enabled),
            updated_at=self._now(),
        )
        return self.detail(name, project)

    # -- install / update / import -------------------------------------------------

    def inspect(self, source: str) -> dict:
        """What installing `source` would bring in, with its review. Installs nothing."""
        spec = parse_source(source)
        with tempfile.TemporaryDirectory(
            prefix="rinari-skill-", ignore_cleanup_errors=True
        ) as work:
            root = self._fetcher.materialize(spec, Path(work))
            candidates = [self._candidate_view(folder, root) for folder in find_candidates(root)]
        return {"source": spec.display, "kind": spec.kind, "candidates": candidates}

    def import_scan(self) -> list[dict]:
        """Skills in Claude's, Codex's and the shared `.agents` folders."""
        found: list[dict] = []
        for kind, relative in IMPORT_FOLDERS:
            base = self._user_home / relative
            if not base.is_dir():
                continue
            for folder in find_candidates(base):
                view = self._candidate_view(folder, None)
                view["kind"] = kind
                found.append(view)
        return found

    def _candidate_view(self, folder: Path, root: Path | None) -> dict:
        try:
            manifest: SkillManifest | None = load_skill_manifest(folder, "user")
            error = None
        except SkillError as exc:
            manifest, error = None, {"code": exc.code, "message": exc.message}
        name = manifest.name if manifest else folder.name
        existing = next((row for row in self.library() if row["name"] == name), None)
        return {
            "name": name,
            "description": manifest.description if manifest else "",
            "version": manifest.version if manifest else None,
            "format": manifest.format if manifest else None,
            "path": folder.relative_to(root).as_posix() if root else str(folder),
            "error": error,
            "review": review_skill(folder).to_dict(),
            "installed": (
                {"origin": existing["origin"], "source": existing["provenance"]["source"]}
                if existing
                else None
            ),
        }

    def install(
        self,
        source: str | Path,
        name: str | None = None,
        *,
        expected_hash: str | None = None,
        replace: bool = False,
    ) -> SkillManifest:
        """Copy one skill from `source` into ~/.rinari/skills.

        A review with findings needs `expected_hash` equal to the reviewed
        content's hash: the owner confirmed exactly this content, not whatever
        the source serves on the second download.
        """
        spec = parse_source(str(source))
        with tempfile.TemporaryDirectory(
            prefix="rinari-skill-", ignore_cleanup_errors=True
        ) as work:
            root = self._fetcher.materialize(spec, Path(work))
            folder = self._pick(find_candidates(root), name)
            manifest = load_skill_manifest(folder, "user")
            review = review_skill(folder)
            if review.findings and expected_hash != review.content_hash:
                raise SkillError(
                    "REVIEW_REQUIRED",
                    f"{manifest.name}: the review found {len(review.findings)} item(s); "
                    "confirm this exact content to install it",
                    details={"name": manifest.name, "review": review.to_dict()},
                )
            self._place(folder, manifest.name, replace=replace)
        kind = import_kind(spec.path, self._user_home) if spec.kind == "dir" else spec.kind
        previous = self._ctx.skill_repo.get(manifest.name) or {}
        now = self._now()
        self._ctx.skill_repo.upsert(
            manifest.name,
            origin="installed",
            source_kind=kind,
            source=spec.display,
            content_hash=review.content_hash,
            status="active",
            enabled=previous.get("enabled", 1),
            installed_at=previous.get("installed_at") if replace else now,
            updated_at=now,
        )
        return load_skill_manifest(self.user_skills_dir() / manifest.name, "user")

    def update(
        self,
        name: str,
        *,
        source: str | None = None,
        expected_hash: str | None = None,
        force: bool = False,
    ) -> SkillManifest:
        """Reinstall an installed skill from its recorded source (or `source`).
        Local edits since the install are kept unless `force`."""
        folder = self.user_skills_dir() / name
        if not folder.is_dir():
            raise SkillError("SKILL_NOT_FOUND", f"skill not installed: {name}")
        record = self._ctx.skill_repo.get(name) or {}
        origin = source or record.get("source")
        if not origin:
            raise SkillError("NOT_UPDATABLE", f"{name} has no recorded source to update from")
        installed = record.get("content_hash")
        if not force and installed and content_hash(folder) != installed:
            raise SkillError(
                "LOCALLY_MODIFIED",
                f"{name} was edited after it was installed; updating would discard those edits",
            )
        return self.install(origin, name, expected_hash=expected_hash, replace=True)

    def remove(self, name: str) -> bool:
        dest = self.user_skills_dir() / name
        if not dest.is_dir():
            return False
        shutil.rmtree(dest)
        shutil.rmtree(self.user_skills_dir() / ".history" / name, ignore_errors=True)
        repo = getattr(self._ctx, "skill_repo", None)
        if repo is not None:
            repo.delete(name)
        return True

    # -- learned skills (rinari.skills.learning) -------------------------------------

    def propose(self, name: str, skill_md: str, references=None, **kwargs) -> dict:
        return self.learning.propose(
            name, skill_md, references, known=self._known_tools(), **kwargs
        )

    def auto_learn(self) -> str:
        from rinari.skills.learning import AUTO_LEARN_KEY

        return self._ctx.config_repo.get(AUTO_LEARN_KEY) or "propose"

    def set_auto_learn(self, mode: str) -> str:
        from rinari.skills.learning import AUTO_LEARN_KEY, AUTO_LEARN_MODES
        from rinari.storage.records import ConfigValue

        if mode not in AUTO_LEARN_MODES:
            raise SkillError("SKILL_INVALID", f"auto_learn must be {' or '.join(AUTO_LEARN_MODES)}")
        self._ctx.config_repo.set(
            ConfigValue(key=AUTO_LEARN_KEY, value=mode, updated_at=self._now())
        )
        return mode

    def notify_learned(self, payload: dict) -> None:
        if self.on_learned is not None:
            with contextlib.suppress(Exception):
                self.on_learned(payload)

    # Small seams the learning module uses; same rules as install/update.
    @staticmethod
    def packaged_dir() -> Path:
        return _PACKAGE_SKILLS

    def place(self, folder: Path, name: str, *, replace: bool) -> None:
        self._place(folder, name, replace=replace)

    def record(self, name: str) -> dict | None:
        repo = getattr(self._ctx, "skill_repo", None)
        return repo.get(name) if repo is not None else None

    def upsert_record(self, name: str, **fields) -> dict:
        return self._ctx.skill_repo.upsert(name, **fields)

    def now(self) -> str:
        return self._now()

    def stamp(self) -> str:
        # Sortable and valid as a Windows folder name (no colons).
        return self._now().replace(":", "").replace(".", "")

    def write(self, name: str, content: str) -> dict:
        """Replace an installed or learned skill's SKILL.md after validating it."""
        folder = self.user_skills_dir() / name
        record = self._ctx.skill_repo.get(name) or {}
        if not folder.is_dir() or _origin("user", record) not in _EDITABLE_ORIGINS:
            raise SkillError("NOT_EDITABLE", f"{name} is not an installed skill")
        with tempfile.TemporaryDirectory(
            prefix="rinari-skill-", ignore_cleanup_errors=True
        ) as work:
            probe = Path(work) / name
            probe.mkdir()
            (probe / SKILL_FILE).write_text(content, encoding="utf-8")
            manifest = load_skill_manifest(probe, "user")
        if manifest.name != name:
            raise SkillError("SKILL_INVALID", f"the name must stay {name!r}")
        target = folder / SKILL_FILE
        staging = folder / f".{SKILL_FILE}.saving"
        staging.write_text(content, encoding="utf-8")
        staging.replace(target)
        # content_hash keeps the installed version, so the library shows the
        # edit as a local modification and an update will not silently drop it.
        self._ctx.skill_repo.upsert(name, origin=_origin("user", record), updated_at=self._now())
        return self.detail(name)

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
        repo = getattr(self._ctx, "skill_repo", None)
        if repo is not None:
            now = self._now()
            repo.upsert(
                name,
                origin="installed",
                source_kind="created",
                content_hash=content_hash(dest),
                installed_at=now,
                updated_at=now,
            )
        return str(dest / SKILL_FILE)

    @staticmethod
    def _pick(candidates: list[Path], name: str | None) -> Path:
        if not candidates:
            raise SkillError("SKILL_NOT_FOUND", "no SKILL.md found in that source")
        if name:
            for folder in candidates:
                with contextlib.suppress(SkillError):
                    if load_skill_manifest(folder, "user").name == name:
                        return folder
                if folder.name == name:
                    return folder
            raise SkillError("SKILL_NOT_FOUND", f"no skill named {name!r} in that source")
        if len(candidates) == 1:
            return candidates[0]
        raise SkillError(
            "SKILL_AMBIGUOUS",
            f"that source has {len(candidates)} skills; choose one",
            details={"candidates": [folder.name for folder in candidates]},
        )

    def _place(self, folder: Path, name: str, *, replace: bool) -> None:
        """Copy into the user folder through a staging folder, so a failed copy
        never leaves a half skill where discovery would load it."""
        root = self.user_skills_dir()
        root.mkdir(parents=True, exist_ok=True)
        dest = root / name
        if dest.exists() and not replace:
            raise SkillError("ALREADY_EXISTS", f"skill already installed: {name}")
        staging = root / f".{name}.incoming"
        backup = root / f".{name}.previous"
        for leftover in (staging, backup):
            if leftover.exists():
                shutil.rmtree(leftover)
        shutil.copytree(folder, staging, ignore=_skip_links_and_vcs)
        if dest.exists():
            dest.rename(backup)
            staging.rename(dest)
            shutil.rmtree(backup, ignore_errors=True)
        else:
            staging.rename(dest)

    def _now(self) -> str:
        return now_iso(self._ctx.clock)

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


def _usable(record: dict | None) -> bool:
    return record is None or (_enabled(record) and (record.get("status") or "active") == "active")


def _enabled(record: dict | None) -> bool:
    return record is None or bool(record.get("enabled", 1))


def _origin(source: str, record: dict | None) -> str:
    if source == "packaged":
        return "rinari"
    if source == "project":
        return "project"
    return (record or {}).get("origin") or "installed"


def _source_of(origin: str) -> str:
    return {"rinari": "packaged", "project": "project"}.get(origin, "user")


def _skip_links_and_vcs(directory: str, names: list[str]) -> list[str]:
    # A symlink could point at ~/.ssh; copying its target would put a secret
    # inside a folder the model is allowed to read.
    base = Path(directory)
    return [name for name in names if name == ".git" or (base / name).is_symlink()]


def _session_active(session) -> frozenset[str]:
    if session is None:
        return frozenset()
    return frozenset(name for name, _ in (session.active_skills or ()))


def _name_like(name: str) -> bool:
    return bool(re.match(r"^[a-z0-9][a-z0-9-]*$", name))


__all__ = ["SkillService"]
