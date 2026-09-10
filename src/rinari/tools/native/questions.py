"""Structured user questions through the host's interaction channel."""

from rinari.tools.definition import (
    ClassifiedAction,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)


def ask_user(arguments, ctx):
    if ctx.ask_user is None:
        return ToolResult(
            ok=False,
            error=ToolErrorInfo(
                ToolErrorCode.DEPENDENCY_ERROR,
                "This host has no structured question channel. Ask in your response instead.",
            ),
        )
    return ToolResult(ok=True, data=ctx.ask_user(arguments, ctx.cancellation, ctx.deadline_at))


def question_tools():
    return [
        ToolDefinition(
            name="user.ask",
            description=(
                "Ask the user 1-3 necessary clarification questions, especially while planning. "
                "Offer meaningful options and allow free text. Wait for an explicit response. "
                "Skipped questions are not approval or consent. Do not use for tool permissions."
            ),
            input_schema={
                "type": "object",
                "required": ["questions"],
                "properties": {
                    "questions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 3,
                        "items": {
                            "type": "object",
                            "required": ["id", "title"],
                            "properties": {
                                "id": {"type": "string", "minLength": 1, "maxLength": 80},
                                "title": {"type": "string", "minLength": 1, "maxLength": 1000},
                                "options": {
                                    "type": "array",
                                    "maxItems": 6,
                                    "items": {
                                        "type": "object",
                                        "required": ["label"],
                                        "properties": {
                                            "label": {
                                                "type": "string",
                                                "minLength": 1,
                                                "maxLength": 200,
                                            },
                                            "description": {"type": "string", "maxLength": 500},
                                            "recommended": {"type": "boolean"},
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
            handler=ask_user,
            classify=lambda _: ClassifiedAction("state.read"),
            namespace="user",
            idempotent=False,
            timeout_ms=3_600_000,
        )
    ]
