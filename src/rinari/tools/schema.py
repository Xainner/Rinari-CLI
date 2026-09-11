"""Minimal JSON Schema validation for tool inputs.

Covers the subset Rinari's native tools use: type (object/array/string/
integer/number/boolean), properties, required, additionalProperties,
items, enum, minimum, maximum. Returning structured error strings keeps
the Tool Runtime free of a heavyweight dependency while remaining strict
enough for model-supplied arguments.
"""

from __future__ import annotations

import math
import re
from typing import Any

_TYPE_NAMES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "integer": int,
    "number": (int, float),
    "null": type(None),
}


def validate_against(schema: dict[str, Any], value: Any) -> list[str]:
    """Return a list of human-readable violations (empty list = valid)."""
    errors: list[str] = []
    _validate(schema, value, "$", errors)
    return errors


def _validate(schema: dict[str, Any], value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(schema, dict):
        return
    for keyword in ("anyOf", "oneOf"):
        if keyword in schema:
            matches = sum(not validate_against(branch, value) for branch in schema[keyword])
            if matches == 0 or (keyword == "oneOf" and matches != 1):
                errors.append(f"{path}: does not match {keyword}")
                return
    for branch in schema.get("allOf", []):
        _validate(branch, value, path, errors)
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value must be one of {schema['enum']}")
        return

    expected_type = schema.get("type")
    if expected_type is not None and not _matches_type(expected_type, value):
        errors.append(f"{path}: expected type {expected_type}, got {type_name(value)}")
        return

    if isinstance(value, dict):
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
            elif isinstance(additional, dict):
                _validate(additional, value[key], f"{path}.{key}", errors)

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: string is shorter than minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: string exceeds maxLength")
        if "pattern" in schema:
            try:
                if re.search(schema["pattern"], value) is None:
                    errors.append(f"{path}: string does not match pattern")
            except re.error:
                errors.append(f"{path}: invalid schema pattern")

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: array has fewer than minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: array exceeds maxItems")
        if schema.get("uniqueItems") and any(item in value[:i] for i, item in enumerate(value)):
            errors.append(f"{path}: array items must be unique")

    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            _validate(schema["items"], item, f"{path}[{index}]", errors)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            errors.append(f"{path}: number must be finite")
            return
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: value {value} is below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: value {value} is above maximum {schema['maximum']}")


def _matches_type(expected: str | list[str], value: Any) -> bool:
    if isinstance(expected, list):
        return any(_matches_type(item, value) for item in expected)
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
