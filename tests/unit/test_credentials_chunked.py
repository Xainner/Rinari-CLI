"""Secrets longer than one OS credential entry (Windows: 1280 UTF-16 units).

An OAuth bundle with a JWT access token does not fit, and CredWrite fails with
error 1783. The store splits it into parts and keeps the rotation guarantees.
"""

from __future__ import annotations

import json

import pytest

from rinari.application.credentials import KeyringCredentialStore, part_service
from rinari.application.credentials_gc import parse_managed_target
from rinari.shared.errors import CredentialWriteError


class LimitedBackend:
    """Windows semantics that matter here: one entry per service, 1280 units."""

    LIMIT = 1280

    def __init__(self) -> None:
        self.entries: dict[str, tuple[str, str]] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        if len(password.encode("utf-16-le")) // 2 > self.LIMIT:
            raise RuntimeError("(1783, 'CredWrite', 'The stub received bad data.')")
        self.entries[service] = (username, password)

    def get_password(self, service: str, username: str) -> str | None:
        entry = self.entries.get(service)
        return entry[1] if entry is not None and entry[0] == username else None

    def delete_password(self, service: str, username: str) -> None:
        entry = self.entries.get(service)
        if entry is None or entry[0] != username:
            raise RuntimeError("not found")
        del self.entries[service]


def _bundle(size: int) -> str:
    return json.dumps({"access_token": "j" * size, "refresh_token": "r", "expires_at": 1})


@pytest.fixture
def store():
    backend = LimitedBackend()
    return backend, KeyringCredentialStore(backend, scope="home1")


def test_a_long_secret_round_trips_through_parts(store) -> None:
    backend, keyring = store
    secret = _bundle(2600)
    ref = keyring.store("providers/p1", secret)
    assert ref == "keyring://providers/p1"
    assert keyring.resolve("providers/p1") == secret
    service = "rinari/home1/providers/p1"
    assert backend.entries[service][1].startswith("rinari-chunked:v1:")
    assert part_service(service, 1) in backend.entries
    # Staging and previous copies are gone once the rotation is confirmed.
    assert not any("/staging/" in name or "/previous/" in name for name in backend.entries)


def test_rotating_from_long_to_short_leaves_no_parts(store) -> None:
    backend, keyring = store
    keyring.store("providers/p1", _bundle(4000))
    keyring.store("providers/p1", "short-key")
    assert keyring.resolve("providers/p1") == "short-key"
    assert not any("#part-" in name for name in backend.entries)


def test_delete_removes_every_part(store) -> None:
    backend, keyring = store
    keyring.store("providers/p1", _bundle(3000))
    assert keyring.delete("providers/p1") is True
    assert backend.entries == {}


def test_a_missing_or_altered_part_never_resolves_to_a_wrong_value(store) -> None:
    backend, keyring = store
    keyring.store("providers/p1", _bundle(2000))
    service = "rinari/home1/providers/p1"
    username, part = backend.entries[part_service(service, 2)]
    backend.entries[part_service(service, 2)] = (username, part[:-1] + "X")
    with pytest.raises(Exception, match="not found"):
        keyring.resolve("providers/p1")


def test_a_short_secret_still_uses_one_entry(store) -> None:
    backend, keyring = store
    keyring.store("providers/p1", "sk-short")
    assert list(backend.entries) == ["rinari/home1/providers/p1"]


def test_a_secret_beyond_the_part_limit_fails_cleanly(store) -> None:
    backend, keyring = store
    with pytest.raises(CredentialWriteError):
        keyring.store("providers/p1", "x" * (600 * 64 + 1))
    assert not any(name.endswith("providers/p1") for name in backend.entries)


def test_cleanup_classifies_a_part_like_the_entry_it_completes() -> None:
    entry = parse_managed_target("rinari/home1/providers/p1", "providers/p1", "home1")
    part = parse_managed_target("rinari/home1/providers/p1#part-3", "providers/p1", "home1")
    assert entry is not None and part == entry
    staging = parse_managed_target(
        "rinari/home1/staging/providers/p1#part-1", "providers/p1", "home1"
    )
    assert staging is not None and staging.kind == "staging"
    assert parse_managed_target("rinari/home1/providers/p1#part-x", "providers/p1", "home1") is None
