"""audio.transcribe: Rinari listens to an audio file (plan 0.4.0 #36).

It uses the whisper.cpp engine and model the owner installed for dictation:
nothing new is downloaded. With it Rinari can check the lyrics of a song it
made, or what a voice recording says. Loud music lowers the accuracy.
"""

from __future__ import annotations

import contextlib
from typing import Any

from rinari.tools.definition import (
    RISK_LOW,
    SIDE_EFFECT_NONE,
    ClassifiedAction,
    ToolContext,
    ToolDefinition,
    ToolErrorCode,
    ToolErrorInfo,
    ToolResult,
)

DESCRIPTION = (
    "Listen to an audio or video file and get what is said or sung, with the local "
    "dictation engine (whisper.cpp). Use it to check lyrics or a voice recording. "
    "timestamps=true returns segments with start_ms/end_ms. Loud music lowers accuracy."
)

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path"],
    "properties": {
        "path": {"type": "string"},
        "language": {"type": "string", "description": "es, en… or auto"},
        "timestamps": {"type": "boolean"},
    },
}

_SETUP = {"NOT_INSTALLED", "MODEL_MISSING"}


def _fail(code: ToolErrorCode, message: str, details: dict | None = None) -> ToolResult:
    return ToolResult(
        ok=False,
        error=ToolErrorInfo(code=code, message=message, retryable=False),
        data=details,
    )


def _transcribe(arguments: dict, ctx: ToolContext) -> ToolResult:
    from rinari.speech.service import SpeechError
    from rinari.tools.native.fs import _resolve_read

    speech = getattr(ctx, "speech", None)
    if speech is None:
        return _fail(ToolErrorCode.DEPENDENCY_ERROR, "listening is not available here")
    path, error = _resolve_read(ctx, (arguments or {}).get("path"))
    if error is not None or path is None:
        return error or _fail(ToolErrorCode.NOT_FOUND, "no such file")
    language = (arguments or {}).get("language")
    if language is not None and not isinstance(language, str):
        return _fail(ToolErrorCode.INVALID_ARGUMENT, "language must be a string")
    try:
        result = speech.transcribe_file(
            path, language=language or None, timestamps=bool((arguments or {}).get("timestamps"))
        )
    except SpeechError as exc:
        if exc.code in _SETUP:
            # The owner sees a card with the way to Settings → Dictation.
            sink = getattr(ctx, "activity_sink", None)
            if callable(sink):
                with contextlib.suppress(Exception):
                    sink(
                        "speech.setup_needed",
                        {"reason": exc.code, "tool": "audio.transcribe", "file": path.name},
                    )
            return _fail(
                ToolErrorCode.DEPENDENCY_ERROR,
                f"{exc.message}. The owner can install it in Settings → Dictation; tell them.",
                {"speech_error": exc.code},
            )
        return _fail(ToolErrorCode.INVALID_ARGUMENT, exc.message, {"speech_error": exc.code})
    return ToolResult(ok=True, data={"file": path.name, **result})


def audio_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="audio.transcribe",
            description=DESCRIPTION,
            input_schema=INPUT_SCHEMA,
            risk=RISK_LOW,
            side_effects=SIDE_EFFECT_NONE,
            idempotent=True,
            handler=_transcribe,
            # A local read: whisper.cpp on this computer, no network, nothing
            # written. It follows the rules of reading the file (V2-5).
            classify=lambda arguments: ClassifiedAction(
                "fs.read", str((arguments or {}).get("path") or "")
            ),
            namespace="audio",
        )
    ]
