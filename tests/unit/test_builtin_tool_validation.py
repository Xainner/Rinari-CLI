"""Every built-in rejects malformed fields before invoking its handler."""

from dataclasses import replace

import pytest

from rinari.tools.catalog import builtin_catalog
from rinari.tools.definition import ToolErrorCode
from tests.unit.test_tool_contract import _ctx, _runtime

CATALOG = builtin_catalog()


@pytest.mark.parametrize("name", CATALOG.names())
def test_every_builtin_handles_empty_input_in_unconfigured_workspace(tmp_path, name):
    # No credentials, network endpoints, browser or process host are bound.
    # Tools with prerequisites must explain the missing dependency/argument.
    runtime = _runtime([CATALOG.get(name)])
    result = runtime.execute(name, {}, _ctx(tmp_path, tmp_path))
    assert result.ok or result.error is not None
    if not result.ok:
        assert result.error.code not in (ToolErrorCode.UNKNOWN, ToolErrorCode.VALIDATION_FAILED), (
            name,
            result.error,
        )


@pytest.mark.parametrize("name", CATALOG.names())
def test_every_builtin_validates_arguments_before_execution(tmp_path, name):
    tool = CATALOG.get(name)
    assert tool.input_schema.get("type") == "object", name
    assert tool.output_schema, name
    fields = tool.input_schema.get("properties", {})

    def forbidden(args, ctx):
        pytest.fail(f"Malformed arguments reached {name}'s handler")

    runtime = _runtime([replace(tool, handler=forbidden)])
    for invalid in (None, [], "not an object", 123):
        result = runtime.execute(name, invalid, _ctx(tmp_path, tmp_path))
        assert not result.ok
        assert result.error.code == ToolErrorCode.INVALID_ARGUMENT
    for field, schema in fields.items():
        if schema.get("type") in ("string", "boolean", "integer", "number", "array", "object"):
            result = runtime.execute(name, {field: None}, _ctx(tmp_path, tmp_path))
            assert not result.ok, (name, field)
            assert result.error.code == ToolErrorCode.INVALID_ARGUMENT, (name, field, result.error)
