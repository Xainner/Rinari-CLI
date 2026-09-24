"""Static review of a skill folder before it is installed.

An external skill is someone else's instructions (and maybe scripts) that the
model will follow. The review does not decide for the owner: it lists what it
found, with file and line, so the install can ask for a confirmation that
covers exactly that content. It never executes anything, and a clean review
grants nothing: every tool call still goes through policy.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

# Text files larger than this are not scanned line by line (reported instead).
_SCAN_MAX_BYTES = 2 * 1024 * 1024
_EXCERPT = 160

# Compiled executables cannot be read as instructions; text scripts (.sh,
# .ps1, .bat…) are scanned line by line like everything else.
_BINARY_SUFFIXES = frozenset({".exe", ".dll", ".so", ".dylib", ".bin", ".msi", ".scr", ".com"})
# Zero-width and bidi-override characters: text that reads differently than it
# looks. Built from code points so this file itself contains none of them.
_ZERO_WIDTH = (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF)
_BIDI_RANGES = ((0x202A, 0x202E), (0x2066, 0x2069))
_HIDDEN_CHARS = re.compile(
    "["
    + "".join(chr(point) for point in _ZERO_WIDTH)
    + "".join(f"{chr(low)}-{chr(high)}" for low, high in _BIDI_RANGES)
    + "]"
)

# (code, severity, pattern). "danger" = the skill tries to act against the
# owner or the system; "warning" = legitimate in context, worth a look.
_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "PROMPT_INJECTION",
        "danger",
        re.compile(
            r"ignore (all |any )?(previous|prior|above) (instructions|rules)"
            r"|disregard (the |your )?(system|previous) (prompt|instructions)"
            r"|reveal (your|the) system prompt"
            r"|do not (tell|inform|show) the user",
            re.IGNORECASE,
        ),
    ),
    (
        "REMOTE_CODE",
        "danger",
        re.compile(
            r"(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(ba|z)?sh\b"
            r"|\b(iwr|irm|invoke-webrequest|invoke-restmethod)\b[^\n|]*\|\s*(iex|invoke-expression)"
            r"|eval\s+\"?\$\((curl|wget)",
            re.IGNORECASE,
        ),
    ),
    (
        "EXFILTRATION",
        "danger",
        re.compile(
            r"(curl|wget|invoke-webrequest|iwr)\b[^\n]*(-d|--data|-F|--form|-Body)\b[^\n]*"
            r"(\$\{?[A-Z_]*(TOKEN|KEY|SECRET|PASSWORD)|\.ssh|\.aws|id_rsa|\.env\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "SECRET_ACCESS",
        "warning",
        re.compile(
            r"~/\.ssh/|\.ssh[\\/]id_|\.aws[\\/]credentials|\.docker[\\/]config\.json"
            r"|\.netrc|\.git-credentials",
            re.IGNORECASE,
        ),
    ),
    (
        "DESTRUCTIVE",
        "danger",
        re.compile(
            r"\brm\s+-(rf|fr)\s+(/|~|\$HOME)(\s|$)"
            r"|remove-item\b[^\n]*-recurse[^\n]*\b[a-z]:\\\s*$"
            r"|\bformat\s+[a-z]:"
            r"|\bmkfs(\.\w+)?\b"
            r"|\bdd\s+if=",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    (
        "OBFUSCATION",
        "warning",
        re.compile(r"base64\s+(-d|--decode)[^\n]*\|\s*(ba)?sh|frombase64string", re.IGNORECASE),
    ),
)


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    severity: str  # "danger" | "warning"
    file: str
    line: int | None
    excerpt: str

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "excerpt": self.excerpt,
        }


@dataclass(frozen=True, slots=True)
class Review:
    content_hash: str
    files: int
    size: int
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def verdict(self) -> str:
        if any(f.severity == "danger" for f in self.findings):
            return "danger"
        return "warning" if self.findings else "ok"

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "content_hash": self.content_hash,
            "files": self.files,
            "size": self.size,
            "findings": [f.to_dict() for f in self.findings],
        }


def content_hash(root: Path) -> str:
    """sha256 over (relative path, bytes) of every file, in a stable order."""
    digest = hashlib.sha256()
    for path in _files(root):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def review_skill(root: Path) -> Review:
    findings: list[Finding] = []
    total = 0
    files = _files(root)
    for path in files:
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        total += size
        if path.suffix.lower() in _BINARY_SUFFIXES:
            findings.append(Finding("EXECUTABLE_FILE", "warning", relative, None, path.name))
            continue
        if size > _SCAN_MAX_BYTES:
            findings.append(Finding("LARGE_FILE", "warning", relative, None, f"{size} bytes"))
            continue
        raw = path.read_bytes()
        if b"\0" in raw:
            continue  # binary asset (image, font…): nothing to read as instructions
        text = raw.decode("utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            if _HIDDEN_CHARS.search(line):
                findings.append(
                    Finding("HIDDEN_UNICODE", "danger", relative, number, _excerpt(line))
                )
            for code, severity, pattern in _RULES:
                if pattern.search(line):
                    findings.append(Finding(code, severity, relative, number, _excerpt(line)))
    return Review(
        content_hash=content_hash(root),
        files=len(files),
        size=total,
        findings=tuple(findings),
    )


def _files(root: Path) -> list[Path]:
    # Same set the installer copies: no symlinks, no .git.
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink() and ".git" not in p.relative_to(root).parts
    )


def _excerpt(line: str) -> str:
    clean = _HIDDEN_CHARS.sub("␣", line.strip())
    return clean if len(clean) <= _EXCERPT else clean[: _EXCERPT - 1] + "…"


__all__ = ["Finding", "Review", "content_hash", "review_skill"]
