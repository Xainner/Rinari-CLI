"""Secret redaction for logs, traces, and user-facing output."""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

# Secrets recognized by shape, for text whose secrets are not known in advance
# (commands and outputs a session recorded). Each pattern keeps its label and
# replaces only the value, so what was hidden stays readable.
_TOKEN_SHAPES = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"|\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}"
    r"|\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bAIza[0-9A-Za-z_-]{35}"
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)(\s+)[A-Za-z0-9._~+/=-]{8,}")
_HEADER = re.compile(
    r"(?i)\b(authorization|proxy-authorization|x-api-key|api-key|x-auth-token|cookie)"
    r"(\s*[:=]\s*[\"']?)(?!bearer\b|basic\b|\[REDACTED\])[^\s\"',;&]{6,}"
)
_QUERY = re.compile(
    r"(?i)([?&](?:access_token|refresh_token|token|api_key|apikey|key|secret|password|"
    r"pwd|sig|signature)=)[^&\s\"'#]+"
)
_ASSIGNMENT = re.compile(
    r"(?i)\b((?:password|passwd|pwd|secret|client_secret|api_key|apikey|access_token|"
    r"refresh_token|auth_token)\s*[:=]\s*[\"']?)(?!\[REDACTED\])[^\s\"',;&]{4,}"
)


def redact_text(text: str, known: list[str] | tuple[str, ...] = ()) -> str:
    """Hide secrets in free text: known values first, then recognizable shapes."""
    if not text:
        return text
    for secret in known:
        if secret and secret in text:
            text = text.replace(secret, REDACTED)
    text = _TOKEN_SHAPES.sub(REDACTED, text)
    text = _BEARER.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _QUERY.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    return _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{REDACTED}", text)


def redact_secret(value: str) -> str:
    """Mask a secret, keeping only enough to identify it."""
    if not value:
        return REDACTED
    if len(value) <= 7:
        return REDACTED
    return f"{value[:3]}...{value[-4:]}"


class Redactor:
    """Replaces known secret values inside arbitrary text."""

    def __init__(self, secrets: list[str] | tuple[str, ...] = ()) -> None:
        self._secrets = [s for s in secrets if s]

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        return text
