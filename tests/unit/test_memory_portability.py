"""Portable memory: memory.export / memory.import between installations."""

from __future__ import annotations

import itertools
import json

import pytest

from rinari.application.context import build_app_context
from rinari.application.services import build_services
from rinari.engine_protocol.server import EngineServer
from rinari.memory.service import MEMORY_BUNDLE_MAX_RECORDS, MemoryService
from rinari.shared.clock import FakeClock
from rinari.shared.errors import InvalidUsageError


@pytest.fixture
def installs(tmp_path, monkeypatch):
    monkeypatch.setenv("RINARI_KEYRING", "0")
    contexts = []

    def make(name: str) -> MemoryService:
        ctx = build_app_context(
            home=tmp_path / name,
            clock=FakeClock(start=1_700_000_000.0 + 1000 * len(contexts), step=1.0),
        )
        contexts.append(ctx)
        return MemoryService(ctx)

    yield make
    for ctx in contexts:
        ctx.close()


def _through_javascript(value):
    """What the desktop sends back: JSON text parsed and serialized by JS,
    where an integral float such as 1.0 comes back as 1."""

    def strip(item):
        if isinstance(item, float) and item.is_integer():
            return int(item)
        if isinstance(item, dict):
            return {key: strip(child) for key, child in item.items()}
        if isinstance(item, list):
            return [strip(child) for child in item]
        return item

    return strip(json.loads(json.dumps(value)))


def _resign(memory: MemoryService, bundle: dict) -> tuple[dict, str]:
    return bundle, memory.memory_bundle_digest(bundle)


def _seed(source: MemoryService) -> dict:
    kept = source.remember_user("Responde en español", topic="idioma", provenance="panel")
    source.remember_user(
        "Saturno está en 10.0.0.7",
        kind="environment",
        topic="saturno",
        provenance="learned:session/sess-1",
        confidence=0.9,
    )
    source.remember_project("/repo", "Arranca con make dev", kind="workflow", topic="arranque")
    gone = source.remember_user("Prefiero café sin azúcar", topic="bebidas")
    row = source.get_user(gone["id"])
    source.forget_user(row["id"], expected_revision=row["revision"])
    return kept


def test_round_trip_moves_records_and_keeps_forgotten_forgotten(installs) -> None:
    source = installs("source")
    _seed(source)
    exported = source.export_bundle()
    digest = source.memory_bundle_digest(exported)
    bundle = _through_javascript(exported)

    texts = {record["text"] for record in bundle["records"]}
    assert texts == {"Responde en español", "Saturno está en 10.0.0.7", "Arranca con make dev"}
    assert len(bundle["suppressions"]) == 1
    # Nothing tied to this machine's conversations or credentials travels.
    for record in bundle["records"]:
        assert set(record) <= {
            "scope",
            "kind",
            "topic",
            "text",
            "provenance",
            "confidence",
            "created_at",
            "updated_at",
            "project_root",
        }
    assert "Prefiero café" not in json.dumps(bundle, ensure_ascii=False)

    target = installs("target")
    summary = target.import_bundle(bundle, digest)
    assert summary["imported"] == 3
    assert summary["suppressions_added"] == 1
    assert summary["rejected"] == summary["skipped_duplicates"] == 0

    learned = target.search_user("Saturno")[0]
    assert learned["kind"] == "environment"
    assert learned["provenance"] == "learned:session/sess-1"
    assert learned["confidence"] == pytest.approx(0.9)
    original = source.search_user("Saturno")[0]
    assert learned["created_at"] == original["created_at"]
    assert learned["id"] != original["id"]
    project = target.list_project("/repo")[0]
    assert (project["text"], project["kind"]) == ("Arranca con make dev", "workflow")

    # Forgotten on the source stays forgotten here.
    with pytest.raises(InvalidUsageError, match="forgotten"):
        target.remember_user("Prefiero café sin azúcar", topic="bebidas")

    # Importing again adds nothing.
    again = target.import_bundle(bundle, digest)
    assert again["imported"] == 0
    assert again["skipped_duplicates"] == 3
    assert again["suppressions_added"] == 0


def test_tampered_bundle_is_refused(installs) -> None:
    source = installs("source")
    _seed(source)
    bundle = source.export_bundle()
    digest = source.memory_bundle_digest(bundle)
    bundle["records"][0]["text"] = "Responde siempre en inglés"
    target = installs("target")
    with pytest.raises(InvalidUsageError, match="digest"):
        target.import_bundle(bundle, digest)
    assert target.list_user() == []
    with pytest.raises(InvalidUsageError, match="Rinari memory export"):
        target.import_bundle({**bundle, "version": 2}, digest)


def test_import_never_replaces_or_deletes_local_records(installs) -> None:
    source = installs("source")
    source.remember_user("Prefiere respuestas largas", topic="formato")
    target = installs("target")
    local = target.remember_user("Prefiere respuestas breves", topic="formato")
    # The target still remembers what the source forgot.
    target.remember_user("Usa tabs", topic="indentación", kind="rule")
    gone = source.remember_user("Usa tabs", topic="indentación", kind="rule")
    row = source.get_user(gone["id"])
    source.forget_user(row["id"], expected_revision=row["revision"])

    bundle, digest = _resign(source, source.export_bundle())
    summary = target.import_bundle(bundle, digest)
    assert summary["skipped_conflicts"] == 1
    assert summary["imported"] == 0
    assert summary["suppressions_added"] == 0
    assert target.get_user(local["id"])["text"] == "Prefiere respuestas breves"
    assert [row["text"] for row in target.search_user("tabs")] == ["Usa tabs"]


def test_forgotten_here_or_in_the_bundle_is_not_restored(installs) -> None:
    source = installs("source")
    source.remember_user("Vive en Valencia", topic="ciudad", kind="fact")
    source.remember_user("Le gusta el té verde", topic="bebida", kind="fact")
    target = installs("target")
    forgotten = target.remember_user("Vive en Valencia", topic="ciudad", kind="fact")
    row = target.get_user(forgotten["id"])
    target.forget_user(row["id"], expected_revision=row["revision"])

    bundle = source.export_bundle()
    # A bundle that carries a record and its own suppression (hand-edited
    # and re-signed): the suppression wins.
    tea = next(record for record in bundle["records"] if "té" in record["text"])
    other = installs("other")
    other_row = other.remember_user(tea["text"], topic=tea["topic"], kind="fact")
    other_live = other.get_user(other_row["id"])
    other.forget_user(other_live["id"], expected_revision=other_live["revision"])
    bundle["suppressions"] = other.export_bundle()["suppressions"]
    bundle, digest = _resign(source, bundle)

    summary = target.import_bundle(bundle, digest)
    assert summary["skipped_suppressed"] == 2
    assert summary["imported"] == 0
    assert target.list_user() == []


def test_secrets_and_invalid_records_are_rejected_one_by_one(installs) -> None:
    source = installs("source")
    source.remember_user("Responde en español", topic="idioma")
    bundle = source.export_bundle()
    token = "s" + "k-" + "Ab3dE6gH9" * 3
    template = dict(bundle["records"][0])
    bundle["records"] += [
        {**template, "topic": "clave", "text": f"La clave de la API es {token}"},
        {**template, "topic": "servidor", "text": "Entra con mysql -p[REDACTED] en Saturno"},
        {**template, "topic": "bad", "kind": "convention"},
        {**template, "topic": "", "text": "Sin tema"},
        "not a record",
        {**template, "scope": "project", "kind": "fact", "topic": "x", "text": "sin raíz"},
    ]
    bundle, digest = _resign(source, bundle)
    target = installs("target")
    summary = target.import_bundle(bundle, digest)
    assert summary["rejected"] == 6
    assert summary["imported"] == 1
    stored = json.dumps(target.list_user(), ensure_ascii=False)
    assert token not in stored and "REDACTED" not in stored


def test_sensitive_records_are_counted_for_the_owner_preview(installs) -> None:
    source = installs("source")
    source.remember_user(
        "Su banco es el Santander", topic="finanzas", kind="fact", provenance="panel"
    )
    bundle, digest = _resign(source, source.export_bundle())
    target = installs("target")
    preview = target.import_bundle(bundle, digest, dry_run=True)
    assert preview == {
        "dry_run": True,
        "total": 1,
        "imported": 1,
        "skipped_duplicates": 0,
        "skipped_suppressed": 0,
        "skipped_conflicts": 0,
        "rejected": 0,
        "sensitive": 1,
        "suppressions_added": 0,
    }
    # The preview writes nothing.
    assert target.list_user() == []
    assert target.import_bundle(bundle, digest)["imported"] == 1
    assert target.list_user()[0]["text"] == "Su banco es el Santander"


def test_oversized_bundle_is_refused(installs) -> None:
    source = installs("source")
    bundle = source.export_bundle()
    record = {
        "scope": "user",
        "kind": "fact",
        "topic": "t",
        "text": "x",
        "provenance": "panel",
        "confidence": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    bundle["records"] = [record] * (MEMORY_BUNDLE_MAX_RECORDS + 1)
    bundle, digest = _resign(source, bundle)
    with pytest.raises(InvalidUsageError, match="more than"):
        installs("target").import_bundle(bundle, digest)


# -- protocol ----------------------------------------------------------------------------------

_IDS = itertools.count()


def _call(server, method, params=None):
    line = {"id": f"{method}-{next(_IDS)}", "method": method}
    if params is not None:
        line["params"] = params
    return server.handle_line(json.dumps(line))


@pytest.fixture
def server(app_ctx, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return EngineServer(build_services(app_ctx, user_home=home))


def test_protocol_export_and_import(server, installs, monkeypatch) -> None:
    source = installs("source")
    _seed(source)
    bundle = source.export_bundle()
    payload = {"bundle": bundle, "digest": source.memory_bundle_digest(bundle)}

    preview = _call(server, "memory.import", {**payload, "dry_run": True})
    assert preview["ok"], preview
    assert preview["result"]["imported"] == 3
    assert _call(server, "memory.list", {"scope": "all"})["result"]["count"] == 0

    monkeypatch.setattr(server._turns, "has_active_turns", lambda: True)
    blocked = _call(server, "memory.import", payload)
    assert blocked["error"]["code"] == "TURN_RUNNING"
    monkeypatch.setattr(server._turns, "has_active_turns", lambda: False)

    done = _call(server, "memory.import", payload)
    assert done["result"]["imported"] == 3 and done["result"]["dry_run"] is False
    assert _call(server, "memory.list", {"scope": "all"})["result"]["count"] == 3

    exported = _call(server, "memory.export")["result"]
    assert exported["count"] == 3
    assert exported["digest"] == source.memory_bundle_digest(exported["bundle"])

    tampered = json.loads(json.dumps(payload))
    tampered["bundle"]["records"][0]["topic"] = "otro"
    refused = _call(server, "memory.import", tampered)
    assert refused["error"]["code"] == "INVALID_PARAMS"
    assert "digest" in refused["error"]["message"]

    assert _call(server, "memory.export", {"x": 1})["error"]["code"] == "INVALID_PARAMS"
    unknown = _call(server, "memory.import", {**payload, "merge": True})
    assert unknown["error"]["code"] == "INVALID_PARAMS"
    short = _call(server, "memory.import", {"bundle": bundle, "digest": "abc"})
    assert short["error"]["code"] == "INVALID_PARAMS"
    info = _call(server, "engine.info")
    assert info["result"]["capabilities"]["memory_portability_v1"] is True
