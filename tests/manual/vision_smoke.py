"""Opt-in live visual check with isolated state and a synthetic image.

Run with --session <existing-session-id>. Only its model/credential selection
is read; all turns, configuration and media belong to a temporary home.
"""

import argparse
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

from PIL import Image, ImageDraw

from rinari.application.context import build_app_context
from rinari.application.provider_service import AddProviderInput
from rinari.application.services import build_services
from rinari.engine_protocol.server import EngineServer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--auxiliary", action="store_true")
    parser.add_argument("--configured-auxiliary", action="store_true")
    parser.add_argument("--home", default=str(Path.home() / ".rinari"))
    args = parser.parse_args()
    original_ctx = build_app_context(args.home)
    original = build_services(original_ctx)
    selection = original.sessions.show(args.session)
    model = original.models.resolve(selection.model_id)
    if args.configured_auxiliary:
        from rinari.application.vision import settings

        model = original.models.resolve(settings(original)["model_id"])
        args.auxiliary = True
    provider = original.providers.get(model.provider_id)
    secret = original.providers.resolve_secret(provider)
    os.environ["RINARI_VISUAL_SMOKE_SECRET"] = secret or ""
    print(
        json.dumps(
            {"model": model.alias, "declared_vision": (model.capabilities or {}).get("vision")}
        ),
        flush=True,
    )
    try:
        with tempfile.TemporaryDirectory(prefix="rinari-visual-smoke-") as directory:
            root = Path(directory).resolve()
            assert root.is_relative_to(Path(tempfile.gettempdir()).resolve())
            ctx = build_app_context(root / "state")
            workspace = root / "workspace"
            workspace.mkdir()
            services = build_services(ctx, user_home=root)
            services.providers.add(
                AddProviderInput(
                    alias="visual-smoke",
                    provider_type=provider.type,
                    auth_method=provider.auth_method,
                    endpoint=provider.endpoint,
                    settings=provider.settings,
                    secret_env="RINARI_VISUAL_SMOKE_SECRET",
                )
            )
            smoke_model = services.models.add(
                "visual-smoke",
                model.provider_model_id,
                "visual-smoke",
                capabilities={**(model.capabilities or {}), "vision": True},
                settings=model.settings,
            )
            services.providers.use("visual-smoke")
            if args.auxiliary:
                from rinari.application.vision import configure

                configure(
                    services,
                    {
                        "mode": "dedicated",
                        "model_id": smoke_model.id,
                        "execution": {
                            "max_concurrency": 8,
                            "providers": {smoke_model.provider_id: 1},
                            "models": {},
                        },
                    },
                )
            server = EngineServer(services, user_home=root)
            try:

                def request(method, params):
                    result = server.handle_line(
                        json.dumps({"id": uuid.uuid4().hex, "method": method, "params": params})
                    )
                    if not result or not result.get("ok"):
                        detail = result.get("error", {}) if result else "empty"
                        raise RuntimeError(f"Protocol request failed: {method}: {detail}")
                    return result["result"]

                sid = request("session.create", {"chat": True, "cwd": str(workspace)})["session"][
                    "id"
                ]
                image = Image.new("RGB", (400, 200), "white")
                draw = ImageDraw.Draw(image)
                draw.rectangle((10, 10, 190, 190), fill="red")
                draw.rectangle((210, 10, 390, 190), fill="blue")
                path = workspace / "prueba visual ñ.png"
                image.save(path)

                def turn(label, message, attachments):
                    request(
                        "session.turn.start",
                        {"session_id": sid, "message": message, "attachments": attachments},
                    )
                    deadline = time.monotonic() + 150
                    actions = []
                    vision = []
                    while time.monotonic() < deadline:
                        for event in server.drain_events():
                            payload = event.get("payload", {})
                            if payload.get("session_id") != sid:
                                continue
                            name = event["event"]
                            if name == "tool.started":
                                actions.append(payload.get("tool"))
                                print(json.dumps({"case": label, "tool": actions[-1]}), flush=True)
                                if len(actions) > 5:
                                    request("session.turn.cancel", {"session_id": sid})
                            if name == "vision.completed":
                                vision.append(payload.get("route"))
                            if (
                                name == "model.content.completed"
                                and payload.get("output_kind") == "final"
                            ):
                                print(
                                    json.dumps(
                                        {"case": label, "answer": payload.get("content")},
                                        ensure_ascii=False,
                                    ),
                                    flush=True,
                                )
                            if name in {
                                "turn.completed",
                                "turn.failed",
                                "turn.cancelled",
                                "turn.stopped",
                            }:
                                print(
                                    json.dumps(
                                        {
                                            "case": label,
                                            "terminal": name,
                                            "actions": actions,
                                            "vision": vision,
                                        }
                                    ),
                                    flush=True,
                                )
                                assert name == "turn.completed"
                                assert bool(vision) == args.auxiliary
                                assert all(route == "dedicated" for route in vision)
                                cleanup_deadline = time.monotonic() + 10
                                while (
                                    server._turns.has_active_turn(sid)
                                    and time.monotonic() < cleanup_deadline
                                ):
                                    time.sleep(0.05)
                                assert not server._turns.has_active_turn(sid)
                                return actions
                        time.sleep(0.1)
                    request("session.turn.cancel", {"session_id": sid})
                    raise TimeoutError("Live visual test exceeded its deadline")

                turn(
                    "attachment",
                    "Indica solo los colores de los dos rectángulos, de izquierda a derecha. "
                    "No uses herramientas.",
                    [{"path": str(path)}],
                )
                actions = turn(
                    "folder",
                    f"Usa fs.read_image para leer {path}. "
                    "Indica solo los colores de izquierda a derecha. No uses otras herramientas.",
                    [],
                )
                assert "fs.read_image" in actions
            finally:
                server.close()
                ctx.close()
    finally:
        os.environ.pop("RINARI_VISUAL_SMOKE_SECRET", None)
        original_ctx.close()


if __name__ == "__main__":
    main()
