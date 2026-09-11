import json
import re
from pathlib import Path


def test_protocol_schema_is_versioned_and_covers_stabilization_methods() -> None:
    path = Path(__file__).parents[2] / "src/rinari/engine_protocol/schema/v1.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    methods = set(schema["$defs"]["method"]["enum"])
    events = set(schema["$defs"]["event"]["enum"])
    assert schema["$id"].endswith("/v1.json")
    assert {"project.status", "session.fork", "model.discovery.start"} <= methods
    assert {"turn.stopped", "governor.progress", "model.discovery.completed"} <= events


def test_protocol_schema_matches_server_method_inventory() -> None:
    root = Path(__file__).parents[2]
    schema = json.loads(
        (root / "src/rinari/engine_protocol/schema/v1.json").read_text(encoding="utf-8")
    )
    source = (root / "src/rinari/engine_protocol/server.py").read_text(encoding="utf-8")
    registered = set(re.findall(r'_dispatcher\.register\("([^"]+)"', source))
    media_source = (root / "src/rinari/engine_protocol/media.py").read_text(encoding="utf-8")
    registered.update(re.findall(r'dispatcher\.register\("([^"]+)"', media_source))
    assert set(schema["$defs"]["method"]["enum"]) == registered


def test_protocol_schema_is_codegen_ready_for_desktop_dtos() -> None:
    path = Path(__file__).parents[2] / "src/rinari/engine_protocol/schema/v1.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    generated = set(schema["x-codegen-definitions"])
    assert {
        "attachment",
        "sessionSummary",
        "projectSummary",
        "usageSnapshot",
        "governorSnapshot",
        "protocolError",
    } <= generated
    governor = schema["$defs"]["governorSnapshot"]
    assert {"compactions", "context_pressure", "progress"} <= set(governor["properties"])
