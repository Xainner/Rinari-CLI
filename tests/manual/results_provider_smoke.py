"""Opt-in real adapter smoke using synthetic tool results and isolated configuration.

Only reads provider/model selection from --session. Never runs its task/tools.
"""

import argparse
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

from rinari.application.context import build_app_context
from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.models.router import ModelRouter
from rinari.models.types import ChatMessage, ModelRequest, ToolSchema


def main():
    parser = argparse.ArgumentParser()
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--session")
    selection.add_argument("--model")
    parser.add_argument("--home", default=str(Path.home() / ".rinari"))
    args = parser.parse_args()
    source_ctx = build_app_context(args.home)
    source = build_services(source_ctx)
    model_ref = source.sessions.show(args.session).model_id if args.session else args.model
    model = source.models.resolve(model_ref)
    provider = source.providers.get(model.provider_id)
    os.environ["RINARI_RECOVERY_SMOKE_SECRET"] = source.providers.resolve_secret(provider) or ""
    try:
        with tempfile.TemporaryDirectory(prefix="rinari-provider-recovery-") as directory:
            ctx = build_app_context(Path(directory) / "state")
            services = build_services(ctx, user_home=Path(directory))
            try:
                target = services.providers.add(
                    AddProviderInput(
                        alias="smoke",
                        provider_type=provider.type,
                        auth_method=provider.auth_method,
                        endpoint=provider.endpoint,
                        settings=provider.settings,
                        secret_env="RINARI_RECOVERY_SMOKE_SECRET",
                    )
                )
                selected = services.models.add(
                    "smoke",
                    model.provider_model_id,
                    "smoke",
                    capabilities=model.capabilities,
                    settings=model.settings,
                )
                router = ModelRouter(services.providers, services.models)
                sid = "ses_smoke_" + uuid.uuid4().hex
                messages = [
                    ChatMessage.system(
                        "This is a synthetic integration test. "
                        "Call smoke.echo with value READY once. After receiving its result, "
                        "the result will contain six synthetic files. "
                        "Reply with each END_MARKER from all six files. "
                        "Do not request other actions."
                    ),
                    ChatMessage.user("Run the synthetic echo test."),
                ]
                schema = ToolSchema(
                    "smoke.echo",
                    "Echo a synthetic value; no external side effects.",
                    {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                )
                started = time.monotonic()

                def invoke():
                    return router.invoke_stream(
                        target,
                        selected.id,
                        ModelRequest(
                            model=selected.id,
                            session_id=sid,
                            messages=tuple(messages),
                            tools=(schema,),
                            max_tokens=512,
                            stream_timeouts={"first_byte": 120, "idle": 120, "total": 180},
                        ),
                        lambda _: None,
                    )

                first = invoke()
                assert len(first.tool_calls) == 1, (
                    "Provider did not produce the requested synthetic call"
                )
                call = first.tool_calls[0]
                assert call.name == "smoke.echo" and not call.arguments_invalid
                messages.append(ChatMessage.assistant(first.content, first.tool_calls))
                from rinari.tools.definition import ArtifactRef, ToolResult
                from rinari.tools.observations import project_result

                files = [
                    {
                        "path": f"file-{i}.md",
                        "ok": True,
                        "data": {
                            "text": ("Synthetic documentation, no instructions.\n" * 240)
                            + f"END_MARKER: PROOF_{i}\n"
                        },
                    }
                    for i in range(6)
                ]

                def spill(suffix, content):
                    path = Path(directory) / (suffix + ".json")
                    path.write_text(content, encoding="utf-8")
                    return ArtifactRef(
                        uri=f"artifact://{sid}/runtime/{path.name}",
                        name=path.name,
                        kind="tool-output",
                    )

                result = project_result(
                    ToolResult(ok=True, data={"files": files}),
                    tool=call.name,
                    budget=65536,
                    spill=spill,
                )
                observation = result.to_model_text(call.name)
                assert all(f"PROOF_{i}" in observation for i in range(6))
                messages.append(ChatMessage.tool_result(call.id, call.name, observation))
                second = invoke()
                print(
                    json.dumps(
                        {
                            "synthetic_response": second.content,
                            "tool_calls": [call.name for call in second.tool_calls],
                        },
                        ensure_ascii=False,
                    )
                )
                assert not second.tool_calls and all(
                    f"PROOF_{i}" in second.content for i in range(6)
                ), "Provider did not return every synthetic marker"
                print(
                    json.dumps(
                        {
                            "model": model.alias,
                            "six_file_observation_roundtrip": True,
                            "observation_utf8_bytes": len(observation.encode("utf-8")),
                            "finish_reason": second.stop_reason.value,
                            "calls": 2,
                            "seconds": round(time.monotonic() - started, 2),
                            "original_session_resumed": False,
                            "external_tools_executed": 0,
                        }
                    )
                )
            finally:
                ctx.close()
    finally:
        os.environ.pop("RINARI_RECOVERY_SMOKE_SECRET", None)
        source_ctx.close()


if __name__ == "__main__":
    main()
