"""A missing required property names what it takes."""

from __future__ import annotations

from rinari.tools.native.verify import verify_tools
from rinari.tools.schema import validate_against


def test_a_missing_enum_property_lists_its_values() -> None:
    # Seen with a small model: verify.record without `result`, retried as is.
    [record] = [tool for tool in verify_tools() if tool.name == "verify.record"]
    errors = validate_against(record.input_schema, {"kind": "test", "summary": "3/3 pass"})
    assert errors == [
        "$: missing required property 'result' (one of: passed, failed, error, skipped)"
    ]


def test_a_missing_plain_property_names_its_type() -> None:
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    assert validate_against(schema, {}) == ["$: missing required property 'path' (string)"]
