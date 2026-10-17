"""Where a skill comes from, and getting it onto disk safely.

Sources the owner can give:

    C:\\path\\to\\skill            a folder with SKILL.md (or a folder of skills)
    C:\\path\\to\\skills.zip        an archive
    https://github.com/o/r/tree/main/skills/pdf   a repo, optionally a subfolder
    https://example.com/x/SKILL.md                a single-file skill
    https://example.com/skills.zip                an archive over HTTPS
    https://skills.sh/owner/repo/skill            a skill listed on skills.sh
    npx skills add owner/repo --skill name        the command skills.sh shows

A repository or archive with many skills only brings in the one chosen: the
GitHub tree is listed and just that skill's files are fetched, and an
archive's index is read before extracting only that skill (plan 0.4.0 #53).
The size limits apply to that skill, not to everything around it.

Everything remote is HTTPS only, size-capped, and extracted without leaving
the work folder (no absolute paths, no `..`, no symlinks). Nothing here runs a
file: installing copies text the owner reviewed.
"""

from __future__ import annotations

import functools
import json
import re
import shlex
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit, urlunsplit

from rinari.skills.frontmatter import as_text, read_frontmatter
from rinari.skills.manifest import SKILL_FILE, SkillError

DOWNLOAD_MAX_BYTES = 25 * 1024 * 1024
EXTRACT_MAX_BYTES = 60 * 1024 * 1024
EXTRACT_MAX_FILES = 2000
SKILL_FILE_MAX_BYTES = 1024 * 1024
_SEARCH_DEPTH = 4
_SKIP_DIRS = frozenset({"node_modules", "__pycache__", ".git"})
_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
_SKILLS_SH_HOSTS = frozenset({"skills.sh", "www.skills.sh"})
# Even "install anyway" stops here, and a member that expands more than this
# against its compressed size is a compression bomb, never extracted.
HARD_MAX_BYTES = 1024 * 1024 * 1024
_MAX_RATIO = 1000
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Folders other agents keep their skills in, and how the library labels them.
IMPORT_FOLDERS: tuple[tuple[str, str], ...] = (
    ("claude", ".claude/skills"),
    ("codex", ".codex/skills"),
    ("agents", ".agents/skills"),
)


@dataclass(frozen=True, slots=True)
class SkillSource:
    kind: str  # dir | zip | github | url | zip_url
    display: str  # what is recorded as provenance (no query string, no credentials)
    path: Path | None = None
    url: str | None = None
    subpath: str = ""
    # The one skill the source names (skills.sh, `--skill`), if any.
    skill: str | None = None
    owner: str = ""
    repo: str = ""
    ref: str = "HEAD"


def parse_source(text: str) -> SkillSource:
    raw = str(text or "").strip().strip('"')
    if not raw:
        raise SkillError("SOURCE_INVALID", "empty skill source")
    if re.match(r"^(?:npx|pnpx|bunx)\s", raw, re.IGNORECASE) or raw.lower().startswith(
        "skills add "
    ):
        return _parse_command(raw)
    if re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.IGNORECASE):
        return _parse_url(raw)
    path = Path(raw).expanduser()
    if path.is_dir():
        return SkillSource("dir", str(path.resolve()), path=path.resolve())
    if path.is_file() and path.name == SKILL_FILE:
        return SkillSource("dir", str(path.parent.resolve()), path=path.parent.resolve())
    if path.is_file() and path.suffix.lower() == ".zip":
        return SkillSource("zip", str(path.resolve()), path=path.resolve())
    raise SkillError("SOURCE_INVALID", f"not a skill folder, SKILL.md, .zip or https URL: {raw}")


def _parse_command(raw: str) -> SkillSource:
    """`npx skills add <repo> [--skill <name>]`, pasted as skills.sh shows it."""
    try:
        words = shlex.split(raw, posix=True)
    except ValueError as exc:
        raise SkillError("SOURCE_INVALID", f"cannot read that command: {raw}") from exc
    words = [w for w in words if w.lower() not in ("npx", "pnpx", "bunx", "-y", "--yes")]
    if not words or not words[0].lower().startswith("skills") or words[1:2] != ["add"]:
        raise SkillError("SOURCE_INVALID", "only `npx skills add <repo> --skill <name>` is read")
    rest = words[2:]
    skill = None
    target = None
    index = 0
    while index < len(rest):
        word = rest[index]
        if word in ("--skill", "-s") and index + 1 < len(rest):
            skill = rest[index + 1]
            index += 2
            continue
        if word.startswith("--skill="):
            skill = word.split("=", 1)[1]
        elif not word.startswith("-") and target is None:
            target = word
        index += 1
    if not target:
        raise SkillError("SOURCE_INVALID", "the command does not say which repository")
    if not target.lower().startswith("https://"):
        if not re.match(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", target):
            raise SkillError("SOURCE_INVALID", f"not a GitHub owner/repo: {target}")
        target = f"https://github.com/{target}"
    source = _parse_url(target)
    if skill is not None and not _NAME_RE.match(skill):
        raise SkillError("NAME_INVALID", f"not a skill name: {skill}")
    return _with_skill(source, skill)


def _with_skill(source: SkillSource, skill: str | None) -> SkillSource:
    return SkillSource(
        source.kind,
        source.display + (f" --skill {skill}" if skill else ""),
        source.path,
        source.url,
        source.subpath,
        skill or source.skill,
        source.owner,
        source.repo,
        source.ref,
    )


def _parse_url(raw: str) -> SkillSource:
    parts = urlsplit(raw)
    if parts.scheme.lower() != "https":
        raise SkillError("SOURCE_INVALID", "only https URLs are accepted for skills")
    if parts.username or parts.password:
        raise SkillError("SOURCE_INVALID", "a skill URL must not carry credentials")
    clean = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    host = parts.hostname or ""
    segments = [s for s in parts.path.split("/") if s]
    if host in _SKILLS_SH_HOSTS and len(segments) >= 2:
        # skills.sh/<owner>/<repo>[/<skill>]: the skill lives in that GitHub repo.
        skill = segments[2] if len(segments) >= 3 else None
        source = _parse_url(f"https://github.com/{segments[0]}/{segments[1]}")
        if skill is not None and not _NAME_RE.match(skill):
            raise SkillError("NAME_INVALID", f"not a skill name: {skill}")
        return _with_skill(source, skill)
    if host in _GITHUB_HOSTS and len(segments) >= 2:
        owner, repo = segments[0], segments[1].removesuffix(".git")
        ref, subpath = "HEAD", ""
        if len(segments) >= 4 and segments[2] in ("tree", "blob"):
            ref = segments[3]
            rest = segments[4:]
            if rest and rest[-1] == SKILL_FILE:
                rest = rest[:-1]
            subpath = "/".join(rest)
        if not re.match(r"^[A-Za-z0-9._/-]+$", ref) or ".." in ref:
            raise SkillError("SOURCE_INVALID", f"unsupported git ref: {ref}")
        archive = f"https://codeload.github.com/{owner}/{repo}/zip/{ref}"
        return SkillSource(
            "github", clean, url=archive, subpath=subpath, owner=owner, repo=repo, ref=ref
        )
    if parts.path.lower().endswith(".zip"):
        return SkillSource("zip_url", clean, url=clean)
    return SkillSource("url", clean, url=clean)


def default_http_get(url: str, limit: int = DOWNLOAD_MAX_BYTES, *, home=None) -> bytes:
    from rinari.net import client as net_client

    with (
        net_client(
            home, timeout=30.0, follow_redirects=True, headers={"User-Agent": "rinari-skills"}
        ) as client,
        client.stream("GET", url) as response,
    ):
        if response.url.scheme != "https":
            raise SkillError("SOURCE_INVALID", "the download was redirected off https")
        if response.status_code >= 400:
            raise SkillError("DOWNLOAD_FAILED", f"HTTP {response.status_code} for {url}")
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > limit:
                raise SkillError("SOURCE_TOO_LARGE", f"download exceeds {limit} bytes")
            chunks.append(chunk)
    return b"".join(chunks)


@dataclass(frozen=True, slots=True)
class Materialized:
    root: Path
    # Only each skill's SKILL.md came in (too many or too large to bring all):
    # the chosen one is fetched whole before it is installed.
    partial: bool = False


class SkillFetcher:
    """Turns a source into a local folder that contains one or more skills."""

    def __init__(self, http_get: Callable[[str], bytes] | None = None, *, home=None) -> None:
        self._get = http_get or functools.partial(default_http_get, home=home)

    def materialize(self, source: SkillSource, work: Path) -> Path:
        return self.fetch(source, work).root

    def fetch(
        self,
        source: SkillSource,
        work: Path,
        *,
        name: str | None = None,
        allow_large: bool = False,
    ) -> Materialized:
        """The source on disk; with `name` (or the source's own), only that skill."""
        wanted = name or source.skill
        if source.kind == "github" and source.owner:
            try:
                return self._github(source, work, wanted, allow_large)
            except _TreeUnavailable:
                pass  # no API (rate limit, private): the archive, as before
        if source.kind == "zip":
            assert source.path is not None
            return extract_skills(source.path, work / "extracted", wanted, allow_large)
        if source.kind in ("zip_url", "github"):
            assert source.url is not None
            archive = work / "download.zip"
            archive.write_bytes(self._get(source.url))
            if source.kind == "zip_url":
                return extract_skills(archive, work / "extracted", wanted, allow_large)
            root = safe_extract(archive, work / "extracted")
            if source.kind == "github":
                root = _single_child(root)
                if source.subpath:
                    root = _inside(root, source.subpath)
            return Materialized(root)
        if source.kind == "dir":
            assert source.path is not None
            return Materialized(source.path)
        if source.kind == "url":
            assert source.url is not None
            data = self._get(source.url)
            if len(data) > SKILL_FILE_MAX_BYTES:
                raise SkillError("SOURCE_TOO_LARGE", "a SKILL.md over 1 MB is not a skill")
            try:
                text = data.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise SkillError("SOURCE_INVALID", "that URL is not a UTF-8 SKILL.md") from exc
            fields, _body = read_frontmatter(text)
            name = as_text(fields.get("name"))
            if not _NAME_RE.match(name):
                raise SkillError("NAME_INVALID", "the SKILL.md at that URL has no valid name")
            folder = work / "single" / name
            folder.mkdir(parents=True)
            (folder / SKILL_FILE).write_text(text, encoding="utf-8")
            return Materialized(folder.parent)
        raise SkillError("SOURCE_INVALID", f"unknown source kind {source.kind}")

    def _github(
        self, source: SkillSource, work: Path, wanted: str | None, allow_large: bool
    ) -> Materialized:
        """List the repository tree once and fetch only the chosen skill's files."""
        api = (
            f"https://api.github.com/repos/{source.owner}/{source.repo}/git/trees/"
            f"{quote(source.ref, safe='')}?recursive=1"
        )
        try:
            listing = json.loads(self._get(api).decode("utf-8"))
        except (SkillError, ValueError, UnicodeDecodeError) as exc:
            raise _TreeUnavailable() from exc
        if not isinstance(listing, dict) or listing.get("truncated"):
            raise _TreeUnavailable()
        entries = [
            (str(item["path"]), int(item.get("size") or 0))
            for item in listing.get("tree") or []
            if isinstance(item, dict) and item.get("type") == "blob" and item.get("path")
        ]
        prefix = source.subpath.strip("/")
        if prefix:
            entries = [
                (path, size)
                for path, size in entries
                if path == prefix or path.startswith(prefix + "/")
            ]
        plan = plan_selection(entries, wanted, allow_large)
        root = work / "extracted"
        for path in plan.files:
            relative = path[len(prefix) + 1 :] if prefix else path
            target = (root / PurePosixPath(relative)).resolve()
            if not target.is_relative_to(root.resolve()):
                raise SkillError("SOURCE_INVALID", f"repository path escapes its folder: {path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            raw = (
                f"https://raw.githubusercontent.com/{source.owner}/{source.repo}/"
                f"{quote(source.ref, safe='')}/{quote(path)}"
            )
            target.write_bytes(self._get(raw))
        return Materialized(root, partial=plan.partial)


class _TreeUnavailable(Exception):
    """The GitHub tree could not be listed; fall back to the archive."""


@dataclass(frozen=True, slots=True)
class Selection:
    files: tuple[str, ...]
    partial: bool


def skill_dirs(paths: list[str]) -> list[str]:
    """Folders (posix, "" for the root) holding a SKILL.md, outermost only."""
    found: set[str] = set()
    for path in paths:
        pure = PurePosixPath(path)
        if pure.name != SKILL_FILE:
            continue
        parent = "" if str(pure.parent) == "." else str(pure.parent)
        hidden = any(
            part.startswith(".") or part in _SKIP_DIRS for part in PurePosixPath(parent).parts
        )
        if not hidden:
            found.add(parent)
    outer: list[str] = []
    for folder in sorted(found):
        # A SKILL.md inside another skill (an example, a template) is not a second skill.
        if not any(parent == "" or folder.startswith(parent + "/") for parent in outer):
            outer.append(folder)
    return outer


def _under(path: str, folder: str) -> bool:
    return folder == "" or path.startswith(folder + "/")


def plan_selection(
    entries: list[tuple[str, int]], wanted: str | None, allow_large: bool
) -> Selection:
    """Which files to bring in: the chosen skill, all skills, or only their SKILL.md."""
    dirs = skill_dirs([path for path, _ in entries])
    if not dirs:
        raise SkillError("SKILL_NOT_FOUND", "no SKILL.md found in that source")
    if wanted:
        named = [d for d in dirs if PurePosixPath(d).name == wanted]
        if named:
            dirs = named[:1]
    chosen = [(p, s) for p, s in entries if any(_under(p, d) for d in dirs)]
    if len(dirs) == 1:
        _check_skill_size(dirs[0], chosen, allow_large)
        return Selection(tuple(p for p, _ in chosen), partial=False)
    size = sum(s for _, s in chosen)
    if len(chosen) <= EXTRACT_MAX_FILES and size <= EXTRACT_MAX_BYTES:
        return Selection(tuple(p for p, _ in chosen), partial=False)
    # Too much to bring every skill: their SKILL.md, to list and choose.
    manifests = tuple(f"{d}/{SKILL_FILE}" if d else SKILL_FILE for d in dirs)
    return Selection(manifests, partial=True)


def _check_skill_size(folder: str, files: list[tuple[str, int]], allow_large: bool) -> None:
    size = sum(s for _, s in files)
    name = PurePosixPath(folder).name or "skill"
    if size > HARD_MAX_BYTES:
        raise SkillError("SOURCE_TOO_LARGE", f"{name} is over {HARD_MAX_BYTES} bytes")
    if allow_large:
        return
    if len(files) > EXTRACT_MAX_FILES or size > EXTRACT_MAX_BYTES:
        raise SkillError(
            "SKILL_TOO_LARGE",
            f"{name} has {len(files)} files and {size} bytes, over the "
            f"{EXTRACT_MAX_FILES} files / {EXTRACT_MAX_BYTES} bytes limit for one skill",
            details={"name": name, "files": len(files), "bytes": size},
        )


def extract_skills(
    archive: Path, dest: Path, wanted: str | None, allow_large: bool
) -> Materialized:
    """Read the archive's index first; extract only the skill(s) it plans to keep."""
    try:
        bundle = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as exc:
        raise SkillError("SOURCE_INVALID", "not a valid .zip archive") from exc
    with bundle:
        members = [info for info in bundle.infolist() if not info.is_dir()]
        names = [info.filename.replace("\\", "/") for info in members]
        # A GitHub archive wraps everything in one top folder; plan inside it.
        tops = {PurePosixPath(n).parts[0] for n in names if "/" in n}
        wrapper = next(iter(tops)) if len(tops) == 1 and all("/" in n for n in names) else ""
        entries = [
            (n[len(wrapper) + 1 :] if wrapper else n, info.file_size)
            for n, info in zip(names, members, strict=True)
        ]
        plan = plan_selection(entries, wanted, allow_large)
        keep = {(wrapper + "/" + p) if wrapper else p for p in plan.files}
        selected = [info for info, n in zip(members, names, strict=True) if n in keep]
        root = _extract_members(bundle, selected, dest, strip=wrapper)
    return Materialized(root, partial=plan.partial)


def _extract_members(bundle, members, dest: Path, *, strip: str = "") -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    for info in members:
        name = info.filename.replace("\\", "/")
        pure = PurePosixPath(name)
        if pure.is_absolute() or ".." in pure.parts or re.match(r"^[A-Za-z]:", name):
            raise SkillError("SOURCE_INVALID", f"archive entry escapes its folder: {name}")
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise SkillError("SOURCE_INVALID", f"archive contains a symlink: {name}")
        if info.compress_size and info.file_size / info.compress_size > _MAX_RATIO:
            raise SkillError("SOURCE_INVALID", f"archive entry is a compression bomb: {name}")
        relative = PurePosixPath(*pure.parts[1:]) if strip and pure.parts[0] == strip else pure
        target = (root / relative).resolve()
        if not target.is_relative_to(root):
            raise SkillError("SOURCE_INVALID", f"archive entry escapes its folder: {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with bundle.open(info) as source, target.open("wb") as sink:
            written = 0
            while chunk := source.read(1024 * 1024):
                written += len(chunk)
                if written > info.file_size:
                    raise SkillError("SOURCE_INVALID", f"archive entry lies about its size: {name}")
                sink.write(chunk)
    return root


def safe_extract(archive: Path, dest: Path) -> Path:
    """Extract a zip without leaving `dest`; caps size and file count."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    try:
        bundle = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as exc:
        raise SkillError("SOURCE_INVALID", "not a valid .zip archive") from exc
    with bundle:
        members = [info for info in bundle.infolist() if not info.is_dir()]
        if len(members) > EXTRACT_MAX_FILES:
            raise SkillError("SOURCE_TOO_LARGE", f"archive has over {EXTRACT_MAX_FILES} files")
        if sum(info.file_size for info in members) > EXTRACT_MAX_BYTES:
            raise SkillError("SOURCE_TOO_LARGE", f"archive expands over {EXTRACT_MAX_BYTES} bytes")
        for info in members:
            name = info.filename.replace("\\", "/")
            pure = PurePosixPath(name)
            if pure.is_absolute() or ".." in pure.parts or re.match(r"^[A-Za-z]:", name):
                raise SkillError("SOURCE_INVALID", f"archive entry escapes its folder: {name}")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise SkillError("SOURCE_INVALID", f"archive contains a symlink: {name}")
            target = (root / pure).resolve()
            if not target.is_relative_to(root):
                raise SkillError("SOURCE_INVALID", f"archive entry escapes its folder: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, target.open("wb") as sink:
                sink.write(source.read())
    return root


def find_candidates(root: Path) -> list[Path]:
    """Skill folders under `root`: itself, or SKILL.md folders a few levels down.
    A SKILL.md nested inside another skill (an example, a template) is not a
    second skill."""
    if (root / SKILL_FILE).is_file():
        return [root]
    found: list[Path] = []

    def walk(folder: Path, depth: int) -> None:
        if depth > _SEARCH_DEPTH:
            return
        for child in sorted(folder.iterdir(), key=lambda p: p.name):
            if not child.is_dir() or child.is_symlink():
                continue
            if child.name.startswith(".") or child.name in _SKIP_DIRS:
                continue
            if (child / SKILL_FILE).is_file():
                found.append(child)
                continue
            walk(child, depth + 1)

    walk(root, 1)
    return found


def import_kind(folder: Path, home: Path) -> str:
    """ "claude" / "codex" / "agents" when the folder lives where those tools
    keep their skills; plain "dir" otherwise."""
    resolved = folder.resolve()
    for kind, relative in IMPORT_FOLDERS:
        base = (home / relative).resolve()
        if resolved == base or resolved.is_relative_to(base):
            return kind
    return "dir"


def _single_child(root: Path) -> Path:
    children = [p for p in root.iterdir() if p.is_dir()]
    return children[0] if len(children) == 1 and not (root / SKILL_FILE).exists() else root


def _inside(root: Path, subpath: str) -> Path:
    target = (root / subpath).resolve()
    if not target.is_relative_to(root.resolve()) or not target.is_dir():
        raise SkillError("SKILL_NOT_FOUND", f"no folder {subpath!r} in that repository")
    return target


__all__ = [
    "IMPORT_FOLDERS",
    "Materialized",
    "SkillFetcher",
    "SkillSource",
    "default_http_get",
    "find_candidates",
    "import_kind",
    "parse_source",
    "safe_extract",
]
