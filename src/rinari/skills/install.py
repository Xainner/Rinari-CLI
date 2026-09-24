"""Where a skill comes from, and getting it onto disk safely.

Sources the owner can give:

    C:\\path\\to\\skill            a folder with SKILL.md (or a folder of skills)
    C:\\path\\to\\skills.zip        an archive
    https://github.com/o/r/tree/main/skills/pdf   a repo, optionally a subfolder
    https://example.com/x/SKILL.md                a single-file skill
    https://example.com/skills.zip                an archive over HTTPS

Everything remote is HTTPS only, size-capped, and extracted without leaving
the work folder (no absolute paths, no `..`, no symlinks). Nothing here runs a
file: installing copies text the owner reviewed.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit

from rinari.skills.frontmatter import as_text, read_frontmatter
from rinari.skills.manifest import SKILL_FILE, SkillError

DOWNLOAD_MAX_BYTES = 25 * 1024 * 1024
EXTRACT_MAX_BYTES = 60 * 1024 * 1024
EXTRACT_MAX_FILES = 2000
SKILL_FILE_MAX_BYTES = 1024 * 1024
_SEARCH_DEPTH = 4
_SKIP_DIRS = frozenset({"node_modules", "__pycache__", ".git"})
_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
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


def parse_source(text: str) -> SkillSource:
    raw = str(text or "").strip().strip('"')
    if not raw:
        raise SkillError("SOURCE_INVALID", "empty skill source")
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


def _parse_url(raw: str) -> SkillSource:
    parts = urlsplit(raw)
    if parts.scheme.lower() != "https":
        raise SkillError("SOURCE_INVALID", "only https URLs are accepted for skills")
    if parts.username or parts.password:
        raise SkillError("SOURCE_INVALID", "a skill URL must not carry credentials")
    clean = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    host = parts.hostname or ""
    segments = [s for s in parts.path.split("/") if s]
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
        return SkillSource("github", clean, url=archive, subpath=subpath)
    if parts.path.lower().endswith(".zip"):
        return SkillSource("zip_url", clean, url=clean)
    return SkillSource("url", clean, url=clean)


def default_http_get(url: str, limit: int = DOWNLOAD_MAX_BYTES) -> bytes:
    import httpx

    with (
        httpx.Client(
            timeout=30.0, follow_redirects=True, headers={"User-Agent": "rinari-skills"}
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


class SkillFetcher:
    """Turns a source into a local folder that contains one or more skills."""

    def __init__(self, http_get: Callable[[str], bytes] | None = None) -> None:
        self._get = http_get or default_http_get

    def materialize(self, source: SkillSource, work: Path) -> Path:
        if source.kind == "dir":
            assert source.path is not None
            return source.path
        if source.kind == "zip":
            assert source.path is not None
            return safe_extract(source.path, work / "extracted")
        if source.kind in ("zip_url", "github"):
            assert source.url is not None
            archive = work / "download.zip"
            archive.write_bytes(self._get(source.url))
            root = safe_extract(archive, work / "extracted")
            if source.kind == "github":
                root = _single_child(root)
                if source.subpath:
                    root = _inside(root, source.subpath)
            return root
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
            return folder.parent
        raise SkillError("SOURCE_INVALID", f"unknown source kind {source.kind}")


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
    "SkillFetcher",
    "SkillSource",
    "default_http_get",
    "find_candidates",
    "import_kind",
    "parse_source",
    "safe_extract",
]
