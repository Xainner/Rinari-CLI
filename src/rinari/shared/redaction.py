"""Secret redaction for logs, traces, and user-facing output."""

from __future__ import annotations

REDACTED = "[REDACTED]"


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
