"""tool.call, the door to tools outside the conversation's list (F42)."""

from __future__ import annotations

from rinari.models.types import ToolCall
from rinari.tools.definition import ToolDefinition
from rinari.tools.door import door_text, door_tool, exact_call, unwrap
from rinari.tools.registry import ToolRegistry


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_all(
        [
            ToolDefinition(
                name="documents.create",
                description="Create a document.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["docx", "pptx"]},
                        "title": {"type": "string"},
                        "pages": {"type": "integer"},
                    },
                    "required": ["kind", "title"],
                },
                always_loaded=False,
            ),
            ToolDefinition(
                name="mcp.Lab.generate_image",
                description="Generate an image.",
                input_schema={"type": "object", "properties": {}},
                manifest={"source": "mcp", "server": "Lab"},
            ),
            door_tool(),
        ]
    )
    return registry


def test_the_exact_call_fills_the_required_arguments_in_order() -> None:
    tool = _registry().get("documents.create")
    assert exact_call(tool) == {
        "name": "documents.create",
        "arguments": '{"kind": "docx", "title": ""}',
    }
    assert door_text([tool]) == door_text([tool])


def test_other_calls_pass_through_untouched() -> None:
    call = ToolCall("1", "fs.read", {"path": "a"})
    assert unwrap(call, _registry()) == (call, None)


def test_arguments_as_text_or_as_an_object_and_a_separator_typo() -> None:
    registry = _registry()
    text = ToolCall("1", "tool.call", {"name": "documents.create", "arguments": '{"kind": "docx"}'})
    real, error = unwrap(text, registry)
    assert error is None and real == ToolCall("1", "documents.create", {"kind": "docx"})
    obj = ToolCall("2", "tool.call", {"name": "mcp_Lab_generate_image", "arguments": {}})
    real, error = unwrap(obj, registry)
    assert error is None and real.name == "mcp.Lab.generate_image"
    empty = ToolCall("3", "tool.call", {"name": "mcp.Lab.generate_image"})
    assert unwrap(empty, registry)[0].arguments == {}


def test_the_door_does_not_open_itself_and_needs_a_name() -> None:
    registry = _registry()
    _real, error = unwrap(
        ToolCall("1", "tool.call", {"name": "tool.call", "arguments": "{}"}), registry
    )
    assert error is not None and error.error.retryable
    _real, error = unwrap(ToolCall("2", "tool.call", {"arguments": "{}"}), registry)
    assert error is not None and "needs `name`" in error.error.message


def test_arguments_that_are_not_an_object_show_the_schema() -> None:
    registry = _registry()
    real, error = unwrap(
        ToolCall("1", "tool.call", {"name": "documents.create", "arguments": "[1, 2]"}), registry
    )
    assert real.name == "documents.create"
    assert "must be a JSON object" in error.error.message
    assert error.error.details["parameters"]["required"] == ["kind", "title"]


def test_the_door_itself_refuses_to_run_outside_the_loop() -> None:
    result = door_tool().handler({"name": "fs.read", "arguments": "{}"}, None)
    assert not result.ok
