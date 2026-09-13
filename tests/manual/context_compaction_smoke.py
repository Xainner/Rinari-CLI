"""Opt-in provider validation using a temporary session and synthetic history only."""

import argparse
import json
import os
import tempfile
import time
from pathlib import Path

from rinari.application.context import build_app_context
from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.context.preparation import request_size
from rinari.context.projection import project
from rinari.context.settings import save
from rinari.models.router import ModelRouter
from rinari.models.types import ChatMessage, ModelRequest
from rinari.prompts.assembler import AssemblerContext
from rinari.runtime.agent import AgentContext
from rinari.runtime.cancellation import CancellationToken
from rinari.runtime.model_caller import ModelCaller
from rinari.storage.records import SessionRecord


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    source_ctx = build_app_context(Path.home() / ".rinari")
    source = build_services(source_ctx)
    model = source.models.resolve(args.model)
    provider = source.providers.get(model.provider_id)
    os.environ["RINARI_CONTEXT_SMOKE_SECRET"] = source.providers.resolve_secret(provider) or ""
    try:
        with tempfile.TemporaryDirectory(prefix="rinari-context-smoke-") as directory:
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
                        secret_env="RINARI_CONTEXT_SMOKE_SECRET",
                    )
                )
                chosen = services.models.add(
                    "smoke",
                    model.provider_model_id,
                    "smoke",
                    capabilities=model.capabilities,
                    settings=model.settings,
                )
                save(
                    services,
                    {
                        "enabled": True,
                        "compact_at_percent": 80,
                        "model_id": None,
                        "model_windows": {chosen.id: 8000},
                    },
                )
                record = SessionRecord(
                    id="synthetic-context",
                    kind="CHAT",
                    title="Synthetic context test",
                    project_id=None,
                    project_root_snapshot=None,
                    created_cwd=directory,
                    current_cwd=directory,
                    provider_id=target.id,
                    model_id=chosen.id,
                    profile_id="",
                    mode="plan",
                    state="active",
                    compact_state=None,
                    created_at="2026-09-13",
                    updated_at="2026-09-13",
                    last_active_at="2026-09-13",
                )
                ctx.session_repo.insert(record)
                original = [ChatMessage.user("Decision: the interface color must remain azure.")]
                original += [
                    ChatMessage.assistant(
                        f"Synthetic observation {i}. " + "This is repetitive test evidence. " * 65
                    )
                    for i in range(24)
                ]
                original.append(
                    ChatMessage.user("What color was selected? Answer with just the color.")
                )
                context = AgentContext(
                    record.id, chosen.id, None, AssemblerContext(), history=list(original)
                )
                caller = ModelCaller(
                    ModelRouter(services.providers, services.models), target, chosen.id
                )

                def request(state):
                    return ModelRequest(
                        model=chosen.id,
                        session_id=record.id,
                        messages=(
                            ChatMessage.system(state.compact_state_text or "Answer the user."),
                            *state.history,
                        ),
                    )

                events = []
                started = time.monotonic()
                reduced = services.context.prepare(
                    context,
                    request(context),
                    caller,
                    request,
                    lambda _event, data: events.append(data),
                    CancellationToken(),
                )
                state = ctx.session_repo.get(record.id).compact_state
                assert state and project(original, state) == context.history
                before = len(events)
                services.context.prepare(
                    context,
                    request(context),
                    caller,
                    request,
                    lambda *_: events.append({}),
                    CancellationToken(),
                )
                assert len(events) == before
                answer = caller.invoke(reduced)
                assert "azure" in answer.content.lower(), answer.content
                print(
                    json.dumps(
                        {
                            "provider": provider.alias,
                            "model": model.alias,
                            "status": "passed",
                            "before_estimate": request_size(
                                request(
                                    AgentContext(
                                        record.id,
                                        chosen.id,
                                        None,
                                        AssemblerContext(),
                                        history=original,
                                    )
                                )
                            ),
                            "after_estimate": request_size(reduced),
                            "retired_messages": len(state["covered_message_ids"]),
                            "seconds": round(time.monotonic() - started, 2),
                        },
                        ensure_ascii=False,
                    )
                )
            finally:
                ctx.close()
    finally:
        os.environ.pop("RINARI_CONTEXT_SMOKE_SECRET", None)
        source_ctx.close()


if __name__ == "__main__":
    main()
