"""Resolve a bounded static OpenSSH alias without running Match exec or proxies."""

from __future__ import annotations

import fnmatch
import getpass
import hashlib
import shlex
from pathlib import Path


def resolve_alias(alias: str, home: Path) -> dict | None:
    if not alias or alias.startswith("-") or any(c.isspace() for c in alias):
        return None
    config = home / ".ssh" / "config"
    if not config.is_file() or config.stat().st_size > 262144:
        return None
    raw = config.read_text(encoding="utf-8-sig")
    options: dict[str, str] = {}
    matched = False
    active = True
    for line in raw.splitlines():
        lexer = shlex.shlex(line, posix=True)
        lexer.whitespace_split = True
        lexer.escape = ""  # Keep Windows path separators; quoted spaces remain supported.
        parts = list(lexer)
        if not parts:
            continue
        first = parts[0].split("=", 1)
        key = first[0].lower()
        values = ([first[1]] if len(first) == 2 and first[1] else []) + parts[1:]
        if values and values[0] == "=":
            values = values[1:]
        elif values and values[0].startswith("="):
            values[0] = values[0][1:]
        if key == "host":
            positive = any(fnmatch.fnmatchcase(alias, p) for p in values if not p.startswith("!"))
            negative = any(fnmatch.fnmatchcase(alias, p[1:]) for p in values if p.startswith("!"))
            active = positive and not negative
            matched |= active and any(p == alias for p in values)
        elif key in {"match", "include"}:
            # Includes and conditional execution require the full OpenSSH evaluator.
            # Do not silently use a different destination than the user's config.
            return None
        elif active and values:
            options.setdefault(key, " ".join(values))
    if not matched or any(options.get(k, "none") != "none" for k in ("proxycommand", "proxyjump")):
        return None
    host = options.get("hostname", alias)
    if host.startswith("-") or any(c.isspace() or c in "%/\\" for c in host):
        return None
    identity = options.get("identityfile")
    key_path = (
        Path(identity.replace("~", str(home), 1))
        if identity
        else next(
            (home / ".ssh" / n for n in ("id_ed25519", "id_rsa") if (home / ".ssh" / n).is_file()),
            home / ".ssh" / "id_ed25519",
        )
    )
    if not key_path.is_absolute() or "%" in str(key_path):
        return None
    known_hosts = options.get("userknownhostsfile")
    known_path = (
        Path(known_hosts.replace("~", str(home), 1))
        if known_hosts
        else home / ".ssh" / "known_hosts"
    )
    if not known_path.is_absolute() or "%" in str(known_path):
        return None
    port = int(options.get("port", "22"))
    if not 1 <= port <= 65535:
        return None
    return {
        "id": alias,
        "name": alias,
        "host": host,
        "port": port,
        "username": options.get("user", getpass.getuser()),
        "identity_path": str(key_path),
        "known_hosts": str(known_path),
        "host_alias": options.get("hostkeyalias"),
        "revision": hashlib.sha256(raw.encode()).hexdigest(),
        "source": "openssh-config",
    }
