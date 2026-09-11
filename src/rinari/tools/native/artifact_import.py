"""Import uses filesystem policy and sandbox before copying any bytes."""

from rinari.artifacts.transfer import import_file
from rinari.tools.definition import (
    ArtifactRef,
    ClassifiedAction,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)
from rinari.tools.native.fs import _resolve_read


def artifact_import_tools(store, *, remote_target=None):
    from rinari.application.ssh_targets import TargetStore

    targets = TargetStore(store._ctx.layout.root)
    snapshot = {r["id"]: r for r in targets.list()}

    def handle(args, ctx):
        temporary = None
        if remote_target is not None or args.get("target_id"):
            from rinari.artifacts.ssh_transfer import retrieve

            target = remote_target or snapshot.get(args.get("target_id"))
            if target is None or (
                remote_target and args.get("target_id", target["id"]) != target["id"]
            ):
                return ToolResult(
                    ok=False,
                    error=ToolErrorInfo(
                        ToolErrorCode.PERMISSION_DENIED, "Unknown or unbound SSH destination"
                    ),
                )
            try:
                temporary, source = retrieve(targets, target, args["path"], ctx)
            except (OSError, ValueError) as exc:
                return ToolResult(
                    ok=False, error=ToolErrorInfo(ToolErrorCode.NETWORK_ERROR, str(exc))
                )
        else:
            source, error = _resolve_read(ctx, args["path"])
            if error:
                return error
        try:
            provenance = f"ssh:{target['id']}:{args['path']}" if temporary else f"local:{source}"
            result = import_file(
                store, ctx.session_id, source, cancellation=ctx.cancellation, provenance=provenance
            )
        except (OSError, ValueError) as exc:
            return ToolResult(
                ok=False, error=ToolErrorInfo(ToolErrorCode.VALIDATION_FAILED, str(exc))
            )
        finally:
            if temporary:
                temporary.cleanup()
        return ToolResult(
            ok=True, data=result.to_dict(), artifacts=(ArtifactRef(result.uri(), result.summary),)
        )

    return [
        ToolDefinition(
            name="artifact.import",
            description="Register an existing authorized file as a durable "
            "artifact, without analyzing its visual content. Use its URI for channel delivery.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}, "target_id": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            namespace="artifact",
            always_loaded=False,
            side_effects="local-reversible",
            classify=lambda a: ClassifiedAction(
                "ssh.read" if remote_target or a.get("target_id") else "fs.read",
                ("ssh://" + (remote_target["id"] if remote_target else a["target_id"]) + a["path"])
                if remote_target or a.get("target_id")
                else a["path"],
            ),
            handler=handle,
            timeout_ms=120000,
        )
    ]
