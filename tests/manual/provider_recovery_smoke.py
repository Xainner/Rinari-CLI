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
                        "reply only READY. Do not request other actions."
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
                messages.append(
                    ChatMessage.tool_result(
                        call.id,
                        call.name,
                        json.dumps(
                            {
                                "ok": True,
                                "value": "READY",
                                "synthetic": True,
                            }
                        ),
                    )
                )
                second = invoke()
                assert not second.tool_calls and "READY" in second.content
                print(
                    json.dumps(
                        {
                            "model": model.alias,
                            "synthetic_tool_roundtrip": True,
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
