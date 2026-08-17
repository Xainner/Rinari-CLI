"""Minimal JSON Schema validation for tool inputs.

Covers the subset Rinari's native tools use: type (object/array/string/
integer/number/boolean), properties, required, additionalProperties,
items, enum, minimum, maximum. Returning structured error strings keeps
the Tool Runtime free of a heavyweight dependency while remaining strict
enough for model-supplied arguments.
"""

from __future__ import annotations

from typing import Any

_TYPE_NAMES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "integer": int,
    "number": (int, float),
}


def validate_against(schema: dict[str, Any], value: Any) -> list[str]:
    """Return a list of human-readable violations (empty list = valid)."""
    errors: list[str] = []
    _validate(schema, value, "$", errors)
    return errors


def _validate(schema: dict[str, Any], value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(schema, dict):
        return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value must be one of {schema['enum']}")
        return

    expected_type = schema.get("type")
    if expected_type is not None and not _matches_type(expected_type, value):
        errors.append(f"{path}: expected type {expected_type}, got {type_name(value)}")
        return

    if isinstance(value, dict) and "properties" in schema:
        props: dict[str, Any] = schema.get("properties", {})
        for key, sub in props.items():
            if key in value:
                _validate(sub, value[key], f"{path}.{key}", errors)
        for key in schema.get("required", ()):
            if key not in value:
                errors.append(f"{path}: missing required property {key!r}")
        additional = schema.get("additionalProperties", True)
        for key in value:
            if key in props:
                continue
            if additional is False:
                errors.append(f"{path}: unexpected property {key!r}")

    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            _validate(schema["items"], item, f"{path}[{index}]", errors)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: value {value} is below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: value {value} is above maximum {schema['maximum']}")


def _matches_type(expected: str, value: Any) -> bool:
    python_type = _TYPE_NAMES.get(expected)
    if python_type is None:
        return True
    if expected in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, python_type)


def type_name(value: Any) -> str:
    if value is None:
        return "null"
    return type(value).__name__
