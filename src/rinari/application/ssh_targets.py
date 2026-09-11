"""Immutable SSH destinations owned by the installation, never the model."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import ipaddress
import json
import re
import sqlite3
from pathlib import Path


class TargetStore:
    def __init__(self, root: Path):
        self.root = root / "ssh"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.root / "identities").mkdir(mode=0o700, exist_ok=True)
        self.path = self.root / "targets.sqlite"
        with self.connect() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                raise ValueError("Unsupported target schema")
            db.execute(
                "CREATE TABLE IF NOT EXISTS targets (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
            )
            db.execute("PRAGMA user_version=1")

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def list(self):
        with self.connect() as db:
            return [
                json.loads(row[0]) for row in db.execute("SELECT data FROM targets ORDER BY id")
            ]

    def get(self, identity):
        return next((row for row in self.list() if row["id"] == identity), None)

    def add(self, data):
        if set(data) != {"id", "name", "host", "port", "username", "identity", "host_key"}:
            raise ValueError("Invalid target fields")
        for key in ("id", "identity", "username"):
            if not isinstance(data[key], str) or not re.fullmatch(
                r"[a-zA-Z0-9_][a-zA-Z0-9_-]{0,63}", data[key]
            ):
                raise ValueError("Invalid target identifier")
        if data["id"] == "gateway":
            raise ValueError("Reserved target identifier")
        if not isinstance(data["name"], str) or not 1 <= len(data["name"]) <= 80:
            raise ValueError("Invalid target name")
        host = str(ipaddress.ip_address(data["host"]))
        if type(data["port"]) is not int or not 1 <= data["port"] <= 65535:
            raise ValueError("Invalid port")
        # Ed25519-only initial contract: validate the wire encoding, not just base64.
        parts = data["host_key"].split()
        if len(parts) != 2 or parts[0] != "ssh-ed25519":
            raise ValueError("An explicit Ed25519 host public key is required")
        raw = base64.b64decode(parts[1], validate=True)
        if len(raw) != 51 or raw[:19] != b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20":
            raise ValueError("Invalid Ed25519 host key")
        record = {**data, "host": host, "kind": "ssh"}
        record["revision"] = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT data FROM targets WHERE id=?", (data["id"],)).fetchone()
            if old and json.loads(old[0]) != record:
                raise ValueError("Target is immutable; register a new identifier")
            db.execute(
                "INSERT OR IGNORE INTO targets VALUES (?,?)", (data["id"], json.dumps(record))
            )
        return record
