"""Secret redaction for logs, traces, stored history and user-facing output."""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

# Secrets recognized by shape, for text whose secrets are not known in advance
# (commands and outputs a session recorded). Each pattern keeps its label and
# replaces only the value, so what was hidden stays readable.
_TOKEN_SHAPES = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"|\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}"
    r"|\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"
    r"|\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}"
    r"|\bglpat-[A-Za-z0-9_-]{20,}"
    r"|\bhf_[A-Za-z0-9]{30,}"
    r"|\bnpm_[A-Za-z0-9]{36}\b"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bAIza[0-9A-Za-z_-]{35}"
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)(\s+)[A-Za-z0-9._~+/=-]{8,}")
_HEADER = re.compile(
    r"(?i)\b(authorization|proxy-authorization|x-api-key|api-key|x-auth-token|cookie)"
    r"(\s*[:=]\s*[\"']?)(?!bearer\b|basic\b|\[REDACTED\])(?![$%<])[^\s\"',;&]{6,}"
)
_QUERY = re.compile(
    r"(?i)([?&](?:access_token|refresh_token|token|api_key|apikey|key|secret|password|"
    r"pwd|sig|signature)=)[^&\s\"'#]+"
)
# What a credential value never is: a reference to where it lives ($VAR,
# %VAR%, <placeholder>, a path) or code that computes it (a type, a call, an
# index). Skipping these keeps source files and env dumps readable.
_NOT_A_VALUE = (
    r"(?!\[REDACTED\])(?![$%<*/~.])(?![A-Za-z]:[\\/])"
    r"(?!(?:str|string|int|bool|boolean|number|any|none|null|true|false|undefined|"
    r"optional|secretstr)\b)"
    r"(?!(?:process\.env|os\.environ|import\.meta\.env)\b)"
    r"(?![A-Za-z_][\w.]*[(\[])"
)
# NAME=value / name: value / "name": "value", with the name allowed to carry a
# prefix (DB_PASSWORD, GITHUB_TOKEN, private-token) so env files, compose
# files, connection strings and JSON configs are all covered.
_ASSIGNMENT = re.compile(
    r"(?i)((?<![A-Za-z0-9])(?:[A-Za-z0-9]+[_.-])*"
    r"(?:password|passwd|passphrase|pwd|secret|client_secret|api_?key|access_?token|"
    r"refresh_?token|auth_?token|token|access_?key|secret_?key|private_?key)"
    r"[\"']?\s*[:=]\s*[\"']?)" + _NOT_A_VALUE + r"[^\s\"',;&]{4,}"
)
# scheme://user:password@host keeps the user and the host.
_URL_USERINFO = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@\"']+:)(?!\[REDACTED\])[^\s/@\"']+(?=@)"
)
_VALUE = r"(\"[^\"]*\"|'[^']*'|[^\s\"']+)"
# Command-line forms: the secret is a positional or flag value, not NAME=value.
_COMMAND_PATTERNS = (
    # net use [X:] \\host\share <password> /user:<name>  (Windows)
    re.compile(
        r"(?i)(\bnet(?:\.exe)?\s+use\b[^\n&|]*?\\\\[^\s\\\"']+\\\S*\s+)(?![/*])"
        r"(?!\[REDACTED\])" + _VALUE
    ),
    # net use \\host\share /user:<name> <password>
    re.compile(
        r"(?i)(\bnet(?:\.exe)?\s+use\b[^\n&|]*?\s/u(?:ser)?:\S+\s+)(?![/*])"
        r"(?!\[REDACTED\])" + _VALUE
    ),
    # net user <name> <password>
    re.compile(r"(?i)(\bnet(?:\.exe)?\s+user\s+(?![/*])\S+\s+)(?![/*])(?!\[REDACTED\])" + _VALUE),
    # mysql -p<password> (attached: `-p` alone prompts, `-P` is the port)
    re.compile(
        r"(\b(?:mysql|mysqldump|mysqladmin|mysqlimport|mysqlcheck|mysqlsh|mariadb"
        r"|mariadb-dump)\b[^\n|;&]*?\s-p)(?!\[REDACTED\])" + _VALUE
    ),
    # sshpass -p <password>
    re.compile(r"(\bsshpass\s+-p\s*)(?!\[REDACTED\])" + _VALUE),
    # curl -u user:password / --user user:password
    re.compile(
        r"(\bcurl\b[^\n|;&]*?\s(?:-u|--user)[\s=]*[\"']?[^\s:\"']+:)(?!\[REDACTED\])"
        r"([^\s\"']+)"
    ),
    # --password x, --db-password=x, -Password "x", --token x, --api-key x ...
    re.compile(
        r"(?i)((?<![\w-])-{1,2}(?:[a-z]+[-_])?(?:password|passwd|pass|pwd|passphrase|token|"
        r"access[-_]?token|auth[-_]?token|api[-_]?key|apikey|secret|client[-_]?secret|"
        r"secret[-_]?key)(?:\s*[=:]\s*|\s+))(?![$%<>|-])(?!\[REDACTED\])" + _VALUE
    ),
    # ConvertTo-SecureString "x" -AsPlainText  (PowerShell)
    re.compile(
        r"(?i)(ConvertTo-SecureString\s+(?:-String\s+)?)(?!\[REDACTED\])"
        r"(\"[^\"]*\"|'[^']*'|[^\s\"'|;)]+)(?=[^\n]*-AsPlainText)"
    ),
)
# Structured fields whose whole value is a credential, whatever it looks like.
_SECRET_KEY = re.compile(
    r"(?i)^(?:.*[_-])?(?:password|passwd|passphrase|secret|client_?secret|token|"
    r"access_?token|refresh_?token|auth_?token|api_?key|apikey|access_?key|secret_?key|"
    r"private_?key|authorization|cookie|credentials?)$"
)
# References to where a secret lives are not the secret.
_REFERENCE_PREFIXES = ("env://", "keyring://", "file://", "artifact://", REDACTED)


def redact_text(text: str, known: list[str] | tuple[str, ...] = ()) -> str:
    """Hide secrets in free text: known values first, then recognizable shapes."""
    if not text:
        return text
    for secret in known:
        if secret and secret in text:
            text = text.replace(secret, REDACTED)
    text = _TOKEN_SHAPES.sub(REDACTED, text)
    text = _URL_USERINFO.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    for pattern in _COMMAND_PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1)}{_hidden(m.group(2))}", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _QUERY.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    return _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{REDACTED}", text)


def _hidden(value: str) -> str:
    # A quoted value keeps its quotes so the command still reads as one.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return f"{value[0]}{REDACTED}{value[0]}"
    return REDACTED


def redact_value(
    value: Any,
    known: list[str] | tuple[str, ...] = (),
    *,
    skip_keys: frozenset[str] = frozenset(),
) -> Any:
    """`redact_text` over every string of a JSON-like value.

    A string under a credential-named key (password, api_key, Authorization…)
    is hidden whole, since its shape alone would not give it away. Keys in
    `skip_keys` hold opaque provider blobs (signatures, encrypted reasoning)
    that must stay byte-exact and cannot carry readable secrets.
    """
    if isinstance(value, str):
        return redact_text(value, known)
    if isinstance(value, dict):
        result: dict[Any, Any] = {}
        for key, item in value.items():
            if key in skip_keys:
                result[key] = item
            elif (
                isinstance(item, str)
                and item
                and isinstance(key, str)
                and _SECRET_KEY.match(key)
                and not item.startswith(_REFERENCE_PREFIXES)
            ):
                result[key] = REDACTED
            else:
                result[key] = redact_value(item, known, skip_keys=skip_keys)
        return result
    if isinstance(value, (list, tuple)):
        return [redact_value(item, known, skip_keys=skip_keys) for item in value]
    return value


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

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(self._secrets)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        return text


REDACTION_WRITE_ERROR = (
    "The new content adds [REDACTED], the marker that hides secrets in stored history; "
    "it is not the real value. Read the real source again (fs.read with fresh=true) or "
    "ask the user, and never write the marker into a file."
)


def adds_redaction_marker(before: str, after: str) -> bool:
    """Whether a write would put the history's redaction marker into a file.

    After a secret is redacted in history, a model rebuilding a file from
    that history would otherwise write `[REDACTED]` where the key was.
    """
    return after.count(REDACTED) > before.count(REDACTED)
