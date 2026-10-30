"""Compressed archives as attachments: an index for the model, a guarded extraction.

A .zip (or .tar, .tar.gz, .tgz) is the natural way to hand over many files at
once: a review package, a project, a batch of images. Its bytes never enter
the context: the model gets the list of what is inside, and `artifact.export`
with `extract=true` unpacks it into a new folder.

What is inside is untrusted data. Extraction refuses members that would land
outside the target folder, links and devices, and stops at bounds on the
number of members, the expanded size and the compression ratio.
"""

from __future__ import annotations

import shutil
import stat
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

#: Bounds for a guarded extraction.
MAX_MEMBERS = 5_000
MAX_EXPANDED_BYTES = 2 * 1024**3
MAX_MEMBER_RATIO = 200
#: Members listed in the model's index; the rest are counted.
MAX_INDEX_ENTRIES = 300


class ArchiveError(ValueError):
    """The archive cannot be read, or extracting it would not be safe."""


@dataclass(frozen=True, slots=True)
class Member:
    name: str
    size: int
    compressed: int | None
    is_dir: bool


def archive_type(name: str) -> tuple[str, str] | None:
    """('zip' | 'tar', MIME) when the name is an archive Rinari reads, else None."""
    lowered = name.lower()
    if lowered.endswith(".zip"):
        return "zip", "application/zip"
    if lowered.endswith((".tar.gz", ".tgz")):
        return "tar", "application/gzip"
    if lowered.endswith(".tar"):
        return "tar", "application/x-tar"
    return None


def members(path: Path, name: str | None = None) -> list[Member]:
    """Every member, from the archive's own directory: nothing is decompressed."""
    kind = archive_type(name or path.name)
    if kind is None:
        raise ArchiveError("Not a supported archive (.zip, .tar, .tar.gz, .tgz)")
    try:
        if kind[0] == "zip":
            with zipfile.ZipFile(path) as archive:
                return [
                    Member(info.filename, info.file_size, info.compress_size, info.is_dir())
                    for info in archive.infolist()
                ]
        with tarfile.open(path) as archive:
            return [
                Member(info.name, info.size, None, info.isdir()) for info in archive.getmembers()
            ]
    except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError) as exc:
        raise ArchiveError("The archive is damaged or not what its name says") from exc


def archive_index(path: Path, name: str | None = None) -> str:
    """What the model reads instead of the bytes: totals, then one line per member."""
    found = members(path, name)
    files = [member for member in found if not member.is_dir]
    total = sum(member.size for member in files)
    lines = [f"{len(files)} files, {total} bytes when extracted. Members (path, bytes):"]
    for member in files[:MAX_INDEX_ENTRIES]:
        lines.append(f"{member.name}\t{member.size}")
    if len(files) > MAX_INDEX_ENTRIES:
        lines.append(f"... and {len(files) - MAX_INDEX_ENTRIES} more files")
    return "\n".join(lines)


def _safe_relative(raw: str) -> PurePosixPath:
    """The member's path inside the target, or ArchiveError when it would escape."""
    normal = raw.replace("\\", "/")
    relative = PurePosixPath(normal)
    if (
        not normal
        or normal.startswith("/")
        or (len(normal) > 1 and normal[1] == ":")
        or any(part == ".." for part in relative.parts)
    ):
        raise ArchiveError(f"A member points outside the folder: {raw!r}")
    return relative


def _check_bounds(found: list[Member]) -> None:
    if len(found) > MAX_MEMBERS:
        raise ArchiveError(f"The archive has more than {MAX_MEMBERS} members")
    total = 0
    for member in found:
        total += member.size
        if member.compressed and member.size > member.compressed * MAX_MEMBER_RATIO:
            raise ArchiveError(f"Unsafe compression ratio in {member.name!r}")
    if total > MAX_EXPANDED_BYTES:
        raise ArchiveError(f"The archive expands beyond {MAX_EXPANDED_BYTES // 1024**3} GiB")


def extract(path: Path, target: Path, name: str | None = None) -> list[Path]:
    """Extract into `target`, which must not exist yet; returns the files written.

    All-or-nothing on validation: every member is checked before the first
    byte is written. A failure while writing removes the half-made folder.
    """
    kind = archive_type(name or path.name)
    if kind is None:
        raise ArchiveError("Not a supported archive (.zip, .tar, .tar.gz, .tgz)")
    found = members(path, name)
    _check_bounds(found)
    for member in found:
        _safe_relative(member.name)
    target.mkdir(parents=True, exist_ok=False)
    written: list[Path] = []
    try:
        if kind[0] == "zip":
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    mode = info.external_attr >> 16
                    if stat.S_ISLNK(mode):
                        raise ArchiveError(f"Links are not extracted: {info.filename!r}")
                    dest = target.joinpath(*_safe_relative(info.filename).parts)
                    if info.is_dir():
                        dest.mkdir(parents=True, exist_ok=True)
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, dest.open("xb") as out:
                        _copy_bounded(source, out, info.file_size)
                    written.append(dest)
        else:
            with tarfile.open(path) as archive:
                for info in archive.getmembers():
                    if not (info.isfile() or info.isdir()):
                        raise ArchiveError(f"Only files and folders are extracted: {info.name!r}")
                    dest = target.joinpath(*_safe_relative(info.name).parts)
                    if info.isdir():
                        dest.mkdir(parents=True, exist_ok=True)
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(info)
                    if source is None:
                        continue
                    with source, dest.open("xb") as out:
                        _copy_bounded(source, out, info.size)
                    written.append(dest)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return written


def _copy_bounded(source, out, declared: int) -> None:
    """Copy at most what the member declared: a lying header cannot expand further."""
    remaining = declared
    while True:
        block = source.read(min(1024 * 1024, remaining + 1))
        if not block:
            return
        remaining -= len(block)
        if remaining < 0:
            raise ArchiveError("A member is larger than the archive declares")
        out.write(block)


__all__ = [
    "MAX_EXPANDED_BYTES",
    "MAX_MEMBERS",
    "ArchiveError",
    "archive_index",
    "archive_type",
    "extract",
    "members",
]
