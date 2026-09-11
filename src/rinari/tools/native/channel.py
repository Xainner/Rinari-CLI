"""Channel capabilities are supplied by the trusted host, never by model arguments."""

from rinari.tools.definition import ToolDefinition


def channel_tools(host):
    definitions = {
        "channel.send_attachment": (
            "Send a current-session artifact to the originating conversation. Import an existing "
            "file first with artifact.import. Never regenerate a file to retry delivery.",
            {"uri": {"type": "string"}, "caption": {"type": "string", "maxLength": 2000}},
            ["uri"],
        ),
        "channel.reply": (
            "Quote a message in the originating conversation. Do not duplicate the final answer.",
            {
                "text": {"type": "string", "maxLength": 16000},
                "message_id": {"type": "string", "maxLength": 512},
            },
            ["text"],
        ),
        "channel.delivery_get": (
            "Check a prior delivery after timeout; uncertainty is not permission to resend.",
            {"delivery_id": {"type": "string", "maxLength": 128}},
            ["delivery_id"],
        ),
    }
    return [
        ToolDefinition(
            name=name,
            description=description,
            input_schema={
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            handler=lambda args, ctx, name=name: host(name, args, ctx),
            namespace="channel",
            always_loaded=False,
            side_effects="none" if name.endswith("delivery_get") else "communication",
            idempotent=False,
            timeout_ms=120000,
            manifest={"source": "channel"},
        )
        for name, (description, properties, required) in definitions.items()
    ]
